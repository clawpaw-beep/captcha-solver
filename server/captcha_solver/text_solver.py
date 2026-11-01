"""Text-captcha solver: preprocessing ensemble + tesseract voting + segmentation."""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from . import imageops as I
from . import ocr

DEFAULT_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_BANK_CACHE = None


def _variants(img: Image.Image, aggressive: bool = False) -> List[Tuple[str, Image.Image, int, Optional[str]]]:
    """Build (name, image, psm, whitelist_override) preprocessing variants."""
    out: List[Tuple[str, Image.Image, int, Optional[str]]] = []
    gray = I.to_gray(img)
    raw = img.convert("L")

    out.append(("raw_l", raw, 7, None))
    out.append(("raw_l_13", raw, 13, None))

    ink = I.binarize(gray, "otsu")
    bin_img = I.to_pil(np.where(ink, 0, 255))
    out.append(("otsu", bin_img, 7, None))
    out.append(("otsu_8", bin_img, 8, None))

    nolines = I.remove_lines(ink)
    out.append(("nolines", I.to_pil(np.where(nolines, 0, 255)), 7, None))
    out.append(("nolines_13", I.to_pil(np.where(nolines, 0, 255)), 13, None))

    cleaned = I.median3(nolines)
    up = I.upscale(I.to_pil(np.where(cleaned, 0, 255)), 2.0)
    out.append(("clean2x", I.sharpen(up), 7, None))
    out.append(("clean2x_8", up, 8, None))

    ink_ad = I.binarize(gray, "adaptive")
    out.append(("adaptive", I.to_pil(np.where(ink_ad, 0, 255)), 7, None))

    ink_inv = ~ink
    out.append(("invert", I.to_pil(np.where(ink_inv, 0, 255)), 7, None))

    trimmed = I.trim(raw, pad=8)
    out.append(("trim3x", I.sharpen(I.upscale(trimmed, 3.0)), 7, None))

    if aggressive:
        den = I.denoise_gray(gray, strength=2)
        ink_d = I.binarize(den, "otsu")
        out.append(("denoise3x", I.upscale(I.to_pil(np.where(ink_d, 0, 255)), 3.0), 7, None))
        out.append(("denoise_inv", I.to_pil(np.where(~ink_d, 0, 255)), 7, None))
        ang = I.deskew_angle(ink)
        out.append(("deskew", I.rotate_pil(raw, -ang), 7, None))
    return out


def _segment_ocr(img: Image.Image, whitelist: str, max_chars: int = 8) -> Optional[ocr.OcrResult]:
    """Per-glyph OCR by connected components; robust when tesseract merges glyphs."""
    gray = I.to_gray(img)
    ink = I.binarize(I.median3(I.remove_lines(I.binarize(gray, "otsu"))), "otsu")
    comps = I.components(ink, min_area=max(12, int(ink.sum() * 0.01)))
    h = ink.shape[0]
    comps = [c for c in comps if c.height > h * 0.22 and c.width < ink.shape[1] * 0.6]
    if not 2 <= len(comps) <= max_chars:
        return None
    chars, confs = [], []
    for c in comps:
        patch = I.crop_ink(img.convert("L"), c, pad=4)
        patch = I.upscale(patch, max(2.0, 40.0 / max(patch.height, 1)))
        r = ocr.tesseract(patch, psm=10, whitelist=whitelist)
        ch = ocr.clean(r.text, whitelist)[:1]
        if not ch:
            # try a widened alphabet once for stubborn glyphs
            r2 = ocr.tesseract(patch, psm=10, whitelist=None)
            ch = ocr.clean(r2.text)[:1]
            r = r2 if ch else r
        if ch:
            chars.append(ch)
            confs.append(r.conf)
    if len(chars) < 2:
        return None
    return ocr.OcrResult(
        text="".join(chars),
        conf=float(np.mean(confs)) * 0.85,  # segmentation reads are less trustworthy
        psm=10,
        variant="segment",
    )


def _template_read(img: Image.Image, charset: str, length: int | None) -> Optional[ocr.OcrResult]:
    """Font-template read; builds (and caches) the glyph bank on first use."""
    global _BANK_CACHE
    try:
        from . import template as tpl

        if _BANK_CACHE is None:
            _BANK_CACHE = tpl.build_bank(charset)
        res = tpl.recognize(img, charset=charset, length=length, bank=_BANK_CACHE)
    except Exception:
        return None
    if not res.get("text"):
        return None
    return ocr.OcrResult(text=res["text"], conf=res["confidence"], psm=-2, variant="template")


def solve_text(
    img: Image.Image,
    charset: str | None = None,
    length: int | None = None,
    case_sensitive: bool = True,
    aggressive: bool = False,
    max_passes: int = 8,
    use_template: bool = True,
    use_deep_engine: bool = True,
    fast: bool = False,
) -> dict:
    """Best-effort single-line text captcha read.

    Engine order: ddddocr (neural, when importable) -> tesseract preprocessing
    ensemble -> font-template recogniser. All candidates are folded onto the
    caller's charset, then voted.
    """
    global _BANK_CACHE
    from . import engine

    charset_given = charset is not None
    charset = charset or DEFAULT_CHARSET
    if not case_sensitive:
        charset = "".join(sorted(set(charset.upper()), key=charset.find))

    results: List[ocr.OcrResult] = []

    # --- engine 1: neural classifier ---------------------------------------
    deep_used = False
    if use_deep_engine and engine.available():
        for vname, vtext in engine.classify_variants(img, fast=fast):
            results.append(
                ocr.OcrResult(text=vtext, conf=82.0, psm=-1, variant=f"ddddocr:{vname}")
            )
            deep_used = True

    # --- engine 2: tesseract preprocessing ensemble ------------------------
    variants = _variants(img, aggressive=aggressive)
    variants.sort(key=lambda v: 0 if v[0] in ("raw_l", "otsu", "nolines") else 1)
    if fast:
        priority = ("raw_l", "otsu", "nolines")
        variants = [v for v in variants if v[0] in priority]
        variants.sort(key=lambda v: priority.index(v[0]))
        max_passes = min(max_passes, 3)
    for name, vimg, psm, wl in variants[:max_passes]:
        r = ocr.tesseract(vimg, psm=psm, whitelist=wl or charset)
        r.variant = name
        if r.text:
            results.append(r)
        if r.text and r.conf > 88 and len(ocr.clean(r.text, charset)) >= (length or 3):
            break
        if name in ("otsu", "nolines"):
            for narrow in ocr.candidates_for_charset(charset)[1:2]:
                r2 = ocr.tesseract(vimg, psm=psm, whitelist=narrow)
                r2.variant = f"{name}+narrow"
                if r2.text:
                    results.append(r2)

    if not fast:
        for narrow in ocr.candidates_for_charset(charset)[1:3]:
            r = ocr.tesseract(img.convert("L"), psm=7, whitelist=narrow)
            r.variant = "narrow_only"
            if r.text:
                results.append(r)

    # --- engine 3: glyph segmentation (tesseract per glyph + templates) ----
    if not fast:
        seg = _segment_ocr(img, charset)
        if seg is not None:
            results.append(seg)
    if use_template and not fast and (length is None or length <= 8):
        tpl_read = _template_read(img, charset, length)
        if tpl_read is not None:
            results.append(tpl_read)

    # No charset hint: settle on the alphanumeric-uppercase alphabet. Neural
    # OCR decodes lowercase for uppercase glyphs, so a read's case is not
    # evidence; callers with a lowercase alphabet pass `charset` explicitly.
    if not charset_given:
        charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for r in results:
            r.text = r.text.upper()
    if not case_sensitive:
        for r in results:
            r.text = r.text.upper()

    text, conf, trail = ocr.vote(results, charset, length)
    if length and text and len(text) > length:
        text = text[:length]

    # settle single-glyph disagreements with the font bank before answering
    if use_template and text:
        rerun = [t["text"] for t in trail[:4] if t.get("text")]
        if len(rerun) >= 2 and length is None or (length and len(rerun) >= 2):
            try:
                from . import template as tpl

                if _BANK_CACHE is None:
                    _BANK_CACHE = tpl.build_bank(charset)
                adj = tpl.adjudicate(img, [text] + [t for t in rerun if t != text], charset, bank=_BANK_CACHE)
            except Exception:
                adj = None
            if adj and adj["text"] != text:
                text = adj["text"]
                conf = min(100.0, conf + 6.0)
                trail = [{"text": text, "score": round(conf, 2), "conf": round(conf, 2), "support": 1,
                          "variants": ["template_adjudicated"], "fixed": adj["fixed"]}] + trail
    # Confidence = evidence strength, not one model's say-so: variant agreement
    # inside the winning engine, length consensus across reads, and whether a
    # different engine corroborates the same string.
    by_engine: dict = {}
    for r in results:
        if not r.text:
            continue
        key = r.variant.split(":")[0].split("@")[0]
        by_engine.setdefault(key, set()).add(ocr.normalize(r.text, charset))
    credible = [r for r in results if r.text and r.conf >= 55.0] or [r for r in results if r.text]
    winner_reads = [r for r in credible if ocr.normalize(r.text, charset) == text] if text else []
    var_agree = len(winner_reads) / max(len(credible), 1) if text else 0.0
    lengths = {len(ocr.normalize(r.text, charset)) for r in credible}
    length_consensus = len(lengths) <= 1
    corroborated = any(
        text in texts for k, texts in by_engine.items() if "ddddocr" not in k
    ) if text else False
    if text:
        conf = conf * (0.55 + 0.45 * var_agree)
        if not length_consensus:
            conf *= 0.8
        if corroborated:
            conf = min(100.0, conf + 8.0)
    engine_agreement = round(var_agree, 2)
    engines = sorted(by_engine)
    return {
        "kind": "text",
        "text": text,
        "confidence": round(min(100.0, conf), 1),
        "variant_agreement": engine_agreement,
        "length_consensus": length_consensus,
        "corroborated": corroborated,
        "engines": engines,
        "deep_engine": deep_used,
        "candidates": trail,
        "charset": charset,
        "length": length,
        "passes": len(results),
    }


ARITH_CHARSET = "0123456789+-xX*/=?"


def _max_run(line: np.ndarray) -> int:
    """Longest consecutive run of True in one row/column."""
    best = cur = 0
    for v in line:
        if v:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _group(idx: List[int], gap: int = 1) -> List[List[int]]:
    groups: List[List[int]] = []
    for i in idx:
        if groups and i - groups[-1][-1] <= gap:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _symbol_kind(sub: np.ndarray, ref: float) -> Optional[str]:
    """Recognise +, -, = from stroke geometry (fonts vary too much for OCR).

    * solid thin bars are '-' when they own one wide-row group, '=' with two
      separated groups (the two strokes of the equals sign);
    * '+' owns one wide row *and* one tall column, both crossing near the middle.
    """
    h, w = sub.shape
    if h < 2 or w < 3:
        return None
    aspect = w / max(h, 1)
    rows_wide = [i for i in range(h) if _max_run(sub[i]) >= 0.75 * w]
    cols_tall = [j for j in range(w) if _max_run(sub[:, j]) >= 0.75 * h]
    if h <= max(0.45 * ref, 6.0):
        groups = _group(rows_wide)
        if len(groups) >= 2 and aspect >= 1.4:
            return "="
        if len(groups) == 1 and aspect >= 1.6:
            return "-"
        return None
    if h <= ref * 1.05 and w <= ref * 1.05 and 0.55 <= aspect <= 1.5:
        rgroups = _group(rows_wide)
        if len(rgroups) == 1 and len(cols_tall) >= 1:
            ry = (rgroups[0][0] + rgroups[0][-1]) / 2.0 / max(h - 1, 1)
            cs = sorted(set(cols_tall))
            cx = (cs[0] + cs[-1]) / 2.0 / max(w - 1, 1)
            span_row = max(_max_run(sub[i]) for i in rgroups[0]) / w
            span_col = max(_max_run(sub[:, j]) for j in cs) / h
            if 0.2 <= ry <= 0.8 and 0.2 <= cx <= 0.8 and span_row >= 0.7 and span_col >= 0.7:
                return "+"
    return None


def _glyph_ref(ink: np.ndarray) -> float:
    """Reference glyph height from the ink row profile, immune to 1px lines.

    Lines add a thin constant to many rows; glyphs own the tall band where the
    row sums are large, so the profile is the stable place to measure.
    """
    rows = ink.sum(axis=1).astype(np.float64)
    if rows.max() <= 0:
        return 12.0
    thr = max(2.0, 0.35 * rows.max())
    idx = np.flatnonzero(rows >= thr)
    if idx.size == 0:
        return 12.0
    groups: List[List[int]] = []
    for i in idx:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    best = max(groups, key=len)
    return float(best[-1] - best[0] + 1)


def _arith_glyphs(img: Image.Image) -> tuple[np.ndarray, List[I.Component], float]:
    """Ordered glyph boxes for an `a op b = ?` captcha.

    Interference lines are not removed globally (that erases '=' and '-' too);
    thin 1-2px lines are dropped by an opening instead, which keeps glyph
    strokes and merged operators intact.
    """
    ink = I.binarize(I.to_gray(img), "otsu")
    ref = _glyph_ref(ink)
    comps = I.components(ink, min_area=6)
    if not comps:
        return ink, [], ref
    keep: List[I.Component] = []
    for c in comps:
        h_ok = ref * 0.45 <= c.height <= ref * 1.5
        bar_ok = c.height <= max(5.0, ref * 0.3) and c.width >= 5
        blob = c.height > ref * 1.6 and c.width > ref * 1.6
        if (h_ok or bar_ok) and not blob and c.area >= 6:
            keep.append(c)
    keep.sort(key=lambda c: c.x0)
    merged = _merge_bar_pairs(keep, ink)
    merged.sort(key=lambda c: c.x0)
    return ink, merged, ref


def _merge_bar_pairs(comps: List[I.Component], ink: np.ndarray) -> List[I.Component]:
    """Fold the two strokes of '=' into one box so it reads as one glyph."""
    out = list(comps)
    for a in list(comps):
        if a.height > 6:
            continue
        for b in list(comps):
            if b is a or b.height > 6:
                continue
            if abs(a.x0 - b.x0) <= 3 and abs(a.x1 - b.x1) <= 3 and 2 <= abs(a.y0 - b.y0) <= 8:
                box = I.Component(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1), a.area + b.area)
                out = [c for c in out if c is not a and c is not b] + [box]
                return _merge_bar_pairs(out, ink)
    return out


def _read_digits(img: Image.Image, box: Optional[Tuple[int, int, int, int]]) -> str:
    """Digits-only read of a sub-rectangle: OCR and per-glyph templates vote.

    Short operands (1-2 glyphs) are where OCR flips 7/1 and 6/5; the font
    template bank is far steadier there, so it gets the deciding vote.
    """
    if box is None:
        return ""
    x0, _y0, x1, _y1 = box
    if x1 - x0 < 4:
        return ""
    crop = img.crop((x0, 0, x1, img.height))
    read = solve_text(crop, charset="0123456789", case_sensitive=True, max_passes=6)
    ocr_text = ocr.clean(read.get("text", ""), "0123456789")

    from . import template as tpl

    guess = ""
    sim_avg = 0.0
    try:
        ink = I.binarize(I.to_gray(crop), "otsu")
        comps = [c for c in I.components(ink, min_area=8) if c.height >= ink.shape[0] * 0.3]
        comps.sort(key=lambda c: c.x0)
        if 1 <= len(comps) <= 3:
            bank = tpl.build_bank("0123456789")
            chars, sims = [], []
            for c in comps:
                vec = _template_vec(I.crop_ink(crop.convert("L"), c, pad=2))
                top = bank.topk(vec, k=1, allowed=set("0123456789")) if vec is not None else []
                if not top:
                    chars = []
                    break
                chars.append(top[0][0])
                sims.append(top[0][1])
            if chars:
                guess = "".join(chars)
                sim_avg = float(np.mean(sims))
    except Exception:
        guess = ""

    if guess and (not ocr_text or len(guess) == len(ocr_text)) and sim_avg >= 0.72:
        if ocr_text != guess:
            return guess
        return ocr_text
    return ocr_text or guess


def _template_vec(patch: Image.Image):
    from . import template as tpl

    return tpl._norm_glyph(patch)


def _arith_from_structure(img: Image.Image, ink: np.ndarray, comps: List[I.Component], bank, ref: float) -> Optional[dict]:
    """Sequence read: symbols by geometry, digits by template bank."""
    chars: List[str] = []
    confs: List[float] = []
    op_x: Optional[int] = None
    eq_x0: Optional[int] = None
    op_char: Optional[str] = None
    for c in comps:
        sub = ink[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
        kind = _symbol_kind(sub, ref)
        if kind in ("+", "-", "="):
            chars.append(kind)
            confs.append(0.92)
            if kind == "=" and eq_x0 is None:
                eq_x0 = c.x0
            elif kind != "=" and op_x is None:
                op_x, op_char = c.x0, kind
            continue
        patch = I.crop_ink(img.convert("L"), c, pad=2)
        vec = _template_vec(patch)
        best_ch, best_conf = "", 0.0
        if vec is not None and bank is not None:
            top = bank.topk(vec, k=2, allowed=set(ARITH_CHARSET))
            if top:
                ch, sim = top[0]
                margin = sim - (top[1][1] if len(top) > 1 else 0.0)
                best_ch = ch
                best_conf = max(0.0, min(1.0, (sim - 0.42) / 0.45)) * (0.8 + 0.2 * min(1.0, margin * 8))
        if best_ch in "xX*" and op_x is None and best_conf > 0.3:
            op_x, op_char = c.x0, best_ch
            chars.append(best_ch)
            confs.append(best_conf)
            continue
        if not best_ch or best_conf < 0.35:
            r = ocr.tesseract(I.upscale(patch, 2.0), psm=10, whitelist=ARITH_CHARSET)
            alt = ocr.clean(r.text, ARITH_CHARSET)[:1]
            if alt and (not best_ch or r.conf / 100.0 > best_conf):
                best_ch, best_conf = alt, min(0.8, r.conf / 100.0)
        chars.append(best_ch)
        confs.append(best_conf)

    seq = "".join(chars)
    parsed = _parse_arith_sequence(list(seq)) or _parse_chain(seq)
    if parsed is None and op_x is not None:
        # fall back to splitting the image at the operator: OCR handles the
        # digits, geometry handles the operator
        right_bound = eq_x0 - 3 if eq_x0 else img.width
        a_text = _read_digits(img, (0, 0, max(4, op_x - 3), img.height))
        b_text = _read_digits(img, (op_x + 4, 0, right_bound, img.height))
        if a_text and b_text:
            parsed = _compose(a_text, op_char or "+", b_text)
            if parsed:
                seq = f"{a_text}{op_char}{b_text}"
    if parsed is None:
        return None
    base = float(np.mean(confs)) * 100.0 if confs else 0.0
    if "=" not in seq:
        base -= 8.0
    return {
        "kind": "arithmetic",
        "text": seq,
        "expr": parsed["expr"],
        "answer": parsed["answer"],
        "confidence": round(max(0.0, min(100.0, base)), 1),
        "route": "structural",
        "glyphs": [{"char": c, "conf": round(k, 2)} for c, k in zip(chars, confs)],
    }


def _compose(a_digits: str, op: str, b_digits: str) -> Optional[dict]:
    try:
        a, b = int(a_digits), int(b_digits)
    except ValueError:
        return None
    if op == "+":
        val = a + b
    elif op == "-":
        val = a - b
    elif op in "xX*":
        val = a * b
    elif op == "/":
        if b == 0:
            return None
        val = a // b if a % b == 0 else a / b
    else:
        return None
    return {"a": a, "b": b, "op": op, "expr": f"{a}{op}{b}", "answer": str(val)}


def _parse_chain(seq: str) -> Optional[dict]:
    """Full-expression parse (any number of operators, real precedence)."""
    from . import expression as E

    parsed = E.parse(seq, min_operands=2, max_operands=6)
    if not parsed:
        return None
    return {
        "a": parsed["operands"][0],
        "b": parsed["operands"][1],
        "op": parsed["operators"][0],
        "expr": parsed["expr"],
        "answer": parsed["answer"],
    }


def _parse_arith_sequence(chars: List[str]) -> Optional[dict]:
    """Cut at '=' and read `a op b` from what is left."""
    seq = list(chars)
    if "=" in seq:
        seq = seq[: seq.index("=")]
    ops = [i for i, ch in enumerate(seq) if ch in "+-xX*/"]
    if not ops:
        return None
    oi = ops[0]
    a_digits = "".join(ch for ch in seq[:oi] if ch.isdigit())
    b_digits = "".join(ch for ch in seq[oi + 1 :] if ch.isdigit())
    if not a_digits or not b_digits:
        return None
    return _compose(a_digits, seq[oi], b_digits)


def _eq_position(img: Image.Image) -> Optional[int]:
    """Left edge of the '=' glyph, used to crop off the trailing '?' entirely."""
    ink = I.binarize(I.to_gray(img), "otsu")
    comps = I.components(ink, min_area=6)
    bars = [c for c in comps if c.height <= 5 and c.width >= 6]
    best: Optional[int] = None
    for a in bars:
        for b in bars:
            if a is b:
                continue
            if abs(a.x0 - b.x0) <= 3 and abs(a.x1 - b.x1) <= 3 and 2 <= abs(a.y0 - b.y0) <= 8:
                x = min(a.x0, b.x0)
                best = x if best is None else max(best, x)
    return best


def _detect_operator(img: Image.Image, ink: np.ndarray, comps: List[I.Component], ref: float) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Locate the arithmetic operator and the '=' from stroke geometry."""
    op_char: Optional[str] = None
    op_x: Optional[int] = None
    eq_x: Optional[int] = None
    for c in comps:
        sub = ink[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
        kind = _symbol_kind(sub, ref)
        if kind == "=":
            eq_x = c.x0 if eq_x is None else max(eq_x, c.x0)
        elif kind in ("+", "-") and op_x is None:
            op_char, op_x = kind, c.x0
        elif kind == "x" and op_x is None:
            op_char, op_x = "x", c.x0
    return op_char, op_x, eq_x


def _split_route(img: Image.Image, ink: np.ndarray, comps: List[I.Component], ref: float) -> Optional[dict]:
    """Operator from geometry, operands from OCR of each clean half."""
    op_char, op_x, eq_x = _detect_operator(img, ink, comps, ref)
    if op_x is None:
        return None
    right = (eq_x - 3) if eq_x else img.width
    left_box = (0, 0, max(4, op_x - 3), img.height)
    right_box = (op_x + 4, 0, right, img.height)
    a_text = _read_digits(img, left_box)
    b_text = _read_digits(img, right_box)
    if not a_text or not b_text:
        return None
    parsed = _compose(a_text, op_char or "+", b_text)
    if parsed is None:
        return None
    clean = len(a_text) + len(b_text) <= 5 and op_char in ("+", "-", "x", "X", "*")
    return {
        "kind": "arithmetic",
        "text": f"{a_text}{op_char}{b_text}",
        "expr": parsed["expr"],
        "answer": parsed["answer"],
        "confidence": 78.0 if clean else 62.0,
        "route": "operator_split",
        "operator": op_char,
    }


def solve_arithmetic(img: Image.Image, charset: str = ARITH_CHARSET, aggressive: bool = False) -> dict:
    """Read `a op b = ?` and evaluate it.

    Three routes, best-parsing-confidence wins:
      1. structural — symbols by stroke geometry, digits by font templates;
      2. operator-split — geometry finds the operator, OCR reads each side;
      3. cropped string — OCR everything left of '=', so '?' cannot pollute it.
    """
    from . import template as tpl

    ink, comps, ref = _arith_glyphs(img)
    bank = None
    if comps:
        try:
            bank = tpl.build_bank(ARITH_CHARSET)
        except Exception:
            bank = None
    candidates: List[dict] = []

    struct = _arith_from_structure(img, ink, comps, bank, ref) if comps else None
    if struct:
        candidates.append(struct)

    split = _split_route(img, ink, comps, ref) if comps else None
    if split:
        candidates.append(split)

    eq_x = _eq_position(img)
    left = img.crop((0, 0, max(24, (eq_x or img.width) - 4), img.height))
    read = solve_text(left, charset=ARITH_CHARSET, case_sensitive=True, aggressive=aggressive, max_passes=9)
    parsed = ocr.eval_arithmetic(read.get("text", ""))
    if parsed:
        residue = ocr.clean(read.get("text", ""), ARITH_CHARSET)
        ambiguous = ocr.clean(parsed["expr"]) != residue
        candidates.append(
            {
                "kind": "arithmetic",
                "text": read.get("text", ""),
                "expr": parsed["expr"],
                "answer": parsed["answer"],
                "confidence": round(float(read.get("confidence", 0.0)) * (0.8 if ambiguous else 1.0), 1),
                "ambiguous": ambiguous,
                "route": "cropped_string",
                "eq_x": eq_x,
            }
        )

    chain = ocr.eval_chain(read.get("text", ""))
    if chain:
        candidates.append(
            {
                "kind": "arithmetic",
                "text": read.get("text", ""),
                "expr": chain["expr"],
                "answer": chain["answer"],
                "confidence": round(float(read.get("confidence", 0.0)) * 0.9, 1),
                "route": "chained",
                "operators": chain["operators"],
            }
        )

    if not candidates:
        return {
            "kind": "arithmetic",
            "text": read.get("text", ""),
            "answer": None,
            "confidence": round(float(read.get("confidence", 0.0)) * 0.5, 1),
            "read": read,
            "note": "expression not parseable — retry with kind=text and a wider charset",
        }

    # arbitration: corroboration across routes beats a single confident guess,
    # and the split route (operator by geometry, digits by OCR) wins bare ties
    route_weight = {"operator_split": 2.0, "cropped_string": 1.0, "structural": 0.0}
    answers = [str(c.get("answer")) for c in candidates]
    def rank(c: dict) -> float:
        agree = answers.count(str(c.get("answer"))) - 1
        return float(c.get("confidence", 0.0)) + 12.0 * agree + route_weight.get(c.get("route", ""), 0.0)

    candidates.sort(key=rank, reverse=True)
    chosen = candidates[0]
    if len(candidates) > 1:
        chosen["alternatives"] = [
            {"route": c.get("route"), "text": c.get("text"), "answer": c.get("answer"), "conf": c.get("confidence")}
            for c in candidates[1:4]
        ]
    return chosen
