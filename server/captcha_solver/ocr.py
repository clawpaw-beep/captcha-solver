"""Tesseract driver: multi-configuration OCR with confidence-weighted voting."""
from __future__ import annotations

import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
from PIL import Image

TESS_BIN = shutil.which("tesseract") or "tesseract"

# Characters tesseract likes to swap, keyed by the char it *returns*.
CONFUSIONS: Dict[str, str] = {
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "I": "1", "l": "1", "i": "1", "|": "1", "!": "1",
    "Z": "2", "z": "2",
    "A": "4",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
    "0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B",
    "/": "I", "\\": "I", "|": "I", "!": "I", "(": "I", ")": "I", "[": "I", "]": "I",
    "}": "I", "{": "I", "『": "I", "」": "I", "「": "I",
    "了": "1", "T": "7", "¥": "Y", "€": "E", "£": "L",
    "u": "U", "v": "V", "w": "W", "x": "X", "y": "Y", "z": "Z",
}


@dataclass
class OcrResult:
    text: str = ""
    conf: float = 0.0
    psm: int = 7
    variant: str = ""
    raw_lines: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "conf": round(self.conf, 2),
            "psm": self.psm,
            "variant": self.variant,
        }


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    if img.mode not in ("L", "RGB"):
        img = img.convert("L")
    img.save(buf, format="PNG")
    return buf.getvalue()


def tesseract(
    img: Image.Image,
    psm: int = 7,
    whitelist: str | None = None,
    lang: str = "eng",
    timeout: float = 12.0,
    oem: int | None = None,
) -> OcrResult:
    """Run one tesseract pass and return text plus mean word confidence."""
    if img.width < 4 or img.height < 4:
        return OcrResult("", 0.0, psm, "tiny")
    cmd = [TESS_BIN, "stdin", "stdout", "--psm", str(int(psm)), "-l", lang]
    if oem is not None:
        cmd += ["--oem", str(int(oem))]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    cmd += ["-c", "preserve_interword_spaces=1", "tsv"]
    try:
        proc = subprocess.run(
            cmd,
            input=_png_bytes(img),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return OcrResult("", 0.0, psm, "timeout")

    text_out = proc.stdout.decode("utf-8", "replace")
    words: List[str] = []
    confs: List[float] = []
    for line in text_out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        if cols[0] != "5":
            continue
        word = cols[11].strip()
        if not word:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            conf = -1.0
        if conf < 0:
            continue
        words.append(word)
        confs.append(conf)
    text = " ".join(words)
    mean_conf = float(np.mean(confs)) if confs else 0.0
    return OcrResult(text=text, conf=mean_conf, psm=psm, variant=f"psm{psm}", raw_lines=words)


def clean(text: str, charset: str | None = None) -> str:
    """Drop whitespace / punctuation noise and optionally keep only charset."""
    t = re.sub(r"\s+", "", text or "")
    if charset:
        allowed = set(charset)
        t = "".join(ch for ch in t if ch in allowed)
    return t


def repair(text: str, charset: str) -> str:
    """Rewrite ambiguous glyphs toward characters that are legal in `charset`."""
    if not text:
        return text
    allowed = set(charset)
    out = []
    for ch in text:
        if ch in allowed:
            out.append(ch)
            continue
        sub = CONFUSIONS.get(ch)
        if sub and sub in allowed:
            out.append(sub)
            continue
        out.append(ch)
    return "".join(out)


def candidates_for_charset(charset: str) -> List[str]:
    """Two alphabets for a retry round: as-given, and heavily narrowed."""
    out = [charset]
    digits = "".join(c for c in "0123456789" if c in charset)
    upper = "".join(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c in charset)
    lower = "".join(c for c in "abcdefghijklmnopqrstuvwxyz" if c in charset)
    if len(digits) >= 2 and len(digits) < len(charset):
        out.append(digits)
    if upper and digits and len(upper + digits) < len(charset):
        out.append(upper + digits)
    if lower and digits and (lower + digits) not in out:
        out.append(lower + digits)
    return out


def fold_case(text: str, charset: str) -> str:
    """Force the case the charset allows (captcha alphabets are usually one case)."""
    if not text:
        return text
    has_lower = any(c.islower() for c in charset)
    has_upper = any(c.isupper() for c in charset)
    if has_upper and not has_lower:
        return text.upper()
    if has_lower and not has_upper:
        return text.lower()
    return text


def _fill_from_peers(text: str, peers: Sequence[str], charset: str | None) -> str:
    """Fill characters that are illegal under `charset` from aligned peer reads."""
    if not charset or not peers:
        return text
    allowed = set(charset)
    if all(ch in allowed for ch in text):
        return text
    best = None
    for p in peers:
        if len(p) != len(text):
            continue
        score = sum(1 for a, b in zip(text, p) if a == b)
        if best is None or score > best[0]:
            best = (score, p)
    if best is None:
        return text
    peer = best[1]
    out = []
    for i, ch in enumerate(text):
        if ch in allowed:
            out.append(ch)
            continue
        cand = peer[i] if i < len(peer) else ch
        out.append(cand if cand in allowed else "")
    return "".join(out)


def normalize(text: str, charset: str | None) -> str:
    t = clean(text, None)
    if not charset:
        return t
    t = fold_case(t, charset)
    return repair(t, charset)


def vote(results: Sequence[OcrResult], charset: str | None = None, length: int | None = None) -> tuple[str, float, List[dict]]:
    """Confidence-weighted vote over several OCR passes.

    Two schemes are computed and the stronger one is returned:

    * whole-string vote — repeated identical reads are a strong signal;
    * per-position vote — survives when every pass differs by one glyph.
    """
    usable: List[tuple[str, OcrResult]] = []
    peers = [clean(r.text, None) for r in results if r.text]
    for r in results:
        if not r.text or r.conf <= 0:
            continue
        t = normalize(r.text, charset)
        t = _fill_from_peers(t, peers, charset)
        if t:
            usable.append((t, r))
    if not usable:
        return "", 0.0, []

    by_text: Dict[str, List[OcrResult]] = {}
    for t, r in usable:
        by_text.setdefault(t, []).append(r)
    scored = []
    for t, rs in by_text.items():
        support = len(rs)
        conf = float(np.mean([r.conf for r in rs]))
        penalty = abs(len(t) - length) * 18.0 if length else 0.0
        score = conf + (support - 1) * 12.0 - penalty
        scored.append((score, t, conf, support, rs))
    scored.sort(key=lambda x: -x[0])
    best_score, best_text, best_conf, support, rs = scored[0]
    trail = [
        {
            "text": t,
            "score": round(s, 2),
            "conf": round(c, 2),
            "support": n,
            "variants": sorted({r.variant for r in rlist}),
        }
        for s, t, c, n, rlist in scored[:6]
    ]

    # ---- per-position consensus -------------------------------------------
    lengths = [len(t) for t, _ in usable]
    if length:
        target_len = length
    else:
        counts: Dict[int, float] = {}
        for (t, r) in usable:
            counts[len(t)] = counts.get(len(t), 0.0) + max(r.conf, 5.0)
        target_len = max(counts, key=counts.get) if counts else 0
    same = [(t, r) for t, r in usable if len(t) == target_len]
    if target_len and len(same) >= 2:
        cols: List[Dict[str, float]] = [{} for _ in range(target_len)]
        for t, r in same:
            w = max(r.conf, 5.0) + 6.0
            for i, ch in enumerate(t):
                cols[i][ch] = cols[i].get(ch, 0.0) + w
        chars, char_confs = [], []
        for col in cols:
            total = sum(col.values())
            ch = max(col, key=col.get)
            chars.append(ch)
            char_confs.append(col[ch] / total if total else 0.0)
        consensus = "".join(chars)
        cons_conf = float(np.mean(char_confs)) * 100.0
        agree = 1.0 if all(c > 0.5 for c in char_confs) else 0.72
        cons_conf *= agree
        if cons_conf > best_conf * 0.92:
            if consensus != best_text:
                trail.insert(
                    0,
                    {
                        "text": consensus,
                        "score": round(cons_conf, 2),
                        "conf": round(cons_conf, 2),
                        "support": len(same),
                        "variants": ["position_consensus"],
                    },
                )
            best_text, best_conf = consensus, max(best_conf, cons_conf)
    # Under-reading is the common failure (an engine drops a glyph). A longer
    # read backed by several passes wins over the shorter consensus unless the
    # short one is clearly better supported.
    def _insertion_only(short: str, long: str) -> bool:
        """True when `long` is `short` plus exactly one inserted character."""
        if len(long) != len(short) + 1:
            return False
        for i in range(len(long)):
            if long[:i] + long[i + 1:] == short:
                return True
        return False

    for cand in scored:
        if len(cand[1]) != len(best_text) + 1:
            continue
        if not _insertion_only(best_text, cand[1]):
            continue
        if cand[3] >= 3 and cand[2] >= 0.8 * best_conf:
            trail.insert(
                0,
                {
                    "text": cand[1],
                    "score": round(cand[0], 2),
                    "conf": round(cand[2], 2),
                    "support": cand[3],
                    "variants": sorted({r.variant for r in cand[4]}),
                    "reason": "longer_read_with_support",
                },
            )
            best_text, best_conf, support = cand[1], cand[2], cand[3]
            break
    return best_text, float(best_conf), trail


# ------------------------------------------------------------ arithmetic ----
_ARITH_RE = re.compile(r"^\s*(-?\d+)\s*([+\-xX*×÷/])\s*(-?\d+)\s*=?\s*\??\s*$")
_ARITH_LOOSE = re.compile(r"(-?\d+)\s*([+\-xX*×÷/])\s*(-?\d+)")


def eval_arithmetic(text: str) -> Optional[dict]:
    """Turn `3+5=?` into `{'expr': '3+5', 'answer': '8'}` — one operator at a time.

    A whole-string match wins; a loose search is the fallback for OCR noise
    around the pair. When the string carries **more than one operator** the loose
    search would cut `10+3*3+8` down to `10+3`, so the result is kept but flagged
    `ambiguous: True` — downstream routes discount it (see the cropped-string
    route in `solve_arithmetic`) and :func:`eval_chain` supplies the
    precedence-correct answer. Silently answering 13 with full confidence was
    the bug the CI smoke test caught.
    """
    from . import expression as E

    t = (text or "").replace(" ", "").replace("？", "?").replace("＝", "=")
    t = t.replace("乘", "x").replace("加", "+").replace("减", "-").replace("除以", "/")
    n_ops = len([c for c in t if c in E.OP_CANON])
    strict = _ARITH_RE.match(t)
    m = strict or _ARITH_LOOSE.search(t)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    try:
        if op in "+":
            val = a + b
        elif op in "-":
            val = a - b
        elif op in "xX*×":
            val = a * b
        else:
            if b == 0:
                return None
            val = a // b if a % b == 0 else a / b
    except ZeroDivisionError:
        return None
    out = {"expr": f"{a}{op}{b}", "answer": str(val), "operator": op}
    if n_ops > 1:
        out["ambiguous"] = True
        out["operators_seen"] = n_ops
    return out


def eval_chain(text: str, min_operators: int = 2, max_operands: int = 6) -> Optional[dict]:
    """Evaluate a chained expression with real precedence (`10+3*3+8` -> 27).

    Only used when a read really contains two or more operators; single-operator
    strings stay on :func:`eval_arithmetic` so existing arbitration is untouched.
    """
    from . import expression as E

    parsed = E.parse(text, min_operands=2, max_operands=max_operands)
    if not parsed or len(parsed["operators"]) < min_operators:
        return None
    return {
        "expr": parsed["expr"],
        "answer": parsed["answer"],
        "operator": parsed["operators"][0],
        "operators": parsed["operators"],
        "operands": parsed["operands"],
    }
