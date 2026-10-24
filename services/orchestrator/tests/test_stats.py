"""Unit tests for rdf_orchestrator.stats (TDM-66). Run: cd services/orchestrator && uv run python -m unittest discover -s tests"""

import unittest

from rdf_orchestrator.stats import (
    EvalRunFinalizer,
    ThresholdSettings,
    confidence_pct,
    explain_threshold,
    format_ci,
    meets_threshold,
    success_rate,
    wilson_interval,
)


class WilsonIntervalTest(unittest.TestCase):
    # Reference values computed independently from the Wilson score formula, z=1.96.
    KNOWN = {
        (8, 10): (0.4902, 0.9433),
        (7, 10): (0.3968, 0.8922),
        (5, 10): (0.2366, 0.7634),
        (9, 10): (0.5958, 0.9821),
        (10, 10): (0.7225, 1.0),
        (0, 10): (0.0, 0.2775),
        (16, 20): (0.5840, 0.9193),
        (3, 3): (0.4385, 1.0),
    }

    def test_known_values(self):
        for (k, n), (lo, hi) in self.KNOWN.items():
            with self.subTest(k=k, n=n):
                low, high = wilson_interval(k, n)
                self.assertAlmostEqual(low, lo, places=4)
                self.assertAlmostEqual(high, hi, places=4)

    def test_contains_point_estimate_and_bounded(self):
        for n in range(1, 31):
            for k in range(n + 1):
                low, high = wilson_interval(k, n)
                self.assertLessEqual(0.0, low)
                self.assertLessEqual(high, 1.0)
                self.assertLessEqual(low, k / n + 1e-12)
                self.assertGreaterEqual(high, k / n - 1e-12)

    def test_symmetry(self):
        low, high = wilson_interval(3, 10)
        low2, high2 = wilson_interval(7, 10)
        self.assertAlmostEqual(low, 1 - high2, places=12)
        self.assertAlmostEqual(high, 1 - low2, places=12)

    def test_zero_trials_is_uninformative(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_invalid_counts(self):
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)
        with self.assertRaises(ValueError):
            wilson_interval(-1, 10)

    def test_ten_trials_cannot_clear_80_on_ci_low(self):
        # Why require_ci_low defaults to False: even 10/10 has a lower bound of ~0.72.
        self.assertLess(wilson_interval(10, 10)[0], 0.8)

    def test_confidence_label(self):
        self.assertEqual(confidence_pct(1.96), 95)
        self.assertEqual(confidence_pct(1.645), 90)


class ThresholdTest(unittest.TestCase):
    def test_success_rate(self):
        self.assertEqual(success_rate(7, 10), 0.7)
        self.assertEqual(success_rate(0, 0), 0.0)

    def test_default_uses_point_estimate(self):
        s = {"success_threshold": 0.8}
        self.assertTrue(meets_threshold({"successes": 8, "trials": 10}, s))
        self.assertFalse(meets_threshold({"successes": 7, "trials": 10}, s))

    def test_require_ci_low(self):
        s = ThresholdSettings(success_threshold=0.8, require_ci_low=True)
        self.assertFalse(meets_threshold({"successes": 10, "trials": 10}, s))
        self.assertTrue(meets_threshold({"successes": 40, "trials": 40}, s))
        self.assertTrue(meets_threshold({"successes": 5, "trials": 10},
                                        ThresholdSettings(0.2, True)))

    def test_zero_trials_never_pass(self):
        self.assertFalse(meets_threshold({"successes": 0, "trials": 0}, {"success_threshold": 0.0}))

    def test_settings_accepts_camelcase_and_target_success(self):
        self.assertTrue(meets_threshold({"successes": 6, "trials": 10}, {"targetSuccess": 0.6}))
        self.assertFalse(meets_threshold({"successes": 10, "trials": 10},
                                         {"successThreshold": 0.8, "requireCiLow": True}))

    def test_object_rows(self):
        class Run:
            successes, trials = 9, 10
        self.assertTrue(meets_threshold(Run(), ThresholdSettings(0.9)))


class FormatTest(unittest.TestCase):
    def test_format_ci(self):
        self.assertEqual(format_ci(7, 10), "7/10, 95% CI 40–89%")
        self.assertEqual(format_ci(8, 10), "8/10, 95% CI 49–94%")
        self.assertEqual(format_ci(10, 10), "10/10, 95% CI 72–100%")

    def test_explain(self):
        self.assertEqual(
            explain_threshold({"successes": 8, "trials": 10}, {"success_threshold": 0.8}),
            "8/10 = 80% (95% CI 49–94%); target 80% on point estimate: met",
        )
        self.assertEqual(
            explain_threshold({"successes": 8, "trials": 10},
                              ThresholdSettings(0.8, require_ci_low=True)),
            "8/10 = 80% (95% CI 49–94%); target 80% on CI lower bound: not met",
        )


class FinalizerTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.f = EvalRunFinalizer(lambda **kw: self.calls.append(kw))
        self.run = {"id": 42, "trials": 10, "successes": 0, "status": "running"}

    def _trial(self, i, success):
        return {"id": f"t{i}", "eval_run_id": 42, "success": success}

    def test_fires_once_on_last_trial(self):
        fired = [self.f.on_eval_trial(self._trial(i, i < 7), self.run) for i in range(10)]
        self.assertEqual(fired, [False] * 9 + [True])
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["eval_run_id"], call["successes"], call["trials"]), (42, 7, 10))
        self.assertAlmostEqual(call["ci_low"], 0.3968, places=4)
        self.assertAlmostEqual(call["ci_high"], 0.8922, places=4)
        self.assertEqual(call["summary"], "7/10, 95% CI 40–89%")
        # Late or replayed trials after finalize don't fire again.
        self.assertFalse(self.f.on_eval_trial(self._trial(3, True), self.run))
        self.assertFalse(self.f.on_eval_trial(self._trial(99, True), self.run))
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_trials_not_double_counted(self):
        for _ in range(3):
            for i in range(9):
                self.f.on_eval_trial(self._trial(i, True), self.run)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.f.on_eval_trial(self._trial(9, False), self.run))
        self.assertEqual(self.calls[0]["successes"], 9)

    def test_skips_already_finalized_run(self):
        done = dict(self.run, status="finalized")
        for i in range(10):
            self.f.on_eval_trial(self._trial(i, True), done)
        self.assertEqual(self.calls, [])

    def test_ci_low_placeholder_does_not_block_finalize(self):
        # Raw eval_run rows carry ci_low=0.0 before finalize; that must not count as final.
        raw = dict(self.run, id=43, status="running", ci_low=0.0)
        for i in range(10):
            self.f.on_eval_trial(dict(self._trial(i, True), eval_run_id=43), raw)
        self.assertEqual(len(self.calls), 1)

    def test_independent_runs(self):
        other = {"id": 7, "trials": 2}
        self.f.on_eval_trial({"id": "a", "success": True}, other)
        for i in range(10):
            self.f.on_eval_trial(self._trial(i, True), self.run)
        self.f.on_eval_trial({"id": "b", "success": False}, other)
        self.assertEqual([c["eval_run_id"] for c in self.calls], [42, 7])
        self.assertEqual(self.calls[1]["summary"], "1/2, 95% CI 9–91%")


if __name__ == "__main__":
    unittest.main()
