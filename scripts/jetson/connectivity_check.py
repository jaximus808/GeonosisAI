"""TDM-44: outbound connectivity check for the robot client, run ON THE JETSON (or the laptop).

    robot-client check                    # on the Jetson: env from /etc/rdf/*.env
    make jetson-check                     # from the laptop, over ssh
    python scripts/jetson/connectivity_check.py --no-policy   # laptop, env from the shell / .env

Checks, each PASS / WARN / FAIL / SKIP:
  clock       local clock vs the SpacetimeDB server's Date header (JWTs and episode timestamps)
  dns+tcp     resolve + TCP connect time to every configured host (firewall problems show up here)
  spacetime   /v1/ping, an authenticated SQL read of `robot`, a WebSocket subscription (the client's
              real path), and the token's identity (the allow-list itself is private; the client's
              register_robot call on start proves it)
  policy      latency to the Modal policy endpoint: reuses spikes/tdm-37-latency/bench_client.py
              (http, optionally ws). WAKES A GPU CONTAINER (~33 s cold start) -> --no-policy skips it.
              Also GET {POLICY_SERVER_URL}/health for the production policy server (TDM-41).
  media       {RDF_MEDIA_STORE_URL}/healthz, and when the orchestrator is reachable a real signed
              upload: POST /uploads/sign -> PUT 1 KiB -> sha256 must match (key tickets/tdm44-probe/...)
  orchestrator {ORCHESTRATOR_URL}/healthz (runs on the laptop tonight: the Jetson needs a LAN IP or tunnel)

Never prints secret values. Exit code 1 if any check FAILs. --json writes a machine-readable report.
"""
from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BENCH = REPO / "spikes" / "tdm-37-latency" / "bench_client.py"
# TDM-37 laptop baseline (H100, us-west), for comparison
LAPTOP_BASELINE = {"http": {"p50": None, "p95": None}, "ws": {"p50": 157.0, "p95": 183.0}}

RESULTS: list[dict[str, Any]] = []
COLORS = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "SKIP": "\033[2m"}


def report(check: str, status: str, detail: str, **data: Any) -> None:
    RESULTS.append({"check": check, "status": status, "detail": detail, **data})
    c, off = (COLORS[status], "\033[0m") if sys.stdout.isatty() else ("", "")
    print(f"  {c}{status:<4}{off}  {check:<22} {detail}", flush=True)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or default


def load_dotenv_fallback() -> None:
    """On the laptop, fill missing vars from the repo .env (the Jetson gets them from systemd/env files)."""
    path = Path(os.environ.get("RDF_ENV_FILE") or REPO / ".env")
    if not path.is_file() or not os.access(path, os.R_OK):
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- checks

def check_clock(host: str) -> None:
    try:
        t0 = time.time()
        r = httpx.get(f"{host}/v1/ping", timeout=10)
        t1 = time.time()
        server = email.utils.parsedate_to_datetime(r.headers["date"]).timestamp()
        skew = (t0 + t1) / 2 - server
        st = "PASS" if abs(skew) < 2 else "WARN" if abs(skew) < 30 else "FAIL"
        report("clock", st, f"skew {skew:+.1f} s vs SpacetimeDB Date header"
               + ("" if st == "PASS" else " (fix NTP: timedatectl set-ntp true)"), skew_s=round(skew, 2))
    except Exception as e:  # noqa: BLE001
        report("clock", "WARN", f"could not compare: {type(e).__name__}: {e}")


def check_tcp(name: str, url: str | None, required: bool = True) -> bool:
    if not url:
        report(f"dns+tcp {name}", "SKIP", "not configured")
        return False
    u = urllib.parse.urlparse(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme in ("https", "wss") else 80)
    try:
        t0 = time.perf_counter()
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        t_dns = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        with socket.create_connection((host, port), timeout=6):
            pass
        t_tcp = (time.perf_counter() - t1) * 1000
        report(f"dns+tcp {name}", "PASS", f"{host}:{port} dns {t_dns:.0f} ms, connect {t_tcp:.0f} ms ({infos[0][4][0]})",
               dns_ms=round(t_dns, 1), connect_ms=round(t_tcp, 1))
        return True
    except Exception as e:  # noqa: BLE001
        hint = " (localhost on the Jetson is the Jetson itself)" if host in ("localhost", "127.0.0.1") else ""
        report(f"dns+tcp {name}", "FAIL" if required else "WARN", f"{host}:{port}: {type(e).__name__}: {e}{hint}")
        return False


def check_spacetime(host: str, db: str, token: str | None, robot_id: str, ws: bool) -> None:
    try:
        t0 = time.perf_counter()
        r = httpx.get(f"{host}/v1/ping", timeout=10)
        ms = (time.perf_counter() - t0) * 1000
        report("spacetime ping", "PASS" if r.status_code < 300 else "FAIL", f"HTTP {r.status_code} in {ms:.0f} ms")
    except Exception as e:  # noqa: BLE001
        report("spacetime ping", "FAIL", f"{type(e).__name__}: {e}")
        return
    if not token:
        report("spacetime auth", "FAIL", "SPACETIME_TOKEN is not set")
        return
    claims = jwt_claims(token)
    ident = claims.get("hex_identity")
    report("spacetime identity", "PASS",
           (f"token identity 0x{str(ident)[:16]}..." if ident else f"token from issuer {claims.get('iss', '?')} (no hex_identity claim)")
           + " (allow-list is private; register_robot on client start proves the robot_client role)",
           identity=str(ident or ""))
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        t0 = time.perf_counter()
        r = httpx.post(f"{host}/v1/database/{db}/sql", content="SELECT * FROM robot", headers=hdr, timeout=15)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 300:
            report("spacetime sql", "FAIL", f"HTTP {r.status_code}: {r.text[:160]}")
        else:
            body = r.json()
            stmt = body[0] if isinstance(body, list) and body else {}
            cols = [(c.get("name") or {}).get("some") if isinstance(c.get("name"), dict) else c.get("name")
                    for c in (stmt.get("schema") or {}).get("elements", [])]
            rows = stmt.get("rows", [])
            mine = None
            if "name" in cols:
                i = cols.index("name")
                mine = next((dict(zip(cols, row)) for row in rows if row[i] == robot_id), None)
            status = None
            if mine and "status" in mine:
                s = mine["status"]
                status = next(iter(s)) if isinstance(s, dict) else s
            report("spacetime sql", "PASS",
                   f"{len(rows)} robot rows in {ms:.0f} ms; '{robot_id}' "
                   + (f"exists (status {status})" if mine else "not registered yet (the client registers it on first start)"),
                   robot_found=bool(mine), robot_status=status)
    except Exception as e:  # noqa: BLE001
        report("spacetime sql", "FAIL", f"{type(e).__name__}: {e}")
    if not ws:
        return
    try:
        from rdf_common.spacetime_client import SpacetimeConfig, Subscription

        cfg = SpacetimeConfig(host=host, db=db, token=token)
        t0 = time.perf_counter()
        sub = Subscription([f"SELECT * FROM robot WHERE name = '{robot_id}'"], config=cfg).start_thread()
        ok = sub.wait_ready(20)
        ms = (time.perf_counter() - t0) * 1000
        sub.stop()
        report("spacetime websocket", "PASS" if ok else "FAIL",
               f"subscription applied in {ms:.0f} ms" if ok else "no subscription within 20 s (WebSocket blocked by a proxy/firewall?)",
               ws_ms=round(ms, 1))
    except Exception as e:  # noqa: BLE001
        report("spacetime websocket", "FAIL", f"{type(e).__name__}: {e}")


def check_policy(url: str | None, server_url: str | None, n: int, modes: list[str], label: str) -> None:
    if server_url:
        try:
            t0 = time.perf_counter()
            r = httpx.get(f"{server_url.rstrip('/')}/health", timeout=90)
            ms = (time.perf_counter() - t0) * 1000
            report("policy server health", "PASS" if r.status_code < 300 else "WARN",
                   f"HTTP {r.status_code} in {ms:.0f} ms ({'cold start' if ms > 5000 else 'warm'}; production server, TDM-41)")
        except Exception as e:  # noqa: BLE001
            report("policy server health", "WARN", f"{type(e).__name__}: {e} (make policy-up warms it)")
    if not url:
        report("policy latency", "SKIP", "POLICY_ENDPOINT_URL not set")
        return
    if not BENCH.is_file():
        report("policy latency", "FAIL", f"{BENCH} missing from the bundle")
        return
    for mode in modes:
        cmd = [sys.executable, str(BENCH), "--url", url, "--mode", mode, "--n", str(n), "--warmup", "3", "--label", label]
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=420, check=False)
        except subprocess.TimeoutExpired:
            report(f"policy latency {mode}", "FAIL", "bench_client.py timed out after 420 s")
            continue
        wall = time.perf_counter() - t0
        out = BENCH.parent / "results" / f"{label}-{mode}.json"
        if p.returncode != 0 or not out.is_file():
            tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["?"]
            report(f"policy latency {mode}", "FAIL", f"bench_client.py exit {p.returncode}: {tail[0][:200]}")
            continue
        res = json.loads(out.read_text())
        rtt, net = res["rtt_ms"], res["network_plus_proxy_ms"]
        st = "PASS" if rtt["p95"] < 250 else "WARN" if rtt["p95"] < 400 else "FAIL"
        base = LAPTOP_BASELINE.get(mode, {})
        cmp_ = f" (laptop: p50 {base['p50']:.0f}/p95 {base['p95']:.0f})" if base.get("p50") else ""
        report(f"policy latency {mode}", st,
               f"n={res['n']} rtt p50 {rtt['p50']:.0f} / p95 {rtt['p95']:.0f} ms{cmp_}, network+proxy p50 {net['p50']:.0f} ms, "
               f"gpu p50 {res['gpu_ms']['p50']:.0f} ms; {wall:.0f} s wall -> {out.relative_to(REPO)}",
               rtt_ms=rtt, network_plus_proxy_ms=net, result_file=str(out))


def check_media(media_url: str | None, orch_url: str | None, orch_ok: bool) -> None:
    if not media_url:
        report("media healthz", "SKIP", "RDF_MEDIA_STORE_URL not set")
    else:
        try:
            t0 = time.perf_counter()
            r = httpx.get(f"{media_url.rstrip('/')}/healthz", timeout=60)
            ms = (time.perf_counter() - t0) * 1000
            report("media healthz", "PASS" if r.status_code < 300 else "FAIL", f"HTTP {r.status_code} in {ms:.0f} ms")
        except Exception as e:  # noqa: BLE001
            report("media healthz", "FAIL", f"{type(e).__name__}: {e}")
    if not (orch_url and orch_ok):
        report("media signed upload", "SKIP", "orchestrator not reachable (the Jetson uploads via orchestrator-signed URLs)")
        return
    try:
        payload = os.urandom(1024)
        host = (platform.node().split(".")[0] or "jetson").replace("_", "-")
        sign = httpx.post(f"{orch_url.rstrip('/')}/uploads/sign", timeout=20, json={
            "ticket_id": "tdm44-probe", "episode_id": f"{host}-{int(time.time())}", "filename": "probe.bin",
            "content_type": "application/octet-stream", "ttl_s": 300})
        if sign.status_code >= 300:
            report("media signed upload", "FAIL", f"/uploads/sign HTTP {sign.status_code}: {sign.text[:160]}")
            return
        s = sign.json()
        t0 = time.perf_counter()
        put = httpx.request(s.get("method", "PUT"), s["upload_url"], content=payload, headers=s.get("headers") or {}, timeout=60)
        ms = (time.perf_counter() - t0) * 1000
        want = hashlib.sha256(payload).hexdigest()
        got = put.headers.get("x-sha256") or (put.json().get("sha256") if put.headers.get("content-type", "").startswith("application/json") else None)
        ok = put.status_code < 300 and (got is None or got == want)
        report("media signed upload", "PASS" if ok else "FAIL",
               f"PUT 1 KiB HTTP {put.status_code} in {ms:.0f} ms, sha256 {'match' if got == want else 'n/a' if got is None else 'MISMATCH'} -> {s.get('storage_uri')}")
    except Exception as e:  # noqa: BLE001
        report("media signed upload", "FAIL", f"{type(e).__name__}: {e}")


def check_orchestrator(url: str | None) -> bool:
    if not url:
        report("orchestrator", "SKIP", "ORCHESTRATOR_URL not set")
        return False
    try:
        t0 = time.perf_counter()
        r = httpx.get(f"{url.rstrip('/')}/healthz", timeout=8)
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code < 300
        report("orchestrator", "PASS" if ok else "WARN", f"/healthz HTTP {r.status_code} in {ms:.0f} ms")
        return ok
    except Exception as e:  # noqa: BLE001
        local = urllib.parse.urlparse(url).hostname in ("localhost", "127.0.0.1")
        report("orchestrator", "WARN", f"{type(e).__name__}: {e}"
               + (" (ORCHESTRATOR_URL is localhost: set JETSON_ORCHESTRATOR_URL in the laptop .env and make jetson-env)" if local else
                  " (the robot loop itself only needs SpacetimeDB; uploads need the orchestrator)"))
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-policy", action="store_true", help="skip the policy checks (they wake a Modal GPU container)")
    ap.add_argument("--policy-n", type=int, default=20, help="bench_client.py calls per mode (default 20)")
    ap.add_argument("--policy-ws", action="store_true", help="also bench the websocket mode (the rollout path)")
    ap.add_argument("--no-ws", action="store_true", help="skip the SpacetimeDB WebSocket subscription test")
    ap.add_argument("--no-upload", action="store_true", help="skip the signed upload probe")
    ap.add_argument("--label", default=None, help="bench label (default jetson-<hostname>)")
    ap.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    a = ap.parse_args(argv)
    load_dotenv_fallback()

    st_host = (env("SPACETIME_HOST") or "").rstrip("/")
    st_db, st_token = env("SPACETIME_DB_NAME"), env("SPACETIME_TOKEN")
    robot_id = env("ROBOT_ID", "yam-1") or "yam-1"
    orch = env("ORCHESTRATOR_URL", "http://localhost:8000")
    policy, policy_server = env("POLICY_ENDPOINT_URL"), env("POLICY_SERVER_URL")
    media = env("RDF_MEDIA_STORE_URL")
    host = platform.node().split(".")[0]
    label = a.label or f"jetson-{host}"
    print(f"connectivity check from {host} ({platform.machine()}, {platform.platform(terse=True)}), robot {robot_id}")

    if not st_host or not st_db:
        report("config", "FAIL", "SPACETIME_HOST / SPACETIME_DB_NAME not set (make jetson-env)")
    else:
        check_tcp("spacetime", st_host)
        check_clock(st_host)
        check_spacetime(st_host, st_db, st_token, robot_id, ws=not a.no_ws)
    if not a.no_policy:
        check_tcp("policy", policy)
        check_policy(policy, policy_server, a.policy_n, ["http", "ws"] if a.policy_ws else ["http"], label)
    else:
        report("policy", "SKIP", "--no-policy")
    check_tcp("media", media)
    # not required: the command loop only needs SpacetimeDB; episode uploads need the orchestrator
    orch_ok = check_tcp("orchestrator", orch, required=False) and check_orchestrator(orch)
    check_media(media, orch, orch_ok and not a.no_upload)

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    print(f"\n{len(RESULTS)} checks: {len(fails)} FAIL, {len(warns)} WARN")
    if a.json:
        Path(a.json).write_text(json.dumps({"host": host, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": RESULTS}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
