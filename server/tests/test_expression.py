"""Tests for chained arithmetic evaluation (`10+3*3+8` -> 27)."""
from __future__ import annotations

import unittest

from captcha_solver import expression as E
from captcha_solver import ocr


class ParseTest(unittest.TestCase):
    def test_single_operator_still_works(self):
        parsed = E.parse("3+5=?")
        self.assertEqual(parsed["expr"], "3+5")
        self.assertEqual(parsed["answer"], "8")

    def test_precedence_beats_left_to_right(self):
        cases = {
            "10+3*3+8": "27",
            "7+9*4+7": "50",
            "5+7+9*0": "12",
            "8*4*0+5": "5",
            "2*4+3+7": "18",
            "10*3+4*6": "54",
            "5+0*4*3": "5",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(E.parse(text)["answer"], want)

    def test_prompt_text_and_fullwidth_are_stripped(self):
        self.assertEqual(E.parse("10+3*3+8答案是?")["answer"], "27")
        self.assertEqual(E.parse("５＋７＝？")["answer"], "12")
        self.assertEqual(E.parse("5 + 7 + 9 * 0 = ?")["answer"], "12")

    def test_ocr_multiplication_spellings(self):
        for spelling in ("3x4", "3X4", "3*4", "3×4", "3·4"):
            with self.subTest(spelling=spelling):
                self.assertEqual(E.parse(spelling)["answer"], "12")

    def test_parentheses(self):
        self.assertEqual(E.parse("(2+3)*4")["answer"], "20")

    def test_exact_fraction_kept_when_not_integral(self):
        self.assertEqual(E.parse("7/2")["answer"], "7/2")

    def test_rejects_noise(self):
        for bad in ("", "???", "答案是", "3", "+", "1/0", "3+", "((3+4)"):
            with self.subTest(bad=bad):
                self.assertIsNone(E.parse(bad))

    def test_unary_minus(self):
        self.assertEqual(E.parse("5*-3")["answer"], "-15")


class OcrBridgeTest(unittest.TestCase):
    def test_eval_arithmetic_keeps_strict_single_operator(self):
        self.assertEqual(ocr.eval_arithmetic("3+5=?")["answer"], "8")
        # a trailing junk digit must still be read as b, not silently dropped
        self.assertEqual(ocr.eval_arithmetic("5+32")["answer"], "37")

    def test_chained_input_is_flagged_ambiguous(self):
        """Regression: the loose single-operator search cut `10+3*3+8` down to
        `10+3` and answered 13 with full confidence. It may still extract the
        pair — the cropped-string route needs that for cross-route voting — but
        it has to say out loud that the input was longer."""
        for text in ("10+3*3+8答案是?", "7+9*4+7", "8*4*0+5", "5+7+9*0", "5+3-2"):
            with self.subTest(text=text):
                got = ocr.eval_arithmetic(text)
                self.assertIsNotNone(got, "keep the pair so the vote can use it")
                self.assertTrue(got.get("ambiguous"), "must be flagged, never silently used")
                self.assertGreater(got.get("operators_seen", 0), 1)

    def test_unambiguous_reads_are_not_flagged(self):
        for text in ("3+5=?", "5+32", "12/4"):
            with self.subTest(text=text):
                self.assertFalse(ocr.eval_arithmetic(text).get("ambiguous"))

    def test_chained_route_answers_what_the_single_route_declines(self):
        self.assertEqual(ocr.eval_chain("10+3*3+8答案是?")["answer"], "27")
        self.assertEqual(ocr.eval_chain("8*4*0+5")["answer"], "5")

    def test_noise_around_a_single_pair_still_parses(self):
        # the loose fallback exists for OCR junk around `a op b` — keep it working
        self.assertEqual(ocr.eval_arithmetic("s3+5")["answer"], "8")
        self.assertEqual(ocr.eval_arithmetic("5+32")["answer"], "37")

    def test_eval_chain_needs_two_operators(self):
        self.assertIsNone(ocr.eval_chain("3+5=?"))
        self.assertEqual(ocr.eval_chain("10+3*3+8")["answer"], "27")

    def test_eval_chain_reports_operators(self):
        chain = ocr.eval_chain("10+3*3+8")
        self.assertEqual(chain["operators"], ["+", "*", "+"])
        self.assertEqual(chain["operands"], [10, 3, 3, 8])


if __name__ == "__main__":
    unittest.main()
