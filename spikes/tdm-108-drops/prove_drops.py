"""TDM-108 proof: a paid task opened through the real Dispatcher.page path produces a dry-run card whose QR
scans to the task link, and in live mode the card is posted to a LOCAL mock Discord API (never discord.com).

    cd services/orchestrator && uv run --with zxing-cpp python ../../spikes/tdm-108-drops/prove_drops.py

Uses a throwaway drop dir under spikes/tdm-108-drops/tmp/, a fake bus mirror and fake reducer writer
(no SpacetimeDB), and a stdlib HTTP server on 127.0.0.1 as Discord. Prints paths, never secrets.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import zxingcpp
from PIL import Image

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import job_drops as jd

HERE = Path(__file__).resolve().parent
TMP = HERE / "tmp" / "proof"
FAKE_BOT = "MTIzNDU2Nzg5MDEyMzQ1Njc4.proof.fake-token"
PUBLIC = "https://tdm108-proof.example"

received: list[dict] = []


class MockDiscord(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        msg = BytesParser(policy=email_default).parsebytes(
            b"Content-Type: " + self.headers["content-type"].encode() + b"\r\n\r\n" + raw)
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
        payload = json.loads(parts["payload_json"].get_content())
        png = parts["files[0]"].get_payload(decode=True)
        received.append({"path": self.path, "auth_ok": self.headers.get("authorization") == f"Bot {FAKE_BOT}",
                         "ua": self.headers.get("user-agent"), "payload": payload, "png": png})
        body = json.dumps({"id": "1300000000000000001", "channel_id": "42", "guild_id": "7"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def qr_of(path_or_png) -> str:
    img = Image.open(path_or_png) if isinstance(path_or_png, (str, Path)) else Image.open(__import__("io").BytesIO(path_or_png))
    res = zxingcpp.read_barcodes(img)
    assert res, "no QR found"
    return res[0].text


def mirror():
    t = {
        "task": [{"id": 7, "ticket_id": 3, "robot_id": 2, "type": "clean_teleop", "status": "open", "price_cents": 350,
                  "instructions": "Drive one clean run: cube into the empty plate. [ref plan:5]", "assignee_id": 0,
                  "data_plan_id": 5}],
        "ticket": [{"id": 3, "task_description": "Pick up the cube and place it in the empty plate"}],
        "robot": [{"id": 2, "name": "sim-yam-mujoco", "camera_config_json": '{"sim": true}'}],
        "contributor": [], "event_log": [],
    }
    return SimpleNamespace(rows=lambda name: t.get(name, []), get=lambda name, k: None)


class Writer:
    def __init__(self):
        self.events = []

    def call_reducer(self, name, *args):
        if name == "append_event":
            self.events.append((args[1], json.loads(args[2])))


def run(mode: str, api: str) -> tuple[jd.Dropper, Writer]:
    w = Writer()
    s = jd.DropSettings(_env_file=None, rdf_drop_mode=mode, rdf_drop_dir=str(TMP / mode), rdf_web_public_url=PUBLIC,
                        rdf_drop_frame="sketch", discord_bot_token=FAKE_BOT, rdf_drop_discord_channel_id="42",
                        rdf_drop_discord_api=api)
    d = jd.Dropper(s, append=lambda t, p: w.call_reducer("append_event", jd.ACTOR, t, json.dumps(p)), inline=True)
    jd.set_dropper(d)
    dsp.Dispatcher(mirror(), lambda: w).page(3, [7], "clean_teleop", 350)  # the real paging entry point
    return d, w


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockDiscord)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{srv.server_address[1]}/api/v10"
    saved = dsp._page_hook
    dsp.set_page_hook(None)
    jd.install()
    try:
        # 1. dry run (the default): cards on disk, QR -> /j/7/<channel>, nothing sent
        d, w = run("dry_run", api)
        drop = d.store.load("task-7")
        for ch in ("discord", "x"):
            card = d.store.dir("task-7") / f"card-{ch}.png"
            got = qr_of(card)
            assert got == f"{PUBLIC}/j/7/{ch}", got
            print(f"dry_run {ch:8s} card {card}  QR -> {got}  (web /j -> /marketplace?task=7&src={ch})")
        assert received == [], "dry run must not post"
        paged = [p for t, p in w.events if t == "dispatch.paged"][0]
        print("dispatch.paged listeners:", json.dumps(paged["listeners"]))
        print("events:", [t for t, _ in w.events])
        assert [t for t, _ in w.events].count("job_drop.preview") == 1
        assert drop.channels["discord"]["state"] == "preview"

        # 2. live, against the local mock Discord only
        assert api.startswith("http://127.0.0.1:")
        d, w = run("live", api)
        (r,) = received
        assert r["path"] == "/api/v10/channels/42/messages" and r["auth_ok"] and r["ua"].startswith("DiscordBot (")
        assert r["payload"]["embeds"][0]["image"]["url"] == "attachment://card.png"
        out = TMP / "mock-discord-received.png"
        out.write_bytes(r["png"])
        assert qr_of(out) == f"{PUBLIC}/j/7/discord"
        posted = [p for t, p in w.events if t == "job_drop.posted"]
        assert len(posted) == 1 and posted[0]["channel"] == "discord"
        print(f"live: mock Discord received the card ({len(r['png'])} bytes) -> {out}")
        print("job_drop.posted:", json.dumps({k: posted[0][k] for k in ("channel", "task_id", "url", "cost_estimate", "post_url")}))
        # 3. same task again: no second post
        dsp.Dispatcher(mirror(), lambda: w).page(3, [7], "clean_teleop", 350)
        assert len(received) == 1
        print("re-page of task 7: not posted again")
        assert FAKE_BOT not in json.dumps(w.events)
        print("PASS")
        return 0
    finally:
        dsp.remove_page_listener(jd.page_listener)
        dsp.set_page_hook(saved)
        jd.set_dropper(None)
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
