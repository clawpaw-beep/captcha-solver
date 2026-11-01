"""The Aliyun PUZZLE transmission curve, pinned to measurements.

These five data points came off business.uniagency.net on 2026-11-01 by reading
`#aliyunCaptcha-puzzle` style.left while dragging the handle. They are the whole
reason earlier drags failed: the piece does not track the handle 1:1.
"""
from __future__ import annotations

import math
import unittest

from captcha_solver import aliyun_slide as A

MEASURED = [(50, 12.72), (170, 115.68), (204, 163.44), (219, 187.12), (240, 222.96)]


class CurveTest(unittest.TestCase):
    def test_fits_every_measured_point(self):
        for slider, puzzle in MEASURED:
            with self.subTest(slider=slider):
                self.assertAlmostEqual(A.puzzle_left(slider), puzzle, delta=1.6,
                                       msg=f"slider={slider} should land near {puzzle}")

    def test_inverse_round_trips(self):
        for target in (20, 90, 150, 200, 255):
            with self.subTest(target=target):
                s = A.slider_for_puzzle(target)
                self.assertAlmostEqual(A.puzzle_left(s), target, delta=0.5)

    def test_piece_saturates_at_travel_limit(self):
        self.assertLessEqual(A.puzzle_left(300), A.PUZZLE_MAX + 1e-6)
        self.assertAlmostEqual(A.puzzle_left(260), A.PUZZLE_MAX, delta=2.0)

    def test_nonlinear_gap_is_big_enough_to_matter(self):
        # dragging the gap width (200) would land the piece ~40px short — this is
        # exactly the failure mode that looked like "position is correct"
        self.assertGreater(200 - A.puzzle_left(200), 30)

    def test_plan_reports_clamping(self):
        self.assertFalse(A.plan(150)["clamped"])
        self.assertTrue(A.plan(400)["clamped"])

    def test_slope_grows_with_offset(self):
        self.assertLess(A.local_slope(50), A.local_slope(200))
        self.assertGreater(A.local_slope(0), 0)


if __name__ == "__main__":
    unittest.main()
