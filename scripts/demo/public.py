"""`make demo-public`: the local demo (``make demo``), reachable from phones over HTTPS through Cloudflare.

    make demo-public                       # quick tunnel (random https://*.trycloudflare.com), make demo underneath
    make demo-public-down                  # from another terminal: tunnel first, then everything else
    make demo-public-status                # URLs, access code, pids
    make demo-public ARGS="--port 3461 --web-port 3462 --stdb-port 3463 --orch-port 8463 --stream-port 8464 \
        --demo-args '--sim-state-port 8465 --policy-port 8466'"          # other ports (all of them free)
    RDF_PUBLIC_DOMAIN=example.com make demo-public                        # named tunnel: https://app.example.com
    make demo-public ARGS="--stack attach --stdb-url https://maincloud.spacetimedb.com --db robo-factory-a706j \
        --orch-url http://127.0.0.1:8001"                                 # a stack you started yourself
    # same as: cd services/orchestrator && uv run --with qrcode python ../../scripts/demo/public.py up|down|status

What it runs (everything listens on 127.0.0.1 only; nothing is reachable on the venue Wi-Fi):
  1. ``scripts/demo/run.py up --no-web`` (``make demo``: SpacetimeDB, orchestrator, MuJoCo [SIM] robot,
     sim policy) with RDF_STREAM_PUBLIC_URL=<public>/_cam (the robot announces its stream on the public
     origin), RDF_WEB_PUBLIC_URL=<public> (absolute push links) and DEMO_OPERATOR_KEY (see "Operator key").
     ``--stack attach`` skips this and uses --stdb-url / --orch-url / --stream-port instead.
  2. The web app as a PRODUCTION build (``next build`` once, then ``next start``: no dev overlay, no
     source maps), dist dir apps/web/.next-public. The build does not depend on the tunnel URL:
     NEXT_PUBLIC_SPACETIME_HOST=same-origin and NEXT_PUBLIC_ORCHESTRATOR_URL=/api/orch.
  3. The edge, ``scripts/demo/public_guard.py``: one local port in front of the web app, SpacetimeDB
     (/v1, allow-listed), the robot stream (/_cam) and, with --api, an orchestrator capability path for a
     remote Jetson / Modal (/_api/<secret>). Access code, rate limits. Docstring there.
  4. ONE tunnel to the edge: a cloudflared quick tunnel (default), or, with RDF_PUBLIC_DOMAIN, a named
     tunnel serving app.<domain> from a generated config.yml (one-time setup below).

Access code (default ON in public mode): RDF_PUBLIC_ACCESS_CODE, else a random 6-character code per run.
It is printed next to the terminal QR code and shown under the dashboard's QR on this laptop; a phone
enters it once at /join (HttpOnly cookie, 7 days). ``--no-access-code`` turns it off (not recommended).

Named tunnel, one-time setup (needs a Cloudflare account with the domain; this script never logs in):
    cloudflared tunnel login                                   # browser: pick the zone (writes ~/.cloudflared/cert.pem)
    cloudflared tunnel create rdf-demo                         # writes ~/.cloudflared/<id>.json
    cloudflared tunnel route dns rdf-demo app.example.com      # CNAME app -> the tunnel
    RDF_PUBLIC_DOMAIN=example.com make demo-public             # RDF_PUBLIC_TUNNEL (default rdf-demo), RDF_PUBLIC_HOSTNAME

Operator key: the web server's operator routes (robot commands, the robot page camera) send the
orchestrator's X-RDF-Operator-Key. run.py makes a throwaway one per run; this passes DEMO_OPERATOR_KEY
and checks that the orchestrator accepts it, and says so if it does not (then use the operator pages on
make demo's own web, or run.py must read DEMO_OPERATOR_KEY).

State (no secrets in public.json): $RDF_PUBLIC_ROOT (default $TMPDIR/rdf-demo-public): public.json,
access_code (0600), api_url (0600, --api only), logs/, cloudflared.yml, demo/ (run.py's RDF_DEMO_ROOT).

TDM-110 ``--mode hybrid`` (``make demo-hybrid``; docs/MORNING.md "Hybrid deploy"): the web app runs on Vercel,
this machine runs only the backend. No web build here. Two public URLs:
  api    -> the edge (public_guard.py without --web): /_rdf/proof, the orchestrator for requests carrying
            x-rdf-edge-secret (RDF_EDGE_SECRET, shared with Vercel), /_api/<secret> (--api), and with
            --expose-stdb the throwaway DB's /v1 allow-list (test only; production uses maincloud).
  robot  -> the robot's TDM-55 stream server (cameras, /ws/state, /ws/control; every one needs a stream token).
``--tunnel quick`` (default): two cloudflared quick tunnels, new random URLs on every start. Nothing user-facing
holds them: the api URL is published to SpacetimeDB as event_log ``orchestrator.endpoint`` (actor
``orchestrator``; append_event refuses anonymous callers and a non-orchestrator service may not log as
``orchestrator``), Vercel's server routes read it (10 s cache) and use it only after /_rdf/proof matches; the
robot URL is announced in robot.camera_config_json.stream (re-announced if the robot overwrites it).
``--tunnel named``: one named tunnel serving RDF_API_HOST and RDF_ROBOT_HOST (default api./robot.<RDF_PUBLIC_DOMAIN>).
  --stack demo     run.py's throwaway stack (local DB; the browser reaches it only via --expose-stdb): tests.
  --stack attach   an orchestrator (--orch-url) and robot (--stream-port, --robot-name) you already run, e.g.
                   against maincloud. Publishing needs an orchestrator (or owner) token: --token-file, else
                   RDF_HYBRID_SPACETIME_TOKEN, else SPACETIME_TOKEN (env or .env).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
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
HERE = Path(__file__).resolve().parent
WEB = REPO / "apps" / "web"
ROOT = Path(os.environ.get("RDF_PUBLIC_ROOT") or Path(tempfile.gettempdir()) / "rdf-demo-public")
STATE = ROOT / "public.json"
LOGS = ROOT / "logs"
DIST = ".next-public"
TRYCLOUDFLARE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
STOP = threading.Event()  # Ctrl-C / SIGTERM (make demo-public-down)


def _log(msg: str) -> None:
    print(f"[demo-public] {msg}", flush=True)


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _get(url: str, timeout: float = 5.0, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"user-agent": "rdf-demo-public/1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 10.0) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def _wait(pred, timeout: float, what: str, every: float = 1.0, alive=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if STOP.is_set():
            raise RuntimeError(f"stopped while waiting for {what}")
        if alive is not None and not alive():
            raise RuntimeError(f"gave up waiting for {what}: a process exited (logs in {LOGS})")
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise RuntimeError(f"timed out after {timeout:.0f}s waiting for {what} (logs in {LOGS})")


def _write_secret(path: Path, value: str) -> None:
    path.write_text(value + "\n")
    os.chmod(path, 0o600)


class Procs:
    """Children in their own process groups, logged to LOGS/<name>.log, stopped in reverse start order."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()  # the web build spawns from its own thread

    def spawn(self, name: str, cmd: list[str], *, env: dict | None = None, cwd: Path | None = None) -> subprocess.Popen:
        log = open(LOGS / f"{name}.log", "ab")
        with self.lock:
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=cwd or REPO,
                                 env={**os.environ, **(env or {})}, start_new_session=True)
            self.procs[name] = p
            self.save()
        return p

    def save(self, extra: dict | None = None) -> None:
        with self.lock:
            s = json.loads(STATE.read_text()) if STATE.exists() else {}
            s.update({"pid": os.getpid(), "pgids": {n: p.pid for n, p in self.procs.items()}}, **(extra or {}))
            STATE.write_text(json.dumps(s, indent=2))

    def dead(self) -> list[str]:
        return [n for n, p in self.procs.items() if p.poll() is not None]

    def stop(self, name: str, timeout: float = 10.0) -> None:
        p = self.procs.get(name)
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop_all(self) -> None:
        for t in [n for n in self.procs if n.startswith("tunnel")]:  # cut the public links first
            self.stop(t, 5)
        for name in reversed(list(self.procs)):
            self.stop(name, 40 if name == "demo" else 10)


# ----------------------------------------------------------------------------------------- tunnels
def start_quick_tunnel(procs: Procs, port: int, name: str = "tunnel") -> str:
    cf = shutil.which("cloudflared")
    if not cf:
        raise RuntimeError("cloudflared not found: brew install cloudflared")
    log = LOGS / f"{name}.log"
    log.write_bytes(b"")  # a URL from a previous run must not be picked up
    procs.spawn(name, [cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"])

    def url():
        m = TRYCLOUDFLARE.findall(log.read_text(errors="replace")) if log.exists() else []
        return m[-1] if m else None
    return _wait(url, 60, f"the cloudflared quick tunnel URL ({name})", 0.5,
                 alive=lambda: procs.procs[name].poll() is None)


def named_tunnel_steps(tunnel: str, hostname: str) -> str:
    return ("One-time setup (this script never logs in for you):\n"
            "    cloudflared tunnel login\n"
            f"    cloudflared tunnel create {tunnel}\n"
            f"    cloudflared tunnel route dns {tunnel} {hostname}\n")


def start_named_tunnel(procs: Procs, port: int, domain: str) -> str:
    cf = shutil.which("cloudflared")
    if not cf:
        raise RuntimeError("cloudflared not found: brew install cloudflared")
    tunnel = os.environ.get("RDF_PUBLIC_TUNNEL") or "rdf-demo"
    hostname = os.environ.get("RDF_PUBLIC_HOSTNAME") or f"app.{domain}"
    r = subprocess.run([cf, "tunnel", "list", "--output", "json", "--name", tunnel], capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except json.JSONDecodeError:
        rows = []
    row = next((t for t in rows if t.get("name") == tunnel and not t.get("deleted_at", "").startswith("2")), None) \
        if isinstance(rows, list) else None
    if not row:
        raise RuntimeError(f"named tunnel {tunnel!r} not found ({(r.stderr or '').strip()[-200:]}).\n"
                           + named_tunnel_steps(tunnel, hostname))
    tid = row["id"]
    cred = Path(os.environ.get("RDF_PUBLIC_TUNNEL_CREDENTIALS") or Path.home() / ".cloudflared" / f"{tid}.json")
    if not cred.exists():
        raise RuntimeError(f"tunnel credentials {cred} missing (cloudflared tunnel create writes it; "
                           "or set RDF_PUBLIC_TUNNEL_CREDENTIALS)")
    cfg = ROOT / "cloudflared.yml"
    # One hostname: the edge routes /v1 (SpacetimeDB), /_cam (robot stream) and /_api (--api) by path,
    # so no db./cam./api. subdomains are needed (fewer DNS records, no CORS).
    cfg.write_text("\n".join([
        "# generated by scripts/demo/public.py (make demo-public); regenerated on every run",
        f"tunnel: {tid}", f"credentials-file: {cred}", "no-autoupdate: true", "ingress:",
        f"  - hostname: {hostname}", f"    service: http://127.0.0.1:{port}",
        "  - service: http_status:404", ""]))
    procs.spawn("tunnel", [cf, "tunnel", "--config", str(cfg), "run", tunnel])
    _wait(lambda: "Registered tunnel connection" in (LOGS / "tunnel.log").read_text(errors="replace"), 45,
          "the named tunnel to connect", 0.5, alive=lambda: procs.procs["tunnel"].poll() is None)
    return f"https://{hostname}"


# ----------------------------------------------------------------------------------------- web build
def _build_stamp(build_env: dict) -> str:
    h = hashlib.sha256(json.dumps(build_env, sort_keys=True).encode())
    h.update(subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.encode())
    newest = 0.0
    for d in (WEB / "src", WEB / "public", REPO / "packages", WEB / "next.config.ts", WEB / "package.json"):
        paths = [d] if d.is_file() else (p for p in d.rglob("*") if "node_modules" not in p.parts) if d.exists() else []
        for p in paths:
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
    h.update(str(newest).encode())
    return h.hexdigest()


def build_web(procs: "Procs", build_env: dict, force: bool) -> None:
    stamp_file = WEB / DIST / "rdf-public-stamp"
    stamp = _build_stamp(build_env)
    if not force and stamp_file.exists() and stamp_file.read_text().strip() == stamp and (WEB / DIST / "BUILD_ID").exists():
        _log("web: production build is current (apps/web/.next-public); --rebuild forces one")
        return
    _log("web: next build (production; ~1-3 min) ...")
    t0 = time.monotonic()
    (LOGS / "build.log").write_bytes(b"")
    p = procs.spawn("build", ["pnpm", "exec", "next", "build"], cwd=WEB, env=build_env)
    rc = p.wait()
    procs.procs.pop("build", None)
    if rc != 0:
        if STOP.is_set():
            raise RuntimeError("stopped during next build")
        tail = (LOGS / "build.log").read_text(errors="replace")[-1500:]
        raise RuntimeError(f"next build failed (full log {LOGS / 'build.log'}):\n{tail}")
    stamp_file.write_text(stamp)
    _log(f"web: built in {time.monotonic() - t0:.0f}s")


# ----------------------------------------------------------------------------------------- output
def print_qr(url: str) -> None:
    q = shutil.which("qrencode")
    if q:
        subprocess.run([q, "-t", "ansiutf8", "-m", "2", url])
        return
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("  (no terminal QR: brew install qrencode, or run through make demo-public, which adds qrcode)")


def banner(url: str, code: str, local: str, robot_id, api_note: str | None, operator_ok: bool | None) -> None:
    line = "=" * 78
    print("\n" + line, flush=True)
    print("  PUBLIC LINK: anyone with this URL (and the access code) can open the site", flush=True)
    print(line, flush=True)
    print_qr(f"{url}/join")
    rows = [("Phone (scan / open)", f"{url}/join"),
            ("Access code", code or "OFF (--no-access-code): anyone with the link gets in"),
            ("Dashboard (laptop)", f"{local}/  (QR + access code shown there too)"),
            ("Operator login", f"{url}/ops/login  (RDF_OPERATOR_PASSCODE)"),
            ("Marketplace", f"{url}/marketplace")]
    if robot_id is not None:
        rows.append(("Robot page (camera)", f"{url}/robots/{robot_id}"))
    if api_note:
        rows.append(("Jetson / Modal API", api_note))
    rows.append(("Logs", str(LOGS)))
    for k, v in rows:
        print(f"  {k:<22} {v}", flush=True)
    if operator_ok is False:
        print("  NOTE: the orchestrator did not accept DEMO_OPERATOR_KEY, so operator robot commands and the\n"
              "        robot page camera fail on the public web (ticket, plan and marketplace pages work).\n"
              "        Fix: scripts/demo/run.py reads DEMO_OPERATOR_KEY (or use --stack attach with your key).", flush=True)
    print("  Stop: Ctrl-C here, or `make demo-public-down` from another terminal (the tunnel goes first).", flush=True)
    print(line + "\n", flush=True)


# ----------------------------------------------------------------------------------------- up / down
def up(a: argparse.Namespace) -> int:
    if a.mode == "hybrid":
        sys.modules.setdefault("public", sys.modules[__name__])  # hybrid.py shares this module's state
        from hybrid import up_hybrid  # scripts/demo/hybrid.py (TDM-110)

        return up_hybrid(a)
    if STATE.exists():
        s = json.loads(STATE.read_text())
        try:
            os.kill(int(s.get("pid", 0)), 0)
            _log(f"already running (pid {s['pid']}); `make demo-public-down` first")
            return 2
        except (ProcessLookupError, ValueError, PermissionError):
            STATE.unlink()
    ROOT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    web_port = a.web_port or a.port + 1
    demo_root = ROOT / "demo"
    attach = a.stack == "attach"
    stdb_url = (a.stdb_url or f"http://127.0.0.1:{a.stdb_port}").rstrip("/")
    orch_url = (a.orch_url or f"http://127.0.0.1:{a.orch_port}").rstrip("/")
    stdb_local = re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", stdb_url) is not None
    need_free = {"edge": a.port, "web": web_port}
    if not attach:
        need_free.update(stdb=a.stdb_port, orchestrator=a.orch_port, stream=a.stream_port)
    busy = {k: p for k, p in need_free.items() if not _port_free(p)}
    if busy:
        _log(f"ports in use: {busy}. Pick free ones (see --help) or stop what holds them.")
        return 2

    code = "" if a.no_access_code else normalize(os.environ.get("RDF_PUBLIC_ACCESS_CODE") or
                                                 "".join(secrets.choice(CODE_ALPHABET) for _ in range(6)))
    if code:
        _write_secret(ROOT / "access_code", code)
    operator_key = os.environ.get("DEMO_OPERATOR_KEY") or ("demo-operator-" + secrets.token_hex(16))
    api_secret = secrets.token_urlsafe(24) if a.api else ""
    STATE.write_text(json.dumps({"pid": os.getpid(), "started": int(time.time()), "mode": a.mode}, indent=2))

    procs = Procs()
    stop = STOP
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        # 1. tunnel (first: its URL feeds everything else; it only answers 502 until the edge is up)
        domain = os.environ.get("RDF_PUBLIC_DOMAIN", "").strip()
        mode = a.mode or ("named" if domain else "quick")
        if mode == "named" and not domain:
            raise RuntimeError("--mode named needs RDF_PUBLIC_DOMAIN=example.com")
        url = start_named_tunnel(procs, a.port, domain) if mode == "named" else start_quick_tunnel(procs, a.port)
        _log(f"tunnel ({mode}): {url} -> 127.0.0.1:{a.port}")
        procs.save({"url": url, "mode": mode})

        # 2. the edge
        guard_cmd = [sys.executable, str(HERE / "public_guard.py"), "--listen", f"127.0.0.1:{a.port}",
                     "--web", f"http://127.0.0.1:{web_port}", "--stdb", stdb_url, "--db", a.db]
        if a.stream_port and (not attach or a.stream_port_given):
            guard_cmd += ["--stream", f"http://127.0.0.1:{a.stream_port}"]
        if a.api:
            guard_cmd += ["--api", orch_url]
        procs.spawn("edge", guard_cmd, env={"RDF_PUBLIC_ACCESS_CODE": code, "RDF_PUBLIC_URL": url,
                                            "RDF_PUBLIC_API_SECRET": api_secret})
        _wait(lambda: _get(f"http://127.0.0.1:{a.port}/_rdf/healthz", 2)[0] == 200, 30, "the edge",
              0.3, alive=lambda: procs.procs["edge"].poll() is None)

        # 3. the stack (make demo without its dev web) and, in parallel, the production web build
        build_env = {"NEXT_DIST_DIR": DIST, "NEXT_PUBLIC_SPACETIME_HOST": "same-origin" if stdb_local else stdb_url,
                     "NEXT_PUBLIC_SPACETIME_DB_NAME": a.db, "NEXT_PUBLIC_ORCHESTRATOR_URL": "/api/orch",
                     "NEXT_PUBLIC_WEB_PUBLIC_URL": "", "NEXT_PUBLIC_RDF_ENV": "public-demo",
                     "NEXT_TELEMETRY_DISABLED": "1"}
        build_err: list[BaseException] = []
        builder = threading.Thread(target=lambda: _try(build_err, build_web, procs, build_env, a.rebuild), daemon=True)
        builder.start()
        robot_id = None
        if not attach:
            demo_root.mkdir(exist_ok=True)
            stale = demo_root / "demo.json"
            if stale.exists():
                stale.unlink()
            cmd = [sys.executable, str(HERE / "run.py"), "up", "--no-web", "--web-port", str(web_port),
                   "--stdb-port", str(a.stdb_port), "--orch-port", str(a.orch_port), "--stream-port", str(a.stream_port),
                   "--db", a.db, *shlex.split(a.demo_args or "")]
            procs.spawn("demo", cmd, cwd=REPO / "services" / "orchestrator",
                        env={"RDF_DEMO_ROOT": str(demo_root), "RDF_STREAM_PUBLIC_URL": f"{url}/_cam",
                             "RDF_WEB_PUBLIC_URL": url, "DEMO_OPERATOR_KEY": operator_key, "RDF_PUBLIC": "1"})
            _log(f"make demo (run.py up --no-web) booting; log {LOGS / 'demo.log'}")
            info = _wait(lambda: json.loads(stale.read_text()) if stale.exists() else None, 420,
                         "make demo to come up (robot online)", 2, alive=lambda: procs.procs["demo"].poll() is None)
            robot_id = info.get("robot_id")
            _log(f"make demo is up (robot id {robot_id})")
        builder.join()
        if build_err:
            raise build_err[0]

        # 4. the production web server
        web_env = {**build_env, "PORT": str(web_port), "RDF_ORCHESTRATOR_URL": orch_url,
                   "RDF_SPACETIME_SERVER_HOST": stdb_url, "SPACETIME_HOST": stdb_url, "SPACETIME_DB_NAME": a.db}
        if not attach:
            web_env["RDF_OPERATOR_KEY"] = operator_key
            tok = next(demo_root.glob("run-*/tokens/web.token"), None)
            if tok:
                web_env["RDF_WEB_SPACETIME_TOKEN"] = tok.read_text().strip()
            pc = _demo_passcode(a.demo_args)
            if pc:
                web_env["RDF_OPERATOR_PASSCODE"] = pc
        procs.spawn("web", ["pnpm", "exec", "next", "start", "-H", "127.0.0.1", "-p", str(web_port)], cwd=WEB, env=web_env)
        _wait(lambda: _get(f"http://127.0.0.1:{web_port}/join", 10)[0] == 200, 90, "next start",
              1, alive=lambda: procs.procs["web"].poll() is None)
        _log(f"web (production) on 127.0.0.1:{web_port}")

        # 5. end to end through Cloudflare (quick-tunnel DNS can take a few seconds)
        _wait(lambda: _get(f"{url}/_rdf/healthz", 8)[0] == 200, 120, f"{url} to answer through Cloudflare", 2,
              alive=lambda: not procs.dead())
        operator_ok = None
        if not attach and robot_id is not None:
            st = _post_json(f"{orch_url}/stream/token", {"scope": "view", "robot_id": int(robot_id)},
                            {"x-rdf-operator-key": operator_key})
            operator_ok = st == 200
        api_note = None
        if a.api:
            _write_secret(ROOT / "api_url", f"{url}/_api/{api_secret}")
            api_note = f"JETSON_ORCHESTRATOR_URL / ORCHESTRATOR_URL = contents of {ROOT / 'api_url'} (secret path)"
        procs.save({"url": url, "local": f"http://localhost:{a.port}", "robot_id": robot_id,
                    "ports": {"edge": a.port, "web": web_port, "stdb": stdb_url, "orchestrator": orch_url,
                              "stream": a.stream_port}})
        banner(url, code, f"http://localhost:{a.port}", robot_id, api_note, operator_ok)

        while not stop.is_set():
            dead = procs.dead()
            if dead:
                _log(f"process exited: {dead}; stopping (logs in {LOGS})")
                break
            stop.wait(1.0)
    except Exception as e:  # noqa: BLE001
        _log(f"failed: {e}")
        return 1
    finally:
        _log("stopping: tunnel first, then the rest ...")
        procs.stop_all()
        for f in (STATE, ROOT / "api_url"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        _log("stopped; the public link is gone")
    return 0


def _try(errs: list, fn, *args) -> None:
    try:
        fn(*args)
    except BaseException as e:  # noqa: BLE001
        errs.append(e)


def _demo_passcode(demo_args: str | None) -> str | None:
    """--passcode in --demo-args (run.py's own flag), so both webs accept the same passcode."""
    parts = shlex.split(demo_args or "")
    for i, p in enumerate(parts):
        if p == "--passcode" and i + 1 < len(parts):
            return parts[i + 1]
        if p.startswith("--passcode="):
            return p.split("=", 1)[1]
    return None


def normalize(code: str) -> str:
    return re.sub(r"[\s\-_.]", "", code).upper()


def down(_: argparse.Namespace) -> int:
    if not STATE.exists():
        _log(f"nothing running ({STATE})")
        return 0
    s = json.loads(STATE.read_text())
    pg = s.get("pgids") or {}
    for t in [n for n in pg if n.startswith("tunnel")]:  # the public links go first, even if the supervisor is stuck
        try:
            os.killpg(int(pg[t]), signal.SIGTERM)
            _log(f"{t} stopped")
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.kill(int(s["pid"]), signal.SIGTERM)
        _log(f"stopping demo-public (pid {s['pid']})")
    except (ProcessLookupError, KeyError):
        pass
    end = time.monotonic() + 60
    while time.monotonic() < end and STATE.exists():
        time.sleep(0.5)
    for name, g in pg.items():
        try:
            os.killpg(int(g), signal.SIGKILL)
            _log(f"killed leftover {name}")
        except (ProcessLookupError, PermissionError):
            pass
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass
    _log("down")
    return 0


def status(_: argparse.Namespace) -> int:
    if not STATE.exists():
        _log("not running")
        return 1
    s = json.loads(STATE.read_text())
    code_file = ROOT / "access_code"
    for k in ("url", "robot_url", "local", "mode", "tunnel", "robot_id", "ports", "pid", "pgids"):
        if k in s:
            print(f"  {k:<10} {s[k]}")
    if code_file.exists():
        print(f"  {'code':<10} {code_file.read_text().strip()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--mode", choices=["quick", "named", "hybrid"], default=None,
                   help="quick = trycloudflare (default); named = RDF_PUBLIC_DOMAIN (the default when it is set); "
                        "hybrid = backend only, the web app is on Vercel (TDM-110, scripts/demo/hybrid.py)")
    u.add_argument("--tunnel", choices=["quick", "named", "external"], default=None,
                   help="hybrid: quick tunnels (default), one named tunnel on RDF_API_HOST / RDF_ROBOT_HOST, or "
                        "external (RDF_API_PUBLIC_URL / RDF_ROBOT_PUBLIC_URL already serve the ports, e.g. Tailscale Funnel)")
    u.add_argument("--expose-stdb", action="store_true",
                   help="hybrid: serve the local DB's /v1 allow-list on the api URL (default with --stack demo)")
    u.add_argument("--token-file", default=None,
                   help="hybrid attach: orchestrator (or owner) SpacetimeDB token for orchestrator.endpoint")
    u.add_argument("--robot-name", default=None, help="hybrid attach: the robot whose stream URL to announce")
    u.add_argument("--port", type=int, default=int(os.environ.get("RDF_PUBLIC_PORT", 3747)), help="the edge (tunnel target)")
    u.add_argument("--web-port", type=int, default=int(os.environ.get("RDF_PUBLIC_WEB_PORT", 0)) or None,
                   help="next start (default --port + 1)")
    u.add_argument("--stack", choices=["demo", "attach"], default="demo",
                   help="demo = run make demo (scripts/demo/run.py) underneath; attach = a stack you already run")
    u.add_argument("--stdb-port", type=int, default=int(os.environ.get("DEMO_STDB_PORT", 3030)))
    u.add_argument("--orch-port", type=int, default=int(os.environ.get("DEMO_ORCH_PORT", 8030)))
    u.add_argument("--stream-port", type=int, default=None,
                   help="robot stream server (make demo: DEMO_STREAM_PORT, 8791). With --stack attach: only if "
                        "the robot runs with RDF_STREAM=none RDF_STREAM_PUBLIC_URL=<public>/_cam")
    u.add_argument("--stdb-url", default=None, help="attach: SpacetimeDB base URL (a non-local one, e.g. maincloud, "
                                                    "is used by the browser directly)")
    u.add_argument("--orch-url", default=None, help="attach: orchestrator base URL")
    u.add_argument("--db", default=os.environ.get("DEMO_DB", "rdfdemo"))
    u.add_argument("--demo-args", default="", help="extra args for run.py up, e.g. \"--sim-state-port 8465 --policy-port 8466\"")
    u.add_argument("--api", action="store_true", help="expose the orchestrator's upload/progress routes to a REMOTE "
                                                      "Jetson / Modal under a secret path (off by default)")
    u.add_argument("--no-access-code", action="store_true", help="no access code (anyone with the link gets in)")
    u.add_argument("--rebuild", action="store_true", help="force next build")
    sub.add_parser("down")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "up":
        a.stream_port_given = a.stream_port is not None
        if a.stream_port is None:
            a.stream_port = int(os.environ.get("DEMO_STREAM_PORT", 8791))
        return up(a)
    return down(a) if a.cmd == "down" else status(a)


if __name__ == "__main__":
    raise SystemExit(main())
