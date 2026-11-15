"""Tests for scripts/render-site-perf.py.

This script runs exactly once per Pages publish, on a runner, against state
that only exists on the gh-pages branch. Every bug in it therefore used to be
a post-merge bug: the only way to find out whether it worked was to merge it
and read the site afterwards. That is what these tests exist to stop.

Stdlib unittest rather than pytest on purpose -- the repo has no Python test
dependency and this needs none. Run them with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render-site-perf.py"


def _load_module():
    # The filename has hyphens in it, so it is not importable by name.
    spec = importlib.util.spec_from_file_location("render_site_perf", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_site_perf"] = module
    spec.loader.exec_module(module)
    return module


rsp = _load_module()


def row(sha: str, **metrics) -> dict:
    """A history row carrying the given metric values in base units."""
    return {
        "generated": "2026-09-26 12:00 UTC",
        "sha": sha,
        "metrics": {
            key: {"label": key, "display": f"{value}", "value": float(value),
                  "base_unit": "B/s"}
            for key, value in metrics.items()
        },
    }


class DistinctValues(unittest.TestCase):
    def test_collapses_consecutive_repeats(self):
        # The case this exists for: a publish is triggered by any report lane
        # finishing and re-records the other lanes' unchanged artifacts, so the
        # same reading can appear many times in a row without being measured
        # again.
        history = [row(str(i), gemm=v) for i, v in enumerate([1, 1, 1, 2, 2, 3])]
        self.assertEqual(rsp.distinct_values(history, "gemm"), [1.0, 2.0, 3.0])

    def test_keeps_non_consecutive_repeats(self):
        # A value that returns after changing is a genuine second reading.
        history = [row(str(i), gemm=v) for i, v in enumerate([1, 2, 1])]
        self.assertEqual(rsp.distinct_values(history, "gemm"), [1.0, 2.0, 1.0])

    def test_skips_rows_missing_the_metric(self):
        # A dropped metric is a gap in the series, not a reading of zero.
        history = [row("a", gemm=5), row("b", other=1), row("c", gemm=7)]
        self.assertEqual(rsp.distinct_values(history, "gemm"), [5.0, 7.0])


class BaselineFor(unittest.TestCase):
    def test_excludes_the_reading_being_judged(self):
        history = [row(str(i), gemm=v) for i, v in enumerate([10, 20, 30])]
        # mean(10, 20), not mean(10, 20, 30).
        self.assertAlmostEqual(rsp.baseline_for(history, "gemm"), 15.0)

    def test_window_is_capped(self):
        values = [1, 2, 3, 4, 5, 6, 7, 100]
        history = [row(str(i), gemm=v) for i, v in enumerate(values)]
        # The last BASELINE_WINDOW distinct readings before 100.
        expected = sum([3, 4, 5, 6, 7]) / 5
        self.assertAlmostEqual(rsp.baseline_for(history, "gemm"), expected)
        self.assertEqual(rsp.BASELINE_WINDOW, 5)

    def test_none_without_a_second_distinct_reading(self):
        self.assertIsNone(rsp.baseline_for([], "gemm"))
        self.assertIsNone(rsp.baseline_for([row("a", gemm=1)], "gemm"))
        repeated = [row(str(i), gemm=1) for i in range(5)]
        self.assertIsNone(rsp.baseline_for(repeated, "gemm"))


class Health(unittest.TestCase):
    def test_thresholds(self):
        cases = [
            (120.0, rsp.COLOR_OK),        # improved
            (100.0, rsp.COLOR_OK),        # exactly at baseline
            (95.0, rsp.COLOR_OK),         # exactly on the warn boundary
            (94.9, rsp.COLOR_WARN),
            (80.0, rsp.COLOR_WARN),       # exactly on the fail boundary
            (79.9, rsp.COLOR_REGRESSED),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                state = rsp.health(value, 100.0, higher_is_better=True)
                self.assertEqual(state["color"], expected)

    def test_lower_is_better_inverts(self):
        # No lower-is-better metric ships today, but the flag is declared on
        # every entry in METRICS and getting it backwards would be silent.
        better = rsp.health(50.0, 100.0, higher_is_better=False)
        worse = rsp.health(200.0, 100.0, higher_is_better=False)
        self.assertEqual(better["color"], rsp.COLOR_OK)
        self.assertEqual(worse["color"], rsp.COLOR_REGRESSED)

    def test_no_baseline_is_not_a_pass(self):
        for baseline in (None, 0.0):
            with self.subTest(baseline=baseline):
                state = rsp.health(1.0, baseline, higher_is_better=True)
                self.assertEqual(state["color"], rsp.COLOR_NO_BASELINE)
                self.assertIsNone(state["delta"])

    def test_delta_is_percent_against_baseline(self):
        self.assertAlmostEqual(
            rsp.health(150.0, 100.0, higher_is_better=True)["delta"], 50.0)
        self.assertAlmostEqual(
            rsp.health(50.0, 100.0, higher_is_better=True)["delta"], -50.0)


class ScaleValue(unittest.TestCase):
    def test_round_trips_through_parse_metric(self):
        meta = {"key": "k", "label": "k", "base_unit": "B/s"}
        for value in (823_000.0, 144_000_000.0, 1.5e12, 487.0):
            with self.subTest(value=value):
                display = rsp.scale_value(value, "B/s")
                parsed = rsp.parse_metric(meta, {"message": display})
                self.assertIsNotNone(parsed, f"{display!r} did not re-parse")
                # %.3g, so allow rounding but not a wrong SI prefix.
                self.assertAlmostEqual(parsed.value / value, 1.0, places=2)


class BadgeOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.perf = Path(self.tmp.name) / "perf"
        self.addCleanup(self.tmp.cleanup)

    def badge(self, name: str) -> dict:
        return json.loads((self.perf / name).read_text())

    def metric_row(self, sha, **values):
        return {
            "generated": "2026-09-26 12:00 UTC",
            "sha": sha,
            "metrics": {
                key: {
                    "label": key,
                    "display": rsp.scale_value(value, "B/s"),
                    "value": float(value),
                    "base_unit": "B/s",
                }
                for key, value in values.items()
            },
        }

    def test_regression_is_red_and_carries_the_delta(self):
        history = [
            self.metric_row("a", gemm=1000.0),
            self.metric_row("b", gemm=1000.0),
            self.metric_row("c", gemm=500.0),
        ]
        rsp.write_badges(self.perf, history)
        badge = self.badge("badge-gemm.json")
        self.assertEqual(badge["color"], rsp.COLOR_REGRESSED)
        self.assertIn("-50%", badge["message"])
        self.assertEqual(badge["schemaVersion"], 1)
        self.assertEqual(badge["label"], "Rocjitsu GEMM")

    def test_improvement_is_green(self):
        history = [
            self.metric_row("a", gemm=500.0),
            self.metric_row("b", gemm=1000.0),
        ]
        rsp.write_badges(self.perf, history)
        badge = self.badge("badge-gemm.json")
        self.assertEqual(badge["color"], rsp.COLOR_OK)
        self.assertIn("+100%", badge["message"])

    def test_first_reading_has_no_baseline(self):
        rsp.write_badges(self.perf, [self.metric_row("a", gemm=500.0)])
        badge = self.badge("badge-gemm.json")
        self.assertEqual(badge["color"], rsp.COLOR_NO_BASELINE)
        self.assertNotIn("%", badge["message"])

    def test_missing_metric_is_grey_and_na(self):
        rsp.write_badges(self.perf, [self.metric_row("a", gemm=500.0)])
        badge = self.badge("badge-hipfile.json")
        self.assertEqual(badge["color"], rsp.COLOR_MISSING)
        self.assertEqual(badge["message"], "n/a")

    def test_empty_history_writes_every_badge(self):
        rsp.write_badges(self.perf, [])
        for meta in rsp.METRICS:
            with self.subTest(metric=meta["key"]):
                badge = self.badge(meta["badge_file"])
                self.assertEqual(badge["color"], rsp.COLOR_MISSING)


class EndToEnd(unittest.TestCase):
    """Drive main() the way publish-pages.yml does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inputs = Path(self.tmp.name) / "perf-in"
        self.site = Path(self.tmp.name) / "docs"
        self.addCleanup(self.tmp.cleanup)
        for meta in rsp.METRICS:
            path = self.inputs / meta["source"][0] / meta["source"][1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schemaVersion": 1, "label": meta["label"],
                "message": f"100 {meta['base_unit']}", "color": "blue",
            }))

    def run_main(self, *extra):
        out = Path(self.tmp.name) / "out"
        argv = [
            "render-site-perf.py",
            "--input-dir", str(self.inputs),
            "--site-dir", str(self.site),
            "--history-out", str(out / "history.jsonl"),
            *extra,
        ]
        old = sys.argv
        sys.argv = argv
        try:
            rsp.main()
        finally:
            sys.argv = old
        return out

    def test_writes_every_expected_artifact(self):
        out = self.run_main()
        rows = (out / "history.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(json.loads(rows[0])["metrics"]),
            {meta["key"] for meta in rsp.METRICS},
        )
        perf = self.site / "perf"
        self.assertTrue((perf / "index.md").exists())
        for meta in rsp.METRICS:
            self.assertTrue((perf / meta["badge_file"]).exists())
            self.assertTrue((perf / f"chart-{meta['key']}.svg").exists())

    def test_only_perf_is_written_to_the_site(self):
        # The inputs are raw benchmark artifacts; nothing but perf/ publishes.
        self.run_main()
        self.assertEqual([p.name for p in self.site.iterdir()], ["perf"])

    def test_perf_page_carries_every_metric(self):
        self.run_main()
        body = (self.site / "perf" / "index.md").read_text()
        for meta in rsp.METRICS:
            with self.subTest(metric=meta["key"]):
                self.assertIn(meta["badge_file"], body)
                self.assertIn(f"chart-{meta['key']}.svg", body)
        # One header cell per metric, plus commit and timestamp.
        table = [line for line in body.splitlines() if line.startswith("| Commit ")]
        self.assertEqual(table[0].count("|"), len(rsp.METRICS) + 3)

    def test_missing_history_is_tolerated(self):
        # First ever publish: the history file is not on the branch yet.
        missing = Path(self.tmp.name) / "nope" / "history.jsonl"
        out = self.run_main("--history-in", str(missing))
        self.assertTrue((out / "history.jsonl").exists())

    def test_no_inputs_still_writes_every_badge(self):
        # Every artifact expired: the shields must read n/a, not 404.
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.inputs = empty
        self.run_main()
        for meta in rsp.METRICS:
            badge = json.loads((self.site / "perf" / meta["badge_file"]).read_text())
            self.assertEqual(badge["message"], "n/a")


class RealHistoryRegression(unittest.TestCase):
    """A fixture taken from the published perf/history.jsonl.

    The shape matters as much as the numbers: rows 1-5 are five different
    commits carrying one unchanged measurement, which is exactly the case
    that a naive mean over the last five rows gets wrong.
    """

    SERIES = [484_000.0] * 6 + [823_000.0] * 7 + [487_000.0]

    def test_gemm_reads_as_a_regression(self):
        history = [row(str(i), gemm=v) for i, v in enumerate(self.SERIES)]
        baseline = rsp.baseline_for(history, "gemm")
        # mean(484k, 823k) -- the two distinct readings before the latest.
        self.assertAlmostEqual(baseline, (484_000.0 + 823_000.0) / 2)
        state = rsp.health(487_000.0, baseline, higher_is_better=True)
        self.assertEqual(state["color"], rsp.COLOR_REGRESSED)
        self.assertAlmostEqual(state["delta"], -25.5, places=1)

    def test_naive_mean_would_have_hidden_it(self):
        # Guard the design decision itself: averaging the last five rows
        # verbatim weights the republished 823k plateau five times and makes
        # the same reading look like a 41% drop rather than 25%, purely
        # because unrelated lanes happened to publish while it was stuck.
        naive = sum(self.SERIES[-6:-1]) / 5
        self.assertNotAlmostEqual(naive, (484_000.0 + 823_000.0) / 2)


if __name__ == "__main__":
    unittest.main()
