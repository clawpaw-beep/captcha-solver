"""Locate drawn operator glyphs (`+`, `*`, `-`, `=`) inside a captcha line.

On chained arithmetic captchas the numbers are set by a font but the operators
are often *drawn* — a six-point star instead of the font's `*`, a hairline cross
instead of `+`. Those shapes are far more stable than the digits they sit
between, so finding them by normalised cross-correlation gives hard anchors: the
line is cut at the operator positions and each remaining span is read as a
number by OCR, which is exactly what text models are good at.

Matching is done per symbol class at a fixed glyph height, sliding in x and
scanning a few y offsets, then non-maximum suppression keeps one hit per symbol.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw
from numpy.lib.stride_tricks import sliding_window_view

from . import imageops as I


def _star_mask(size: int, r_ratio: float = 0.44, inner: float = 0.42, points: int = 6, angle: float = 0.0) -> np.ndarray:
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    cx = cy = size / 2.0
    R = size * r_ratio
    pts = []
    for k in range(points * 2):
        a = math.pi * k / points - math.pi / 2 + math.radians(angle)
        rad = R if k % 2 == 0 else R * inner
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(pts, fill=0)
    return 255.0 - np.asarray(img, dtype=np.float32)


def _cross_mask(size: int, thick: int) -> np.ndarray:
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    half = thick // 2
    d.rectangle([0, size // 2 - half, size - 1, size // 2 - half + thick - 1], fill=0)
    d.rectangle([size // 2 - half, 0, size // 2 - half + thick - 1, size - 1], fill=0)
    return 255.0 - np.asarray(img, dtype=np.float32)


def _bar_mask(size: int, thick: int, double: bool = False) -> np.ndarray:
    h = size
    img = Image.new("L", (size, h), 255)
    d = ImageDraw.Draw(img)
    if double:
        y1 = int(h * 0.32)
        y2 = int(h * 0.66)
        d.rectangle([0, y1, size - 1, y1 + thick - 1], fill=0)
        d.rectangle([0, y2, size - 1, y2 + thick - 1], fill=0)
    else:
        d.rectangle([0, h // 2 - thick // 2, size - 1, h // 2 - thick // 2 + thick - 1], fill=0)
    return 255.0 - np.asarray(img, dtype=np.float32)


def build_templates(gh: int) -> Dict[str, List[np.ndarray]]:
    """Symbol templates sized to a glyph height `gh`.

    A cross spans about two thirds of the digit height while a drawn star fills
    almost the whole cell, so the two classes need different scale ladders —
    matching a 15px cross against a 23px window just dilutes the correlation.
    """
    out: Dict[str, List[np.ndarray]] = {"+": [], "*": [], "-": [], "=": []}
    for scale in (0.55, 0.62, 0.70, 0.78):
        s = max(6, int(round(gh * scale)))
        for thick in (1, 2, 3):
            out["+"].append(_cross_mask(s, min(thick, max(1, s // 5))))
            out["-"].append(_bar_mask(max(6, int(s * 0.95)), min(thick, 3)))
            out["="].append(_bar_mask(max(8, int(s * 0.95)), min(thick, 3), double=True))
    for scale in (0.82, 0.90, 0.98, 1.06):
        s = max(6, int(round(gh * scale)))
        for r_ratio in (0.42, 0.46, 0.50):
            for inner in (0.38, 0.44, 0.50):
                for angle in (0.0, 15.0, 30.0):
                    out["*"].append(_star_mask(s, r_ratio, inner, 6, angle))
    return out


def _zero_mean_unit(v: np.ndarray) -> np.ndarray:
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


def scan_symbol(
    ink: np.ndarray,
    band: Tuple[int, int],
    template: np.ndarray,
    y_offsets: Optional[Sequence[int]] = None,
) -> Optional[np.ndarray]:
    """NCC of one template along x for the best y offset inside the band."""
    y0, y1 = band
    h, w = template.shape
    img_f = ink.astype(np.float32)
    if w >= img_f.shape[1] or h > (y1 - y0 + 1) + 6:
        return None
    tv = _zero_mean_unit(template.ravel())
    if y_offsets is None:
        y_offsets = range(max(0, y0 - 3), min(img_f.shape[0] - h, y1 - h + 4))
    best: Optional[np.ndarray] = None
    best_y: Optional[np.ndarray] = None
    for y in y_offsets:
        if y < 0 or y + h > img_f.shape[0]:
            continue
        win = sliding_window_view(img_f[y : y + h], (h, w))[0]
        P = win.reshape(win.shape[0], -1)
        P = P - P.mean(axis=1, keepdims=True)
        nrm = np.linalg.norm(P, axis=1, keepdims=True)
        nrm[nrm < 1e-6] = 1e-6
        scores = (P @ tv) / nrm[:, 0]
        if best is None:
            best = scores
            best_y = np.full(scores.shape[0], y, dtype=np.int32)
        else:
            better = scores > best
            best = np.where(better, scores, best)
            best_y = np.where(better, y, best_y)
    if best is None:
        return None
    return best, best_y


def _confirm_shape(ink: np.ndarray, ch: str, x: int, y: int, w: int, h: int, gh: int) -> bool:
    """Reject template hits whose ink does not actually look like the symbol.

    A `4` contains a real cross, so a cross template scores ~0.77 on it; what
    gives it away is that its strokes are thick and its fill ratio is high,
    while a drawn `+` is two hairlines over mostly empty space.
    """
    y0, y1 = max(0, y), min(ink.shape[0], y + h)
    x0, x1 = max(0, x), min(ink.shape[1], x + w)
    if y1 <= y0 or x1 <= x0:
        return False
    sub = ink[y0:y1, x0:x1]
    if sub.sum() < 6:
        return False
    fill = float(sub.mean())
    if fill > 0.44:
        return False
    rows = [int(sub[i].sum()) for i in range(sub.shape[0])]
    cols = [int(sub[:, j].sum()) for j in range(sub.shape[1])]
    if ch == "+":
        span_r = max(rows) / max(sub.shape[1], 1)
        span_c = max(cols) / max(sub.shape[0], 1)
        thick_r = sum(1 for v in rows if v >= 0.7 * sub.shape[1])
        thick_c = sum(1 for v in cols if v >= 0.7 * sub.shape[0])
        if not (span_r >= 0.7 and span_c >= 0.7 and thick_r <= 4 and thick_c <= 4):
            return False
        # the crossing must sit near the middle: a `4` puts its bar low and its
        # stem to the left, a drawn `+` is centred by definition
        y_mid = rows.index(max(rows)) / max(sub.shape[0] - 1, 1)
        x_mid = cols.index(max(cols)) / max(sub.shape[1] - 1, 1)
        if not (0.30 <= y_mid <= 0.70 and 0.30 <= x_mid <= 0.70):
            return False
        # everything except the two strokes must be empty: a `4` keeps a
        # diagonal plus a closed upper triangle that a cross never has
        core = np.zeros_like(sub)
        yi = rows.index(max(rows))
        xi = cols.index(max(cols))
        core[max(0, yi - 1) : yi + 2, :] = True
        core[:, max(0, xi - 1) : xi + 2] = True
        residual = float((sub & ~core).mean())
        return residual <= 0.45 * fill
    if ch == "-":
        return max(rows) >= 0.7 * sub.shape[1] and sum(1 for v in rows if v >= 0.7 * sub.shape[1]) <= 4
    if ch == "=":
        bars = sum(1 for v in rows if v >= 0.7 * sub.shape[1])
        return 2 <= bars <= 8
    return True  # stars need no extra shape test


def find_symbols(
    ink: np.ndarray,
    band: Tuple[int, int],
    gh: int,
    min_score: float = 0.62,
    max_symbols: int = 5,
) -> List[dict]:
    """Return the best-scoring symbol hits in left-to-right order."""
    templates = build_templates(gh)
    hits: List[dict] = []
    for ch, tmpls in templates.items():
        best: Optional[Tuple[float, int, int, np.ndarray]] = None
        for t in tmpls:
            res = scan_symbol(ink, band, t)
            if res is None:
                continue
            scores, ys = res
            if not np.isfinite(scores).any():
                continue
            x = int(np.argmax(scores))
            sc = float(scores[x])
            y = int(ys[x])
            if not _confirm_shape(ink, ch, x, y, t.shape[1], t.shape[0], gh):
                continue
            if best is None or sc > best[0]:
                best = (sc, x, y, t)
        if best and best[0] >= min_score:
            hits.append({"char": ch, "x": best[1], "y": best[2], "width": best[3].shape[1],
                         "height": best[3].shape[0], "score": round(best[0], 3)})
    hits.sort(key=lambda h: h["x"])
    kept: List[dict] = []
    for h in sorted(hits, key=lambda h: -h["score"]):
        if any(abs(h["x"] - k["x"]) < max(6, gh // 2) for k in kept):
            continue
        kept.append(h)
    kept.sort(key=lambda h: h["x"])
    return kept[:max_symbols]
