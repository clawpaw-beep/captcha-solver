"""The piece's transparent margin must be subtracted from the drag distance.

GeeTest v4 ships the piece as an 80x80 RGBA PNG whose silhouette sits 10-15px
inside the canvas. Matching runs on the trimmed content, so a gap at x=137 with
a 14px margin means the slider travels ~123px — not 137. Getting this wrong is a
guaranteed "Please try again" even with a perfect gap read.

The real fixtures below are the GeeTest v4 challenge images captured during the
2026-11-01 live run (out/live/gt/), so this test fails if the correction is ever
dropped or applied twice.
"""
from __future__ import annotations

import os
import unittest

from PIL import Image, ImageDraw

from captcha_solver import slider

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "gt")


def _padded_piece(margin: int = 14, size: int = 80) -> Image.Image:
    piece = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(piece).rectangle(
        [margin, margin, margin + 50, margin + 50], fill=(90, 140, 210, 255)
    )
    return piece


class PieceOriginTest(unittest.TestCase):
    def test_trimmed_origin_is_reported(self):
        gray, mask, origin = slider._piece_gray(_padded_piece(14))
        self.assertEqual(origin, (14, 14))
        self.assertEqual(gray.shape, (51, 51))
        self.assertTrue(mask.any())

    def test_full_bleed_piece_has_zero_origin(self):
        piece = Image.new("RGBA", (50, 60), (90, 140, 210, 255))
        gray, _mask, origin = slider._piece_gray(piece)
        self.assertEqual(origin, (0, 0))
        self.assertEqual(gray.shape, (60, 50))

    @unittest.skipUnless(os.path.exists(os.path.join(FIX, "bg3.png")), "live fixtures missing")
    def test_real_geetest_piece_margin(self):
        piece = Image.open(os.path.join(FIX, "slice3.png"))
        _gray, _mask, origin = slider._piece_gray(piece)
        self.assertGreaterEqual(origin[0], 10, f"expected a real transparent margin, got {origin}")

    @unittest.skipUnless(os.path.exists(os.path.join(FIX, "bg3.png")), "live fixtures missing")
    def test_drag_is_gap_minus_margin(self):
        bg = Image.open(os.path.join(FIX, "bg3.png"))
        piece = Image.open(os.path.join(FIX, "slice3.png"))
        res = slider.solve_slider(background=bg, piece=piece)
        self.assertIsNotNone(res.get("offset"), "solver found no gap on the fixture")
        dx = res.get("piece_shape_dx", 0)
        self.assertGreaterEqual(dx, 10)
        self.assertLessEqual(
            abs(res["drag_distance"] - (res["offset"] - dx)),
            1,
            f"drag={res['drag_distance']} gap={res['offset']} margin={dx}",
        )

    def test_margin_never_pushes_drag_negative(self):
        bg = Image.new("RGB", (300, 200), (60, 110, 200))
        ImageDraw.Draw(bg).rectangle([0, 70, 40, 130], fill=(20, 40, 80))
        res = slider.solve_slider(background=bg, piece=_padded_piece(14))
        self.assertGreaterEqual(res.get("drag_distance", 0), 0)


if __name__ == "__main__":
    unittest.main()


class RestingSlotTest(unittest.TestCase):
    """Aliyun PUZZLE paints a light copy of the piece at its resting place on the
    left edge. Without a guard the matcher locks onto that slot and reports
    gap ~= 0 — then the slider "drags 0px" and the challenge silently resets.

    Fixtures are the real challenge captured from business.uniagency.net on
    2026-11-01; the gap is at x≈215 and the resting slot at x≈0.
    """

    ALIYUN_BG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "uniA_bg.png")
    ALIYUN_PIECE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "uniA_piece.png")

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "uniA_bg.png")) and os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "uniA_piece.png")), "aliyun fixture missing")
    def test_gap_is_not_the_resting_slot(self):
        bg = Image.open(self.ALIYUN_BG)
        piece = Image.open(self.ALIYUN_PIECE)
        res = slider.solve_slider(background=bg, piece=piece)
        self.assertGreater(
            res["offset"], 150,
            f"solver locked onto the resting slot at x={res['offset']}",
        )
        self.assertGreater(res["drag_distance"], 140)

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "live", "gt", "bg3.png")), "geetest fixture missing")
    def test_geetest_scene_unaffected(self):
        bg = Image.open(os.path.join(FIX, "bg3.png"))
        piece = Image.open(os.path.join(FIX, "slice3.png"))
        res = slider.solve_slider(background=bg, piece=piece)
        self.assertGreater(res["offset"], 100, "resting-slot guard must not break GeeTest reads")
