"""Tests for the drawn-operator detector (`+`, `*`, `-`, `=`)."""
from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from captcha_solver import arith, symbols


def _ink_from(img: Image.Image) -> np.ndarray:
    return arith.prepare(img)["clean"]


def _render(text: str, size: int = 32, font: str = "Arial.ttf", width: int = 240) -> Image.Image:
    img = Image.new("RGB", (width, 70), (250, 250, 250))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(f"/System/Library/Fonts/Supplemental/{font}", size)
    d.text((8, 18), text, font=f, fill=(10, 10, 10))
    return img


class TemplateTest(unittest.TestCase):
    def test_templates_built_for_every_symbol(self):
        bank = symbols.build_templates(22)
        for ch in "+*-=":
            self.assertGreater(len(bank[ch]), 0, f"missing templates for {ch}")
            for t in bank[ch]:
                self.assertEqual(t.ndim, 2)

    def test_star_template_is_star_shaped(self):
        star = symbols._star_mask(24, 0.46, 0.44, 6, 0.0)
        self.assertEqual(star.shape, (24, 24))
        # a 6-point star has a much lower ink ratio than a filled square
        ratio = float((star > 128).mean())
        self.assertLess(ratio, 0.45)


class FindSymbolsTest(unittest.TestCase):
    def test_finds_plus_between_digits(self):
        img = _render("5+7")
        prep = arith.prepare(img)
        hits = symbols.find_symbols(prep["clean"], prep["band"], prep["band_height"], min_score=0.5)
        chars = [h["char"] for h in hits]
        self.assertIn("+", chars, f"plus not found, hits={hits}")

    def test_digit_four_scores_below_a_real_plus(self):
        """A `4` has a genuine cross — the classic false positive.

        It cannot always be rejected outright, so the contract is the margin: a
        drawn `+` scores >= 0.8 while a `4` stays clearly below it.
        """
        four = _render("4")
        plus = _render("5+7")
        p4 = arith.prepare(four)
        pp = arith.prepare(plus)
        four_hits = symbols.find_symbols(p4["clean"], p4["band"], p4["band_height"], min_score=0.3)
        plus_hits = symbols.find_symbols(pp["clean"], pp["band"], pp["band_height"], min_score=0.3)
        four_plus = [h for h in four_hits if h["char"] == "+"]
        plus_plus = [h for h in plus_hits if h["char"] == "+"]
        self.assertTrue(plus_plus, f"no + found in 5+7: {plus_hits}")
        if four_plus:
            self.assertLess(four_plus[0]["score"], plus_plus[0]["score"])
            self.assertLess(four_plus[0]["score"], 0.8, f"4 too confident: {four_plus}")
        # and with the production threshold the 4 must not surface at all
        strict = symbols.find_symbols(p4["clean"], p4["band"], p4["band_height"], min_score=0.8)
        self.assertNotIn("+", [h["char"] for h in strict], f"4 misread as + at 0.8: {strict}")

    def test_symbols_sorted_left_to_right(self):
        img = _render("5+7")
        prep = arith.prepare(img)
        hits = symbols.find_symbols(prep["clean"], prep["band"], prep["band_height"], min_score=0.4)
        xs = [h["x"] for h in hits]
        self.assertEqual(xs, sorted(xs))

    def test_scan_symbol_returns_scores_per_column(self):
        img = _render("5+7")
        prep = arith.prepare(img)
        tmpl = symbols._cross_mask(15, 2)
        res = symbols.scan_symbol(prep["clean"], prep["band"], tmpl)
        self.assertIsNotNone(res)
        scores, ys = res
        self.assertEqual(scores.shape[0], prep["clean"].shape[1] - 15 + 1)
        self.assertEqual(ys.shape[0], scores.shape[0])
        self.assertLessEqual(float(scores.max()), 1.0001)


if __name__ == "__main__":
    unittest.main()
