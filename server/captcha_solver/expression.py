"""Arithmetic-expression parsing for captcha answers.

Single-operator captchas (`3+5=?`) are the easy case; real sites also render
longer chains (`10+3*3+8`, `2*4+3+7`) where operator precedence changes the
answer. This module owns the tokenizer, the evaluator and the OCR-noise
cleanup, so solvers only have to produce a character string.

Everything is exact: fractions are used internally, and an answer is reported
as an integer whenever the result is integral (captcha answers almost always
are), otherwise as the reduced fraction string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

# OCR spellings of the four operations. 'x'/'X' are what a text model returns
# for the multiplication sign; '×' and '÷' show up when the site renders them.
MUL_CHARS = "*xX×✕✖·⋅"
DIV_CHARS = "/÷➗"

OP_CANON = {
    "+": "+",
    "-": "-",
    "*": "*", "x": "*", "X": "*", "×": "*", "✕": "*", "✖": "*", "·": "*", "⋅": "*",
    "/": "/", "÷": "/", "➗": "/",
}

# Things a solver must throw away before parsing: the trailing question mark,
# the '=' sign, CJK prompt text ("答案是", "等于几"), spaces and fullwidth forms.
_FILLER_RE = re.compile(
    r"(答案是|答案|等于|=|≈|≒|\?|？|\uFF1D|结果|请计算|计算|多少|几)"
)
_FULLWIDTH = str.maketrans(
    {
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "＋": "+", "－": "-", "＊": "*", "／": "/", "（": "(", "）": ")",
    }
)


class ExprError(ValueError):
    """Raised when a string cannot be read as an arithmetic expression."""


@dataclass
class Token:
    kind: str  # "num" | "op" | "lparen" | "rparen"
    value: object  # int | str
    raw: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "raw": self.raw}


def strip_prompt(text: str) -> str:
    """Remove prompt text / '=' / '?' / spaces so only the expression remains."""
    s = (text or "").translate(_FULLWIDTH).lower()
    s = _FILLER_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    return s


def tokenize(text: str) -> List[Token]:
    """Split a cleaned expression into numbers, operators and parentheses."""
    s = strip_prompt(text)
    toks: List[Token] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            toks.append(Token("num", int(s[i:j]), s[i:j]))
            i = j
            continue
        if ch in OP_CANON:
            toks.append(Token("op", OP_CANON[ch], ch))
        elif ch == "(":
            toks.append(Token("lparen", "(", ch))
        elif ch == ")":
            toks.append(Token("rparen", ")", ch))
        # anything else (leftover CJK, punctuation) is dropped
        i += 1
    return toks


def _render(toks: List[Token]) -> str:
    out = []
    for t in toks:
        if t.kind == "num":
            out.append(str(t.value))
        elif t.kind == "op":
            out.append(t.value)
        elif t.kind == "lparen":
            out.append("(")
        else:
            out.append(")")
    return "".join(out)


class _Parser:
    """Recursive descent: expr := term (± term)* ; term := factor (×/÷ factor)*."""

    def __init__(self, toks: List[Token]):
        self.toks = toks
        self.pos = 0

    def peek(self) -> Optional[Token]:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def take(self) -> Token:
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> Fraction:
        val = self.expr()
        if self.peek() is not None:
            raise ExprError(f"trailing tokens at {self.pos}")
        return val

    def expr(self) -> Fraction:
        val = self.term()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "op" or tok.value not in "+-":
                return val
            self.take()
            rhs = self.term()
            val = val + rhs if tok.value == "+" else val - rhs

    def term(self) -> Fraction:
        val = self.factor()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "op" or tok.value not in "*/":
                return val
            self.take()
            rhs = self.factor()
            if tok.value == "*":
                val = val * rhs
            else:
                if rhs == 0:
                    raise ExprError("division by zero")
                val = val / rhs

    def factor(self) -> Fraction:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        if tok.kind == "op" and tok.value in "+-":
            self.take()
            val = self.factor()
            return val if tok.value == "+" else -val
        if tok.kind == "num":
            self.take()
            return Fraction(int(tok.value))
        if tok.kind == "lparen":
            self.take()
            val = self.expr()
            nxt = self.peek()
            if nxt is None or nxt.kind != "rparen":
                raise ExprError("unbalanced parenthesis")
            self.take()
            return val
        raise ExprError(f"unexpected token {tok.kind}:{tok.value}")


def evaluate(tokens: List[Token]) -> Fraction:
    if not tokens:
        raise ExprError("empty expression")
    return _Parser(tokens).parse()


def _format_value(val: Fraction) -> str:
    if val.denominator == 1:
        return str(val.numerator)
    # captcha answers are integers in practice; keep the exact fraction if not
    return f"{val.numerator}/{val.denominator}"


def parse(text: str, min_operands: int = 2, max_operands: int = 6) -> Optional[dict]:
    """Parse `10+3*3+8` / `3 x 5` / `(2+3)*4` into an evaluable record.

    Returns None when the string does not form a usable expression, so callers
    can fall back to a different OCR route rather than crash on noise.
    """
    toks = tokenize(text)
    if not toks:
        return None
    numbers = [t for t in toks if t.kind == "num"]
    ops = [t for t in toks if t.kind == "op"]
    if not (min_operands <= len(numbers) <= max_operands) or not ops:
        return None
    try:
        val = evaluate(toks)
    except (ExprError, ZeroDivisionError):
        return None
    expr = _render(toks)
    return {
        "expr": expr,
        "answer": _format_value(val),
        "value": float(val),
        "operands": [int(t.value) for t in numbers],
        "operators": [str(t.value) for t in ops],
        "tokens": [t.as_dict() for t in toks],
    }


def is_plausible_answer(parsed: Optional[dict], max_digits: int = 4) -> bool:
    """Guard against OCR garbage that happens to parse (e.g. `0-0-0`)."""
    if not parsed:
        return False
    ans = parsed.get("answer", "")
    return bool(ans) and len(ans.split("/")[0].lstrip("-")) <= max_digits
