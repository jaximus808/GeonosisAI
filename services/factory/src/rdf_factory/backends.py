"""Droid backends: ``local`` (droid-sdk subprocess on this machine) and ``cloud`` (Factory Sessions API).

Both expose ``run_turn(spec, factory_session_id, text, emit, on_session_id) -> RawTurn``:
  * ``factory_session_id=None`` creates a new Factory session (``on_session_id`` fires with its id
    before the turn runs), otherwise the turn continues that session (Protocol A, native resume).
  * ``emit(kind, text, tool=None, **data)`` receives normalized events (see events.py) as they happen.
  * A session that no longer exists raises :class:`SessionGone`; the client then falls back to
    Protocol B (new session seeded with ``resume_payload``).

Local turns open the droid process, run one turn and close it again, so a parked session costs
nothing and can be resumed from any process (``droid_sdk.Session.resume(id)``; S-04).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .config import FactoryConfig
from .events import content_preview, input_preview, tool_summary

log = logging.getLogger("rdf_factory.backends")

Emit = Callable[..., None]  # emit(kind, text, tool=None, **data)
OnSessionId = Callable[[str], Awaitable[None]]


class SessionGone(RuntimeError):
    """The Factory session (or its computer) is gone: use Protocol B."""


class FactoryApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(f"Factory API {status}: {detail}")


@dataclass
class SessionSpec:
    """Everything needed to (re)open an agent_session's droid; persisted in the state dir."""

    agent_session_id: int
    role: str
    name: str
    ticket_id: int
    mode: str
    model: str
    reasoning: str
    cwd: str  # droid working directory (local: the repo checkout; cloud: workdir)
    workdir: str  # per-agent_session scratch dir (resume_payload.json lives here)
    write_roots: list[str] = field(default_factory=list)  # local: where file edits are allowed
    allow_commands: list[str] = field(default_factory=list)  # local: Execute prefixes; [] = Execute off
    repo_ref: str = ""
    computer_id: str = ""
    env: dict = field(default_factory=dict)  # extra context given to the droid in the brief
    factory_session_ids: list[str] = field(default_factory=list)
    turns: int = 0
    credits_total: float = 0.0
    created_at: float = field(default_factory=time.time)

    def save(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        p = state_dir / f"{self.agent_session_id}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(p)

    @classmethod
    def load(cls, state_dir: Path, agent_session_id: int) -> "SessionSpec | None":
        p = state_dir / f"{agent_session_id}.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class RawTurn:
    factory_session_id: str
    subtype: str  # success | interrupted | error_during_execution | timeout | error
    success: bool
    text: str
    credits: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    seconds: float = 0.0
    error: str | None = None


class Backend(Protocol):
    mode: str

    async def run_turn(self, spec: SessionSpec, factory_session_id: str | None, text: str,
                       emit: Emit, on_session_id: OnSessionId) -> RawTurn: ...

    async def interrupt(self, spec: SessionSpec, factory_session_id: str) -> None: ...

    async def aclose(self) -> None: ...


# ============================================================================ local (droid-sdk)

_SHELL_META = set(";&|<>`$\n")


def _inside(path: str, roots: list[str], cwd: str) -> bool:
    p = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    for r in roots:
        r = os.path.realpath(r)
        if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _command_allowed(cmd: str, prefixes: list[str]) -> bool:
    if not prefixes or any(c in _SHELL_META for c in cmd):
        return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    for pre in prefixes:
        pt = shlex.split(pre)
        if toks[: len(pt)] == pt:
            return True
    return False


class LocalBackend:
    mode = "local"

    def __init__(self, cfg: FactoryConfig):
        self.cfg = cfg
        cfg.api_key()  # the droid subprocess reads FACTORY_API_KEY from the environment

    def _handlers(self, spec: SessionSpec, emit: Emit):
        from droid_sdk import InteractionHandlers, ToolConfirmationOutcome
        from droid_sdk.permissions import CreateFile, EditAction
        from droid_sdk import ApplyPatchAction, ExecuteAction

        roots = spec.write_roots or [spec.workdir]

        def on_permission(req):
            denied: list[str] = []
            for a in req.actions or ():
                if isinstance(a, (CreateFile, EditAction)):
                    if not _inside(a.file_path, roots, spec.cwd):
                        denied.append(f"write outside allowed dirs: {a.file_path}")
                elif isinstance(a, ApplyPatchAction):
                    paths = [f.file_path for f in (a.files or ())] + [f.move_to for f in (a.files or ()) if f.move_to]
                    paths = paths or [a.file_path]
                    bad = [p for p in paths if not _inside(p, roots, spec.cwd)]
                    if bad:
                        denied.append(f"patch outside allowed dirs: {', '.join(bad)}")
                elif isinstance(a, ExecuteAction):
                    if not _command_allowed(a.full_command, spec.allow_commands):
                        denied.append(f"command not allow-listed: {a.full_command}")
                else:
                    denied.append(f"{type(a).__name__} not permitted")
            if not req.actions:
                denied.append("empty permission request")
            if denied:
                emit("permission_denied", f"denied: {denied[0]}", tool=None, reasons=denied)
                return req.respond(ToolConfirmationOutcome.CANCEL)
            return req.respond(ToolConfirmationOutcome.PROCEED_ONCE)

        return InteractionHandlers(on_permission=on_permission)

    def _disabled_tools(self, spec: SessionSpec) -> set[str]:
        return set() if spec.allow_commands else {"Execute"}

    async def run_turn(self, spec, factory_session_id, text, emit, on_session_id) -> RawTurn:
        from droid_sdk import (Autonomy, ReasoningEffort, Session, SessionConfig, SessionTag)
        from droid_sdk.errors import DroidError, RunTimeoutError, SessionNotFoundError

        handlers = self._handlers(spec, emit)
        if factory_session_id:
            session = Session.resume(factory_session_id, interactions=handlers,
                                     disabled_tools=self._disabled_tools(spec))
        else:
            Path(spec.workdir).mkdir(parents=True, exist_ok=True)
            tags = [SessionTag(name=t) for t in self.cfg.tags] + [
                SessionTag(name=f"agent_session-{spec.agent_session_id}"), SessionTag(name=spec.role)]
            session = Session(
                cwd=spec.cwd, model=spec.model, reasoning_effort=ReasoningEffort(spec.reasoning),
                config=SessionConfig(autonomy=Autonomy.LOW, disabled_tools=self._disabled_tools(spec), tags=tags),
                interactions=handlers,
            )
        t0 = time.time()
        try:
            await session.open()
        except SessionNotFoundError as e:
            raise SessionGone(str(e)) from e
        except DroidError as e:
            if factory_session_id and "not found" in str(e).lower():
                raise SessionGone(str(e)) from e
            raise
        sid = session.id
        try:
            if not factory_session_id:
                await on_session_id(sid)
            names: dict[str, str] = {}
            seen_tools: set[str] = set()
            result = None
            try:
                async with session.stream(text, timeout=self.cfg.turn_timeout_s) as stream:
                    async for ev in stream:
                        self._map(ev, emit, names, seen_tools)
                result = stream.result
            except RunTimeoutError as e:
                emit("error", f"turn timed out after {self.cfg.turn_timeout_s:.0f}s", error_type="timeout",
                     detail=str(e))
                return RawTurn(sid, "timeout", False, "", seconds=round(time.time() - t0, 1), error=str(e))
            usage = getattr(result, "usage", None)
            err = getattr(result, "error", None)
            return RawTurn(
                sid, result.subtype, bool(result.success), getattr(result, "text", "") or "",
                credits=getattr(usage, "factory_credits", None) if usage else None,
                input_tokens=getattr(usage, "input_tokens", None) if usage else None,
                output_tokens=getattr(usage, "output_tokens", None) if usage else None,
                seconds=round(time.time() - t0, 1),
                error=(getattr(err, "message", None) or str(err)) if err else None,
            )
        finally:
            try:
                await session.close()  # the droid subprocess exits: nothing runs while parked
            except Exception as e:  # noqa: BLE001
                log.warning("closing droid session %s: %s", sid, e)

    def _map(self, ev: Any, emit: Emit, names: dict[str, str], seen_tools: set[str]) -> None:
        import droid_sdk as d

        repo = str(self.cfg.repo_dir)

        def tool_call(name: str, tool_use_id: str, inp: Any) -> None:
            if tool_use_id in seen_tools:
                return
            seen_tools.add(tool_use_id)
            names[tool_use_id] = name
            emit("tool_call", tool_summary(name, dict(inp) if hasattr(inp, "items") else inp, repo),
                 tool=name, tool_use_id=tool_use_id, input=input_preview(_plain(inp)))

        if isinstance(ev, d.ToolCall):
            tool_call(ev.name, ev.tool_use_id, ev.input)
        elif isinstance(ev, d.AssistantMessage):
            for b in ev.content:
                if isinstance(b, d.ToolUseBlock):
                    tool_call(b.name, b.id, b.input)
            if (ev.text or "").strip():
                emit("assistant_text", ev.text, chars=len(ev.text), message_id=ev.id,
                     full_text=ev.text[:4000])
        elif isinstance(ev, d.ToolResult):
            name = ev.tool_name or names.get(ev.tool_use_id) or "tool"
            prev = content_preview(_plain(ev.content))
            emit("tool_result", f"{name} {'failed' if ev.is_error else 'ok'}: {prev[:120]}", tool=name,
                 tool_use_id=ev.tool_use_id, is_error=bool(ev.is_error), preview=prev)
        elif isinstance(ev, d.WorkingStateChanged):
            state = getattr(ev.state, "value", str(ev.state))
            emit("state", state, state=state)
            if state == "thinking":
                emit("thinking", "thinking", state=state)
        elif isinstance(ev, d.TokenUsageUpdate):
            emit("usage", f"{ev.input_tokens} in / {ev.output_tokens} out", input_tokens=ev.input_tokens,
                 output_tokens=ev.output_tokens, factory_credits=getattr(ev, "factory_credits", None))
        elif isinstance(ev, d.ErrorEvent):
            emit("error", ev.message, error_type=getattr(ev.error_type, "value", str(ev.error_type)))

    async def interrupt(self, spec, factory_session_id) -> None:
        return None  # local turns run in-process; cancel the task that awaits run_turn

    async def aclose(self) -> None:
        return None


def _plain(v: Any) -> Any:
    """FrozenJson (mappingproxy/tuples) -> plain JSON-able values."""
    if hasattr(v, "items"):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


# ============================================================================ cloud (Sessions API)


class CloudBackend:
    """Factory Sessions API (https://api.factory.ai/api/v0), Protocol A with the S-04 quirks:

    * the first POST /messages right after POST /sessions can 404 transiently -> retried;
    * 429 / 5xx / transport errors -> retried with backoff (Retry-After honoured);
    * GET /sessions/{id} 404 or a computer in ``error`` -> SessionGone (Protocol B);
    * each agent_session works in its own dir, ``/home/factory-user/agents/<id>`` (shared disk).
    Events: GET /sessions/{id}/messages is polled every ``poll_s`` while the turn runs.
    """

    mode = "cloud"

    def __init__(self, cfg: FactoryConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 poll_s: float = 3.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.cfg = cfg
        self.poll_s = poll_s
        self._sleep = sleep
        self.http = httpx.AsyncClient(base_url=cfg.api_base, timeout=120, transport=transport,
                                      headers={"Authorization": f"Bearer {cfg.api_key()}"})

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ http

    async def _req(self, method: str, path: str, *, retry_404: int = 0, **kw) -> httpx.Response:
        delay = 1.0
        for attempt in range(6):
            try:
                r = await self.http.request(method, path, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == 5:
                    raise
                log.warning("factory %s %s: %s (retrying)", method, path, e)
                await self._sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 404 and attempt < retry_404:
                await self._sleep(1 + attempt)  # S-04: a just-created session can 404 for a moment
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 5:
                    return r
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                await self._sleep(min(wait, 60) + random.random() * 0.5)
                delay = min(delay * 2, 30)
                continue
            return r
        return r  # pragma: no cover

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text[:500]}

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code >= 300:
            body = self._json(r)
            raise FactoryApiError(r.status_code, str(body.get("detail") or body)[:500])

    # ------------------------------------------------------------------ computers

    async def ensure_computer(self, spec: SessionSpec) -> str:
        """spec.computer_id, else FACTORY_COMPUTER_ID, else a cached/new computer from the Environment."""
        cache = self.cfg.state_dir / "cloud_computer.json"
        for cid in (spec.computer_id, self.cfg.computer_id,
                    json.loads(cache.read_text()).get("id", "") if cache.exists() else ""):
            if cid and await self._computer_ok(cid):
                spec.computer_id = cid
                return cid
        r = await self._req("POST", "/computers", json={
            "name": f"rdf-{spec.role}-{int(time.time()) % 100000}", "provider": "e2b",
            "source": {"kind": "environment", "environmentId": self.cfg.environment_id, "isListed": True}})
        self._raise(r)
        cid = self._json(r)["id"]
        t0 = time.time()
        while time.time() - t0 < 600:
            st = self._json(await self._req("GET", f"/computers/{cid}")).get("status")
            if st == "active":
                break
            if st == "error":
                raise FactoryApiError(500, f"computer {cid} failed to provision")
            await self._sleep(5)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"id": cid, "environment_id": self.cfg.environment_id}))
        spec.computer_id = cid
        return cid

    async def _computer_ok(self, cid: str) -> bool:
        r = await self._req("GET", f"/computers/{cid}")
        return r.status_code == 200 and self._json(r).get("status") not in ("error", "deleted", "terminated")

    # ------------------------------------------------------------------ sessions

    async def _create_session(self, spec: SessionSpec) -> str:
        cid = await self.ensure_computer(spec)
        tags = [{"name": t} for t in self.cfg.tags] + [
            {"name": f"agent_session-{spec.agent_session_id}"}, {"name": spec.role}]
        r = await self._req("POST", "/sessions", json={"computerId": cid, "sessionSettings": {
            "model": spec.model, "reasoningEffort": spec.reasoning, "interactionMode": "auto",
            "autonomyLevel": "medium", "tags": tags}})
        self._raise(r)
        return self._json(r)["sessionId"]

    async def _session(self, sid: str) -> dict:
        r = await self._req("GET", f"/sessions/{sid}")
        if r.status_code == 404:
            raise SessionGone(f"session {sid} not found")
        self._raise(r)
        return self._json(r)

    async def _messages(self, sid: str) -> list[dict]:
        r = await self._req("GET", f"/sessions/{sid}/messages", params={"limit": "50"})
        if r.status_code >= 300:
            return []
        body = self._json(r)
        msgs = body.get("messages", body.get("items", [])) or []
        return list(reversed(msgs))  # API returns newest first

    async def run_turn(self, spec, factory_session_id, text, emit, on_session_id) -> RawTurn:
        t0 = time.time()
        new = not factory_session_id
        if new:
            sid = await self._create_session(spec)
            await on_session_id(sid)
            before_credits = 0.0
        else:
            sid = factory_session_id
            s = await self._session(sid)
            if s.get("computerId"):
                if not await self._computer_ok(s["computerId"]):
                    raise SessionGone(f"computer {s['computerId']} is gone or in error")
            before_credits = float(s.get("factoryCredits") or 0)

        seen = {self._mkey(m): len(m.get("content") or []) for m in (await self._messages(sid) if not new else [])}
        r = await self._req("POST", f"/sessions/{sid}/messages", json={"text": text}, retry_404=5 if new else 2)
        if r.status_code == 404:
            if new:
                self._raise(r)
            raise SessionGone(f"session {sid}: POST /messages 404")
        self._raise(r)

        names: dict[str, str] = {}
        seen_busy, last_status, final_text = False, None, ""
        s: dict = {}
        while time.time() - t0 < self.cfg.turn_timeout_s:
            await self._sleep(self.poll_s)
            s = await self._session(sid)
            st = s.get("status")
            if st != last_status:
                emit("state", str(st), state=st)
                last_status = st
            seen_busy |= st in ("pending", "running", "active", "busy")
            for m in await self._messages(sid):
                final_text = self._emit_new(m, seen, names, emit) or final_text
            if st == "idle" and (seen_busy or time.time() - t0 > 30):
                break
            if st in ("error", "failed"):
                break
        else:
            emit("error", f"turn timed out after {self.cfg.turn_timeout_s:.0f}s", error_type="timeout")
            return RawTurn(sid, "timeout", False, final_text, seconds=round(time.time() - t0, 1), error="timeout")

        credits = float(s.get("factoryCredits") or 0)
        ok = s.get("status") == "idle"
        return RawTurn(sid, "success" if ok else "error_during_execution", ok, final_text,
                       credits=round(credits - before_credits, 1) if credits else None,
                       seconds=round(time.time() - t0, 1), error=None if ok else f"session status {s.get('status')}")

    @staticmethod
    def _mkey(m: dict) -> str:
        return str(m.get("id") or m.get("messageId") or json.dumps(m, sort_keys=True, default=str)[:200])

    def _emit_new(self, m: dict, seen: dict[str, int], names: dict[str, str], emit: Emit) -> str:
        """Emit events for content parts of message m not seen yet; returns its newest text if assistant."""
        key = self._mkey(m)
        parts = m.get("content") or []
        if isinstance(parts, str):
            parts = [{"type": "text", "text": parts}]
        start = seen.get(key, 0)
        seen[key] = len(parts)
        role = m.get("role")
        text = ""
        repo = str(self.cfg.repo_dir)
        for p in parts[start:]:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text" and role == "assistant" and (p.get("text") or "").strip():
                text = p["text"]
                emit("assistant_text", text, chars=len(text), message_id=key, full_text=text[:4000])
            elif t in ("tool_use", "tool_call"):
                name = p.get("name") or p.get("toolName") or "tool"
                tid = str(p.get("id") or p.get("toolUseId") or "")
                names[tid] = name
                inp = p.get("input") or p.get("parameters") or {}
                emit("tool_call", tool_summary(name, inp, repo), tool=name, tool_use_id=tid,
                     input=input_preview(inp))
            elif t == "tool_result":
                tid = str(p.get("tool_use_id") or p.get("toolUseId") or "")
                name = names.get(tid, "tool")
                prev = content_preview(p.get("content"))
                err = bool(p.get("is_error") or p.get("isError"))
                emit("tool_result", f"{name} {'failed' if err else 'ok'}: {prev[:120]}", tool=name,
                     tool_use_id=tid, is_error=err, preview=prev)
        return text

    async def interrupt(self, spec, factory_session_id) -> None:
        r = await self._req("POST", f"/sessions/{factory_session_id}/interrupt")
        self._raise(r)
