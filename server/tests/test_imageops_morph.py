"""Tests for the morphological primitives used by the arithmetic pipeline."""
from __future__ import annotations

import unittest

import numpy as np

from captcha_solver import imageops as I


def _mask(shape=(60, 120)) -> np.ndarray:
    return np.zeros(shape, dtype=bool)


class ReconstructTest(unittest.TestCase):
    def test_keeps_thin_strokes_connected_to_a_core(self):
        ink = _mask()
        ink[20:40, 20:24] = True      # 4px thick body
        ink[10:20, 21:22] = True      # 1px "serif" hanging off it
        rebuilt = I.reconstruct(I.erode2(ink), ink)
        self.assertTrue(rebuilt[21:40, 21:22].all(), "thin attached stroke must survive")

    def test_drops_isolated_one_pixel_line(self):
        ink = _mask()
        ink[20:40, 20:24] = True
        ink[5, 10:110] = True         # 1px line, no thick core
        rebuilt = I.reconstruct(I.erode2(ink), ink)
        self.assertFalse(rebuilt[5, 10:110].any(), "isolated 1px line must vanish")

    def test_three_by_three_erode_kills_one_pixel_strokes(self):
        ink = _mask()
        ink[30, 10:100] = True
        self.assertFalse(I.erode3(ink).any())


class DropSmallTest(unittest.TestCase):
    def test_removes_speckle_keeps_glyph(self):
        ink = _mask()
        ink[20:40, 20:40] = True
        for x in range(50, 110, 7):
            ink[8:10, x : x + 2] = True
        out = I.drop_small_components(ink, min_area=40, min_extent=9)
        self.assertTrue(out[20:40, 20:40].all())
        self.assertLess(out.mean(), ink.mean())
        self.assertFalse(out[8:10, 50:52].any())


class SlantTest(unittest.TestCase):
    def test_removes_diagonal_scribble(self):
        ink = _mask()
        ink[20:40, 30:50] = True          # a "glyph"
        for i in range(60):
            y, x = 5 + i // 2, 40 + i
            if y < ink.shape[0] and x < ink.shape[1]:
                ink[y, x] = True
        lines = I.slant_mask(ink, min_len=12)
        self.assertTrue(lines.any(), "diagonal scribble should be detected")
        self.assertLess(lines[20:40, 30:50].sum(), 40 * 20, "glyph body must not be eaten")


class ProjectionTest(unittest.TestCase):
    def test_column_runs_finds_separated_groups(self):
        ink = _mask((20, 60))
        ink[:, 5:12] = True
        ink[:, 20:30] = True
        ink[:, 45:50] = True
        runs = I.column_runs(ink, thr=1.0, smooth=1, min_width=2)
        self.assertEqual(len(runs), 3)
        self.assertLess(runs[0][0], runs[1][0])

    def test_row_band_ignores_isolated_rows(self):
        ink = _mask((60, 40))
        ink[22:38, 5:35] = True
        ink[2, 5] = True
        y0, y1 = I.row_band(ink)
        self.assertLessEqual(abs(y0 - 22), 2)
        self.assertLessEqual(abs(y1 - 37), 2)


if __name__ == "__main__":
    unittest.main()
