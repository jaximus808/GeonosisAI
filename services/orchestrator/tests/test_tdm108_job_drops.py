"""TDM-108 job drops: share card, dry-run default, channel adapters against mocked HTTP (httpx.MockTransport),
rate limits, the dispatcher listener and the /drops routes. No request leaves the process."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import job_drop_card as card
from rdf_orchestrator import job_drop_channels as ch
from rdf_orchestrator import job_drops as jd

BOT = "MTIzNDU2Nzg5MDEyMzQ1Njc4.tdm108.not-a-real-token"
BASE = "https://drop-test.example"


@pytest.fixture(autouse=True)
def _tdm108_clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith(("RDF_DROP_", "DISCORD_", "RDF_PUBLIC", "RDF_WEB_PUBLIC", "STRIPE_SECRET")):
            monkeypatch.delenv(k, raising=False)
    yield
    jd.set_dropper(None)


def tdm108_settings(tmp_path, **kw) -> jd.DropSettings:
    base = dict(rdf_drop_dir=str(tmp_path / "drops"), rdf_web_public_url=BASE, rdf_public_root=str(tmp_path / "pub"),
                rdf_drop_frame="sketch")
    base.update(kw)
    return jd.DropSettings(_env_file=None, **base)


class Tdm108Http:
    """A MockTransport recording every request; ``routes`` maps (method, path-suffix) -> handler."""

    def __init__(self, routes=None):
        self.calls: list[httpx.Request] = []
        self.routes = routes or {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        for (m, suffix), fn in self.routes.items():
            if req.method == m and req.url.path.endswith(suffix):
                return fn(req)
        raise AssertionError(f"unexpected request {req.method} {req.url.path}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def tdm108_rows(tasks=None, *, robot_cfg='{"sim": true}', instructions="Take over when it pauses."):
    tables = {
        "task": tasks or [{"id": 12, "ticket_id": 4, "robot_id": 2, "type": "recovery_takeover", "status": "open",
                           "price_cents": 350, "instructions": instructions + " [ref iv:abc]", "assignee_id": 0}],
        "ticket": [{"id": 4, "task_description": "Pick up the cube and place it in the empty plate"}],
        "robot": [{"id": 2, "name": "sim-yam-mujoco", "camera_config_json": robot_cfg}],
        "event_log": [],
    }
    return lambda t: tables.get(t, [])


def tdm108_req(task_ids=(12,), task_type="recovery_takeover", price=350):
    return dsp.PageRequest(ticket_id=4, task_ids=list(task_ids), task_type=task_type, price_cents=price,
                           contributor_ids=[])


def tdm108_dropper(tmp_path, http: Tdm108Http | None = None, **kw):
    events: list[tuple[str, dict]] = []
    d = jd.Dropper(tdm108_settings(tmp_path, **kw), http=(http or Tdm108Http()).client(),
                   append=lambda t, p: events.append((t, p)), sleep=lambda s: None, inline=True)
    return d, events


def multipart_json(req: httpx.Request, field: str) -> dict:
    body = req.content.decode("latin-1")
    i = body.index(f'name="{field}"')
    start = body.index("\r\n\r\n", i) + 4
    return json.loads(body[start:body.index("\r\n--", start)])


# --------------------------------------------------------------------------- card

def test_card_sizes_and_brand_tokens(tmp_path):
    info = card.CardInfo(task_id=12, kind="Recovery takeover", title="Pick up the cube", instructions="x",
                         price="$3.50", facts=["About 2 min", "yam-01", "From your phone"])
    for size, dims in card.SIZES.items():
        png = card.render_card(info, size=size, link=f"{BASE}/j/12/x", short="drop-test.example/j/12/x",
                               frame=lambda w, h: card.scene_sketch(None, w, h))
        with Image.open(io.BytesIO(png)) as im:
            assert im.size == dims
            # Foundry ground in the corner (tokens.json), not a neutral fallback
            assert "%02x%02x%02x" % im.convert("RGB").getpixel((2, 2)) == card.load_theme().c("ground").lstrip("#")
    assert card.load_theme().c("ochre") == "#d39457" and card.load_theme().product == "GeonosisAI"


def test_qr_matrix_encodes_the_link():
    import qrcode

    link = f"{BASE}/j/12/discord"
    img = card.qr_image(link, 200, (0, 0, 0), (255, 255, 255), border=0)
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    q.add_data(link)
    q.make(fit=True)
    m = q.get_matrix()
    scale = img.width // len(m)
    got = [[img.getpixel((x * scale + scale // 2, y * scale + scale // 2)) == (0, 0, 0) for x in range(len(m))]
           for y in range(len(m))]
    assert got == m


# --------------------------------------------------------------------------- dry run (the default)

def test_dry_run_is_the_default_and_posts_nothing(tmp_path):
    http = Tdm108Http()  # any request raises
    d, events = tdm108_dropper(tmp_path, http, discord_bot_token=BOT, rdf_drop_discord_channel_id="42")
    assert d.mode == "dry_run"
    out = d.on_page(tdm108_req(), tdm108_rows())
    assert out["job_drop"] == "queued" and out["channels"] == ["discord"]
    assert http.calls == []
    (t, p), = events
    assert t == "job_drop.preview" and p["task_ids"] == [12] and p["mode"] == "dry_run"
    assert p["channels"] == {"discord": "preview", "bluesky": "off", "x": "off", "telegram": "off"}
    assert p["url"] == f"{BASE}/j/12/discord"
    drop = d.store.load("task-12")
    for name in ch.CHANNELS:
        assert (d.store.dir("task-12") / f"card-{name}.png").exists()
        assert drop.channels[name]["link"] == f"{BASE}/j/12/{name}"
    assert "[ref" not in json.dumps(drop.channels)
    # the same task is never dropped twice
    assert d.on_page(tdm108_req(), tdm108_rows())["job_drop"] == "skipped"


@pytest.mark.parametrize("task,why", [
    ({"instructions": "Reset by hand [operator]"}, "operator_only"),
    ({"instructions": "x [no-drop]"}, "operator_only"),
    ({"price_cents": 0}, "unpaid"),
    ({"status": "claimed"}, "not_open"),
    ({"type": "fixture_check"}, "type_off"),
])
def test_operator_only_and_other_restricted_tasks_are_not_dropped(tmp_path, task, why):
    row = {"id": 12, "ticket_id": 4, "robot_id": 2, "type": "reset", "status": "open", "price_cents": 200,
           "instructions": "Put the cube back.", **task}
    d, events = tdm108_dropper(tmp_path)
    out = d.on_page(tdm108_req(), tdm108_rows([row]))
    assert out["job_drop"] == "skipped" and out["reasons"] == {"12": why}
    assert [t for t, _ in events] == ["job_drop.skipped"]


def test_access_code_only_when_asked(tmp_path):
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "access_code").write_text("k7p3qx\n")
    d, _ = tdm108_dropper(tmp_path)
    assert jd.access_code(d.s) is None
    d2, _ = tdm108_dropper(tmp_path, rdf_drop_show_access_code=True)
    assert jd.access_code(d2.s) == "K7P3QX"
    d2.on_page(tdm108_req(), tdm108_rows())
    c = d2.store.load("task-12").channels["discord"]
    assert c["link"] == f"{BASE}/j/12/discord?code=K7P3QX" and "Access code K7P3QX." in c["text"]


def test_public_url_from_demo_public_state(tmp_path):
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "public.json").write_text(json.dumps({"url": "https://abc.trycloudflare.com"}))
    s = jd.DropSettings(_env_file=None, rdf_public_root=str(tmp_path / "pub"))
    assert jd.public_base(s) == "https://abc.trycloudflare.com"


# --------------------------------------------------------------------------- Discord

def discord_ok(req):
    return httpx.Response(200, json={"id": "900", "channel_id": "42", "guild_id": "7"})


def test_discord_bot_live_posts_card_once(tmp_path):
    http = Tdm108Http({("POST", "/channels/42/messages"): discord_ok})
    d, events = tdm108_dropper(tmp_path, http, rdf_drop_mode="live", discord_bot_token=BOT,
                               rdf_drop_discord_channel_id="42", rdf_drop_discord_api="http://mock.local/api/v10")
    d.on_page(tdm108_req(), tdm108_rows())
    (req,) = http.calls
    assert req.headers["authorization"] == f"Bot {BOT}"
    assert req.headers["user-agent"].startswith("DiscordBot (")
    body = multipart_json(req, "payload_json")
    emb = body["embeds"][0]
    assert emb["image"]["url"] == "attachment://card.png" and emb["url"] == f"{BASE}/j/12/discord"
    assert emb["title"] == "Pick up the cube and place it in the empty plate"
    assert {"name": "Pay", "value": "$3.50 (test mode)", "inline": True} in emb["fields"]
    assert body["components"][0]["components"][0]["url"] == f"{BASE}/j/12/discord"
    assert body["allowed_mentions"] == {"parse": []}
    assert b'name="files[0]"; filename="card.png"' in req.content and b"\x89PNG" in req.content
    assert "!" not in body["content"]  # voice: no exclamation marks
    posted = [p for t, p in events if t == "job_drop.posted"]
    assert posted == [{"channel": "discord", "task_id": 12, "task_ids": [12], "drop_id": "task-12",
                       "url": f"{BASE}/j/12/discord", "cost_estimate": 0.0, "sample": False,
                       "post_url": "https://discord.com/channels/7/42/900", "post_id": "900", "note": "discord bot",
                       "manual": False}]
    # post now on an already-posted channel: still one post
    assert d.post_now("task-12", "discord") == {"ok": True, "state": "posted", "duplicate": True}
    assert len(http.calls) == 1
    assert BOT not in json.dumps(events)


def test_discord_429_waits_retry_after_then_posts(tmp_path):
    seen = []

    def flaky(req):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 0.4, "global": False})
        return discord_ok(req)

    http = Tdm108Http({("POST", "/channels/42/messages"): flaky})
    slept = []
    d = jd.Dropper(tdm108_settings(tmp_path, rdf_drop_mode="live", discord_bot_token=BOT, rdf_drop_discord_channel_id="42"),
                   http=http.client(), append=lambda t, p: None, sleep=slept.append, inline=True)
    d.on_page(tdm108_req(), tdm108_rows())
    assert slept == [0.4] and len(http.calls) == 2
    assert d.store.load("task-12").channels["discord"]["state"] == "posted"


def test_discord_webhook_path_and_failure_is_logged_without_secret(tmp_path):
    hook = "http://mock.local/api/webhooks/1/secret-webhook-token"
    http = Tdm108Http({("POST", "/webhooks/1/secret-webhook-token"):
                       lambda r: httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"})})
    d, events = tdm108_dropper(tmp_path, http, rdf_drop_mode="live", rdf_drop_discord_webhook=hook)
    d.on_page(tdm108_req(), tdm108_rows())
    (req,) = http.calls
    assert req.url.params["wait"] == "true" and "components" not in multipart_json(req, "payload_json")
    (failed,) = [p for t, p in events if t == "job_drop.failed"]
    assert failed["error"] == "discord HTTP 403: 50013 Missing Permissions" and failed["channel"] == "discord"
    assert "secret-webhook-token" not in json.dumps(events)


def test_hourly_cap(tmp_path):
    http = Tdm108Http({("POST", "/channels/42/messages"): discord_ok})
    d, events = tdm108_dropper(tmp_path, http, rdf_drop_mode="live", discord_bot_token=BOT,
                               rdf_drop_discord_channel_id="42", rdf_drop_max_per_hour=1)
    two = tdm108_rows([{"id": i, "ticket_id": 4, "robot_id": 2, "type": "clean_teleop", "status": "open",
                        "price_cents": 500, "instructions": ""} for i in (1, 2)])
    d.on_page(tdm108_req([1], "clean_teleop"), two)
    d.on_page(tdm108_req([2], "clean_teleop"), two)
    assert len(http.calls) == 1
    assert d.store.load("task-2").channels["discord"]["state"] == "rate_limited"
    assert [p["reason"] for t, p in events if t == "job_drop.skipped"] == ["hourly_cap"]
    # an operator's "post now" skips the hourly cap (not the mode, not the one-per-channel rule)
    assert d.post_now("task-2", "discord")["ok"] is True and len(http.calls) == 2


def test_post_now_refused_in_dry_run(tmp_path):
    d, _ = tdm108_dropper(tmp_path, discord_bot_token=BOT, rdf_drop_discord_channel_id="42")
    d.on_page(tdm108_req(), tdm108_rows())
    assert d.post_now("task-12", "discord")["code"] == "dry_run"


# --------------------------------------------------------------------------- Bluesky

def test_bluesky_session_blob_record_and_facets(tmp_path):
    def session(req):
        assert json.loads(req.content) == {"identifier": "geonosis.bsky.social", "password": "app-pw-1234"}
        return httpx.Response(200, json={"did": "did:plc:abc", "accessJwt": "jwt1", "handle": "geonosis.bsky.social"})

    def blob(req):
        assert req.headers["authorization"] == "Bearer jwt1" and req.headers["content-type"] == "image/png"
        assert req.content[:4] == b"\x89PNG" and len(req.content) <= ch.BSKY_MAX_BLOB
        return httpx.Response(200, json={"blob": {"$type": "blob", "ref": {"$link": "bafy"}, "mimeType": "image/png",
                                                  "size": len(req.content)}})

    records = []

    def record(req):
        records.append(json.loads(req.content))
        return httpx.Response(200, json={"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c"})

    http = Tdm108Http({("POST", "com.atproto.server.createSession"): session,
                       ("POST", "com.atproto.repo.uploadBlob"): blob,
                       ("POST", "com.atproto.repo.createRecord"): record})
    d, events = tdm108_dropper(tmp_path, http, rdf_drop_mode="live", rdf_drop_bsky_handle="geonosis.bsky.social",
                               rdf_drop_bsky_app_password="app-pw-1234", rdf_drop_bsky_pds="http://mock.local")
    rows = tdm108_rows()
    tdm108 = rows("ticket")[0]
    tdm108["task_description"] = "Pon la taza en el cubo — rápido"  # non-ASCII: byte offsets != char offsets
    d.on_page(tdm108_req(), rows)
    (body,) = records
    rec = body["record"]
    assert body["repo"] == "did:plc:abc" and body["collection"] == "app.bsky.feed.post"
    assert ch.graphemes(rec["text"]) <= 300
    (facet,) = rec["facets"]
    link = f"{BASE}/j/12/bluesky"
    b = rec["text"].encode("utf-8")
    assert b[facet["index"]["byteStart"]:facet["index"]["byteEnd"]] == link.encode()
    assert facet["features"] == [{"$type": "app.bsky.richtext.facet#link", "uri": link}]
    img = rec["embed"]["images"][0]
    assert rec["embed"]["$type"] == "app.bsky.embed.images" and img["aspectRatio"] == {"width": 1200, "height": 675}
    assert img["image"]["ref"] == {"$link": "bafy"}
    (posted,) = [p for t, p in events if t == "job_drop.posted"]
    assert posted["post_url"] == "https://bsky.app/profile/geonosis.bsky.social/post/3kxyz"
    assert "app-pw-1234" not in json.dumps(events) and "jwt1" not in json.dumps(events)


def test_bluesky_text_stays_under_300_graphemes():
    s = SimpleNamespace(rdf_drop_bsky_handle="h", rdf_drop_bsky_app_password="p")
    p = ch.DropPost(task_id=1, kind="Clean demo", title="word " * 200, instructions="i " * 300, price="$3.50",
                    price_note="", minutes=3, robot="yam-01", where="From your phone", link=f"{BASE}/j/1/bluesky")
    t = ch.Bluesky(s).text(p)
    assert ch.graphemes(t) <= 300 and t.endswith(f"Claim it: {BASE}/j/1/bluesky")


# --------------------------------------------------------------------------- X

def test_oauth1_signature_matches_the_documented_example():
    # developer.x.com "Creating a signature" worked example
    h = ch.oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog", consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        params={"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", ts=1318622958)
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h


def x_settings(tmp_path, **kw):
    return dict(rdf_drop_mode="live", rdf_drop_x_api_key="ck", rdf_drop_x_api_secret="cs",
                rdf_drop_x_access_token="at", rdf_drop_x_access_secret="as", rdf_drop_x_api="http://mock.local", **kw)


def test_x_uploads_media_then_posts_without_a_url(tmp_path):
    tweets = []

    def upload(req):
        assert req.headers["authorization"].startswith("OAuth ") and b'name="media_category"' in req.content
        assert b"tweet_image" in req.content and b"\x89PNG" in req.content
        return httpx.Response(200, json={"data": {"id": "1880", "media_key": "3_1880"}})

    def tweet(req):
        tweets.append(json.loads(req.content))
        return httpx.Response(201, json={"data": {"id": "555", "text": "x"}})

    http = Tdm108Http({("POST", "/2/media/upload"): upload, ("POST", "/2/tweets"): tweet})
    d, events = tdm108_dropper(tmp_path, http, **x_settings(tmp_path))
    rows = tdm108_rows()
    rows("ticket")[0]["task_description"] = "Move the cup to bin.example.com quickly"
    d.on_page(tdm108_req(), rows)
    (t,) = tweets
    assert t["media"] == {"media_ids": ["1880"]}
    assert "http" not in t["text"] and "example.com" not in t["text"] and "Scan the QR code" in t["text"]
    assert len(t["text"]) <= 280
    (posted,) = [p for t_, p in events if t_ == "job_drop.posted"]
    assert posted["cost_estimate"] == ch.X_COST_POST_USD and posted["post_url"] == "https://x.com/i/web/status/555"


def test_x_include_link_costs_more(tmp_path):
    s = SimpleNamespace(rdf_drop_x_include_link=True)
    p = ch.DropPost(task_id=1, kind="Reset", title="t", instructions="", price="$2.00", price_note="", minutes=1,
                    robot="yam-01", where="At the robot", link=f"{BASE}/j/1/x")
    x = ch.X(s)
    assert x.cost(x.text(p)) == ch.X_COST_POST_WITH_URL_USD


# --------------------------------------------------------------------------- Telegram

def test_telegram_send_photo_and_errors_hide_the_token(tmp_path):
    token = "123456:telegram-secret-token"

    def boom(req):
        raise httpx.ConnectError(f"could not reach {req.url}")

    http = Tdm108Http({("POST", "/sendPhoto"): boom})
    d, events = tdm108_dropper(tmp_path, http, rdf_drop_mode="live", rdf_drop_telegram_bot_token=token,
                               rdf_drop_telegram_chat_id="@geonosis_drops", rdf_drop_telegram_api="http://mock.local")
    d.on_page(tdm108_req(), tdm108_rows())
    (req,) = http.calls
    assert req.url.path == f"/bot{token}/sendPhoto"
    (failed,) = [p for t, p in events if t == "job_drop.failed"]
    assert failed["error"] == "network error (ConnectError)"
    assert token not in json.dumps(events) and token not in json.dumps(d.store.load("task-12").public())


# --------------------------------------------------------------------------- dispatcher + routes

class Tdm108Writer:
    def __init__(self):
        self.calls = []

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))


def test_dispatcher_page_runs_listener_next_to_push(tmp_path):
    d, events = tdm108_dropper(tmp_path)
    jd.set_dropper(d)
    saved_hook = dsp._page_hook
    dsp.set_page_hook(None)
    jd.install()
    try:
        rows = tdm108_rows()
        m = SimpleNamespace(rows=rows, get=lambda t, k: None)
        w = Tdm108Writer()
        dsp.Dispatcher(m, lambda: w).page(4, [12], "recovery_takeover", 350)
        (body,) = [json.loads(c[3]) for c in w.calls if c[0] == "append_event" and c[2] == "dispatch.paged"]
        assert body["listeners"]["job_drops"]["job_drop"] == "queued"
        assert [t for t, _ in events] == ["job_drop.preview"]
    finally:
        dsp.remove_page_listener(jd.page_listener)
        dsp.set_page_hook(saved_hook)


def test_listener_is_off_under_pytest_without_an_installed_dropper():
    jd.set_dropper(None)
    assert jd.page_listener(tdm108_req(), tdm108_rows()) is None


def tdm108_app(d: jd.Dropper, rows, *, contributor=None):
    from rdf_orchestrator import job_drops_api as api
    from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb

    app = FastAPI()
    app.include_router(api.job_drops_router)
    app.dependency_overrides[api.get_dropper] = lambda: d
    app.dependency_overrides[api.get_drop_rows] = lambda: rows

    class Stdb:
        async def sql(self, q):
            return [contributor] if contributor else []

    async def verifier():
        async def verify(identity, token):
            return token == "browser-token-ok"
        return verify

    app.dependency_overrides[get_stream_stdb] = lambda: Stdb()
    app.dependency_overrides[get_identity_verifier] = verifier
    return TestClient(app)


def test_routes_share_card_post_now_and_operator_key(tmp_path, monkeypatch):
    from rdf_orchestrator import job_drops_api as api

    monkeypatch.setattr(api, "get_operator_key", lambda: "op-key")
    d, _ = tdm108_dropper(tmp_path)
    c = tdm108_app(d, tdm108_rows())
    r = c.get("/drops/share/12.png?size=wide")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (1200, 675)
    assert c.get("/drops/share/99.png").status_code == 404
    assert c.get("/drops").status_code == 401
    assert c.post("/drops/preview", json={"task_id": 12}, headers={"x-rdf-operator-key": "op-key"}).status_code == 200
    lst = c.get("/drops", headers={"x-rdf-operator-key": "op-key"}).json()
    assert lst["mode"] == "dry_run" and lst["drops"][0]["drop_id"] == "task-12"
    assert "card_path" not in json.dumps(lst)
    assert c.get("/drops/task-12/card/discord.png", headers={"x-rdf-operator-key": "op-key"}).status_code == 200
    r = c.post("/drops/task-12/post", json={"channel": "discord"}, headers={"x-rdf-operator-key": "op-key"})
    assert r.status_code == 409 and "dry_run" in r.json()["detail"]
    assert c.get("/drops/..%2Fetc/card/discord.png", headers={"x-rdf-operator-key": "op-key"}).status_code == 404


def test_claim_src_needs_the_claimant(tmp_path):
    ident = "c2" + "ab" * 31
    d, events = tdm108_dropper(tmp_path)
    task = {"id": 12, "ticket_id": 4, "robot_id": 2, "type": "recovery_takeover", "status": "claimed",
            "price_cents": 350, "instructions": "", "assignee_id": 5}
    c = tdm108_app(d, tdm108_rows([task]), contributor={"id": 5, "identity": ident, "kind": "contributor"})
    body = {"task_id": 12, "src": "discord", "identity": ident, "spacetime_token": "browser-token-ok"}
    assert c.post("/drops/claim-src", json=body).json() == {"ok": True}
    assert c.post("/drops/claim-src", json=body).json() == {"ok": True, "duplicate": True}
    assert [p for t, p in events if t == "job_drop.claimed"] == [{"task_id": 12, "contributor_id": 5, "src": "discord"}]
    assert c.post("/drops/claim-src", json={**body, "spacetime_token": "wrong-token-xx"}).status_code == 401
    assert jd.attribution([{"type": "job_drop.claimed", "payload_json": json.dumps(events[-1][1])}])["by_src"] == \
        {"discord": {"claims": 1, "posts": 0}}


def test_discord_check_and_invite_url_are_read_only(tmp_path):
    http = Tdm108Http({("GET", "/users/@me"): lambda r: httpx.Response(200, json={"username": "geonosis-drops"}),
                       ("GET", "/channels/42"): lambda r: httpx.Response(200, json={"name": "paid-tasks", "type": 0})})
    s = tdm108_settings(tmp_path, discord_bot_token=BOT, rdf_drop_discord_channel_id="42",
                        rdf_drop_discord_api="http://mock.local/api/v10")
    assert jd.discord_check(s, http.client()) == {"bot": "geonosis-drops", "channel": "#paid-tasks (type 0)", "ok": True}
    assert all(r.method == "GET" for r in http.calls)
    assert jd.discord_invite_url(s) == ("https://discord.com/oauth2/authorize?client_id=123456789012345678"
                                        "&scope=bot&permissions=52224")
