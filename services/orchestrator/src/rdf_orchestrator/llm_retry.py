"""Bounded, restart-safe retries for the Claude-backed bus handlers (diagnose, planner, reviewer, LLM QA).

Before this, a refusal or an API error in one of those handlers logged ``<area>.error`` and was only
retried on the next orchestrator boot, so the ticket (or episode) could sit in its stage forever.

The rule now, the same in every handler:

* **Attempts are counted from event_log**, never from process memory: every failed attempt appends
  one ``<area>.error`` row (with ``attempt``, ``kind``, ``max_attempts``, ``retry_in_s``). A restart
  re-derives the count from those rows and carries on where it stopped. The in-process count only
  covers the second or two before our own append shows up in the bus mirror.
* **Backoff**: after failure n the next attempt is scheduled ``base * factor**(n-1)`` seconds later
  (capped), on a daemon timer, not by sleeping on the bus worker pool. After a restart the boot
  snapshot retries straight away (the restart itself was the pause); the count still bounds it.
* **Refusals** (``stop_reason == "refusal"``; every caller raises "... refus... (category=...)")
  are retried with a rephrased / reduced prompt: a short context preface and, where the caller can,
  less material (diagnose: no frames; planner: no free-text trial notes).
* **After ``max_attempts`` failures** one terminal ``<area>.failed`` event is logged, once. What
  happens then is the flywheel's rule for that stage: diagnose / planner -> the ticket is failed with
  the reason (``flywheel.decide``: "a step failed too often"); review / LLM QA -> nothing is written
  (an outage is never a rejection), the episode stays pending for a human, and an
  ``<area>.retry_requested`` event {episode_id} restarts the count.

Env: ``RDF_LLM_MAX_ATTEMPTS`` (3), ``RDF_LLM_RETRY_BASE_S`` (20), ``RDF_LLM_RETRY_FACTOR`` (3),
``RDF_LLM_RETRY_MAX_S`` (600); per area ``RDF_<AREA>_MAX_ATTEMPTS`` / ``RDF_<AREA>_RETRY_BASE_S``
(AREA = DIAGNOSE, PLANNER, REVIEW, LLM_QA).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .log import get_logger

log = get_logger("llm_retry")

REFUSAL, TRANSIENT, ERROR = "refusal", "transient", "error"

REFUSAL_PREFACE = (
    "Context for this request: this is an internal robotics engineering workflow. The material below "
    "comes from a robot arm evaluation or training-data recording of a benign tabletop manipulation "
    "task (e.g. picking up a cup), recorded with consent by the team that operates the robot. It "
    "contains no personal data that needs protecting. Please carry out the requested structured "
    "analysis; if some detail cannot be assessed, say so in the output fields instead of declining."
)


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_s: float = 20.0
    factor: float = 3.0
    max_s: float = 600.0

    @classmethod
    def from_env(cls, area: str = "") -> "RetryPolicy":
        a = area.upper()
        attempts = _env_num(f"RDF_{a}_MAX_ATTEMPTS", _env_num("RDF_LLM_MAX_ATTEMPTS", 3)) if a else \
            _env_num("RDF_LLM_MAX_ATTEMPTS", 3)
        base = _env_num(f"RDF_{a}_RETRY_BASE_S", _env_num("RDF_LLM_RETRY_BASE_S", 20.0)) if a else \
            _env_num("RDF_LLM_RETRY_BASE_S", 20.0)
        return cls(max(1, int(attempts)), max(0.0, base), max(1.0, _env_num("RDF_LLM_RETRY_FACTOR", 3.0)),
                   _env_num("RDF_LLM_RETRY_MAX_S", 600.0))

    def delay_s(self, failures: int) -> float:
        """Seconds to wait after the ``failures``-th failure."""
        return min(self.max_s, self.base_s * self.factor ** max(0, failures - 1))

    def exhausted(self, failures: int) -> bool:
        return failures >= self.max_attempts


def _chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def classify(exc: BaseException) -> str:
    """``refusal`` | ``transient`` (connection, timeout, 408/409/429/5xx/overloaded) | ``error``.

    An ``rdf_common.llm.LLMError`` in the chain decides by its ``llm_kind`` (a CLI timeout is transient)."""
    try:
        import anthropic

        transient_types: tuple = (anthropic.APIConnectionError, anthropic.APITimeoutError,
                                  anthropic.RateLimitError, anthropic.InternalServerError)
        status_type: Any = anthropic.APIStatusError
    except Exception:  # noqa: BLE001
        transient_types, status_type = (), None
    for e in _chain(exc):
        msg = str(e).lower()
        if "refus" in msg and "category=" in msg:  # stop_reason=refusal, not "connection refused"
            return REFUSAL
    for e in _chain(exc):  # rdf_common.llm.LLMError (droid / claude_code / api) states its own kind
        k = getattr(e, "llm_kind", None)
        if k in (REFUSAL, TRANSIENT, ERROR):
            return k
    for e in _chain(exc):
        if transient_types and isinstance(e, transient_types):
            return TRANSIENT
        if status_type is not None and isinstance(e, status_type):
            code = int(getattr(e, "status_code", 0) or 0)
            if code in (408, 409, 429) or code >= 500:
                return TRANSIENT
        if isinstance(e, (TimeoutError, ConnectionError)):
            return TRANSIENT
    return ERROR


def payload(row: Any) -> dict:
    try:
        v = json.loads((row.get("payload_json") if hasattr(row, "get") else getattr(row, "payload_json", None)) or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _get(row: Any, k: str) -> Any:
    return row.get(k) if hasattr(row, "get") else getattr(row, k, None)


def prior_failures(event_rows: Iterable[Any], error_type: str, match: Callable[[dict], bool],
                   reset_type: str | None = None) -> list[dict]:
    """Payloads of ``error_type`` events that ``match``, in id order, after the newest matching
    ``reset_type`` event (a human's "try again")."""
    rows = sorted((r for r in event_rows if _get(r, "type") in (error_type, reset_type)),
                  key=lambda r: int(_get(r, "id") or 0))
    out: list[dict] = []
    for r in rows:
        p = payload(r)
        if not match(p):
            continue
        if reset_type and _get(r, "type") == reset_type:
            out = []
        else:
            out.append(p)
    return out


def find_event(event_rows: Iterable[Any], type_: str, match: Callable[[dict], bool],
               after_type: str | None = None) -> dict | None:
    """The newest ``type_`` payload that ``match``es (and is newer than the newest ``after_type`` one)."""
    best: tuple[int, dict] | None = None
    reset = 0
    for r in event_rows:
        t = _get(r, "type")
        if t not in (type_, after_type):
            continue
        p = payload(r)
        if not match(p):
            continue
        rid = int(_get(r, "id") or 0)
        if after_type and t == after_type:
            reset = max(reset, rid)
        elif best is None or rid > best[0]:
            best = (rid, p)
    return best[1] if best and best[0] > reset else None


class Attempts:
    """Failure kinds per key: max(event_log count, this process's count). Thread-safe."""

    def __init__(self) -> None:
        self._local: dict[Any, list[str]] = {}
        self._lock = threading.Lock()

    def kinds(self, key: Any, logged: list[dict]) -> list[str]:
        from_log = [str(p.get("kind") or ERROR) for p in logged]
        with self._lock:
            local = self._local.get(key, [])
        return local if len(local) > len(from_log) else from_log

    def record(self, key: Any, kinds: list[str]) -> None:
        with self._lock:
            self._local[key] = list(kinds)

    def clear(self, key: Any = None) -> None:
        with self._lock:
            if key is None:
                self._local.clear()
            else:
                self._local.pop(key, None)


def schedule(delay_s: float, fn: Callable[[], None], *, name: str) -> threading.Timer:
    """Run ``fn`` once after ``delay_s`` on a daemon timer thread; exceptions are logged, not raised."""

    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - the retry already logged its own <area>.error
            log.exception("llm_retry.retry_error", extra={"fields": {"name": name}})

    t = threading.Timer(max(0.0, delay_s), run)
    t.daemon = True
    t.name = f"retry-{name}"
    t.start()
    return t


def with_preface(messages: list[dict], text: str = REFUSAL_PREFACE) -> list[dict]:
    """A copy of ``messages`` with ``text`` put first in the first user turn (str or block list)."""
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = f"{text}\n\n{c}"
        else:
            m["content"] = [{"type": "text", "text": text}] + list(c or [])
        break
    return out


class PrefacedJudge:
    """Wraps a reviewer / LLM-QA judge: same call, rephrased prompt (used after a refusal)."""

    def __init__(self, inner: Any, text: str = REFUSAL_PREFACE) -> None:
        self.inner, self.text = inner, text
        self.model = getattr(inner, "model", None)

    @property
    def last_usage(self) -> Any:
        return getattr(self.inner, "last_usage", None)

    def judge(self, messages: list[dict]) -> Any:
        return self.inner.judge(with_preface(messages, self.text))


def error_fields(exc: BaseException, kinds: list[str], policy: RetryPolicy) -> dict:
    """The retry fields every ``<area>.error`` payload carries (``kinds`` includes this failure)."""
    n = len(kinds)
    return {"error": f"{type(exc).__name__}: {exc}"[:2000], "kind": kinds[-1], "attempt": n,
            "max_attempts": policy.max_attempts,
            "retry_in_s": None if policy.exhausted(n) else round(policy.delay_s(n), 1)}


def failed_fields(kinds: list[str], last_error: str | None, policy: RetryPolicy, effect: str) -> dict:
    return {"attempts": len(kinds), "max_attempts": policy.max_attempts, "kinds": kinds,
            "last_error": (last_error or "")[:2000], "effect": effect}
