"""Multi-operator arithmetic captchas (`10+3*3+8答案是?`).

The single-operator solver in :mod:`text_solver` assumes exactly one operator
and a clean left-to-right read. Sites like chsi render longer chains, use a
drawn star for multiplication (not the font's `*`) and append a CJK prompt, so
the read has to be structural:

1. threshold high enough to keep anti-aliased strokes, then knock out the
   scribbled interference lines with a multi-angle run-length filter;
2. rebuild the glyphs from their thick cores (morphological reconstruction) so
   speckle disappears without eating thin strokes;
3. find the text band, segment it into glyph blocks, and classify each block as
   an operator (geometry), a digit (font-template + OCR vote) or CJK (reject) —
   the first CJK block ends the expression;
4. hand the resulting character string to :mod:`expression` for precedence-aware
   evaluation.

Nothing here trusts a single engine: every digit block carries the agreement
between the template bank and tesseract, and the final answer is only as
confident as the weakest glyph in the chain.
"""
from __future__ import annotations

import glob
import os
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import expression as E
from . import imageops as I
from . import ocr

CELL_W = 40
CELL_H = 32

SERIF_FONTS = [
    "Times New Roman.ttf",
    "Times New Roman Bold.ttf",
    "Times New Roman Italic.ttf",
    "Georgia.ttf",
    "Georgia Bold.ttf",
    "Georgia Italic.ttf",
    "Palatino.ttc",
    "Baskerville.ttc",
    "Arial.ttf",
    "Arial Bold.ttf",
    "Courier New.ttf",
    "Courier New Bold.ttf",
]
FONT_DIRS = [
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
    "C:\\Windows\\Fonts",
]


# --------------------------------------------------------------- templates ----
def _font_paths(extra: Optional[Sequence[str]] = None) -> List[str]:
    out: List[str] = []
    for name in list(SERIF_FONTS) + list(extra or []):
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.exists(p):
                out.append(p)
                break
        else:
            if os.path.isabs(name) and os.path.exists(name):
                out.append(name)
    if not out:
        for d in FONT_DIRS[:2]:
            for pat in ("*.ttf", "*.ttc"):
                out.extend(sorted(glob.glob(os.path.join(d, pat)))[:6])
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def glyph_vec(patch: Image.Image) -> Optional[np.ndarray]:
    """Aspect-preserving glyph descriptor: ink-crop, height-normalise, pad.

    Unlike a plain squash to a square this keeps `1` narrow and `8` wide, which
    is the only thing separating `1/7` and `0/8` on serif captchas.
    """
    g = I.to_gray(patch)
    mask = g < 200
    if mask.sum() < 4:
        return None
    ys, xs = np.where(mask)
    crop = patch.convert("L").crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    h, w = crop.height, crop.width
    if h < 2 or w < 1:
        return None
    scale = CELL_H / float(h)
    nw = max(1, min(CELL_W, int(round(w * scale))))
    nh = CELL_H
    if nw == CELL_W and w * scale > CELL_W:
        nh = max(2, int(round(h * (CELL_W / float(w)))))
    crop = crop.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("L", (CELL_W, CELL_H), 255)
    canvas.paste(crop, ((CELL_W - nw) // 2, (CELL_H - nh) // 2))
    v = 255.0 - np.asarray(canvas, dtype=np.float32)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    if n < 1e-6:
        return None
    return (v / n).ravel()


@lru_cache(maxsize=8)
def digit_bank(fonts_key: Tuple[str, ...] = ()) -> Tuple[np.ndarray, List[str]]:
    """Pre-normalised digit vectors plus their character labels."""
    fonts = _font_paths(fonts_key) or _font_paths()
    vecs: List[np.ndarray] = []
    labels: List[str] = []
    for ch in "0123456789":
        for f in fonts:
            for size in (22, 26, 30, 34):
                try:
                    font = ImageFont.truetype(f, size)
                except Exception:
                    continue
                canvas = Image.new("L", (size * 3, size * 3), 255)
                ImageDraw.Draw(canvas).text((size // 2, size // 2), ch, font=font, fill=0)
                v = glyph_vec(canvas)
                if v is None:
                    continue
                vecs.append(v)
                labels.append(ch)
    if not vecs:
        return np.zeros((0, CELL_W * CELL_H), dtype=np.float32), []
    return np.stack(vecs).astype(np.float32), labels


def digit_scores(vec: np.ndarray, allowed: str = "0123456789") -> Dict[str, float]:
    bank, labels = digit_bank()
    if bank.size == 0:
        return {}
    sims = bank @ vec
    out: Dict[str, float] = {}
    for ch in allowed:
        idx = [i for i, c in enumerate(labels) if c == ch]
        if idx:
            out[ch] = float(sims[idx].max())
    return out


def best_digit(vec: np.ndarray) -> Tuple[str, float, float]:
    """(char, similarity, margin over the runner-up)."""
    scores = digit_scores(vec)
    if not scores:
        return "", 0.0, 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    ch, sim = ranked[0]
    margin = sim - (ranked[1][1] if len(ranked) > 1 else 0.0)
    return ch, sim, margin


# ------------------------------------------------------------ preprocessing ----
def _text_band(keep: np.ndarray, min_height: int = 10) -> Tuple[int, int, int]:
    """Text line extent and glyph height, measured from the glyphs themselves.

    A row-projection threshold is not enough: on a large font the densest rows
    are the middle of the digits, so the "band" collapses to half the glyph and
    every later width estimate is half of what it should be. Components know
    their own extent, so the median component height is the honest glyph height
    and the union of their rows is the honest band.
    """
    comps = [c for c in I.components(keep, min_area=12) if c.height >= 7]
    if not comps:
        y0, y1 = I.row_band(keep, min_height)
        return int(y0), int(y1), int(max(min_height, y1 - y0 + 1))
    heights = sorted(c.height for c in comps)
    # the tallest components are the digits: an isolated `+` or a broken glyph
    # would drag a plain median down and every template would come out small
    gh = int(np.percentile(heights, 75))
    tall = [c for c in comps if c.height >= 0.62 * gh]
    y0 = min(c.y0 for c in tall)
    y1 = max(c.y1 for c in tall)
    if y1 - y0 + 1 < min_height:
        pad = (min_height - (y1 - y0 + 1)) // 2 + 1
        y0, y1 = max(0, y0 - pad), min(keep.shape[0] - 1, y1 + pad)
    return int(y0), int(y1), int(max(gh, min_height))


def _prepare_with(gray: np.ndarray, otsu: float, thr: float) -> dict:
    """One threshold hypothesis: clean, rebuild, denoise, find the text line."""
    ink = gray < thr
    slant = I.slant_mask(ink)
    clean = ink & ~slant
    core = I.erode2(clean)
    rebuilt = I.reconstruct(core, clean)
    keep = I.drop_small_components(rebuilt, min_area=45, min_extent=9)
    y0, y1, gh = _text_band(keep)
    return {
        "gray": gray,
        "thr": round(float(thr), 1),
        "otsu": round(float(otsu), 1),
        "ink": ink,
        "clean": clean,
        "keep": keep,
        "slant_ratio": round(float(slant.mean()), 4),
        "band": (int(y0), int(y1)),
        "band_height": int(gh),
        "band_span": int(y1 - y0 + 1),
    }


def prepare(img: Image.Image, high_threshold: bool = True, thr: Optional[float] = None) -> dict:
    """Return the cleaned ink mask plus the text band for an arithmetic image."""
    gray = I.to_gray(img)
    otsu = I.otsu(gray)
    if thr is None:
        thr = max(otsu + 64.0, 200.0) if high_threshold else otsu
        thr = min(thr, 220.0)
    return _prepare_with(gray, otsu, float(thr))


def prepare_variants(img: Image.Image) -> List[dict]:
    """Threshold sweep — one global cut never fits both clean and noisy renders.

    A clean synthetic captcha breaks apart at otsu+64, a speckle-showered JPEG
    loses whole strokes at plain otsu. Rather than guess, each candidate runs
    the full read and the best-scoring one wins (see `solve`).
    """
    gray = I.to_gray(img)
    otsu = I.otsu(gray)
    cands = {round(float(otsu), 1), round(min(otsu + 35.0, 210.0), 1), round(min(otsu + 70.0, 228.0), 1)}
    return [_prepare_with(gray, otsu, thr) for thr in sorted(cands)]


def glyph_blocks(
    keep: np.ndarray,
    band: Tuple[int, int],
    min_part: int = 20,
    band_h: Optional[int] = None,
) -> List[I.Component]:
    """Components of the text band, with same-line fragments glued together.

    Speckle survives morphological reconstruction when it happens to be 2x2 or
    thicker, so the size filter here is expressed as a fraction of the glyph
    height: a block must be at least ~45% as tall as the text line, or wide and
    short like a `-` / `=` bar sitting inside the line.
    """
    y0, y1 = band
    sub = keep[y0 : y1 + 1]
    bh = int(band_h if band_h is not None else (y1 - y0 + 1))
    comps = []
    for c in I.components(sub, min_area=min_part):
        if c.area < min_part:
            continue
        tall = c.height >= 0.45 * bh
        bar = c.height >= 3 and c.width >= 0.42 * bh and c.height <= 0.4 * bh
        if tall or bar:
            comps.append(c)
    if not comps:
        return []
    # glue fragments that belong to one glyph: overlapping rows and <=1px apart
    boxes = sorted(comps, key=lambda c: c.x0)
    merged: List[I.Component] = []
    for c in boxes:
        if merged:
            p = merged[-1]
            h_overlap = min(p.y1, c.y1) - max(p.y0, c.y0)
            if c.x0 - p.x1 <= 1 and h_overlap > 0:
                merged[-1] = I.Component(
                    min(p.x0, c.x0), min(p.y0, c.y0), max(p.x1, c.x1), max(p.y1, c.y1), p.area + c.area
                )
                continue
        merged.append(I.Component(c.x0, c.y0, c.x1, c.y1, c.area))
    for c in merged:
        c.y0 += y0
        c.y1 += y0
    return merged


def segment_columns(
    keep: np.ndarray,
    band: Tuple[int, int],
    band_h: int,
    unit: float,
    zero_ratio: float = 0.05,
) -> List[I.Component]:
    """Cut on near-empty columns first, then fall back to equal-width splitting.

    Captcha glyphs are set 1-3px apart, so after speckle removal the columns
    between them are empty while columns inside a glyph never are. Splitting on
    those gaps is far more faithful than guessing a pitch; only blobs with no
    internal gap at all (glued pairs) need the width estimate.
    """
    y0, y1 = band
    sub = keep[y0 : y1 + 1]
    if sub.size == 0:
        return []
    cols = sub.sum(axis=0).astype(np.float64)
    thr = max(1.0, zero_ratio * max(band_h, 1))
    empty = cols <= thr
    # runs of empty columns -> cut positions at the middle of each run
    cuts: List[int] = []
    x = 0
    W = sub.shape[1]
    while x < W:
        if empty[x]:
            j = x
            while j < W and empty[j]:
                j += 1
            cuts.append((x + j - 1) // 2)
            x = j
        else:
            x += 1
    bounds = [0] + [c for c in cuts if 0 < c < W - 1] + [W - 1]
    blocks: List[I.Component] = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a < 2:
            continue
        piece = sub[:, a : b + 1]
        if piece.sum() < 20:
            continue
        ys, xs = np.where(piece)
        box = I.Component(a + int(xs.min()), y0 + int(ys.min()), a + int(xs.max()), y0 + int(ys.max()), int(piece.sum()))
        blocks.append(box)
    # drop slivers and glue blocks that are one glyph split in two
    out: List[I.Component] = []
    for c in blocks:
        if out:
            p = out[-1]
            overlap_rows = min(p.y1, c.y1) - max(p.y0, c.y0)
            if (c.width < 0.28 * unit or p.width < 0.28 * unit) and c.x0 - p.x1 <= 2 and overlap_rows > 0:
                out[-1] = I.Component(min(p.x0, c.x0), min(p.y0, c.y0), max(p.x1, c.x1),
                                      max(p.y1, c.y1), p.area + c.area)
                continue
        out.append(c)
    return split_wide(out, keep, unit)


def split_wide(
    blocks: List[I.Component],
    keep: np.ndarray,
    unit: float,
    max_parts: int = 6,
) -> List[I.Component]:
    """Split merged multi-glyph blobs into ~`unit`-wide pieces.

    `unit` is the expected advance width of one glyph (estimated from the band
    height, not from the blob widths — a run of glued glyphs would otherwise be
    its own median and never get split at all). Each cut lands on the deepest
    column-projection valley inside a +-28% window around the nominal position.
    """
    if not blocks:
        return blocks
    unit = max(6.0, float(unit))
    out: List[I.Component] = []
    for c in blocks:
        n = int(round(c.width / unit))
        if n <= 1:
            out.append(c)
            continue
        n = min(n, max_parts)
        sub = keep[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
        cols = I.smooth1d(sub.sum(axis=0).astype(np.float64), 3)
        cuts: List[int] = []
        for k in range(1, n):
            nominal = c.width * k / n
            lo = int(nominal - unit * 0.30)
            hi = int(nominal + unit * 0.30)
            lo, hi = max(1, lo), min(c.width - 2, hi)
            if hi <= lo:
                continue
            cuts.append(lo + int(np.argmin(cols[lo:hi])))
        cuts = sorted(set(cuts))
        if len(cuts) != n - 1:
            out.append(c)
            continue
        bounds = [0] + cuts + [c.width]
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b - a < 3:
                continue
            out.append(I.Component(c.x0 + a, c.y0, c.x0 + b - 1, c.y1, max(1, c.area * (b - a) // max(c.width, 1))))
    return out


# ------------------------------------------------------------- classification ----
def _longest_run(mask: np.ndarray, axis: int) -> int:
    best = cur = 0
    for v in mask:
        if v:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def star_like(patch: np.ndarray) -> Tuple[bool, dict]:
    """Decide whether a block is the drawn 6-point star used for `*`."""
    h, w = patch.shape
    if h < 6 or w < 6:
        return False, {}
    ar = w / float(h)
    fill = float(patch.mean())
    max_row = max(_longest_run(patch[y], 1) for y in range(h))
    max_col = max(_longest_run(patch[:, x], 0) for x in range(w))
    sub = patch[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3]
    centre = float(sub.mean()) if sub.size else 0.0
    # star: square-ish, moderately dense, no long straight run, dense centre
    ok = (
        0.72 <= ar <= 1.45
        and 0.26 <= fill <= 0.62
        and max_row < 0.74 * w
        and max_col < 0.80 * h
        and centre >= 0.45
    )
    return ok, {
        "aspect": round(ar, 2),
        "fill": round(fill, 2),
        "max_row": int(max_row),
        "max_col": int(max_col),
        "centre": round(centre, 2),
    }


def plus_like(ink: np.ndarray, box: I.Component, band_h: int) -> bool:
    """A `+` is a thin horizontal and a thin vertical stroke crossing near mid."""
    sub = ink[box.y0 : box.y1 + 1, box.x0 : box.x1 + 1]
    h, w = sub.shape
    if h < 8 or w < 8:
        return False
    rows_full = [y for y in range(h) if _longest_run(sub[y], 1) >= 0.80 * w]
    cols_full = [x for x in range(w) if _longest_run(sub[:, x], 0) >= 0.80 * h]
    if not rows_full or not cols_full:
        return False
    # ± thickness must stay thin (a glyph body would be much thicker)
    if len(rows_full) > max(3, 0.18 * h) or len(cols_full) > max(3, 0.18 * w):
        return False
    if float(sub.mean()) > 0.42:
        return False
    ry = (rows_full[0] + rows_full[-1]) / 2.0 / max(h - 1, 1)
    cx = (cols_full[0] + cols_full[-1]) / 2.0 / max(w - 1, 1)
    return 0.25 <= ry <= 0.75 and 0.25 <= cx <= 0.75


def minus_like(patch: np.ndarray, band_h: int) -> bool:
    h, w = patch.shape
    if h > max(4.0, 0.34 * band_h) or w < 6:
        return False
    return max(_longest_run(patch[y], 1) for y in range(h)) >= 0.7 * w


def equals_like(patch: np.ndarray, band_h: int) -> bool:
    h, w = patch.shape
    if h > max(7.0, 0.4 * band_h) or w < 8:
        return False
    rows = [y for y in range(h) if _longest_run(patch[y], 1) >= 0.7 * w]
    if len(rows) < 2:
        return False
    groups: List[List[int]] = []
    for y in rows:
        if groups and y - groups[-1][-1] <= 1:
            groups[-1].append(y)
        else:
            groups.append([y])
    return len(groups) == 2 and 1 <= groups[-1][0] - groups[0][-1] <= 8


def read_digit(crop: Image.Image, trust_ocr: bool = True) -> dict:
    """Digit read by template first, OCR as the corroborating second opinion."""
    vec = glyph_vec(crop)
    ch, sim, margin = best_digit(vec) if vec is not None else ("", 0.0, 0.0)
    text, conf = "", 0.0
    if trust_ocr:
        big = I.upscale(crop, 2.2)
        r = ocr.tesseract(big, psm=10, whitelist="0123456789")
        text = ocr.clean(r.text, "0123456789")[:1]
        conf = float(r.conf)
    agree = bool(ch) and text == ch
    if agree:
        return {"char": ch, "sim": round(sim, 3), "ocr": text, "agree": True, "conf": 92.0}
    if ch and sim >= 0.34:
        return {"char": ch, "sim": round(sim, 3), "ocr": text, "agree": False,
                "conf": round(min(90.0, 40.0 + sim * 60.0 * (1.0 + min(margin, 0.2))), 1)}
    if text and conf >= 55:
        return {"char": text, "sim": round(sim, 3), "ocr": text, "agree": False,
                "conf": round(min(85.0, conf * 0.85), 1)}
    if ch:
        return {"char": ch, "sim": round(sim, 3), "ocr": text, "agree": False,
                "conf": round(max(25.0, sim * 55.0), 1)}
    return {"char": "", "sim": round(sim, 3), "ocr": text, "agree": False, "conf": 0.0}


def classify_block(
    block: I.Component,
    ink: np.ndarray,
    clean: np.ndarray,
    band_h: int,
    trust_ocr: bool = True,
) -> dict:
    """Operator by geometry, digit by template+OCR, otherwise likely CJK."""
    y0, y1 = block.y0, block.y1
    x0, x1 = block.x0, block.x1
    patch = ink[y0 : y1 + 1, x0 : x1 + 1]
    clean_patch = clean[y0 : y1 + 1, x0 : x1 + 1]
    h, w = patch.shape
    base = {
        "box": [int(x0), int(y0), int(w), int(h)],
        "area": int(block.area),
        "aspect": round(w / max(h, 1), 2),
        "fill": round(float(patch.mean()), 2),
    }
    if minus_like(clean_patch, band_h):
        return {**base, "char": "-", "kind": "operator", "conf": 80.0, "why": "thin bar"}
    if equals_like(clean_patch, band_h):
        return {**base, "char": "=", "kind": "operator", "conf": 80.0, "why": "double bar"}
    if plus_like(ink, block, band_h):
        return {**base, "char": "+", "kind": "operator", "conf": 85.0, "why": "thin cross"}
    star, feats = star_like(clean_patch)
    if star:
        return {**base, "char": "*", "kind": "operator", "conf": 74.0, "why": "star blob", "feats": feats}

    crop = I.crop_ink(I.to_pil(np.where(clean_patch, 0, 255)), I.Component(0, 0, w - 1, h - 1, int(clean_patch.sum())), pad=2)
    digit = read_digit(crop, trust_ocr=trust_ocr)
    if digit["char"] and (digit["sim"] >= 0.30 or digit["ocr"]):
        return {**base, "kind": "digit", **digit}
    # nothing fits a digit or an operator: treat as CJK prompt text, but only
    # when the block actually looks like a square CJK cell
    cjk_shape = 0.55 <= base["aspect"] <= 2.4 and h <= 0.95 * band_h and w >= max(9, 0.42 * band_h)
    if cjk_shape and (digit.get("sim", 0.0) < 0.33 or not digit.get("char")):
        return {**base, "char": "", "kind": "cjk", "conf": 20.0, "why": "cjk cell"}
    return {**base, "kind": "digit", **digit, "why": "weak digit"}


# ------------------------------------------------------------------ solving ----
def _attempt(
    blocks: List[I.Component],
    prep: dict,
    trust_ocr: bool,
    label: str,
) -> Optional[dict]:
    ink = prep["ink"]
    clean = prep["keep"]
    band_h = prep["band_height"]
    glyphs: List[dict] = []
    cut = False
    for b in blocks:
        g = classify_block(b, ink, clean, band_h, trust_ocr=trust_ocr)
        if g["kind"] == "cjk" and not cut and len([x for x in glyphs if x.get("char")]) >= 2:
            cut = True
            glyphs.append(g)
            break
        glyphs.append(g)
    chars = "".join(g.get("char", "") for g in glyphs)
    parsed = E.parse(chars)
    confs = [g.get("conf", 0.0) for g in glyphs if g.get("kind") in ("digit", "operator")]
    conf = float(np.mean(confs)) if confs else 0.0
    if cut:
        conf = min(100.0, conf + 4.0)
    return {
        "route": label,
        "chars": chars,
        "glyphs": glyphs,
        "parsed": parsed,
        "confidence": round(conf, 1),
        "cjk_cut": cut,
    }


def _solve_one(prep: dict, trust_ocr: bool, extra_debug: bool = False) -> dict:
    band = prep["band"]
    keep = prep["keep"]
    band_h = prep["band_height"]
    blocks = glyph_blocks(keep, band, band_h=band_h)
    unit = max(8.0, 0.62 * band_h)
    attempts: List[dict] = []
    for label, cand in (
        ("columns", segment_columns(keep, band, band_h, unit)),
        ("blocks", split_wide(blocks, keep, unit)),
        ("raw_blocks", blocks),
        ("tight", split_wide(blocks, keep, unit * 0.82)),
    ):
        if not cand:
            continue
        res = _attempt(cand, prep, trust_ocr, label)
        if res:
            attempts.append(res)
    ok = [a for a in attempts if a["parsed"]]
    best = max(ok, key=lambda a: a["confidence"]) if ok else (attempts[0] if attempts else None)
    out: dict = {
        "kind": "arithmetic",
        "engine": "multi_operator",
        "text": best["chars"] if best else "",
        "expr": best["parsed"]["expr"] if best and best["parsed"] else None,
        "answer": best["parsed"]["answer"] if best and best["parsed"] else None,
        "confidence": best["confidence"] if best else 0.0,
        "band": [int(band[0]), int(band[1])],
        "threshold": prep["thr"],
        "slant_ratio": prep["slant_ratio"],
        "block_count": len(blocks),
        "routes": [
            {"route": a["route"], "chars": a["chars"], "conf": a["confidence"], "parsed": bool(a["parsed"])}
            for a in attempts
        ],
    }
    if best:
        out["glyphs"] = best["glyphs"]
    if extra_debug:
        out["debug"] = {
            "otsu": prep["otsu"],
            "blocks": [{"box": [c.x0, c.y0, c.width, c.height], "area": c.area} for c in blocks],
        }
    return out


def solve(img: Image.Image, trust_ocr: bool = True, debug: bool = False) -> dict:
    """Read an `a op b op c ...` captcha and evaluate it with real precedence."""
    variants = prepare_variants(img)
    results = [_solve_one(p, trust_ocr, extra_debug=debug) for p in variants]
    parsed = [r for r in results if r["answer"] is not None]
    if parsed:
        best = max(parsed, key=lambda r: r["confidence"])
        best["thresholds_tried"] = [r["threshold"] for r in results]
        return best
    # nothing parsed: report the most confident read so the caller can see it
    best = max(results, key=lambda r: r["confidence"])
    best["note"] = "expression not parseable on any threshold — inspect glyphs"
    best["thresholds_tried"] = [r["threshold"] for r in results]
    best["variants"] = [
        {"thr": r["threshold"], "text": r["text"], "conf": r["confidence"], "answer": r["answer"]}
        for r in results
    ]
    return best
