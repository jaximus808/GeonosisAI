"""Success statistics for eval runs (TDM-66).

Pure, stdlib-only logic:

- ``wilson_interval``: Wilson score interval for a binomial success rate.
- ``success_rate``: point estimate.
- ``meets_threshold``: the ship rule. It compares the point estimate by default.
  With ``require_ci_low`` set, it compares the CI lower bound instead.
- ``format_ci`` / ``explain_threshold``: the "show the math" strings for the
  dashboard. The dashboard must always show the CI, never only the point estimate.
- ``EvalRunFinalizer``: fed ``eval_trial`` rows; calls ``finalize_eval_run``
  exactly once per eval run, when the trial count reaches ``eval_run.trials``.

Why the default compares the point estimate: at n=10 the Wilson 95% lower bound
is at most ~0.72 (even for 10/10). So with ``require_ci_low`` set, a 0.8 target
can never be met in a 10-trial eval.

Rows and settings can be dicts or objects with attributes (SpacetimeDB rows,
dataclasses). Keys can be snake_case or camelCase.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_Z = 1.96  # two-sided 95%


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` (snake_case, or its camelCase form) from a dict or object."""
    parts = name.split("_")
    camel = parts[0] + "".join(p.title() for p in parts[1:])
    for key in (name, camel):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
        elif hasattr(obj, key):
            return getattr(obj, key)
    return default


def _check_counts(successes: int, trials: int) -> None:
    if trials < 0 or successes < 0:
        raise ValueError(f"counts must be non-negative (successes={successes}, trials={trials})")
    if successes > trials:
        raise ValueError(f"successes ({successes}) > trials ({trials})")


def wilson_interval(successes: int, trials: int, z: float = DEFAULT_Z) -> tuple[float, float]:
    """Wilson score interval (low, high) for ``successes`` out of ``trials``.

    With 0 trials there is no information, so this returns (0.0, 1.0).
    """
    _check_counts(successes, trials)
    if trials == 0:
        return (0.0, 1.0)
    n = trials
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    # Remove float noise at the edges: 0/n has low 0 and n/n has high 1.
    if successes == 0:
        low = 0.0
    if successes == n:
        high = 1.0
    return (low, high)


def success_rate(successes: int, trials: int) -> float:
    """Point estimate successes/trials. Returns 0.0 when there are no trials."""
    _check_counts(successes, trials)
    return successes / trials if trials else 0.0


def confidence_pct(z: float = DEFAULT_Z) -> int:
    """Two-sided confidence level, in percent, for ``z`` (1.96 gives 95)."""
    return round(math.erf(z / math.sqrt(2)) * 100)


@dataclass(frozen=True)
class ThresholdSettings:
    success_threshold: float = 0.8
    require_ci_low: bool = False
    z: float = DEFAULT_Z


def _settings(settings: Any) -> ThresholdSettings:
    if isinstance(settings, ThresholdSettings):
        return settings
    if settings is None:
        return ThresholdSettings()
    base = ThresholdSettings()
    threshold = _field(settings, "success_threshold", None)
    if threshold is None:
        threshold = _field(settings, "target_success", base.success_threshold)
    return ThresholdSettings(
        success_threshold=float(threshold),
        require_ci_low=bool(_field(settings, "require_ci_low", base.require_ci_low)),
        z=float(_field(settings, "z", base.z)),
    )


def _counts(eval_run: Any) -> tuple[int, int]:
    return int(_field(eval_run, "successes", 0)), int(_field(eval_run, "trials", 0))


def meets_threshold(eval_run: Any, settings: Any = None) -> bool:
    """The ship rule. Uses ``rate >= threshold`` by default, or ``ci_low >= threshold``
    when ``settings.require_ci_low`` is set. An eval with 0 trials never passes.
    """
    s = _settings(settings)
    successes, trials = _counts(eval_run)
    if trials == 0:
        return False
    if s.require_ci_low:
        low, _ = wilson_interval(successes, trials, s.z)
        return low >= s.success_threshold
    return success_rate(successes, trials) >= s.success_threshold


def _pct(x: float) -> int:
    return int(math.floor(x * 100 + 0.5))  # round half up, so 0.5 shows as 50


def format_ci(successes: int, trials: int, z: float = DEFAULT_Z) -> str:
    """Short dashboard string, e.g. ``'7/10, 95% CI 40–89%'``."""
    low, high = wilson_interval(successes, trials, z)
    return f"{successes}/{trials}, {confidence_pct(z)}% CI {_pct(low)}–{_pct(high)}%"


def explain_threshold(eval_run: Any, settings: Any = None) -> str:
    """Longer "show the math" line, with the rule used and the verdict, e.g.
    ``'8/10 = 80% (95% CI 49–94%); target 80% on point estimate: met'``.
    """
    s = _settings(settings)
    successes, trials = _counts(eval_run)
    low, high = wilson_interval(successes, trials, s.z)
    rate = success_rate(successes, trials)
    basis = "CI lower bound" if s.require_ci_low else "point estimate"
    verdict = "met" if meets_threshold(eval_run, s) else "not met"
    return (
        f"{successes}/{trials} = {_pct(rate)}% ({confidence_pct(s.z)}% CI {_pct(low)}–{_pct(high)}%); "
        f"target {_pct(s.success_threshold)}% on {basis}: {verdict}"
    )


FinalizeFn = Callable[..., Any]


class EvalRunFinalizer:
    """Calls ``finalize_eval_run`` once, when an eval run's last trial arrives.

    Feed it every ``eval_trial`` row (for example from a SpacetimeDB subscription).
    Trials are de-duplicated by id, so replays and resubscriptions don't double-count.
    It finalizes when the number of distinct trials equals ``eval_run.trials``.
    It never finalizes a run that is already finalized: one this instance
    finalized, one whose status is ``finalized``/``done``, or one that already
    has ``ci_low`` set.

    ``finalize`` is called as
    ``finalize(eval_run_id=..., successes=..., trials=..., ci_low=..., ci_high=..., summary=...)``.
    Wire it to the ``finalize_eval_run`` reducer.
    """

    FINAL_STATUSES = frozenset({"finalized", "done", "complete", "completed"})

    def __init__(self, finalize: FinalizeFn, z: float = DEFAULT_Z) -> None:
        self._finalize = finalize
        self._z = z
        self._seen: dict[Any, dict[Any, bool]] = {}  # eval_run_id -> {trial_id: success}
        self._finalized: set[Any] = set()

    def _already_final(self, eval_run: Any) -> bool:
        status = _field(eval_run, "status", None)
        # Status only: eval_run.ci_low is always present (0.0 until finalized).
        return isinstance(status, str) and status.lower() in self.FINAL_STATUSES

    def on_eval_trial(self, trial: Any, eval_run: Any) -> bool:
        """Record one trial. Returns True only on the call that fired finalize."""
        run_id = _field(eval_run, "id")
        if run_id is None:
            run_id = _field(trial, "eval_run_id")
        if run_id in self._finalized:
            return False
        if self._already_final(eval_run):
            self._finalized.add(run_id)
            return False

        trial_id = _field(trial, "id")
        seen = self._seen.setdefault(run_id, {})
        key = trial_id if trial_id is not None else _field(trial, "trial_index", len(seen))
        seen[key] = bool(_field(trial, "success", False))

        expected = int(_field(eval_run, "trials", 0))
        if expected <= 0 or len(seen) < expected:
            return False

        successes = sum(seen.values())
        trials = len(seen)
        low, high = wilson_interval(successes, trials, self._z)
        self._finalized.add(run_id)
        self._seen.pop(run_id, None)
        self._finalize(
            eval_run_id=run_id,
            successes=successes,
            trials=trials,
            ci_low=low,
            ci_high=high,
            summary=format_ci(successes, trials, self._z),
        )
        return True
