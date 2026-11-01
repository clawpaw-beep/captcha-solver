"""unittest suite for captcha_solver.behavior (stdlib unittest only).

    cd server && ../.venv/bin/python -m unittest tests.test_behavior -v
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from captcha_solver import behavior as B  # noqa: E402


def _xs(track):
    return [p["x"] for p in track["points"]]


class DragTrackTest(unittest.TestCase):
    def test_first_and_last_point(self):
        t = B.drag_track(185, seed=7)
        self.assertEqual(t["distance"], 185.0)
        self.assertEqual(t["points"][0]["x"], 0.0)
        self.assertEqual(t["points"][0]["y"], 0.0)
        self.assertEqual(t["points"][0]["t"], 0)
        self.assertAlmostEqual(t["points"][-1]["x"], 185.0, places=9)
        self.assertEqual(len(t["points"]), len(t["durations_ms"]))

    def test_x_monotonic_up_to_overshoot_peak(self):
        t = B.drag_track(233, seed=11)
        xs = _xs(t)
        peak = int(np.argmax(xs))
        self.assertGreater(xs[peak], 233.0, "overshoot must pass the target")
        for i in range(peak):
            self.assertGreaterEqual(xs[i + 1], xs[i] - 1e-9, f"x dropped at {i}")
        for i in range(peak, len(xs) - 1):
            self.assertLessEqual(xs[i + 1], xs[i] + 1e-9, f"rebound did not settle at {i}")

    def test_durations_are_not_uniform(self):
        t = B.drag_track(185, seed=7)
        durs = np.asarray(t["durations_ms"], dtype=float)
        cv = durs.std() / durs.mean()
        self.assertGreater(cv, 0.05, f"step timing too uniform: cv={cv:.3f}")
        self.assertGreaterEqual(durs.min(), B.SPEED_MIN_MS)
        self.assertLessEqual(durs.max(), B.SPEED_MAX_MS)
        self.assertEqual(t["total_ms"], int(sum(t["durations_ms"])))

    def test_front_faster_than_tail(self):
        t = B.drag_track(240, seed=5)
        durs = t["durations_ms"]
        head = np.mean(durs[: len(durs) // 3])
        tail = np.mean(durs[-len(durs) // 3:])
        self.assertLess(head, tail, "trajectory should decelerate towards the end")

    def test_seed_is_reproducible(self):
        a = B.drag_track(185, seed=7)
        b = B.drag_track(185, seed=7)
        c = B.drag_track(185, seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual(a["points"], c["points"])
        self.assertNotEqual(a["durations_ms"], c["durations_ms"])

    def test_overshoot_rebounds_to_distance(self):
        t = B.drag_track(185, seed=7, overshoot=True)
        xs = _xs(t)
        self.assertAlmostEqual(xs[-1], 185.0, places=9)
        self.assertGreater(max(xs), 185.0)
        self.assertGreater(t["profile"]["overshoot_px"], 0.0)
        self.assertLessEqual(t["profile"]["overshoot_px"], 2.5)

    def test_no_overshoot_never_exceeds_distance(self):
        t = B.drag_track(185, seed=7, overshoot=False)
        self.assertEqual(t["profile"]["overshoot_px"], 0.0)
        self.assertLessEqual(max(_xs(t)), 185.0 + 1e-9)
        self.assertAlmostEqual(_xs(t)[-1], 185.0, places=9)

    def test_invalid_distance(self):
        for bad in (0, -1, -0.5):
            with self.assertRaises(ValueError):
                B.drag_track(bad)
        with self.assertRaises(ValueError):
            B.drag_track(185, easing="ease_out_bogus")
        with self.assertRaises(ValueError):
            B.drag_track(185, jitter=-0.1)
        with self.assertRaises(ValueError):
            B.drag_track(185, steps=2)

    def test_adaptive_step_count(self):
        self.assertEqual(B.drag_track(185)["profile"]["steps"], round(185 / 8))
        self.assertEqual(B.drag_track(20)["profile"]["steps"], 12)
        self.assertEqual(B.drag_track(1000)["profile"]["steps"], 40)
        self.assertEqual(B.drag_track(185, steps=28)["profile"]["steps"], 28)
        self.assertEqual(len(B.drag_track(185, steps=28)["points"]), 28)

    def test_easing_variants_end_on_target(self):
        for name in B.EASINGS:
            t = B.drag_track(96, seed=3, easing=name)
            self.assertEqual(t["profile"]["easing"], name)
            self.assertEqual(t["points"][0]["x"], 0.0)
            self.assertAlmostEqual(t["points"][-1]["x"], 96.0, places=9)
            xs = _xs(t)
            peak = int(np.argmax(xs))
            for i in range(peak):
                self.assertGreaterEqual(xs[i + 1], xs[i] - 1e-9)

    def test_duration_ms_is_honoured_when_feasible(self):
        t = B.drag_track(185, seed=7, duration_ms=360)
        self.assertEqual(t["total_ms"], 360)
        self.assertEqual(t["total_ms"], sum(t["durations_ms"]))

    def test_duration_ms_clamps_to_the_human_band(self):
        t = B.drag_track(185, seed=7, duration_ms=10_000)
        n = len(t["durations_ms"])
        self.assertEqual(t["total_ms"], n * B.SPEED_MAX_MS)
        self.assertEqual(t["profile"]["duration_ms_requested"], 10_000)
        self.assertEqual(t["profile"]["speed_range_ms"], [B.SPEED_MIN_MS, B.SPEED_MAX_MS])

    def test_y_drift_is_bounded_and_smooth(self):
        t = B.drag_track(185, seed=7)
        ys = np.asarray([p["y"] for p in t["points"]])
        self.assertLessEqual(float(np.abs(ys).max()), 0.8 + 1e-9)
        steps = np.abs(np.diff(ys))
        self.assertLess(float(steps.max()), 0.6, "y should drift, not jump like white noise")


class ClickTrackTest(unittest.TestCase):
    def test_point_count_and_ranges(self):
        pts = [[120, 44], [300, 88], [210, 150]]
        t = B.click_track(pts, seed=3)
        self.assertEqual(len(t["clicks"]), len(pts))
        for click, (tx, ty) in zip(t["clicks"], pts):
            self.assertEqual(click["target"], [float(tx), float(ty)])
            self.assertLessEqual(abs(click["x"] - tx), B.OFFSET_MAX_PX + 1e-9)
            self.assertLessEqual(abs(click["y"] - ty), B.OFFSET_MAX_PX + 1e-9)
            self.assertTrue(60 <= click["hold_ms"] <= 140, click["hold_ms"])
            self.assertTrue(150 <= click["move_ms"] <= 400, click["move_ms"])
            self.assertEqual(len(click["micro"]), 2, "landing must be split into two micro-moves")
        self.assertGreater(t["total_ms"], 0)

    def test_seed_is_reproducible(self):
        pts = [[10, 10], [90, 60]]
        self.assertEqual(B.click_track(pts, seed=1), B.click_track(pts, seed=1))
        self.assertNotEqual(B.click_track(pts, seed=1)["clicks"], B.click_track(pts, seed=2)["clicks"])

    def test_rejects_empty_or_malformed_points(self):
        for bad in ([], None, [[1]], [["a", "b"]], [{"x": 1}], ["nope"]):
            with self.assertRaises(ValueError):
                B.click_track(bad)


class BuildTrackTest(unittest.TestCase):
    def test_dispatch_drag_and_click(self):
        drag = B.build_track({"distance": 185, "seed": 7})
        self.assertIn("points", drag)
        self.assertIn("humanize_check", drag)
        self.assertTrue(drag["humanize_check"]["ok"], drag["humanize_check"]["issues"])
        click = B.build_track({"points": [[10, 10], [100, 60]], "seed": 7})
        self.assertEqual(len(click["clicks"]), 2)
        self.assertEqual(click["humanize_check"]["metrics"]["kind"], "click")
        self.assertTrue(click["humanize_check"]["ok"], click["humanize_check"]["issues"])

    def test_rejects_missing_or_bad_input(self):
        for bad in ({}, {"distance": 0}, {"distance": -5}, {"points": []}, {"points": [[1]]}):
            with self.assertRaises(ValueError):
                B.build_track(bad)


class HumanizeCheckTest(unittest.TestCase):
    def test_flags_uniform_machine_track(self):
        n = 28
        track = {
            "distance": 185.0,
            "points": [{"x": round(i * 185.0 / (n - 1), 3), "y": 0.0, "t": i * 12} for i in range(n)],
            "durations_ms": [12] * n,
            "total_ms": 12 * n,
        }
        res = B.humanize_check(track)
        self.assertTrue(res["metrics"]["uniform_risk"])
        self.assertFalse(res["ok"])
        self.assertTrue(res["issues"])
        self.assertEqual(res["metrics"]["duration_cv"], 0.0)

    def test_accepts_generated_drag(self):
        res = B.humanize_check(B.drag_track(185, seed=7))
        self.assertTrue(res["ok"], res["issues"])
        self.assertTrue(res["metrics"]["x_monotonic"])
        self.assertFalse(res["metrics"]["uniform_risk"])
        self.assertGreater(res["metrics"]["duration_cv"], 0.05)
        self.assertGreaterEqual(res["metrics"]["steps"], 12)

    def test_accepts_generated_clicks(self):
        res = B.humanize_check(B.click_track([[120, 44], [300, 88]], seed=3))
        self.assertTrue(res["ok"], res["issues"])
        self.assertEqual(res["metrics"]["kind"], "click")
        self.assertEqual(res["metrics"]["steps"], 2)

    def test_flags_non_monotonic_x(self):
        track = {
            "distance": 100.0,
            "points": [
                {"x": 0.0, "y": 0.0, "t": 0},
                {"x": 40.0, "y": 0.0, "t": 15},
                {"x": 20.0, "y": 0.0, "t": 30},
                {"x": 100.0, "y": 0.0, "t": 45},
            ],
            "durations_ms": [15, 15, 12, 18],
            "total_ms": 60,
        }
        res = B.humanize_check(track)
        self.assertFalse(res["metrics"]["x_monotonic"])
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
