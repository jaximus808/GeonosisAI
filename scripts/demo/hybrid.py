"""TDM-110 `make demo-hybrid`: the backend half of the hybrid deploy (the web app runs on Vercel).

    make demo-hybrid                                   # --stack demo: run.py's throwaway stack, quick tunnels (tests)
    make demo-hybrid ARGS="--stack attach --orch-url http://127.0.0.1:8031 --stream-port 8792 \\
        --robot-name sim-yam-mujoco --stdb-url https://maincloud.spacetimedb.com --db robo-factory-a706j"
    make demo-hybrid-down / make demo-hybrid-status
    # same as: cd services/orchestrator && RDF_PUBLIC_ROOT=$TMPDIR/rdf-demo-hybrid uv run python \\
    #          ../../scripts/demo/public.py up --mode hybrid ...

Called from ``public.py up --mode hybrid`` (shares its process supervisor, logs and state file). Docs: the
``public.py`` docstring ("TDM-110") and docs/MORNING.md "Hybrid deploy".

What it runs (everything on 127.0.0.1; only cloudflared reaches out):
  1. the robot tunnel -> the robot's stream server (--stream-port): cameras, /ws/state, /ws/control
  2. the edge (public_guard.py, no --web) and the api tunnel -> it
  3. --stack demo: run.py up --no-web with RDF_STREAM_PUBLIC_URL=<robot URL> (the robot announces it itself)
  4. publishes event_log ``orchestrator.endpoint`` {url, robot_url, tunnel, started_at} with an orchestrator
     (or owner) identity, checks /_rdf/proof and an authenticated /healthz through Cloudflare, then keeps
     the robot row's announced stream URL on the robot tunnel (every 20 s) until stopped
  5. on stop: ``orchestrator.endpoint`` {url: null, state: "down"} (best effort), tunnels first, then the rest

RDF_EDGE_SECRET (env, else the repo .env): the shared secret between Vercel's server routes and this edge,
24+ characters. Vercel must have the same value (scripts/vercel/env-push.sh). Never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import public as P  # noqa: E402  (public.py aliases itself as `public` before importing this)
from public_guard import EDGE_SECRET_HEADER, edge_proof  # noqa: E402

ENDPOINT_EVENT = "orchestrator.endpoint"
ENDPOINT_ACTOR = "orchestrator"
DEFAULT_PORT = 3767  # never the user's demo ports (3028 / 8028 / 3737)


def _log(msg: str) -> None:
    print(f"[demo-hybrid] {msg}", flush=True)


def env_value(name: str) -> str:
    """process env, else the repo-root .env (the value is never printed)."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    f = P.REPO / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") or line.startswith(f"export {name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ----------------------------------------------------------------------------------------- stream URLs
_TAIL = re.compile(r"(/ws/[a-z_]+|/control)$")


def rebase_stream(stream: dict, robot_url: str) -> dict:
    """camera_config_json.stream with every URL moved onto ``robot_url`` (https://host[/prefix])."""
    base = robot_url.rstrip("/")
    ws_base = re.sub(r"^http", "ws", base)
    out = dict(stream)
    out["http"] = base
    for key, default in (("url", "/ws/cameras"), ("control_url", None), ("state_url", None)):
        cur = stream.get(key)
        if not cur and default is None:
            continue
        m = _TAIL.search(urlpath(cur)) if cur else None
        out[key] = ws_base + (m.group(1) if m else default or "")
    out["tunnel"] = "hybrid"
    out["updated_at"] = int(time.time())
    return out


def urlpath(u: str) -> str:
    return re.sub(r"^[a-z]+://[^/]+", "", u or "")


def stream_on(stream: dict | None, robot_url: str) -> bool:
    host = re.sub(r"^https?://", "", robot_url.rstrip("/")).split("/")[0]
    return bool(stream) and all(host in (stream.get(k) or "") for k in ("url", "http"))


# ----------------------------------------------------------------------------------------- SpacetimeDB
class Bus:
    """The few SpacetimeDB calls this script makes, with an orchestrator (or owner) token."""

    def __init__(self, host: str, db: str, token: str):
        from rdf_common.spacetime_client import SpacetimeClient

        self.c = SpacetimeClient(host, db, token)

    def publish_endpoint(self, payload: dict) -> None:
        self.c.call_reducer("append_event", ENDPOINT_ACTOR, ENDPOINT_EVENT, json.dumps(payload))

    def robot(self, name: str) -> dict | None:
        safe = name.replace("'", "''")
        rows = self.c.sql(f"SELECT * FROM robot WHERE name = '{safe}'")
        return rows[0] if rows else None

    def announce(self, row: dict, robot_url: str) -> bool:
        """Point the robot row's stream URLs at the robot tunnel. False when it already does."""
        try:
            cc = json.loads(row.get("camera_config_json") or "{}")
        except json.JSONDecodeError:
            cc = {}
        if stream_on(cc.get("stream"), robot_url):
            return False
        cc["stream"] = rebase_stream(cc.get("stream") or {}, robot_url)
        self.c.call_reducer("register_robot", row["name"], row["embodiment"], row["adapter_version"], json.dumps(cc))
        return True


def demo_stdb(demo_args: str | None) -> tuple[str | None, str | None]:
    """run.py's own SpacetimeDB flags in --demo-args (its maincloud mode), so hybrid follows run.py instead
    of assuming the throwaway local DB: --stdb-url URL / --db NAME, or --maincloud (the .env maincloud DB)."""
    parts = shlex.split(demo_args or "")
    url = db = None
    for i, p in enumerate(parts):
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        if p == "--maincloud":
            url = url or "https://maincloud.spacetimedb.com"
            db = db or env_value("SPACETIME_DB_NAME") or "robo-factory-a706j"
        elif p == "--stdb-url" and nxt:
            url = nxt
        elif p.startswith("--stdb-url="):
            url = p.split("=", 1)[1]
        elif p == "--db" and nxt:
            db = nxt
        elif p.startswith("--db="):
            db = p.split("=", 1)[1]
    return url, db


def _newest(paths) -> Path | None:
    paths = [p for p in paths if p.exists()]
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


# ----------------------------------------------------------------------------------------- tunnels
def start_named(procs, edge_port: int, stream_port: int | None) -> tuple[str, str | None]:
    cf = shutil.which("cloudflared")
    if not cf:
        raise RuntimeError("cloudflared not found: brew install cloudflared")
    domain = os.environ.get("RDF_PUBLIC_DOMAIN", "").strip()
    api_host = os.environ.get("RDF_API_HOST", "").strip() or (f"api.{domain}" if domain else "")
    robot_host = os.environ.get("RDF_ROBOT_HOST", "").strip() or (f"robot.{domain}" if domain else "")
    if not api_host or (stream_port and not robot_host):
        raise RuntimeError("--tunnel named needs RDF_PUBLIC_DOMAIN (api./robot.<domain>) or RDF_API_HOST + RDF_ROBOT_HOST")
    tunnel = os.environ.get("RDF_PUBLIC_TUNNEL") or "rdf-hybrid"
    r = subprocess.run([cf, "tunnel", "list", "--output", "json", "--name", tunnel], capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except json.JSONDecodeError:
        rows = []
    row = next((t for t in rows if t.get("name") == tunnel and not str(t.get("deleted_at", "")).startswith("2")), None)
    if not row:
        raise RuntimeError(f"named tunnel {tunnel!r} not found. One-time setup (needs the zone on Cloudflare):\n"
                           f"    cloudflared tunnel login\n    cloudflared tunnel create {tunnel}\n"
                           f"    cloudflared tunnel route dns {tunnel} {api_host}\n"
                           + (f"    cloudflared tunnel route dns {tunnel} {robot_host}\n" if robot_host else ""))
    cred = Path(os.environ.get("RDF_PUBLIC_TUNNEL_CREDENTIALS") or Path.home() / ".cloudflared" / f"{row['id']}.json")
    if not cred.exists():
        raise RuntimeError(f"tunnel credentials {cred} missing (or set RDF_PUBLIC_TUNNEL_CREDENTIALS)")
    lines = ["# generated by scripts/demo/hybrid.py (make demo-hybrid); regenerated on every run",
             f"tunnel: {row['id']}", f"credentials-file: {cred}", "no-autoupdate: true", "ingress:",
             f"  - hostname: {api_host}", f"    service: http://127.0.0.1:{edge_port}"]
    if stream_port:
        lines += [f"  - hostname: {robot_host}", f"    service: http://127.0.0.1:{stream_port}"]
    lines += ["  - service: http_status:404", ""]
    cfg = P.ROOT / "cloudflared.yml"
    cfg.write_text("\n".join(lines))
    procs.spawn("tunnel", [cf, "tunnel", "--config", str(cfg), "run", tunnel])
    P._wait(lambda: "Registered tunnel connection" in (P.LOGS / "tunnel.log").read_text(errors="replace"), 45,
            "the named tunnel to connect", 0.5, alive=lambda: procs.procs["tunnel"].poll() is None)
    return f"https://{api_host}", (f"https://{robot_host}" if stream_port else None)


# ----------------------------------------------------------------------------------------- up
def up_hybrid(a: argparse.Namespace) -> int:
    if P.STATE.exists():
        s = json.loads(P.STATE.read_text())
        try:
            os.kill(int(s.get("pid", 0)), 0)
            _log(f"already running (pid {s['pid']}); `make demo-hybrid-down` first")
            return 2
        except (ProcessLookupError, ValueError, PermissionError):
            P.STATE.unlink()
    secret = env_value("RDF_EDGE_SECRET")
    if len(secret) < 24:
        _log("RDF_EDGE_SECRET is not set (24+ chars, shared with Vercel). One-time: add it to .env, e.g.\n"
             "    echo \"RDF_EDGE_SECRET=$(openssl rand -base64 33 | tr '+/' '-_')\" >> .env\n"
             "then push it to Vercel: scripts/vercel/env-push.sh")
        return 2
    P.ROOT.mkdir(parents=True, exist_ok=True)
    P.LOGS.mkdir(exist_ok=True)
    attach = a.stack == "attach"
    port = a.port if a.port != 3747 else int(os.environ.get("RDF_HYBRID_PORT", DEFAULT_PORT))
    demo_root = P.ROOT / "demo"
    db = a.db if a.db != "rdfdemo" or not attach else (env_value("SPACETIME_DB_NAME") or a.db)
    stdb_url = (a.stdb_url or (f"http://127.0.0.1:{a.stdb_port}" if not attach else env_value("SPACETIME_HOST"))
                or "").rstrip("/")
    orch_url = (a.orch_url or f"http://127.0.0.1:{a.orch_port}").rstrip("/")
    stream_port = a.stream_port if (not attach or a.stream_port_given) else None
    run_stdb, run_db = demo_stdb(a.demo_args) if not attach else (None, None)
    if run_stdb:  # run.py talks to a remote DB (maincloud mode): follow it, never expose it
        stdb_url, db = run_stdb.rstrip("/"), run_db or db
    local_stdb = re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", stdb_url or "") is not None
    expose = a.expose_stdb or (not attach and local_stdb)
    if expose and not local_stdb:
        _log(f"--expose-stdb is for a LOCAL throwaway DB only (got {stdb_url}); the browser uses maincloud directly")
        return 2
    need_free = {"edge": port}
    if not attach:
        need_free.update(orchestrator=a.orch_port, stream=a.stream_port)
        if local_stdb:
            need_free["stdb"] = a.stdb_port
    busy = {k: p for k, p in need_free.items() if not P._port_free(p)}
    if busy:
        _log(f"ports in use: {busy}. Pick free ones (see --help) or stop what holds them.")
        return 2
    if attach and not stdb_url:
        _log("--stack attach needs --stdb-url (or SPACETIME_HOST)")
        return 2
    operator_key = os.environ.get("DEMO_OPERATOR_KEY") or ("demo-operator-" + secrets.token_hex(16))
    api_secret = secrets.token_urlsafe(24) if a.api else ""
    tunnel_kind = a.tunnel or "quick"
    P.STATE.write_text(json.dumps({"pid": os.getpid(), "started": int(time.time()), "mode": "hybrid",
                                   "tunnel": tunnel_kind}, indent=2))
    procs = P.Procs()
    stop = P.STOP
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    bus: Bus | None = None
    api_url = robot_url = None
    try:
        # 1. tunnels first: their URLs feed the edge (proof) and the robot (announce)
        if tunnel_kind == "named":
            api_url, robot_url = start_named(procs, port, stream_port)
        elif tunnel_kind == "external":  # something else serves the ports (e.g. Tailscale Funnel); no cloudflared
            api_url = os.environ.get("RDF_API_PUBLIC_URL", "").strip().rstrip("/")
            robot_url = os.environ.get("RDF_ROBOT_PUBLIC_URL", "").strip().rstrip("/") or None
            if not re.match(r"^https://[^/\s]+$", api_url) or (stream_port and not robot_url):
                raise RuntimeError("--tunnel external needs RDF_API_PUBLIC_URL (https://host[:port], -> the edge port) "
                                   "and RDF_ROBOT_PUBLIC_URL (-> the stream port)")
        else:
            robot_url = P.start_quick_tunnel(procs, stream_port, "tunnel-robot") if stream_port else None
            api_url = P.start_quick_tunnel(procs, port, "tunnel-api")
        _log(f"api tunnel ({tunnel_kind}): {api_url} -> 127.0.0.1:{port}")
        if robot_url:
            _log(f"robot tunnel ({tunnel_kind}): {robot_url} -> 127.0.0.1:{stream_port}")
        procs.save({"url": api_url, "robot_url": robot_url, "mode": "hybrid", "tunnel": tunnel_kind})

        # 2. the edge (no web: Vercel serves it)
        guard_cmd = [sys.executable, str(P.HERE / "public_guard.py"), "--listen", f"127.0.0.1:{port}",
                     "--orch", orch_url]
        if expose:
            guard_cmd += ["--stdb", stdb_url, "--db", db]
        if a.api:
            guard_cmd += ["--api", orch_url]
        procs.spawn("edge", guard_cmd, env={"RDF_PUBLIC_URL": api_url, "RDF_EDGE_SECRET": secret,
                                            "RDF_PUBLIC_API_SECRET": api_secret, "RDF_PUBLIC_ACCESS_CODE": ""})
        P._wait(lambda: P._get(f"http://127.0.0.1:{port}/_rdf/healthz", 2)[0] == 200, 30, "the edge",
                0.3, alive=lambda: procs.procs["edge"].poll() is None)

        # 3. the stack
        robot_name = a.robot_name
        if not attach:
            demo_root.mkdir(exist_ok=True)
            stale = demo_root / "demo.json"
            if stale.exists():
                stale.unlink()
            cmd = [sys.executable, str(P.HERE / "run.py"), "up", "--no-web", "--stdb-port", str(a.stdb_port),
                   "--orch-port", str(a.orch_port), "--stream-port", str(a.stream_port), "--db", db,
                   *shlex.split(a.demo_args or "")]
            procs.spawn("demo", cmd, cwd=P.REPO / "services" / "orchestrator",
                        env={"RDF_DEMO_ROOT": str(demo_root), "RDF_STREAM_PUBLIC_URL": robot_url or "",
                             "DEMO_OPERATOR_KEY": operator_key, "RDF_PUBLIC": "1"})
            _log(f"make demo (run.py up --no-web) booting; log {P.LOGS / 'demo.log'}")
            info = P._wait(lambda: json.loads(stale.read_text()) if stale.exists() else None, 420,
                           "make demo to come up (robot online)", 2, alive=lambda: procs.procs["demo"].poll() is None)
            robot_name = robot_name or "sim-yam-mujoco"
            _log(f"make demo is up (robot id {info.get('robot_id')})")
            tok_file = _newest(demo_root.glob("run-*/tokens/orchestrator.token")) if local_stdb else None
            token = tok_file.read_text().strip() if tok_file else (
                env_value("RDF_HYBRID_SPACETIME_TOKEN") or env_value("SPACETIME_TOKEN"))
        else:
            token = (Path(a.token_file).read_text().strip() if a.token_file else
                     env_value("RDF_HYBRID_SPACETIME_TOKEN") or env_value("SPACETIME_TOKEN"))
        if not token:
            raise RuntimeError("no SpacetimeDB token to publish orchestrator.endpoint with "
                               "(--token-file, RDF_HYBRID_SPACETIME_TOKEN or SPACETIME_TOKEN)")
        bus = Bus(stdb_url, db, token)

        # 4. end to end through Cloudflare: the proof Vercel will check, and an authenticated orchestrator call
        nonce = secrets.token_urlsafe(18)

        def proof_ok():
            st, body = P._get(f"{api_url}/_rdf/proof?nonce={nonce}", 8)
            try:
                return st == 200 and json.loads(body).get("proof") == edge_proof(secret, nonce, api_url)
            except ValueError:
                return False
        P._wait(proof_ok, 120, f"{api_url}/_rdf/proof through Cloudflare", 2, alive=lambda: not procs.dead())
        st, _ = P._get(f"{api_url}/healthz", 15, {EDGE_SECRET_HEADER: secret})
        if st != 200:
            raise RuntimeError(f"orchestrator /healthz through the api tunnel answered {st} (is {orch_url} up?)")
        if P._get(f"{api_url}/healthz", 8)[0] != 401:
            raise RuntimeError("the edge served the orchestrator WITHOUT the edge secret; refusing to publish")
        payload = {"url": api_url, "robot_url": robot_url, "tunnel": tunnel_kind, "started_at": int(time.time()),
                   "proof": "rdf-edge-proof-v1", "host": os.uname().nodename}
        bus.publish_endpoint(payload)
        _log(f"published {ENDPOINT_EVENT} -> {api_url} (Vercel picks it up within ~10 s, no redeploy)")

        if a.api:
            P._write_secret(P.ROOT / "api_url", f"{api_url}/_api/{api_secret}")
        announced = None
        if robot_url and robot_name:
            row = bus.robot(robot_name)
            if row is None:
                _log(f"robot {robot_name!r} not found in {db}; its stream URL is not announced")
            else:
                if bus.announce(row, robot_url):
                    _log(f"announced robot {robot_name} (id {row['id']}) stream on {robot_url}")
                announced = int(row["id"])
        procs.save({"url": api_url, "robot_url": robot_url, "robot_id": announced,
                    "ports": {"edge": port, "orchestrator": orch_url, "stream": stream_port, "stdb": stdb_url,
                              "db": db}})
        banner(api_url, robot_url, announced, expose, stdb_url, db, a.api)

        last_check = time.monotonic()
        while not stop.is_set():
            dead = procs.dead()
            if dead:
                _log(f"process exited: {dead}; stopping (logs in {P.LOGS})")
                break
            if robot_url and robot_name and time.monotonic() - last_check > 20:
                last_check = time.monotonic()
                try:
                    row = bus.robot(robot_name)
                    if row and bus.announce(row, robot_url):
                        _log(f"re-announced robot {robot_name} stream on {robot_url} (it had been overwritten)")
                except Exception as e:  # noqa: BLE001  (a DB blip must not take the demo down)
                    _log(f"robot stream check failed (will retry): {e}")
            stop.wait(1.0)
    except Exception as e:  # noqa: BLE001
        _log(f"failed: {e}")
        return 1
    finally:
        if bus is not None and api_url:
            try:
                bus.publish_endpoint({"url": None, "state": "down", "previous_url": api_url,
                                      "stopped_at": int(time.time())})
                _log(f"published {ENDPOINT_EVENT} state=down")
            except Exception as e:  # noqa: BLE001
                _log(f"could not publish state=down ({e}); Vercel finds the old URL dead via /_rdf/proof anyway")
        _log("stopping: tunnels first, then the rest ...")
        procs.stop_all()
        for f in (P.STATE, P.ROOT / "api_url"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        _log("stopped")
    return 0


def banner(api_url: str, robot_url: str | None, robot_id, expose: bool, stdb_url: str, db: str, api: bool) -> None:
    line = "=" * 78
    print("\n" + line, flush=True)
    print("  HYBRID BACKEND UP: the web app (Vercel) finds this backend through SpacetimeDB", flush=True)
    print(line, flush=True)
    rows = [("Orchestrator (api)", f"{api_url}  (edge secret only; /_rdf/proof for Vercel)"),
            ("Robot stream", robot_url or "(none: no --stream-port)"),
            ("Robot id", str(robot_id) if robot_id is not None else "-"),
            ("SpacetimeDB", f"{stdb_url} / {db}" + (f"  (browser: {api_url}/v1, test only)" if expose else ""))]
    if api:
        rows.append(("Jetson / Modal API", f"contents of {P.ROOT / 'api_url'} (secret path)"))
    rows.append(("Logs", str(P.LOGS)))
    for k, v in rows:
        print(f"  {k:<22} {v}", flush=True)
    print("  Vercel needs RDF_EDGE_SECRET (same value) and NO RDF_ORCHESTRATOR_URL (or the discovery sentinel);\n"
          "  see docs/MORNING.md 'Hybrid deploy'. These URLs change on every start; nothing needs updating.", flush=True)
    print("  Stop: Ctrl-C here, or `make demo-hybrid-down` from another terminal.", flush=True)
    print(line + "\n", flush=True)
