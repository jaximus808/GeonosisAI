"""FactoryClient (TDM-30): start, message and resume Factory Droid sessions, mirrored into SpacetimeDB.

    from rdf_factory import FactoryClient
    async with FactoryClient() as fc:                     # FACTORY_MODE=local (default) | cloud
        r = await fc.start_session("data_droid", "Write the LeRobot converter ...", ticket_id=1)
        r.agent_session_id, r.status                      # e.g. 7, "waiting_for"
        ...
        r = await fc.resume(7, "dataset 1 now has 5 accepted episodes")   # the TDM-27 bridge entry point

Every Factory session is mirrored into its ``agent_session`` row:
  * create            -> upsert_agent_session(0, role, "", "starting", ...)
  * session id known  -> upsert_agent_session(id, role, <factory id>, "running", ...)  (appends factory_session_ids)
  * each droid step   -> agent_step(id, text) (tool calls, messages; heartbeat at least every 20 s)
                         + event_log droid.<kind> rows with actor droid:<role> (events.py)
  * turn end          -> agent_wait_for (WAITING_FOR line) | upsert done (DONE line) | paused | failed
  * resume            -> agent_resume claim (unless the bridge already claimed: status resuming),
                         Protocol A (same Factory session), Protocol B on SessionGone (new session
                         seeded with resume_payload; its id is appended to factory_session_ids)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from rdf_common.spacetime_client import AsyncSpacetimeClient

from .backends import Backend, CloudBackend, LocalBackend, RawTurn, SessionGone, SessionSpec
from .config import FactoryConfig
from .events import (LOGGED_KINDS, STEP_KINDS, DroidEvent, actor_for, clip, now_us, parse_waiting_for,
                     says_done)
from .mirror import Mirror, ResumeConflict

log = logging.getLogger("rdf_factory")

ROLES = ("onboarding_droid", "data_droid", "training_droid", "planner", "reviewer", "dispatcher")
RESUMABLE = ("waiting_for", "paused")

CONTRACT = """[rdf-factory droid contract]
You are the {role} of the Robot Deployment Factory (agent_session {agent_session_id}{ticket}).
- Your own working directory is {workdir}{mkdir}. Write files only there unless the task explicitly names other paths you may change{refused}.
- Do the task, then end your reply with exactly one final status line:
    DONE                                      when the task is complete, or
    WAITING_FOR <table>:<field><op><value>    when you need the physical world or another agent first,
                                              e.g. WAITING_FOR dataset:accepted_count>=5
- Before a WAITING_FOR line, write your state (what is done, what is next) to {workdir}/resume_payload.json
  and also print it on one line as: RESUME_PAYLOAD {{"done": "...", "next": "..."}}
- A later message starting with [resume-bridge] means the condition is now true: continue from resume_payload.json.
"""

# Appended to every follow-up message: droids forget the final-line rule after the first turn.
REMINDER = "\n\n(End your reply with the final status line from the rdf-factory contract: DONE or WAITING_FOR <table>:<field><op><value>.)"

RESUME_PAYLOAD_RE = re.compile(r"^\s*RESUME_PAYLOAD\s+(\{.*\})\s*$", re.MULTILINE)


@dataclass
class TurnResult:
    agent_session_id: int
    factory_session_id: str
    protocol: str  # start | send | native_resume | restart
    status: str  # agent_session status after the turn
    ok: bool
    text: str  # final assistant text
    turn: int
    waiting_for: dict | None = None
    resume_payload: Any = None
    credits: float | None = None
    credits_total: float = 0.0
    seconds: float = 0.0
    error: str | None = None
    factory_session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class _Turn:
    """Collects one turn's events: publishes them, mirrors steps/events in order, heartbeats."""

    def __init__(self, fc: "FactoryClient", spec: SessionSpec, turn: int, fsid: str | None):
        self.fc, self.spec, self.turn, self.fsid = fc, spec, turn, fsid
        self.q: asyncio.Queue[DroidEvent | None] = asyncio.Queue()
        self.t0 = time.monotonic()
        self.last_step = time.monotonic()
        self.base_step = ""
        self.active = True
        self.worker = asyncio.create_task(self._work())

    def emit(self, kind: str, text: str, tool: str | None = None, **data: Any) -> DroidEvent:
        ev = DroidEvent(kind=kind, agent_session_id=self.spec.agent_session_id, role=self.spec.role,
                        mode=self.spec.mode, factory_session_id=self.fsid, turn=self.turn,
                        seq=self.fc._next_seq(self.spec.agent_session_id), ts_us=now_us(), text=clip(text),
                        tool=tool, data=data)
        self.fc._publish(ev)
        if kind in LOGGED_KINDS or kind in STEP_KINDS:
            self.q.put_nowait(ev)
        return ev

    @staticmethod
    def _step_text(ev: DroidEvent) -> str:
        if ev.kind == "assistant_text":
            return f"says: {ev.text}"
        if ev.kind == "error":
            return f"error: {ev.text}"
        return ev.text

    async def _work(self) -> None:
        m, sid = self.fc.mirror, self.spec.agent_session_id
        hb = self.fc.cfg.heartbeat_s
        while True:
            try:
                ev = await asyncio.wait_for(self.q.get(), timeout=min(5.0, hb / 2))
            except asyncio.TimeoutError:
                if self.active and m.enabled and time.monotonic() - self.last_step >= hb:
                    elapsed = int(time.monotonic() - self.t0)
                    await m.step(sid, f"{self.base_step or 'working'} · still working ({elapsed}s into turn {self.turn})")
                    self.last_step = time.monotonic()
                continue
            if ev is None:
                return
            try:
                if ev.kind in STEP_KINDS:
                    self.base_step = self._step_text(ev)
                    await m.step(sid, self.base_step)
                    self.last_step = time.monotonic()
                if ev.kind in LOGGED_KINDS:
                    await m.event(actor_for(self.spec.role), f"droid.{ev.kind}", ev.to_json())
            except Exception as e:  # noqa: BLE001 - mirroring must never kill a droid turn
                log.warning("mirror %s: %s", ev.kind, e)

    async def close(self) -> None:
        self.active = False
        self.q.put_nowait(None)
        await self.worker


class FactoryClient:
    """Starts, messages and resumes droid sessions; mirrors each into ``agent_session``.

    ``stdb``: an ``AsyncSpacetimeClient``; ``"env"`` (default) builds one from SPACETIME_HOST /
    SPACETIME_DB_NAME / SPACETIME_TOKEN; ``None`` disables mirroring (dry runs, tests).
    """

    def __init__(self, cfg: FactoryConfig | None = None, *, mode: str | None = None,
                 stdb: AsyncSpacetimeClient | str | None = "env",
                 backends: dict[str, Backend] | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.cfg = cfg or FactoryConfig.from_env(mode)
        if mode:
            self.cfg.mode = mode
        self._own_stdb = stdb == "env"
        self.stdb = AsyncSpacetimeClient() if stdb == "env" else stdb
        self.mirror = Mirror(self.stdb)  # type: ignore[arg-type]
        self._backends: dict[str, Backend] = dict(backends or {})
        self._subs: list[tuple[int | None, Callable[[dict], None]]] = []
        if on_event:
            self._subs.append((None, on_event))
        self._seq: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "FactoryClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for b in self._backends.values():
            await b.aclose()
        if self._own_stdb and self.stdb is not None:
            await self.stdb.aclose()

    def backend(self, mode: str) -> Backend:
        if mode not in self._backends:
            self._backends[mode] = LocalBackend(self.cfg) if mode == "local" else CloudBackend(self.cfg)
        return self._backends[mode]

    # ------------------------------------------------------------------ events

    def subscribe(self, agent_session_id: int | None, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Call ``callback(event_dict)`` for every event (of one agent_session, or all with None)."""
        entry = (agent_session_id, callback)
        self._subs.append(entry)
        return lambda: self._subs.remove(entry) if entry in self._subs else None

    async def stream_events(self, agent_session_id: int | None = None, *,
                            until_turn_end: bool = False) -> AsyncIterator[dict]:
        """Async iterator of normalized events (dicts, see events.py) as droid turns run in this process."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        unsub = self.subscribe(agent_session_id, q.put_nowait)
        try:
            while True:
                ev = await q.get()
                yield ev
                if until_turn_end and ev["kind"] == "turn_completed":
                    return
        finally:
            unsub()

    def _next_seq(self, sid: int) -> int:
        self._seq[sid] = self._seq.get(sid, 0) + 1
        return self._seq[sid]

    def _publish(self, ev: DroidEvent) -> None:
        d = ev.to_dict()
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.state_dir / f"{ev.agent_session_id}.events.jsonl", "a") as f:
                f.write(ev.to_json(64_000) + "\n")
        except OSError as e:
            log.warning("events jsonl: %s", e)
        for sid, cb in list(self._subs):
            if sid is None or sid == ev.agent_session_id:
                try:
                    cb(d)
                except Exception as e:  # noqa: BLE001
                    log.warning("event subscriber failed: %s", e)

    # ------------------------------------------------------------------ public API

    async def start_session(self, role: str, brief: str, env: dict | None = None, repo_ref: str = "", *,
                            ticket_id: int = 0, name: str = "", mode: str | None = None,
                            write_roots: list[str] | None = None, allow_commands: list[str] | None = None,
                            finish_on_idle: bool = False) -> TurnResult:
        """Create the agent_session row and a Factory session, run the first turn with ``brief``.

        ``env``: extra context passed to the droid (JSON). ``repo_ref``: local = checkout dir (default
        FACTORY_REPO_DIR); cloud = the repo/branch the Environment cloned. ``write_roots`` (local):
        directories file edits are allowed in, default only the session workdir. ``allow_commands``
        (local): allow-listed Execute command prefixes, e.g. ["uv run pytest"]; default none (Execute
        disabled). Returns after the first turn (status waiting_for / done / paused / failed).
        """
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        mode = mode or self.cfg.mode
        name = name or f"{role}-{time.strftime('%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        agent_id = await self._create_row(role, name, ticket_id, brief)
        workdir = self.cfg.workdir_for(agent_id, mode)
        cwd = (repo_ref or str(self.cfg.repo_dir)) if mode == "local" else workdir
        spec = SessionSpec(agent_session_id=agent_id, role=role, name=name, ticket_id=int(ticket_id), mode=mode,
                           model=self.cfg.model, reasoning=self.cfg.reasoning, cwd=cwd, workdir=workdir,
                           write_roots=[str(Path(p).resolve()) for p in (write_roots or [])] or [workdir],
                           allow_commands=list(allow_commands or []), repo_ref=repo_ref, env=dict(env or {}),
                           computer_id=self.cfg.computer_id)
        spec.save(self.cfg.state_dir)
        text = self._contract(spec) + "\n" + self._context(spec) + "\nTask:\n" + brief.strip()
        async with self._lock(agent_id):
            return await self._run(spec, None, text, "start", finish_on_idle=finish_on_idle)

    async def send(self, agent_session_id: int, text: str, *, finish_on_idle: bool = False) -> TurnResult:
        """Another turn on the session's current Factory session (no resume claim; any non-busy status)."""
        spec = await self._spec(agent_session_id)
        row = await self.mirror.get(agent_session_id)
        fsid = (row or {}).get("factory_session_id") or (spec.factory_session_ids or [None])[-1]
        if not fsid:
            raise RuntimeError(f"agent_session {agent_session_id} has no Factory session yet")
        async with self._lock(agent_session_id):
            await self.mirror.upsert(agent_session_id, spec.role, "running", f"message: {clip(text, 200)}")
            try:
                return await self._run(spec, fsid, text + REMINDER, "send", finish_on_idle=finish_on_idle)
            except SessionGone:
                return await self._restart(spec, text, row, None, finish_on_idle)

    async def resume(self, agent_session_id: int, text: str, *, reason: str = "", claim: bool = True,
                     finish_on_idle: bool = False) -> TurnResult:
        """Wake a parked droid with ``text`` (the TDM-27 resume-bridge entry point).

        1. Claim: if the row is ``waiting_for``/``paused`` and ``claim`` is true, call
           ``agent_resume(id, "", reason)``; a lost race raises ``ResumeConflict``. If the bridge
           already claimed (status ``resuming``) this step is skipped.
        2. Protocol A: message the same Factory session (``[resume-bridge] <text>``).
        3. Protocol B on SessionGone (404 / computer error / local session missing): a new session
           seeded with the contract, ``resume_payload_json`` and the row snapshot.
        Returns the TurnResult of the resumed turn (status waiting_for again, done, paused or failed).
        """
        spec = await self._spec(agent_session_id)
        row = await self.mirror.get(agent_session_id) if self.mirror.enabled else None
        status = (row or {}).get("status", "waiting_for")
        if self.mirror.enabled and row is None:
            raise KeyError(f"agent_session {agent_session_id} not found")
        if status in RESUMABLE:
            if claim:
                await self.mirror.resume_claim(agent_session_id, reason or clip(text, 300))
        elif status != "resuming":
            raise ResumeConflict(f"agent_session {agent_session_id} is {status}, not waiting_for/paused/resuming")
        fsid = (row or {}).get("factory_session_id") or (spec.factory_session_ids or [None])[-1]
        msg = f"[resume-bridge] {text}{REMINDER}"
        async with self._lock(agent_session_id):
            if fsid:
                try:
                    return await self._run(spec, fsid, msg, "native_resume", finish_on_idle=finish_on_idle)
                except SessionGone as e:
                    log.warning("agent_session %s: Factory session %s gone (%s): protocol B", agent_session_id,
                                fsid, e)
            return await self._restart(spec, msg, row, None, finish_on_idle)

    async def restart_with_payload(self, agent_session_id: int, text: str = "", resume_payload: Any = None,
                                   *, finish_on_idle: bool = False) -> TurnResult:
        """Protocol B explicitly: new Factory session under the same agent_session, seeded with the payload."""
        spec = await self._spec(agent_session_id)
        row = await self.mirror.get(agent_session_id)
        async with self._lock(agent_session_id):
            if row and row.get("status") in RESUMABLE:
                await self.mirror.resume_claim(agent_session_id, "restart_with_payload")
            return await self._restart(spec, text, row, resume_payload, finish_on_idle)

    async def finish(self, agent_session_id: int, status: str = "done", step: str = "") -> None:
        """Close an agent_session for good (done | failed)."""
        if status not in ("done", "failed"):
            raise ValueError("status must be done or failed")
        spec = await self._spec(agent_session_id)
        await self.mirror.upsert(agent_session_id, spec.role, status, step or status, critical=True)

    async def interrupt(self, agent_session_id: int) -> None:
        spec = await self._spec(agent_session_id)
        if spec.factory_session_ids:
            await self.backend(spec.mode).interrupt(spec, spec.factory_session_ids[-1])

    # ------------------------------------------------------------------ internals

    def _lock(self, sid: int) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def _create_row(self, role: str, name: str, ticket_id: int, brief: str) -> int:
        if self.mirror.enabled:
            return await self.mirror.create(role, name, ticket_id, f"starting: {clip(brief, 200)}")
        # dry run without SpacetimeDB: local ids from the state dir
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        ids = [int(p.stem) for p in self.cfg.state_dir.glob("*.json") if p.stem.isdigit()]
        return max([900_000, *ids]) + 1

    async def _spec(self, agent_session_id: int) -> SessionSpec:
        spec = SessionSpec.load(self.cfg.state_dir, agent_session_id)
        if spec:
            return spec
        # Started by another machine/state dir: rebuild from the row (cloud sessions keep their own disk).
        row = await self.mirror.get(agent_session_id)
        if not row:
            raise KeyError(f"agent_session {agent_session_id} not found")
        mode = self.cfg.mode
        workdir = self.cfg.workdir_for(agent_session_id, mode)
        ids = json.loads(row.get("factory_session_ids_json") or "[]")
        spec = SessionSpec(agent_session_id=agent_session_id, role=row["role"], name=row["name"],
                           ticket_id=int(row.get("ticket_id") or 0), mode=mode, model=self.cfg.model,
                           reasoning=self.cfg.reasoning, cwd=str(self.cfg.repo_dir) if mode == "local" else workdir,
                           workdir=workdir, write_roots=[workdir], factory_session_ids=ids,
                           computer_id=self.cfg.computer_id)
        spec.save(self.cfg.state_dir)
        return spec

    def _contract(self, spec: SessionSpec) -> str:
        cloud = spec.mode == "cloud"
        return CONTRACT.format(
            role=spec.role, agent_session_id=spec.agent_session_id,
            ticket=f", ticket {spec.ticket_id}" if spec.ticket_id else "", workdir=spec.workdir,
            mkdir=" (create it first with mkdir -p and work from there)" if cloud else "",
            refused="" if cloud else "; other writes are refused")

    def _context(self, spec: SessionSpec) -> str:
        lines = []
        if spec.mode == "local":
            lines.append(f"Repository checkout: {spec.cwd} (your current directory; read anything you need).")
        elif spec.repo_ref:
            lines.append(f"Repository: {spec.repo_ref}")
        if spec.env:
            lines.append("Context JSON: " + json.dumps(spec.env, default=str))
        return "\n".join(lines) + ("\n" if lines else "")

    async def _restart(self, spec: SessionSpec, text: str, row: dict | None, resume_payload: Any,
                       finish_on_idle: bool) -> TurnResult:
        payload = resume_payload
        if payload is None and row and row.get("resume_payload_json"):
            try:
                payload = json.loads(row["resume_payload_json"])
            except json.JSONDecodeError:
                payload = row["resume_payload_json"]
        if payload is None:
            payload = self._read_payload_file(spec)
        snapshot = {k: row.get(k) for k in ("id", "ticket_id", "role", "status", "current_step",
                                            "factory_session_ids_json")} if row else {}
        prev = spec.factory_session_ids[-1] if spec.factory_session_ids else None
        seeded = (self._contract(spec) + "\n" + self._context(spec) +
                  f"\nYou are continuing agent_session {spec.agent_session_id}; its previous Factory session "
                  f"({prev or 'none'}) is gone, so you start without its history.\n"
                  f"resume_payload: {json.dumps(payload, default=str) if payload is not None else '{}'}\n"
                  f"SpacetimeDB snapshot: {json.dumps(snapshot, default=str)}\n\n{text}".rstrip())
        return await self._run(spec, None, seeded, "restart", finish_on_idle=finish_on_idle, previous=prev)

    def _read_payload_file(self, spec: SessionSpec) -> Any:
        if spec.mode != "local":
            return None
        p = Path(spec.workdir) / "resume_payload.json"
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

    def _payload_from(self, spec: SessionSpec, text: str) -> Any:
        found = RESUME_PAYLOAD_RE.findall(text or "")
        if found:
            try:
                return json.loads(found[-1])
            except json.JSONDecodeError:
                pass
        return self._read_payload_file(spec)

    async def _run(self, spec: SessionSpec, fsid: str | None, text: str, protocol: str, *,
                   finish_on_idle: bool = False, previous: str | None = None) -> TurnResult:
        spec.turns += 1
        turn = _Turn(self, spec, spec.turns, fsid)
        backend = self.backend(spec.mode)
        t0 = time.time()

        async def on_session_id(sid: str) -> None:
            turn.fsid = sid
            if sid not in spec.factory_session_ids:
                spec.factory_session_ids.append(sid)
            spec.save(self.cfg.state_dir)
            label = "restarted" if protocol == "restart" else "started"
            await self.mirror.upsert(spec.agent_session_id, spec.role, "running",
                                     f"Factory session {label} ({spec.mode}, {spec.model})", sid)
            if protocol == "restart":
                turn.emit("session_restarted", f"protocol B: new Factory session {sid[:8]}",
                          previous_factory_session_id=previous, model=spec.model, cwd=spec.cwd, workdir=spec.workdir)
            else:
                turn.emit("session_started", f"Factory session {sid[:8]} started ({spec.mode})", model=spec.model,
                          cwd=spec.cwd, workdir=spec.workdir, protocol=protocol)

        first = text.strip().splitlines()[-1] if protocol in ("start", "restart") else text
        turn.emit("turn_started", f"turn {spec.turns} ({protocol}): {clip(first, 200)}", protocol=protocol,
                  prompt_preview=clip(text, 600))
        try:
            raw = await backend.run_turn(spec, fsid, text, turn.emit, on_session_id)
        except SessionGone:
            spec.turns -= 1
            await turn.close()
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            turn.emit("error", f"{type(e).__name__}: {e}", error_type=type(e).__name__, detail=str(e)[:2000])
            raw = RawTurn(turn.fsid or fsid or "", "error", False, "", seconds=round(time.time() - t0, 1),
                          error=f"{type(e).__name__}: {e}")
        if raw.credits:
            spec.credits_total = round(spec.credits_total + float(raw.credits), 1)

        waiting = payload = None
        if raw.success:
            waiting = parse_waiting_for(raw.text, ticket_id=spec.ticket_id)
            if waiting:
                status = "waiting_for"
            elif says_done(raw.text) or finish_on_idle:
                status = "done"
            else:
                status = "paused"
        elif raw.subtype == "interrupted":
            status = "paused"
        else:
            status = "failed"

        last_line = clip((raw.text or "").strip().splitlines()[-1] if (raw.text or "").strip() else raw.subtype, 200)
        await turn.close()  # flush steps/events before the status transition
        try:
            if status == "waiting_for":
                payload = self._payload_from(spec, raw.text)
                turn2 = _Turn(self, spec, spec.turns, raw.factory_session_id or fsid)
                turn2.emit("waiting_for", f"waiting for {waiting.get('raw', waiting)}", predicate=waiting,
                           resume_payload=payload)
                await turn2.close()
                await self.mirror.step(spec.agent_session_id, f"waiting for {waiting.get('raw', '')}")
                await self.mirror.wait_for(spec.agent_session_id, waiting, payload)
            else:
                step = {"done": f"done: {last_line}", "paused": f"idle after turn {spec.turns}: {last_line}",
                        "failed": f"failed: {raw.error or raw.subtype}"}[status]
                await self.mirror.upsert(spec.agent_session_id, spec.role, status, step, critical=True)
        except Exception as e:  # noqa: BLE001
            log.error("agent_session %s: status %s not mirrored: %s", spec.agent_session_id, status, e)
        spec.save(self.cfg.state_dir)
        final = _Turn(self, spec, spec.turns, raw.factory_session_id or fsid)
        final.emit("turn_completed", f"turn {spec.turns} {status} ({raw.seconds:.0f}s, {raw.credits or 0:.0f} credits)",
                   subtype=raw.subtype, success=raw.success, status=status, credits=raw.credits,
                   credits_total=spec.credits_total, seconds=raw.seconds, input_tokens=raw.input_tokens,
                   output_tokens=raw.output_tokens, final_text=(raw.text or "")[-2000:], error=raw.error)
        await final.close()
        return TurnResult(agent_session_id=spec.agent_session_id, factory_session_id=raw.factory_session_id or fsid or "",
                          protocol=protocol, status=status, ok=raw.success, text=raw.text, turn=spec.turns,
                          waiting_for=waiting, resume_payload=payload, credits=raw.credits,
                          credits_total=spec.credits_total, seconds=raw.seconds, error=raw.error,
                          factory_session_ids=list(spec.factory_session_ids))
