"""TDM-92: deterministic QA on every purchased episode as it is ingested.

``episode`` entering ``kind == purchased and review_status == pending`` (``ingest_purchased_episode``,
TDM-14) -> load + normalise its actions (a raw LeRobot parquet is converted in memory by
``dataset_import``) -> ``dataset_qa.run_dataset_qa`` against the ticket's robot and the fingerprints of
every earlier episode of the ticket -> one ``record_qa_result`` per check, the summary
``deterministic_qa`` row LAST (the TDM-93 gate) -> on a failure ``submit_review(false, "<checks>: ...")``.
On a pass nothing else is written: TDM-93's ``handlers/llm_qa.py`` fires on the ``deterministic_qa``
passed=true row and writes the LLM checks plus the final review. ``after_pass`` is an in-process hook
for callers that run without that handler (e.g. a proof script); it is None in the app.

Idempotent: an episode that already has a ``deterministic_qa`` row or a ``review`` (bus mirror) is
skipped; an in-process claim set stops two pool workers taking the same episode; ``submit_review``
refuses a second review. A storage outage on our side (``QAInfraError``) writes only a ``qa.error``
event; the episode stays pending and is retried on the next boot.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import dataset_qa as dq
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("dataset_qa")

for _t in ("ticket", "robot", "qa_result", "review"):
    watch(_t)

ACTOR = "orchestrator.dataset_qa"
_lock = threading.Lock()
_claimed: set[int] = set()
_fp_cache: dict[int, dq.Fingerprint] = {}
_config: dq.QAConfig | None = None


def _cfg() -> dq.QAConfig:
    global _config
    if _config is None:
        _config = dq.load_config()
    return _config


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


# Swappable for tests and the proof script.
make_config: Callable[[], dq.QAConfig] = _cfg
make_stdb: Callable[[], Any] = _default_stdb
after_pass: Callable[[Any, dict, dict], None] | None = None  # (stdb, episode, ticket); TDM-93 hook


def already_done(bus, episode_id: int) -> bool:
    if any(int(r.get("episode_id") or 0) == episode_id for r in bus.rows("review")):
        return True
    return any(int(r.get("episode_id") or 0) == episode_id and r.get("check_name") == dq.SUMMARY_CHECK
               for r in bus.rows("qa_result"))


def peer_fingerprints(bus, episode: dict, cfg: dq.QAConfig) -> list[dq.Fingerprint]:
    """Fingerprints of every EARLIER episode of the ticket's dataset (any status): from their own
    ``duplicate`` qa_result when they went through dataset QA, else computed from their media (cached)."""
    eid, tid = int(episode["id"]), int(episode["ticket_id"])
    latest: dict[int, Any] = {}
    for r in bus.rows("qa_result"):
        if r.get("check_name") == "duplicate":
            e = int(r.get("episode_id") or 0)
            if e not in latest or int(r.get("id") or 0) > int(latest[e].get("id") or 0):
                latest[e] = r
    out = []
    for p in sorted(bus.rows("episode"), key=lambda r: int(r["id"])):
        pid = int(p["id"])
        if pid >= eid or int(p.get("ticket_id") or 0) != tid or p.get("kind") not in dq.TRAINING_KINDS:
            continue
        fp = dq.fingerprint_from_qa_detail(latest[pid].get("detail_json")) if pid in latest else None
        if fp is None:
            with _lock:
                fp = _fp_cache.get(pid)
        if fp is None:
            try:
                acts, err, _ = dq.load_purchased_actions(dict(p))
                if acts is None or acts.n < 2:
                    continue
                cam, hashes = dq.episode_phashes(dict(p), acts, cfg.dataset)
                fp = dq.action_fingerprint(pid, acts, cfg.dataset, hashes, cam)
            except Exception as e:  # noqa: BLE001 - an unreadable peer cannot be a duplicate source
                log.info("dataset_qa.peer_skip", extra={"fields": {"episode_id": pid, "error": str(e)[:200]}})
                continue
            with _lock:
                _fp_cache[pid] = fp
        out.append(fp)
    return out


def record(stdb: Any, report: dq.QAReport) -> None:
    """One record_qa_result per check, the summary last (TDM-93 gates on it)."""
    rows = [r for r in report.results if r.name != dq.SUMMARY_CHECK] + [report.summary]
    for r in rows:
        stdb.call_reducer("record_qa_result", report.episode_id, r.name, not r.failed, r.detail_json())


def qa_episode(episode: dict, ticket: dict, robot: dict | None, peers: list[dq.Fingerprint], stdb: Any,
               cfg: dq.QAConfig, *, load: Callable[[dict], tuple] = dq.load_purchased_actions) -> dq.QAReport:
    actions, err, normalized = load(episode)
    report = dq.run_dataset_qa(episode, actions, robot=robot, peers=peers, config=cfg, actions_error=err)
    if normalized:
        report.summary.detail["normalized_from"] = normalized
    record(stdb, report)
    eid = report.episode_id
    payload = {"episode_id": eid, "ticket_id": episode.get("ticket_id"),
               "dataset_offer_id": episode.get("dataset_offer_id"), "passed": report.passed,
               "failed_checks": report.failed_names, "reason": report.reason()[:1500],
               "normalized_from": normalized}
    if not report.passed:
        try:
            stdb.call_reducer("submit_review", eid, False, report.reason()[:2000], report.metrics_json())
        except Exception as e:  # noqa: BLE001
            if "already reviewed" not in str(e):
                raise
            payload["review"] = "already reviewed; the failing qa_result rows still keep it unbillable"
    stdb.call_reducer("append_event", ACTOR, "qa.dataset_checked", json.dumps(payload, default=str))
    if report.passed and after_pass is not None:
        after_pass(stdb, episode, ticket)
    return report


@on_enter("episode", where=lambda r: r.kind == "purchased" and r.review_status == "pending",
          name="tdm92_dataset_qa")
def dataset_qa_episode(ev: Event) -> None:
    ep = dict(ev.row)
    eid = int(ep["id"])
    with _lock:
        if eid in _claimed:
            return
        if already_done(ev.bus, eid):
            log.info("dataset_qa.skip", extra={"fields": {"episode_id": eid, "reason": "already checked"}})
            return
        _claimed.add(eid)
    stdb = make_stdb()
    try:
        cfg = make_config()
        ticket = dict(ev.bus.get("ticket", ep["ticket_id"]) or {"id": ep["ticket_id"]})
        robot = ev.bus.get("robot", ticket.get("robot_id") or ep.get("robot_id"))
        log.info("dataset_qa.start", extra={"fields": {"episode_id": eid, "offer_id": ep.get("dataset_offer_id"),
                                                       "source": ev.source}})
        rep = qa_episode(ep, ticket, dict(robot) if robot else None, peer_fingerprints(ev.bus, ep, cfg), stdb, cfg)
        if rep.fingerprint is not None:
            with _lock:
                _fp_cache[eid] = rep.fingerprint
        log.info("dataset_qa.done", extra={"fields": {"episode_id": eid, "passed": rep.passed,
                                                      "failed": rep.failed_names}})
    except Exception as e:
        with _lock:
            _claimed.discard(eid)
        try:
            stdb.call_reducer("append_event", ACTOR, "qa.error", json.dumps({
                "episode_id": eid, "ticket_id": ep.get("ticket_id"), "error": f"{type(e).__name__}: {e}"[:2000]}))
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        close = getattr(stdb, "close", None)
        if close:
            close()
