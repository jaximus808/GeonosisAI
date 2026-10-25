"""`make demo`: the whole Robot Deployment Factory on this laptop, with a MuJoCo [SIM] YAM arm.

    make demo                         # boots everything, prints the URLs, stays up until Ctrl-C
    make demo ARGS="--web-port 3783 --stdb-port 3083 --orch-port 8083"   # other ports
    make demo-down                    # stops a running demo (from another terminal)
    # same as: cd services/orchestrator && uv run python ../../scripts/demo/run.py up|down [...]

What runs (all local; nothing touches maincloud or the Modal media Volume):
  1. SpacetimeDB on 127.0.0.1:$stdb (TDM-28 ``LocalStack``: fresh data dir, module published,
     service identities minted for orchestrator / robot_client / web; tokens 0600 under the run dir).
  2. Local media store stand-in (spikes/tdm-58-recording ``LocalMediaStore``) for episode uploads.
  3. The FULL orchestrator (every handler; real Claude for ticket parse, diagnose, planner and the
     reviewer's vision check, read from the root .env), ``RDF_MODAL_STUB=1`` + a stub trainer by default
     (``--real-modal`` launches real Modal training instead).
  4. The MuJoCo [SIM] robot client (``--adapter mujoco``): physics + rendered top/wrist cameras, the
     TDM-55 stream and TDM-52 ``/ws/control`` on 127.0.0.1 (``RDF_STREAM=none``), advertising
     DAgger ``interventions``.
  5. The [SIM] scripted policy server (``rdf_robot_client.sim_policy_server``, NOT pi0.5).
     ``--policy production`` uses the real TDM-41 Modal endpoint from .env instead (unchanged path).
  6. The web app (Next dev, own dist dir) on $web, pointed at all of the above.

Attach mode (``--maincloud``, or ``--stdb-url URL --db NAME``; ``make demo-maincloud``): step 1 is skipped,
nothing is published, deleted or cleared. The orchestrator runs as the owner (``SPACETIME_TOKEN`` from
``--env-file``, default the root .env); the robot client and the web get their own service identities, reused
from the env file (``ROBOT_CLIENT_SPACETIME_TOKEN``, ``RDF_WEB_SPACETIME_TOKEN``) when allow-listed, else minted
and allow-listed ONCE and appended there. The sim robot row is reused by name. It refuses to start while another
orchestrator is attached (a lockfile + a ps scan of this user's orchestrator processes). Media, stream secret and
operator key come from .env (no local stand-ins) and solo resets are OFF. Training is STUB by default (also on
maincloud: "[STUB] no GPU" checkpoints, no Modal credit); --real-modal spends credits. --seed-evals: SEEDED evals.

Labels: the robot is ``sim-yam-mujoco`` (embodiment ``sim_yam``), every episode is ``fixture: true``,
eval notes start with ``[SIM]``, the policy describes itself as a scripted sim policy everywhere.

Secrets: services read the root .env themselves (ANTHROPIC_API_KEY, Stripe TEST key, ...). This
script never prints them; everything it generates (operator key, stream / media / policy secrets)
is a throwaway value for this run only. Spend approval: the web "Approve plan" button / MCP
approve_spend; ``--auto-approve-max-cents N`` turns on the flywheel's auto-approve (default off).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(REPO / "spikes" / "tdm-58-recording"))
ROOT = Path(os.environ.get("RDF_DEMO_ROOT") or Path(tempfile.gettempdir()) / "rdf-demo")
STATE = ROOT / "demo.json"
REVIEW_CONFIG = REPO / "scripts" / "demo" / "review_sim.yaml"
ROBOT_NAME = "sim-yam-mujoco"


def _log(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _root_env_value(name: str) -> str:
    """One value from the repo-root .env (never printed)."""
    p = REPO / ".env"
    if not p.exists():
        return ""
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}=") or line.startswith(f"export {name}="):
            v = line.split("=", 1)[1].strip()
            return v.strip('"').strip("'")
    return ""


# ---------------------------------------------------------------- attach mode (maincloud or any existing DB)
MAINCLOUD = "https://maincloud.spacetimedb.com"
ID_KEYS = {"robot_client": ("ROBOT_CLIENT_SPACETIME_TOKEN", "robot_client@sim-demo"),
           "web": ("RDF_WEB_SPACETIME_TOKEN", "web@demo")}


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def _append_env(path: Path, key: str, value: str, comment: str) -> None:
    """Append KEY=value to the env file (the way scripts/jetson/mint_robot_identity.py does). Never prints it."""
    text = path.read_text() if path.exists() else ""
    with path.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"# {comment}\n{key}={value}\n")
    os.chmod(path, 0o600)


def _token_identity(token: str) -> str:
    """The identity hex (no 0x) from a SpacetimeDB token's claims (decoded locally, nothing printed)."""
    import base64

    p = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    return str(claims.get("hex_identity") or "").lower().removeprefix("0x")


def _stdb_post(host: str, path: str, token: str | None, body: object | None, raw: str | None = None) -> tuple[int, str]:
    data = raw.encode() if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    headers = {"Content-Type": "application/json" if raw is None else "text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{host}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _owner_sql(host: str, db: str, owner: str, q: str) -> list[dict]:
    from rdf_common.spacetime_client import SpacetimeClient

    c = SpacetimeClient(host, db, owner)
    try:
        return c.sql(q)
    finally:
        c.close()


def ensure_identity(host: str, db: str, owner: str, env_path: Path, role: str) -> tuple[str, str, str]:
    """(token, identity hex, what happened) for ``role``: reuse the env file's token if it is allow-listed with
    that role; allow-list it if it is not; else mint ONE identity, allow-list it as the owner and append it
    to the env file so later starts reuse it. Writes to the database only in those last two cases."""
    key, label = ID_KEYS[role]
    token = _read_env(env_path).get(key) or ""
    allow = {str(r.get("identity") or "").lower().removeprefix("0x"): r.get("role")
             for r in _owner_sql(host, db, owner, "SELECT * FROM service_identity")}
    if token:
        ident = _token_identity(token)
        if allow.get(ident) == role:
            return token, ident, f"reused {key} (allow-listed as {role})"
        st, body = _stdb_post(host, f"/v1/database/{db}/call/register_service_identity", owner,
                              [["0x" + ident], role, label])
        if st >= 300:
            raise RuntimeError(f"register_service_identity({role}) failed: HTTP {st}: {body[:200]}")
        return token, ident, f"allow-listed the existing {key} as {role}"
    st, body = _stdb_post(host, "/v1/identity", None, None)
    if st >= 300:
        raise RuntimeError(f"POST /v1/identity failed: HTTP {st}")
    d = json.loads(body)
    token, ident = d["token"], str(d["identity"]).lower().removeprefix("0x")
    st, body = _stdb_post(host, f"/v1/database/{db}/call/register_service_identity", owner, [["0x" + ident], role, label])
    if st >= 300:
        raise RuntimeError(f"register_service_identity({role}) failed: HTTP {st}: {body[:200]} (nothing written to {env_path})")
    _append_env(env_path, key, token, f"make demo: {role} service identity 0x{ident} ({label}) on {db}")
    return token, ident, f"minted + allow-listed a new {role} identity, stored as {key} in {env_path.name}"


def other_orchestrators(host: str, env_host: str, mine: set[int]) -> list[int]:
    """PIDs of orchestrator processes (this user) attached to ``host``: SPACETIME_HOST in their env, or the
    .env one when they have none. Only our own processes' env is readable, which is the case on this laptop."""
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    hits = []
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) in mine:
            continue
        if not ("rdf-orchestrator" in cmd or "-m rdf_orchestrator" in cmd):
            continue
        if cmd.lstrip().startswith("uv "):  # the uv wrapper; its python child is the one that counts
            continue
        env = subprocess.run(["ps", "eww", "-o", "command=", "-p", pid_s], capture_output=True, text=True).stdout
        m = next((t.split("=", 1)[1] for t in env.split() if t.startswith("SPACETIME_HOST=")), None)
        if (m or env_host).rstrip("/") == host.rstrip("/"):
            hits.append(int(pid_s))
    return hits


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _http_json(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def up(a: argparse.Namespace) -> int:
    from e2e_stack import LocalMediaStore
    from harness import LocalStack, wait_for

    if a.maincloud:
        a.stdb_url = a.stdb_url or MAINCLOUD
        if a.db in (None, "rdfdemo"):
            a.db = json.loads((REPO / "spacetime" / "spacetime.json").read_text())["database"]
    attach = bool(a.stdb_url)
    if not attach and a.db is None:
        a.db = "rdfdemo"
    ports = {"web": a.web_port, "orchestrator": a.orch_port, "stream": a.stream_port,
             "sim_state": a.sim_state_port, "policy": a.policy_port, **({} if attach else {"stdb": a.stdb_port})}
    busy = {k: p for k, p in ports.items() if not _port_free(p)}
    if busy:
        _log(f"ports in use: {busy}. Pick others (see --help) or stop what holds them (make demo-down).")
        return 2
    ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["TDM28_ROOT"] = str(ROOT)
    st = LocalStack(a.stdb_port, a.db, root=ROOT)
    on_maincloud = attach and "maincloud" in a.stdb_url
    env_path = Path(a.env_file)
    lock = ROOT / f"attach-{a.db}.lock"
    if attach:
        # ATTACH: an existing database (maincloud for the real demo). Nothing is published, deleted or cleared.
        st.host, st.db = a.stdb_url.rstrip("/"), a.db
        st.root = ROOT / f"run-attach-{a.db}"
        st.logs, st.tokens, st.media = st.root / "logs", st.root / "tokens", st.root / "media"
        envf = _read_env(env_path)
        owner = envf.get("SPACETIME_TOKEN") or ""
        if not owner:
            _log(f"attach mode needs the owner token SPACETIME_TOKEN in {env_path}")
            return 2
        if lock.exists():
            try:
                held = int(json.loads(lock.read_text()).get("pid") or 0)
            except (ValueError, OSError):
                held = 0
            if held and _pid_alive(held):
                _log(f"another demo (pid {held}) is attached to {a.db} ({lock}); refusing a second orchestrator")
                return 2
        others = other_orchestrators(st.host, envf.get("SPACETIME_HOST", ""), {os.getpid()})
        if others:
            _log(f"orchestrator process(es) {others} already attached to {st.host}: exactly ONE orchestrator may "
                 "run against a shared database (docs/pre-run-checklist.md). Stop them first.")
            return 2
        lock.write_text(json.dumps({"pid": os.getpid(), "host": st.host, "db": st.db, "started": time.time()}))
    solo = a.solo_resets if a.solo_resets is not None else not attach
    modal_banner = "Modal: REAL (spends credits)" if a.real_modal else "Modal: STUB (no credits used)"
    _log(modal_banner)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # throwaway per-run secrets (never the .env ones)
    stream_secret = "demo-stream-" + secrets.token_hex(16)
    media_key = "demo-media-" + secrets.token_hex(16)
    operator_key = os.environ.get("DEMO_OPERATOR_KEY") or "demo-operator-" + secrets.token_hex(16)  # make demo-public passes its own
    policy_token = "demo-policy-" + secrets.token_hex(16)
    passcode = a.passcode or _root_env_value("RDF_OPERATOR_PASSCODE")
    passcode_note = "(from --passcode)" if a.passcode else "(your RDF_OPERATOR_PASSCODE from .env)"
    if not passcode:
        passcode = "demo-" + secrets.token_hex(4)
        passcode_note = f"{passcode} (generated for this run; pass --passcode to choose one)"
    orch_url = f"http://127.0.0.1:{a.orch_port}"
    policy_url = f"http://127.0.0.1:{a.policy_port}"
    trainer = None
    try:
        if attach:
            if st.root.exists():
                shutil.rmtree(st.root)
            for d in (st.logs, st.tokens, st.media):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(st.tokens, 0o700)
            toks = {"orchestrator": owner}
            for role in ("robot_client", "web"):
                tok, ident, what = ensure_identity(st.host, st.db, owner, env_path, role)
                toks[role] = tok
                _log(f"identity {role}: 0x{ident[:12]}... {what}")
            for role, tok in toks.items():  # 0600 files under the run dir, removed on exit
                (st.tokens / f"{role}.token").write_text(tok)
                os.chmod(st.tokens / f"{role}.token", 0o600)
            row = next(iter(_owner_sql(st.host, st.db, owner, f"SELECT * FROM robot WHERE name = '{ROBOT_NAME}'")), None)
            if row is not None:
                bound = str(row.get("client_identity") or "").lower().removeprefix("0x")
                rid_hex = _token_identity(toks["robot_client"])
                if bound and bound not in ("none", "{}") and rid_hex not in bound:
                    _log(f"robot row {ROBOT_NAME} (id {row['id']}) is bound to another robot_client identity; refusing")
                    return 2
                _log(f"reusing robot row {ROBOT_NAME} (id {row['id']})")
            operator_key = os.environ.get("DEMO_OPERATOR_KEY") or envf.get("RDF_OPERATOR_KEY") or operator_key
            media = None
        else:
            st.start_server()
            st.publish()
            st.make_identity("orchestrator")
            st.make_identity("robot_client")
            st.make_identity("web")
            media = LocalMediaStore(st.root / "media-store", key=media_key)
            _log(f"media store stand-in at {media.url}")
        keyfile = st.tokens / "operator.key"  # 0600: for proof scripts (never printed)
        keyfile.write_text(operator_key)
        os.chmod(keyfile, 0o600)
        local_db = {"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db}

        # ---- sim policy server (before the robot: the robot's first rollout warms it)
        if a.policy == "sim":
            st._spawn("sim-policy", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
                                     "--extra", "sim", "python", "-m", "rdf_robot_client.sim_policy_server",
                                     "--port", str(a.policy_port),
                                     "--state-url", f"http://127.0.0.1:{a.sim_state_port}/sim/state"],
                      env={"SIM_POLICY_TOKEN": policy_token})

        # ---- orchestrator (full handler set)
        orch_env = {**st.env("orchestrator"), "RDF_REVIEW_CONFIG": str(a.review_config), "RDF_OPERATOR_KEY": operator_key,
                    "RDF_WEB_ORIGINS": f"http://localhost:{a.web_port},http://127.0.0.1:{a.web_port}",
                    "EVAL_SOLO_MODE": "1" if solo else "0", "EVAL_SOLO_RESET_DELAY_S": "2",
                    "EVAL_TRIAL_TIMEOUT_S": str(a.trial_timeout_s),
                    "INTERVENTION_ROLLOUT_TIMEOUT_S": str(a.trial_timeout_s), "INTERVENTION_CLAIM_TIMEOUT_S": "900",
                    "INTERVENTION_TAKEOVER_MAX_S": "300", "INTERVENTION_VERDICT_WAIT_S": "30",
                    "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": str(a.auto_approve_max_cents)}
        for k in ("RDF_DROP_SHOW_ACCESS_CODE", "RDF_WEB_PUBLIC_URL",  # .env settings the orchestrator reads from os.environ
                  "RDF_DATA_DROID", "RDF_TRAINING_DROID", "RDF_ONBOARDING_DROID", "RDF_DATA_DROID_BACKEND",  # Factory Droids
                  "FACTORY_API_KEY"):
            v = os.environ.get(k) or _root_env_value(k)
            if v:
                orch_env[k] = v
        if media is not None:  # local: the media store stand-in + throwaway stream secret; attach: .env's (real) ones
            orch_env.update(RDF_MEDIA_BACKEND="modal", RDF_MEDIA_STORE_URL=media.url, RDF_MEDIA_SIGNING_KEY=media_key,
                            RDF_STREAM_TOKEN_SECRET=stream_secret)
        if not a.real_modal:  # stub by default everywhere, maincloud included (logged there as modal.stub_on_maincloud)
            orch_env["RDF_MODAL_STUB"] = "1"
        if a.seed_evals:  # sim robots only (the orchestrator ignores it, with an event, for a real robot)
            orch_env["RDF_SEED_EVALS"] = "1"
        if a.policy == "sim":
            orch_env.update(POLICY_SERVER_URL=policy_url, POLICY_SERVER_TOKEN=policy_token)
        st._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                   "rdf-orchestrator", "--host", "127.0.0.1", "--port", str(a.orch_port)], env=orch_env)
        wait_for(lambda: _http_json(f"{orch_url}/healthz")["subscription"]["state"] == "live", 150,
                 f"orchestrator live (log {st.logs / 'orchestrator.log'})")
        _log(f"orchestrator live on {orch_url} ({modal_banner})")
        if not a.real_modal:
            from stub_modal import StubTrainer

            trainer = StubTrainer(st.client("orchestrator"), mode="stub-call", train_seconds=a.train_seconds,
                                  poll=2.0 if attach else 0.5, ckpt_prefix="fake://stub-no-gpu/",
                                  label="[STUB] no GPU").start()

        # ---- the MuJoCo [SIM] robot
        robot_env = {**local_db, "RDF_STREAM": "none", "RDF_STREAM_PORT": str(a.stream_port),
                     **({"RDF_STREAM_TOKEN_SECRET": stream_secret} if media is not None else {}), "ORCHESTRATOR_URL": orch_url,
                     "RDF_EPISODES_DIR": str(st.root / "episodes"), "RDF_SIM_STATE_PORT": str(a.sim_state_port),
                     "MUJOCO_GL": os.environ.get("MUJOCO_GL", "cgl" if sys.platform == "darwin" else "egl")}
        if a.policy == "sim":
            robot_env.update(POLICY_BACKEND="sim", SIM_POLICY_URL=policy_url, SIM_POLICY_TOKEN=policy_token)
        else:
            robot_env.update(POLICY_BACKEND="production")
        st._spawn("robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "--extra", "sim",
                            "rdf-robot-client", "--adapter", "mujoco", "--name", ROBOT_NAME, "--host", st.host,
                            "--db", st.db, "--token-file", str(st.tokens / "robot_client.token")], env=robot_env)
        orch = st.client("orchestrator")
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT_NAME}'")
                                       if r["status"] == "online"
                                       and json.loads(r["camera_config_json"] or "{}").get("stream")), None),
                         180, f"sim robot online with its stream (log {st.logs / 'robot.log'})")
        robot_id = int(robot["id"])
        caps = json.loads(robot["camera_config_json"]).get("capabilities")
        _log(f"robot {ROBOT_NAME} (id {robot_id}) online, capabilities {caps}")

        # ---- web
        web_proc = None
        if not a.no_web:
            web_env = {"NEXT_PUBLIC_SPACETIME_HOST": st.host, "NEXT_PUBLIC_SPACETIME_DB_NAME": st.db,
                       "SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db,
                       "RDF_ORCHESTRATOR_URL": orch_url, "NEXT_PUBLIC_ORCHESTRATOR_URL": orch_url,
                       "RDF_OPERATOR_KEY": operator_key, "RDF_OPERATOR_PASSCODE": passcode,
                       "RDF_WEB_SPACETIME_TOKEN": st.token("web"), "NEXT_DIST_DIR": f".next-demo-{a.web_port}",
                       "NEXT_PUBLIC_RDF_ENV": "maincloud-sim-demo" if on_maincloud else "local-sim-demo",
                       "PORT": str(a.web_port)}
            web_proc = st._spawn("web", ["pnpm", "--dir", str(REPO / "apps" / "web"), "exec", "next", "dev",
                                         "--port", str(a.web_port)], env=web_env)
            wait_for(lambda: urllib.request.urlopen(f"http://127.0.0.1:{a.web_port}/", timeout=60).status == 200, 240,
                     f"web on :{a.web_port} (log {st.logs / 'web.log'})", every=2)
        STATE.write_text(json.dumps({"pid": os.getpid(), "pgids": [p.pid for p in st.procs.values()],
                                     "ports": ports, "robot_id": robot_id, "root": str(st.root)}, indent=2))
        web = f"http://localhost:{a.web_port}"
        print("\n" + "=" * 78, flush=True)
        print("  Robot Deployment Factory: " + (f"demo on {st.db} @ {st.host} (ATTACHED: nothing published or cleared)"
                                                 if attach else "local demo") + " with a MuJoCo [SIM] YAM arm", flush=True)
        print("=" * 78, flush=True)
        for label, url in (("Dashboard", web), ("Operator login", f"{web}/ops/login"),
                           ("New ticket", f"{web}/tickets/new"), ("Robot page (camera)", f"{web}/robots/{robot_id}"),
                           ("Marketplace", f"{web}/marketplace"), ("Teleop", f"{web}/teleop"),
                           ("Orchestrator", f"{orch_url}/healthz"),
                           ("Policy server", f"{policy_url}/health" if a.policy == "sim" else "TDM-41 Modal endpoint (.env)"),
                           ("Robot stream", f"http://127.0.0.1:{a.stream_port}/healthz")):
            print(f"  {label:<22} {url}", flush=True)
        print(f"  {'Operator passcode':<22} {passcode_note}", flush=True)
        print(f"  {'Training':<22} {modal_banner}" + ("" if a.real_modal else "  [STUB] no GPU checkpoints"), flush=True)
        print(f"  {'Evals':<22} " + ("SEEDED: outcomes drawn at the sim policy's rates, not run ([SEEDED])"
                                      if a.seed_evals else "real sim trials on the MuJoCo robot"), flush=True)
        print(f"  {'Spend approval':<22} " + (f"AUTO up to {a.auto_approve_max_cents} cents (flywheel)" if a.auto_approve_max_cents
                                               else "manual: 'Approve plan' on the ticket page (or MCP approve_spend)"), flush=True)
        print(f"  {'Policy':<22} " + ("[SIM] scripted IK pick-and-place (NOT pi0.5); base = baseline noise, "
                                       "fine-tuned checkpoint = less noise" if a.policy == "sim" else "production (Modal)"), flush=True)
        print(f"  {'Logs':<22} {st.logs}", flush=True)
        print("  Suggested prompt: \"Pick up the cube and place it in the empty plate.\"", flush=True)
        print("  Ctrl-C (or make demo-down) stops everything.\n", flush=True)
        while not stop.is_set():
            dead = [n for n, p in st.procs.items() if p.poll() is not None]
            if dead:
                _log(f"process exited: {dead}; logs in {st.logs}")
                break
            stop.wait(1.0)
    except Exception as e:  # noqa: BLE001
        _log(f"boot failed: {e}")
        return 1
    finally:
        if trainer is not None:
            trainer.stop()
        st.stop()
        if attach:  # token copies and the attach lock go with the stack (the env file keeps the identities)
            for f in st.tokens.glob("*") if st.tokens.exists() else []:
                f.unlink(missing_ok=True)
            if lock.exists() and json.loads(lock.read_text() or "{}").get("pid") == os.getpid():
                lock.unlink(missing_ok=True)
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        _log("stopped")
    return 0


def down(a: argparse.Namespace) -> int:
    if not STATE.exists():
        _log(f"no running demo found ({STATE})")
        return 0
    s = json.loads(STATE.read_text())
    try:
        os.kill(int(s["pid"]), signal.SIGTERM)
        _log(f"stopping demo (pid {s['pid']})")
    except ProcessLookupError:
        pass
    end = time.monotonic() + 20
    while time.monotonic() < end and STATE.exists():
        time.sleep(0.5)
    for pg in s.get("pgids", []):  # anything the runner could not stop
        try:
            os.killpg(int(pg), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass
    _log("down")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--web-port", type=int, default=int(os.environ.get("DEMO_WEB_PORT", 3737)))
    u.add_argument("--stdb-port", type=int, default=int(os.environ.get("DEMO_STDB_PORT", 3030)))
    u.add_argument("--orch-port", type=int, default=int(os.environ.get("DEMO_ORCH_PORT", 8030)))
    u.add_argument("--stream-port", type=int, default=int(os.environ.get("DEMO_STREAM_PORT", 8791)))
    u.add_argument("--sim-state-port", type=int, default=int(os.environ.get("DEMO_SIM_STATE_PORT", 8792)))
    u.add_argument("--policy-port", type=int, default=int(os.environ.get("DEMO_POLICY_PORT", 8765)))
    u.add_argument("--db", default=None, help="database name (local default rdfdemo; --maincloud: spacetime/spacetime.json)")
    u.add_argument("--maincloud", action="store_true",
                   help="attach to maincloud (= --stdb-url https://maincloud.spacetimedb.com --db <spacetime.json database>)")
    u.add_argument("--stdb-url", default=os.environ.get("DEMO_STDB_URL") or None,
                   help="ATTACH to an existing SpacetimeDB instead of a throwaway local one: never published, deleted or "
                        "cleared; owner token = SPACETIME_TOKEN from --env-file; robot/web identities reused or minted once")
    u.add_argument("--env-file", default=str(REPO / ".env"), help="attach mode: where tokens are read and new identities stored")
    u.add_argument("--solo-resets", dest="solo_resets", action="store_true", default=None,
                   help="EVAL_SOLO_MODE=1: auto-resolve open reset tasks (default on locally, OFF when attached: it acts on "
                        "every robot's reset tasks)")
    u.add_argument("--no-solo-resets", dest="solo_resets", action="store_false")
    u.add_argument("--policy", choices=["sim", "production"], default="sim",
                   help="sim = [SIM] scripted local policy (default); production = the TDM-41 Modal endpoint from .env")
    u.add_argument("--real-modal", dest="real_modal", action="store_true", default=False,
                   help="REAL Modal training (spends credits). Default everywhere, maincloud included: --stub-modal")
    u.add_argument("--stub-modal", dest="real_modal", action="store_false",
                   help="(default) RDF_MODAL_STUB=1 + the stub trainer: [STUB] no GPU checkpoints, no credits used")
    u.add_argument("--seed-evals", action="store_true", default=os.environ.get("RDF_SEED_EVALS") == "1",
                   help="RDF_SEED_EVALS=1: evals complete at once with SEEDED outcomes (sim robots only; '[SEEDED]' notes). "
                        "Off by default: real sim trials")
    u.add_argument("--train-seconds", type=float, default=20.0, help="stub trainer: seconds a training run takes")
    u.add_argument("--auto-approve-max-cents", type=int, default=0,
                   help="FLYWHEEL_AUTO_APPROVE_MAX_CENTS (default 0 = off: approve on the ticket page)")
    u.add_argument("--trial-timeout-s", type=float, default=45.0, help="per-trial rollout limit (eval + collection)")
    u.add_argument("--passcode", default=None, help="operator passcode (default: RDF_OPERATOR_PASSCODE from .env)")
    u.add_argument("--review-config", type=Path, default=REVIEW_CONFIG)
    u.add_argument("--no-web", action="store_true")
    sub.add_parser("down")
    a = ap.parse_args()
    return up(a) if a.cmd == "up" else down(a)


if __name__ == "__main__":
    raise SystemExit(main())
