"""TDM-28 integration harness: a throwaway local SpacetimeDB + the fake robot (+ orchestrator).

One command, from the repo root:

    make test-loop                                   # pytest tests/integration (boots everything)
    make fake-stack                                  # boot it and keep it running for manual work
    # same as: cd services/orchestrator && uv run python ../../tests/integration/harness.py up

What ``up`` / the pytest fixture does:
  1. ``spacetime start`` on 127.0.0.1:$TDM28_PORT (default 3028) with a fresh data dir under
     $TMPDIR/tdm-28/ (never maincloud, never the docker 3300 container).
  2. Publishes a copy of spacetime/spacetimedb to DB ``tdm28loop`` with --delete-data=always.
     The CLI login is the publisher, so it is the owner; nothing reads SPACETIME_TOKEN.
  3. Mints fresh identities (POST /v1/identity) for ``orchestrator`` and ``robot_client`` and
     allow-lists them with ``spacetime call register_service_identity``. Tokens are written to
     $TMPDIR/tdm-28/run-<port>/tokens/*.token (0600) and never printed.
  4. Starts ``rdf-robot-client --fake`` (services/robot-client) as the robot_client identity.
  5. Optionally starts ``rdf-orchestrator`` pointed at the same DB (``--with-orchestrator``).

Point your own service at it with the env lines ``up`` prints (SPACETIME_HOST etc.).
Logs: $TMPDIR/tdm-28/run-<port>/logs/{spacetime,fake-robot,orchestrator}.log
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
MODULE_SRC = REPO / "spacetime" / "spacetimedb"
DEFAULT_PORT = int(os.environ.get("TDM28_PORT", "3028"))
DEFAULT_DB = os.environ.get("TDM28_DB", "tdm28loop")
ROOT = Path(os.environ.get("TDM28_ROOT", Path(tempfile.gettempdir()) / "tdm-28"))


def _log(msg: str) -> None:
    print(f"[tdm28-harness] {msg}", file=sys.stderr, flush=True)


def wait_for(pred: Callable[[], Any], timeout: float, what: str, every: float = 0.2) -> Any:
    end = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception as e:  # noqa: BLE001 - retry until timeout
            last_err = e
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}" + (f" (last error: {last_err})" if last_err else ""))


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class LocalStack:
    def __init__(self, port: int = DEFAULT_PORT, db: str = DEFAULT_DB, root: Path | None = None) -> None:
        self.port, self.db = port, db
        self.host = f"http://127.0.0.1:{port}"
        self.root = (root or ROOT) / f"run-{port}"
        self.logs = self.root / "logs"
        self.tokens = self.root / "tokens"
        self.media = self.root / "media"
        self.procs: dict[str, subprocess.Popen] = {}
        self._clients: dict[str, Any] = {}

    # ------------------------------------------------------------------ server
    def _reap_stale(self) -> None:
        """Kill children a previous (crashed) harness run left behind on this port."""
        pidfile = self.root / "pids.json"
        if not pidfile.exists():
            return
        for pid in json.loads(pidfile.read_text() or "[]"):
            try:
                os.killpg(pid, signal.SIGTERM)
                _log(f"stopped stale harness process group {pid}")
            except (ProcessLookupError, PermissionError):
                pass
        wait_for(lambda: not _port_open(self.port), 10, f"stale server on :{self.port} to exit")

    def start_server(self) -> None:
        self._reap_stale()
        if _port_open(self.port):
            raise RuntimeError(f"port {self.port} is already in use; set TDM28_PORT to a free port "
                               f"(or stop the old harness: pkill -f 'listen-addr 127.0.0.1:{self.port}')")
        if self.root.exists():
            shutil.rmtree(self.root)
        for d in (self.logs, self.tokens, self.media):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.tokens, 0o700)
        self._spawn("spacetime", ["spacetime", "start", "--listen-addr", f"127.0.0.1:{self.port}",
                                  "--data-dir", str(self.root / "stdb"), "--non-interactive"])
        wait_for(lambda: urllib.request.urlopen(f"{self.host}/v1/ping", timeout=1).status == 200, 30,
                 f"spacetime on :{self.port}")
        _log(f"spacetime up at {self.host} (data {self.root / 'stdb'})")

    def publish(self) -> None:
        work = self.root / "module"
        (work / "spacetimedb").mkdir(parents=True, exist_ok=True)
        for name in ("src", "package.json", "tsconfig.json"):
            src = MODULE_SRC / name
            dst = work / "spacetimedb" / name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        nm = work / "spacetimedb" / "node_modules"
        if not nm.exists():
            nm.symlink_to(MODULE_SRC / "node_modules")
        t0 = time.monotonic()
        r = subprocess.run(["spacetime", "publish", "--no-config", "-s", self.host, "-p", "./spacetimedb",
                            self.db, "--delete-data=always", "--yes"], cwd=work, capture_output=True, text=True)
        (self.logs / "publish.log").write_text(r.stdout + r.stderr)
        if r.returncode != 0:
            raise RuntimeError(f"spacetime publish failed (see {self.logs / 'publish.log'}):\n{r.stderr[-2000:]}")
        _log(f"published module to {self.db} in {time.monotonic() - t0:.1f}s")

    def owner_call(self, reducer: str, *args: Any) -> None:
        """Call a reducer as the CLI login (the publisher, i.e. the owner)."""
        r = subprocess.run(["spacetime", "call", "--no-config", "-s", self.host, self.db, reducer,
                            *[json.dumps(a) for a in args]], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"owner call {reducer} failed: {r.stderr.strip()[-500:]}")

    def make_identity(self, role: str) -> Path:
        req = urllib.request.Request(f"{self.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        path = self.tokens / f"{role}.token"
        path.write_text(d["token"])
        os.chmod(path, 0o600)
        self.owner_call("register_service_identity", {"__identity__": "0x" + d["identity"]}, role, f"test.tdm28 {role}")
        return path

    def token(self, role: str) -> str:
        return (self.tokens / f"{role}.token").read_text().strip()

    def client(self, role: str = "orchestrator"):
        """A keep-alive rdf_common SpacetimeClient acting as ``role``."""
        from rdf_common.spacetime_client import SpacetimeClient

        if role not in self._clients:
            self._clients[role] = SpacetimeClient(self.host, self.db, self.token(role))
        return self._clients[role]

    def env(self, role: str = "orchestrator") -> dict[str, str]:
        return {"SPACETIME_HOST": self.host, "SPACETIME_DB_NAME": self.db, "SPACETIME_TOKEN": self.token(role)}

    # ------------------------------------------------------------------ processes
    def _spawn(self, name: str, cmd: list[str], *, env: dict | None = None, cwd: Path | None = None) -> subprocess.Popen:
        log = open(self.logs / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=cwd or REPO,
                             env={**os.environ, **(env or {})}, start_new_session=True)
        self.procs[name] = p
        (self.root / "pids.json").write_text(json.dumps([q.pid for q in self.procs.values()]))
        return p

    def spawn_fake_robot(self, *extra: str, name: str = "fake-robot") -> subprocess.Popen:
        cmd = ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
               "rdf-robot-client", "--fake", "--host", self.host, "--db", self.db,
               "--token-file", str(self.tokens / "robot_client.token"), "--media-dir", str(self.media), *extra]
        p = self._spawn(name, cmd)
        _log(f"fake robot starting (log {self.logs / (name + '.log')})")
        return p

    def spawn_orchestrator(self, port: int = 8028, extra_env: dict | None = None) -> subprocess.Popen:
        env = {**self.env("orchestrator"), "PORT": str(port), **(extra_env or {})}
        p = self._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                         "rdf-orchestrator", "--host", "127.0.0.1", "--port", str(port)], env=env)
        wait_for(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read())
                 ["subscription"]["state"] == "live", 90, "orchestrator /healthz live")
        _log(f"orchestrator live on :{port}")
        return p

    def robot(self, name: str = "fake-yam-1", timeout: float = 60) -> dict:
        """Wait for the fake robot to register and come online; returns its row."""
        def ready():
            rows = self.client().sql(f"SELECT * FROM robot WHERE name = '{name}'")
            return rows[0] if rows and rows[0]["status"] == "online" else None
        return wait_for(ready, timeout, f"robot {name} online (see {self.logs / 'fake-robot.log'})")

    def boot(self, *, fake_robot_args: tuple[str, ...] = (), orchestrator: bool = False) -> "LocalStack":
        self.start_server()
        self.publish()
        self.make_identity("orchestrator")
        self.make_identity("robot_client")
        self.spawn_fake_robot(*fake_robot_args)
        self.robot()
        if orchestrator:
            self.spawn_orchestrator()
        return self

    def stop(self) -> None:
        for c in self._clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        for name in reversed(list(self.procs)):
            p = self.procs[name]
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in self.procs.values():
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.procs.clear()

    # ------------------------------------------------------------------ queries
    def sql(self, q: str) -> list[dict]:
        return self.client().sql(q)

    def events(self, after_id: int = 0) -> list[dict]:
        return sorted(self.sql(f"SELECT * FROM event_log WHERE id > {after_id}"), key=lambda r: r["id"])

    def newest(self, table: str, where: str = "") -> dict | None:
        rows = self.sql(f"SELECT * FROM {table}" + (f" WHERE {where}" if where else ""))
        return max(rows, key=lambda r: r["id"]) if rows else None


def main() -> None:
    p = argparse.ArgumentParser(description="TDM-28 local stack: throwaway SpacetimeDB + fake robot")
    p.add_argument("cmd", choices=["up"], nargs="?", default="up")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--with-orchestrator", action="store_true")
    p.add_argument("fake_args", nargs="*", help="extra args for the fake robot, after --  (e.g. -- --success-prob 0.5)")
    a = p.parse_args()
    st = LocalStack(a.port, a.db)

    def _term(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)  # `timeout`/kill: still stop the children
    try:
        st.boot(fake_robot_args=tuple(a.fake_args), orchestrator=a.with_orchestrator)
        print("\n# TDM-28 local stack is up. Point a service at it (orchestrator role):")
        print(f"export SPACETIME_HOST={st.host} SPACETIME_DB_NAME={st.db}")
        print(f'export SPACETIME_TOKEN="$(cat {st.tokens / "orchestrator.token"})"')
        print(f"# robot_client token: {st.tokens / 'robot_client.token'}   logs: {st.logs}")
        print("# Ctrl-C stops everything.\n", flush=True)
        while all(p.poll() is None for p in st.procs.values()):
            time.sleep(1)
        dead = [n for n, p in st.procs.items() if p.poll() is not None]
        _log(f"process exited: {dead}; see {st.logs}")
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()


if __name__ == "__main__":
    main()
