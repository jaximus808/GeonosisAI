"""TDM-108 job drops: every new paid task is announced on social channels with a share card + QR code.

Flow (hooked in next to TDM-65's web push)::

    dispatcher creates tasks -> Dispatcher.page(...) -> page listeners (dsp.add_page_listener)
        -> job_drops.page_listener(req, rows) -> Dropper.on_page: pick the droppable tasks, queue the drop
        -> (worker thread) build: one card PNG + one post text per channel, written to RDF_DROP_DIR
           -> event ``job_drop.preview``
        -> live mode only: post to every configured channel -> ``job_drop.posted`` / ``job_drop.failed``

A drop covers one page request (a plan posts its N tasks at once: one drop, "3 paid tasks at $3.50 each",
linking to the first). The QR code and link on each channel's card are ``<web>/j/<task>/<channel>``, a short
link the web app redirects to ``/marketplace?task=<task>&src=<channel>``; the marketplace sends that ``src``
back with the claim (``POST /drops/claim-src``), logged as ``job_drop.claimed`` so the ops page can show which
channel converted.

SAFETY. Posting is public and outward-facing:
  * ``RDF_DROP_MODE=dry_run`` (default): cards + texts are written and ``job_drop.preview`` is logged; nothing
    is sent anywhere. ``live`` posts, and only to channels whose credentials are set. The switch is the
    user's: no code path here turns ``live`` on.
  * At most one post per task per channel (persisted in the drop folder, so a restart does not re-post).
  * At most ``RDF_DROP_MAX_PER_HOUR`` (6) drops per hour go out live; the rest are logged as
    ``job_drop.skipped`` {reason: hourly_cap}. "Post now" on /ops/drops is an operator action and skips the
    hourly cap, never the one-per-channel rule or the mode.
  * Operator-only tasks are never dropped: instructions tagged ``[operator]``, ``[private]`` or
    ``[no-drop]``, task types outside ``RDF_DROP_TYPES``, unpaid tasks, tasks no longer open.
  * Secrets are never logged: ``ChannelError`` messages carry status codes, not URLs.

Channels (``job_drop_channels.py``): Discord (bot token + channel id, or a webhook), Bluesky (app password),
X (API v2 pay-per-use, OAuth 1.0a user context), Telegram (bot). Instagram is out of scope (professional
account + Meta app review).

Public URL: ``RDF_WEB_PUBLIC_URL`` (``make demo-public`` sets it for the orchestrator), else the ``url`` in
``$RDF_PUBLIC_ROOT/public.json``, else http://localhost:3000. With the demo-public access code on, a phone
that has not entered it is sent to ``/join?next=/j/<task>/<channel>`` by the edge and comes back after.
``RDF_DROP_SHOW_ACCESS_CODE=1`` prints the code on the card and in the post and pre-fills it on /join
(never auto-submitted). Off by default: anyone who sees the post can then get in.

CLI (prints no secrets)::

    uv run python -m rdf_orchestrator.job_drops status
    uv run python -m rdf_orchestrator.job_drops test --channel discord      # a SAMPLE drop (no task behind it)
    uv run python -m rdf_orchestrator.job_drops test --task-id 12           # a real open task from SpacetimeDB
    uv run python -m rdf_orchestrator.job_drops discord-invite              # the bot's OAuth2 invite URL
    uv run python -m rdf_orchestrator.job_drops discord-check               # read-only: bot sees the channel?
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import httpx
from pydantic import SecretStr

from rdf_common.settings import RdfSettings

from . import job_drop_card as card
from . import job_drop_channels as chans
from .job_drop_theme import load_theme
from .log import get_logger

log = get_logger("job_drops")

ACTOR = "orchestrator:job_drops"
EV_PREVIEW, EV_POSTED, EV_FAILED, EV_SKIPPED, EV_CLAIMED = (
    "job_drop.preview", "job_drop.posted", "job_drop.failed", "job_drop.skipped", "job_drop.claimed")
DEFAULT_DIR = Path.home() / ".local" / "state" / "rdf" / "job_drops"
KIND = {"clean_teleop": "Clean demo", "recovery_takeover": "Recovery takeover", "reset": "Reset"}
MINUTES = {"clean_teleop": 3, "recovery_takeover": 2, "reset": 1}  # = the marketplace's estimates (tdm61.ts)
BLURB = {"clean_teleop": "Drive the arm from your phone through one clean, full run of the task.",
         "recovery_takeover": "The robot is stuck. Take over, finish the task, then hand control back.",
         "reset": "Put the objects back where they started so the robot can try again."}
OPERATOR_TAGS = re.compile(r"\[(operator|private|no-drop)\]", re.I)
REF_TAG = re.compile(r"\s*\[ref [^\]]+\]")
SRC_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
DISCORD_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15)  # View Channel, Send Messages, Embed Links, Attach Files


class DropSettings(RdfSettings):
    rdf_drop_mode: Literal["dry_run", "live"] = "dry_run"
    rdf_drop_max_per_hour: int = 6
    rdf_drop_dir: str | None = None
    rdf_drop_types: str = "clean_teleop,recovery_takeover,reset"
    rdf_drop_show_access_code: bool = False
    rdf_drop_frame: Literal["auto", "sketch", "none"] = "auto"
    rdf_web_public_url: str | None = None
    rdf_public_root: str | None = None
    rdf_public_access_code: SecretStr | None = None
    stripe_secret_key: SecretStr | None = None  # only its sk_test_/sk_live_ prefix is read ("Test mode" footer)
    # Discord: bot (preferred) or webhook
    discord_bot_token: SecretStr | None = None
    discord_application_id: str | None = None
    rdf_drop_discord_channel_id: str | None = None
    rdf_drop_discord_webhook: SecretStr | None = None
    rdf_drop_discord_api: str = "https://discord.com/api/v10"
    # Bluesky
    rdf_drop_bsky_handle: str | None = None
    rdf_drop_bsky_app_password: SecretStr | None = None
    rdf_drop_bsky_pds: str = "https://bsky.social"
    # X (API v2, OAuth 1.0a user context)
    rdf_drop_x_api_key: SecretStr | None = None
    rdf_drop_x_api_secret: SecretStr | None = None
    rdf_drop_x_access_token: SecretStr | None = None
    rdf_drop_x_access_secret: SecretStr | None = None
    rdf_drop_x_api: str = "https://api.x.com"
    rdf_drop_x_include_link: bool = False
    # Telegram
    rdf_drop_telegram_bot_token: SecretStr | None = None
    rdf_drop_telegram_chat_id: str | None = None
    rdf_drop_telegram_api: str = "https://api.telegram.org"

    @property
    def discord_color(self) -> int:
        return load_theme().int_color("melt")

    @property
    def discord_user_agent(self) -> str:
        return f"DiscordBot ({public_base(self)}, 1.0)"

    @property
    def test_mode(self) -> bool:
        k = self.stripe_secret_key.get_secret_value() if self.stripe_secret_key else ""
        return not k.startswith(("sk_live_", "rk_live_"))

    @property
    def types(self) -> set[str]:
        return {t.strip() for t in self.rdf_drop_types.split(",") if t.strip()}


def _public_root(s: DropSettings) -> Path:
    return Path(s.rdf_public_root or os.environ.get("RDF_PUBLIC_ROOT") or Path(tempfile.gettempdir()) / "rdf-demo-public")


def public_base(s: DropSettings) -> str:
    if s.rdf_web_public_url:
        return s.rdf_web_public_url.rstrip("/")
    try:
        url = json.loads((_public_root(s) / "public.json").read_text()).get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url.rstrip("/")
    except (OSError, ValueError):
        pass
    return "http://localhost:3000"


def access_code(s: DropSettings) -> str | None:
    """The demo-public access code, only when RDF_DROP_SHOW_ACCESS_CODE=1."""
    if not s.rdf_drop_show_access_code:
        return None
    if s.rdf_public_access_code and s.rdf_public_access_code.get_secret_value().strip():
        return s.rdf_public_access_code.get_secret_value().strip().upper()
    try:
        code = (_public_root(s) / "access_code").read_text().strip().upper()
        return code or None
    except OSError:
        return None


def task_link(base: str, task_id: int, src: str, code: str | None = None) -> str:
    return f"{base}/j/{int(task_id)}/{src}" + (f"?code={code}" if code else "")


def fmt_money(cents: int) -> str:
    return f"${int(cents) // 100}.{int(cents) % 100:02d}"


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- facts

@dataclass
class DropFacts:
    task_ids: list[int]
    ticket_id: int
    robot_id: int
    task_type: str
    price_cents: int
    title: str
    instructions: str
    robot: str
    sim: bool = False
    scene_spec: dict | None = None
    sample: bool = False


def restricted(task: dict, types: set[str]) -> str | None:
    """Why a task must not be dropped (None = droppable)."""
    if (task.get("status") or "open") != "open":
        return "not_open"
    if task.get("type") not in types:
        return "type_off"
    if _i(task.get("price_cents")) <= 0:
        return "unpaid"
    if OPERATOR_TAGS.search(task.get("instructions") or ""):
        return "operator_only"
    return None


def _is_sim(robot: dict | None) -> bool:
    if not robot:
        return False
    try:
        cfg = json.loads(robot.get("camera_config_json") or "{}")
    except (TypeError, ValueError):
        cfg = {}
    return bool(isinstance(cfg, dict) and cfg.get("sim") is True) or str(robot.get("embodiment") or "").startswith("sim")


def facts_from_rows(task_ids: Iterable[int], rows: Callable[[str], list], types: set[str]) -> tuple[DropFacts | None, dict]:
    """Read the tasks, their ticket and robot (and the ticket's scene spec) from the bus mirror."""
    ids = [int(t) for t in task_ids]
    by_id = {_i(r.get("id")): dict(r) for r in rows("task")}
    ok, skipped = [], {}
    for t in ids:
        row = by_id.get(t)
        why = "unknown_task" if row is None else restricted(row, types)
        if why:
            skipped[t] = why
        else:
            ok.append(row)
    if not ok:
        return None, skipped
    first = ok[0]
    ticket = next((dict(r) for r in rows("ticket") if _i(r.get("id")) == _i(first.get("ticket_id"))), {})
    robot = next((dict(r) for r in rows("robot") if _i(r.get("id")) == _i(first.get("robot_id"))), {})
    spec = None
    try:
        from . import scene as scn

        ev = list(rows("event_log"))
        spec = scn.ticket_scene(ev, _i(first.get("ticket_id"))).get("spec") or \
            scn.robot_scene(ev, _i(first.get("robot_id"))).get("spec")
    except Exception:  # noqa: BLE001 - the sketch falls back to the default scene
        spec = None
    instr = REF_TAG.sub("", first.get("instructions") or "").strip()
    return DropFacts(
        task_ids=[_i(r["id"]) for r in ok], ticket_id=_i(first.get("ticket_id")), robot_id=_i(first.get("robot_id")),
        task_type=str(first.get("type") or ""), price_cents=_i(first.get("price_cents")),
        title=(ticket.get("task_description") or "").strip() or instr or KIND.get(first.get("type"), "Paid task"),
        instructions=instr or BLURB.get(first.get("type"), ""), robot=robot.get("name") or f"robot-{_i(first.get('robot_id'))}",
        sim=_is_sim(robot), scene_spec=spec if isinstance(spec, dict) else None), skipped


def sample_facts() -> DropFacts:
    return DropFacts(task_ids=[0], ticket_id=0, robot_id=0, task_type="recovery_takeover", price_cents=350,
                     title="Pick up the cube and place it in the empty plate",
                     instructions="The robot drives. When it pauses at the plate, you take over and finish the drop.",
                     robot="sim-yam-mujoco", sim=True, sample=True)


# --------------------------------------------------------------------------- drop record

@dataclass
class Drop:
    drop_id: str
    task_ids: list[int]
    ticket_id: int
    task_type: str
    price_cents: int
    title: str
    robot: str
    created_at: float
    mode: str
    sample: bool = False
    frame: str = ""  # photo | sketch | none
    channels: dict[str, dict] = field(default_factory=dict)  # name -> {state, link, card, text, post_url, ...}
    share: dict[str, str] = field(default_factory=dict)

    @property
    def task_id(self) -> int:
        return self.task_ids[0] if self.task_ids else 0

    def public(self) -> dict:
        d = asdict(self)
        for c in d["channels"].values():
            c.pop("card_path", None)
        return d


class DropStore:
    """One folder per drop: drop.json + card-<channel>.png + text-<channel>.txt (mode 0700 dir)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def dir(self, drop_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]{1,64}", drop_id):
            raise ValueError("bad drop id")
        return self.root / drop_id

    def save(self, drop: Drop) -> None:
        with self._lock:
            d = self.dir(drop.drop_id)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "drop.json.tmp"
            tmp.write_text(json.dumps(asdict(drop), indent=2))
            tmp.replace(d / "drop.json")

    def write(self, drop_id: str, name: str, data: bytes | str) -> Path:
        p = self.dir(drop_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def load(self, drop_id: str) -> Drop | None:
        try:
            j = json.loads((self.dir(drop_id) / "drop.json").read_text())
            return Drop(**j)
        except (OSError, ValueError, TypeError):
            return None

    def recent(self, limit: int = 30) -> list[Drop]:
        out = []
        for p in self.root.glob("*/drop.json"):
            d = self.load(p.parent.name)
            if d:
                out.append(d)
        return sorted(out, key=lambda d: d.created_at, reverse=True)[:limit]


# --------------------------------------------------------------------------- dropper

Append = Callable[[str, dict], None]


def _default_append() -> Append:
    from . import dispatcher as dsp

    holder: dict[str, Any] = {}

    def append(type_: str, payload: dict) -> None:
        if "w" not in holder:
            holder["w"] = dsp.make_writer()
        holder["w"].call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str, separators=(",", ":")))

    return append


class Dropper:
    def __init__(self, settings: DropSettings | None = None, *, store: DropStore | None = None,
                 http: httpx.Client | None = None, append: Append | None = None,
                 photo: Callable[[int], bytes | None] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, inline: bool = False) -> None:
        self.s = settings or DropSettings()
        self.store = store or DropStore(self.s.rdf_drop_dir or DEFAULT_DIR)
        self.http = http or httpx.Client(timeout=20.0, follow_redirects=False)
        self._append = append
        self.photo = photo if photo is not None else self._reset_photo
        self.clock = clock
        self.channels = chans.build_channels(self.s, sleep=sleep)
        self.inline = inline
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tdm108-drop")
        self._lock = threading.RLock()
        self._dropped: set[int] = set()  # task ids already given a drop (this process)
        self._claims_logged: set[int] = set()  # task ids whose claim src is already logged

    # ---- plumbing
    @property
    def mode(self) -> str:
        return self.s.rdf_drop_mode

    def configured(self) -> dict[str, bool]:
        return {n: bool(c.configured) for n, c in self.channels.items()}

    def event(self, type_: str, payload: dict) -> None:
        log.info(type_, extra={"fields": payload})
        try:
            if self._append is None:
                self._append = _default_append()
            self._append(type_, payload)
        except Exception as e:  # noqa: BLE001 - the log line above is the fallback record
            log.warning("job_drop.event_failed", extra={"fields": {"type": type_, "error": type(e).__name__}})

    def _reset_photo(self, robot_id: int) -> bytes | None:
        """The robot's reset start photo from the media store (TDM-62), if one was set. Best effort."""
        if not robot_id:
            return None
        try:
            from . import reset_routine, storage

            backend = storage.build_backend(storage.MediaStoreSettings())
            url = backend.signed_get(reset_routine.reference_key(robot_id), ttl_s=120).url
            r = self.http.get(url, timeout=4.0)
            return r.content if r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff" else None
        except BaseException:  # noqa: BLE001 - incl. SystemExit from settings; no photo is fine
            return None

    def _frame(self, f: DropFacts) -> tuple[card.FrameSource, str, str]:
        if self.s.rdf_drop_frame == "none":
            return None, "", "none"
        if self.s.rdf_drop_frame == "auto" and not f.sample:
            jpg = self.photo(f.robot_id)
            if jpg:
                from PIL import Image

                return Image.open(io.BytesIO(jpg)), f"START PHOTO  {f.robot.upper()}"[:40], "photo"
        spec = f.scene_spec
        if spec is None and (f.sim or f.sample):
            from rdf_common import scene_spec as ss

            spec = ss.default_spec()
        label = "SIM SCENE  TOP VIEW" if (f.sim or f.sample) else "SCENE SKETCH  TOP VIEW"
        return (lambda w, h: card.scene_sketch(spec, w, h)), label, "sketch"

    # ---- building
    def card_info(self, f: DropFacts) -> card.CardInfo:
        where = "At the robot" if f.task_type == "reset" else "From your phone"
        return card.CardInfo(task_id=f.task_ids[0] if f.task_ids else 0, kind=KIND.get(f.task_type, f.task_type),
                             title=f.title, instructions=f.instructions, price=fmt_money(f.price_cents),
                             price_note="per reset" if f.task_type == "reset" else "per accepted episode",
                             facts=[f"About {MINUTES.get(f.task_type, 2)} min", f.robot, where],
                             count=len(f.task_ids), access_code=access_code(self.s), test_mode=self.s.test_mode,
                             sample=f.sample)

    def post_of(self, f: DropFacts, link: str) -> chans.DropPost:
        info = self.card_info(f)
        return chans.DropPost(task_id=info.task_id, kind=info.kind, title=f.title, instructions=f.instructions,
                              price=info.price, price_note=info.price_note, minutes=MINUTES.get(f.task_type, 2),
                              robot=f.robot, where=info.facts[2], link=link, count=info.count,
                              access_code=info.access_code, test_mode=info.test_mode, sample=f.sample)

    def build(self, f: DropFacts) -> Drop:
        base, code = public_base(self.s), access_code(self.s)
        tid = f.task_ids[0] if f.task_ids else 0
        drop_id = f"task-{tid}" if not f.sample else f"sample-{int(self.clock() * 1000)}"
        old = self.store.load(drop_id)
        drop = old or Drop(drop_id=drop_id, task_ids=f.task_ids, ticket_id=f.ticket_id, task_type=f.task_type,
                           price_cents=f.price_cents, title=f.title, robot=f.robot, created_at=self.clock(),
                           mode=self.mode, sample=f.sample)
        frame, label, kind = self._frame(f)
        drop.frame = kind
        if callable(frame):
            frame = _memo_frame(frame)  # each well size is drawn once, not once per channel
        info = self.card_info(f)
        self.store.write(drop_id, "facts.json", json.dumps(asdict(f), default=str))
        for name, ch in self.channels.items():
            prev = drop.channels.get(name) or {}
            if prev.get("state") == "posted":
                continue
            link = task_link(base, tid, name, code)
            png = card.render_card(info, size=ch.size, link=link, short=_short(link), frame=frame, frame_label=label)
            text = ch.text(self.post_of(f, link))
            p = self.store.write(drop_id, f"card-{name}.png", png)
            self.store.write(drop_id, f"text-{name}.txt", text)
            drop.channels[name] = {**prev, "state": "preview" if ch.configured else "off", "link": link,
                                   "size": ch.size, "card": f"card-{name}.png", "card_path": str(p), "text": text,
                                   "configured": bool(ch.configured)}
            if name == "x":
                drop.channels[name]["cost_estimate_usd"] = ch.cost(text)
        self.store.save(drop)
        return drop

    # ---- the dispatcher hook
    def on_page(self, req: Any, rows: Callable[[str], list]) -> dict:
        ids = [int(t) for t in req.task_ids]
        with self._lock:
            fresh = [t for t in ids if t not in self._dropped and not self._posted_anywhere(t)]
        if not fresh:
            return {"job_drop": "skipped", "reason": "already dropped", "task_ids": ids}
        facts, skipped = facts_from_rows(fresh, rows, self.s.types)
        if facts is None:
            if skipped:
                self.event(EV_SKIPPED, {"task_ids": ids, "ticket_id": int(req.ticket_id), "reasons": skipped})
            return {"job_drop": "skipped", "reasons": {str(k): v for k, v in skipped.items()}}
        with self._lock:
            self._dropped.update(facts.task_ids)
        if self.inline:
            self._run(facts)
        else:
            self._pool.submit(self._run_safe, facts)
        return {"job_drop": "queued", "mode": self.mode, "task_ids": facts.task_ids,
                "channels": [n for n, ok in self.configured().items() if ok]}

    def _posted_anywhere(self, task_id: int) -> bool:
        d = self.store.load(f"task-{task_id}")
        return bool(d and any(c.get("state") == "posted" for c in d.channels.values()))

    def _run_safe(self, f: DropFacts) -> None:
        try:
            self._run(f)
        except Exception as e:  # noqa: BLE001
            log.exception("job_drop.error", extra={"fields": {"task_ids": f.task_ids, "error": type(e).__name__}})

    def _run(self, f: DropFacts) -> Drop:
        drop = self.build(f)
        self.event(EV_PREVIEW, {"drop_id": drop.drop_id, "task_id": drop.task_id, "task_ids": drop.task_ids,
                                "ticket_id": drop.ticket_id, "mode": self.mode, "frame": drop.frame,
                                "channels": {n: c["state"] for n, c in drop.channels.items()},
                                "url": drop.channels.get("discord", {}).get("link"),
                                "dir": str(self.store.dir(drop.drop_id)), "sample": drop.sample})
        if self.mode == "live":
            self.publish(drop, f)
        return drop

    # ---- posting
    def posted_last_hour(self) -> int:
        cutoff = self.clock() - 3600
        n = 0
        for d in self.store.recent(200):
            ts = [c.get("posted_at") or 0 for c in d.channels.values() if c.get("state") == "posted"]
            if ts and min(ts) >= cutoff:
                n += 1
        return n

    def publish(self, drop: Drop, f: DropFacts | None = None, *, only: list[str] | None = None,
                manual: bool = False) -> dict[str, dict]:
        """Post ``drop`` to its configured channels (or ``only`` those). Live mode only."""
        out: dict[str, dict] = {}
        if self.mode != "live":
            return {n: {"state": "dry_run"} for n in (only or drop.channels)}
        f = f or self._facts_of(drop)
        with self._lock:
            already_out = any(c.get("state") == "posted" for c in drop.channels.values())
            if not manual and not already_out and self.posted_last_hour() >= self.s.rdf_drop_max_per_hour:
                for n in only or [n for n, c in self.channels.items() if c.configured]:
                    drop.channels.setdefault(n, {})["state"] = "rate_limited"
                    out[n] = {"state": "rate_limited"}
                self.store.save(drop)
                self.event(EV_SKIPPED, {"drop_id": drop.drop_id, "task_id": drop.task_id, "reason": "hourly_cap",
                                        "max_per_hour": self.s.rdf_drop_max_per_hour})
                return out
            for name, ch in self.channels.items():
                if only and name not in only:
                    continue
                c = drop.channels.setdefault(name, {})
                if not ch.configured:
                    out[name] = {"state": "off"}
                    continue
                if c.get("state") == "posted":  # one post per task per channel, ever
                    out[name] = {"state": "posted", "duplicate": True}
                    continue
                link = c.get("link") or task_link(public_base(self.s), drop.task_id, name, access_code(self.s))
                try:
                    png = Path(c.get("card_path") or self.store.dir(drop.drop_id) / f"card-{name}.png").read_bytes()
                except OSError:
                    out[name] = {"state": "failed", "error": "card missing"}
                    continue
                cost = ch.cost(ch.text(self.post_of(f, link))) if name == "x" else 0.0
                base = {"channel": name, "task_id": drop.task_id, "task_ids": drop.task_ids, "drop_id": drop.drop_id,
                        "url": link, "cost_estimate": cost, "sample": drop.sample}
                try:
                    res = ch.post(self.http, self.post_of(f, link), png)
                except chans.ChannelError as e:
                    c.update(state="failed", error=str(e), failed_at=self.clock())
                    self.event(EV_FAILED, {**base, "error": str(e), "status": e.status})
                    out[name] = {"state": "failed", "error": str(e)}
                    continue
                except Exception as e:  # noqa: BLE001
                    c.update(state="failed", error=type(e).__name__, failed_at=self.clock())
                    self.event(EV_FAILED, {**base, "error": type(e).__name__})
                    out[name] = {"state": "failed", "error": type(e).__name__}
                    continue
                c.update(state="posted", posted_at=self.clock(), post_id=res.post_id, post_url=res.post_url,
                         cost_estimate_usd=res.cost_estimate_usd, note=res.note, error=None, manual=manual)
                self.event(EV_POSTED, {**base, "post_url": res.post_url, "post_id": res.post_id,
                                       "cost_estimate": res.cost_estimate_usd, "note": res.note, "manual": manual})
                out[name] = {"state": "posted", "post_url": res.post_url}
            self.store.save(drop)
        return out

    def _facts_of(self, drop: Drop) -> DropFacts:
        try:
            return DropFacts(**json.loads((self.store.dir(drop.drop_id) / "facts.json").read_text()))
        except (OSError, ValueError, TypeError):
            return DropFacts(task_ids=drop.task_ids, ticket_id=drop.ticket_id, robot_id=0, task_type=drop.task_type,
                             price_cents=drop.price_cents, title=drop.title,
                             instructions=BLURB.get(drop.task_type, ""), robot=drop.robot, sample=drop.sample)

    def post_now(self, drop_id: str, channel: str) -> dict:
        drop = self.store.load(drop_id)
        if drop is None:
            return {"ok": False, "code": "unknown_drop"}
        if channel not in self.channels:
            return {"ok": False, "code": "unknown_channel"}
        if self.mode != "live":
            return {"ok": False, "code": "dry_run", "detail": "RDF_DROP_MODE is dry_run: nothing is posted. "
                    "The user switches it to live in .env."}
        if not self.channels[channel].configured:
            return {"ok": False, "code": "not_configured", "detail": f"{channel} has no credentials set"}
        res = self.publish(drop, only=[channel], manual=True)
        return {"ok": res.get(channel, {}).get("state") == "posted", **res.get(channel, {})}

    # ---- attribution
    def record_claim(self, task_id: int, contributor_id: int, src: str) -> dict:
        if not SRC_RE.match(src or ""):
            return {"ok": False, "code": "bad_src"}
        with self._lock:
            if int(task_id) in self._claims_logged:
                return {"ok": True, "duplicate": True}
            self._claims_logged.add(int(task_id))
        self.event(EV_CLAIMED, {"task_id": int(task_id), "contributor_id": int(contributor_id), "src": src})
        return {"ok": True}

    def status(self) -> dict:
        return {"mode": self.mode, "channels": self.configured(), "max_per_hour": self.s.rdf_drop_max_per_hour,
                "posted_last_hour": self.posted_last_hour(), "public_url": public_base(self.s),
                "show_access_code": bool(self.s.rdf_drop_show_access_code), "test_mode": self.s.test_mode,
                "types": sorted(self.s.types), "dir": str(self.store.root)}


def _memo_frame(draw: Callable[[int, int], Any]) -> Callable[[int, int], Any]:
    cache: dict[tuple[int, int], Any] = {}

    def get(w: int, h: int) -> Any:
        if (w, h) not in cache:
            cache[(w, h)] = draw(w, h)
        return cache[(w, h)]

    return get


def _short(link: str) -> str:
    return re.sub(r"^https?://", "", link)


def attribution(event_rows: Iterable[dict]) -> dict:
    """{by_src: {src: {claims, posts}}, claims: [...]} from job_drop.claimed / job_drop.posted events."""
    by: dict[str, dict[str, int]] = {}
    claims = []
    for r in event_rows:
        t = r.get("type")
        if t not in (EV_CLAIMED, EV_POSTED):
            continue
        try:
            p = json.loads(r.get("payload_json") or "{}")
        except ValueError:
            continue
        src = str(p.get("src") or p.get("channel") or "")
        row = by.setdefault(src, {"claims": 0, "posts": 0})
        if t == EV_CLAIMED:
            row["claims"] += 1
            claims.append({"task_id": p.get("task_id"), "src": src, "contributor_id": p.get("contributor_id")})
        else:
            row["posts"] += 1
    return {"by_src": by, "claims": claims[-50:]}


# --------------------------------------------------------------------------- singleton + listener

_dropper: Dropper | None = None
_dropper_lock = threading.Lock()


def get_dropper() -> Dropper:
    global _dropper
    with _dropper_lock:
        if _dropper is None:
            _dropper = Dropper()
        return _dropper


def _listener_dropper() -> Dropper | None:
    """The dropper the dispatcher listener uses. Under pytest it is off unless a test installed one with
    ``set_dropper``: other tests import ``rdf_orchestrator.handlers`` (which installs the listener) and must
    not write drops to ~/.local/state or job_drop events through the real SpacetimeDB writer."""
    if _dropper is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return get_dropper()


def set_dropper(d: Dropper | None) -> None:
    global _dropper
    with _dropper_lock:
        _dropper = d


def page_listener(req: Any, rows: Callable[[str], list]) -> dict | None:
    d = _listener_dropper()
    return d.on_page(req, rows) if d is not None else None


def install() -> None:
    from . import dispatcher as dsp

    dsp.add_page_listener(page_listener)


# --------------------------------------------------------------------------- CLI

def discord_invite_url(s: DropSettings) -> str:
    app_id = (s.discord_application_id or "").strip()
    if not app_id and s.discord_bot_token:
        head = s.discord_bot_token.get_secret_value().split(".", 1)[0]  # base64 of the bot's user id = app id
        try:
            app_id = base64.b64decode(head + "=" * (-len(head) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            app_id = ""
    if not app_id.isdigit():
        raise SystemExit("Set DISCORD_BOT_TOKEN (or DISCORD_APPLICATION_ID) in .env first.")
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions={DISCORD_PERMISSIONS}"


def discord_check(s: DropSettings, http: httpx.Client | None = None) -> dict:
    """Read-only Discord check: GET /users/@me and GET /channels/{id}. Sends no message."""
    if not s.discord_bot_token or not (s.rdf_drop_discord_channel_id or "").strip():
        return {"ok": False, "detail": "set DISCORD_BOT_TOKEN and RDF_DROP_DISCORD_CHANNEL_ID"}
    http = http or httpx.Client(timeout=10.0)
    api = s.rdf_drop_discord_api.rstrip("/")
    h = {"Authorization": f"Bot {s.discord_bot_token.get_secret_value()}", "User-Agent": s.discord_user_agent}
    out: dict[str, Any] = {}
    try:
        me = http.get(f"{api}/users/@me", headers=h)
        out["bot"] = me.json().get("username") if me.status_code == 200 else f"HTTP {me.status_code} (token rejected?)"
        cid = re.sub(r"\D", "", s.rdf_drop_discord_channel_id or "")
        c = http.get(f"{api}/channels/{cid}", headers=h)
        out["channel"] = (f"#{c.json().get('name')} (type {c.json().get('type')})" if c.status_code == 200 else
                          f"HTTP {c.status_code} (bot not in that server, or no View Channel)")
        out["ok"] = me.status_code == 200 and c.status_code == 200
    except httpx.HTTPError as e:
        out = {"ok": False, "detail": f"network error ({type(e).__name__})"}
    return out


def _sql_rows(task_id: int) -> Callable[[str], list]:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    c = SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)
    cache: dict[str, list] = {}

    def rows(table: str) -> list:
        if table not in cache:
            q = {"task": f"SELECT * FROM task WHERE id = {int(task_id)}",
                 "event_log": "SELECT * FROM event_log WHERE type = 'scene.created' OR type = 'scene.deployed'"}
            cache[table] = list(c.sql(q.get(table, f"SELECT * FROM {table}")))
        return cache[table]

    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="job_drops", description="TDM-108 job drops (prints no secrets)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    t = sub.add_parser("test", help="build one drop; posts only when RDF_DROP_MODE=live")
    t.add_argument("--channel", action="append", choices=chans.CHANNELS, help="limit posting to these (repeatable)")
    t.add_argument("--task-id", type=int, help="a real open task (read from SpacetimeDB); default: a SAMPLE drop")
    sub.add_parser("discord-invite")
    sub.add_parser("discord-check", help="read-only: can the bot token see RDF_DROP_DISCORD_CHANNEL_ID? (posts nothing)")
    a = ap.parse_args(argv)
    s = DropSettings()
    if a.cmd == "discord-invite":
        print(discord_invite_url(s))
        return 0
    if a.cmd == "discord-check":
        print(json.dumps(discord_check(s), indent=2))
        return 0
    d = Dropper(s, inline=True)
    if a.cmd == "test" and not a.task_id and d.mode != "live":
        d._append = lambda t, p: None  # a dry-run SAMPLE stays local: no job_drop.* rows in the real event_log
    if a.cmd == "status":
        print(json.dumps(d.status(), indent=2))
        return 0
    if a.task_id:
        f, skipped = facts_from_rows([a.task_id], _sql_rows(a.task_id), s.types)
        if f is None:
            print(json.dumps({"skipped": skipped}))
            return 1
    else:
        f = sample_facts()
    drop = d.build(f)
    d.event(EV_PREVIEW, {"drop_id": drop.drop_id, "task_id": drop.task_id, "mode": d.mode, "sample": f.sample,
                         "channels": {n: c["state"] for n, c in drop.channels.items()}, "cli": True})
    res = d.publish(drop, f, only=a.channel, manual=True) if d.mode == "live" else {}
    print(json.dumps({"mode": d.mode, "drop_id": drop.drop_id, "dir": str(d.store.dir(drop.drop_id)),
                      "cards": {n: c.get("card_path") for n, c in drop.channels.items()},
                      "configured": d.configured(), "posted": res or "dry_run: nothing posted"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
