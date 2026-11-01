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

    def test_eval_chain_needs_two_operators(self):
        self.assertIsNone(ocr.eval_chain("3+5=?"))
        self.assertEqual(ocr.eval_chain("10+3*3+8")["answer"], "27")

    def test_eval_chain_reports_operators(self):
        chain = ocr.eval_chain("10+3*3+8")
        self.assertEqual(chain["operators"], ["+", "*", "+"])
        self.assertEqual(chain["operands"], [10, 3, 3, 8])


if __name__ == "__main__":
    unittest.main()
