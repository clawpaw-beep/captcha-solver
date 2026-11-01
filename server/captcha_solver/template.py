"""Font-template recognizer: the offline second opinion for glyph OCR.

Captchas rendered with (or close to) a system font are often read far more
reliably by normalised-template matching than by a general text OCR model:
the bank is synthesised from real font files, so it knows the exact shapes.

The bank is cached to `out/glyph_bank_<hash>.npz` and rebuilt only when the
charset / font set / geometry changes.
"""
from __future__ import annotations

import glob
import hashlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import imageops as I

FONT_DIRS = [
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts/truetype",
    "C:\\Windows\\Fonts",
]
DEFAULT_FONT_NAMES = [
    "Arial.ttf",
    "Arial Bold.ttf",
    "Courier New.ttf",
    "Georgia.ttf",
    "Times New Roman.ttf",
    "Trebuchet MS.ttf",
    "Verdana.ttf",
    "Arial Unicode.ttf",
    "Helvetica.ttc",
    "Tahoma.ttf",
    "Verdana Bold.ttf",
]
CELL = 32
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def discover_fonts(extra: Optional[Sequence[str]] = None) -> List[str]:
    """Existing font files, preferring the common sans/serif families."""
    found: List[str] = []
    for name in DEFAULT_FONT_NAMES:
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.exists(p):
                found.append(p)
                break
    if not found:
        for d in FONT_DIRS:
            for ext in ("*.ttf", "*.otf", "*.ttc"):
                found.extend(sorted(glob.glob(os.path.join(d, ext)))[:10])
    if extra:
        found.extend([p for p in extra if os.path.exists(p)])
    # dedupe, keep order
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _norm_glyph(img: Image.Image) -> Optional[np.ndarray]:
    """Ink-crop a glyph bitmap, squash to CELL x CELL, zero-mean unit-norm."""
    g = I.to_gray(img)
    mask = g < 200
    if mask.sum() < 4:
        return None
    ys, xs = np.where(mask)
    crop = img.convert("L").crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    crop = crop.resize((CELL, CELL), Image.LANCZOS)
    v = 255.0 - np.asarray(crop, dtype=np.float32)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    if n < 1e-6:
        return None
    return (v / n).ravel()


@dataclass
class Bank:
    chars: List[str]
    vectors: np.ndarray  # (N, CELL*CELL) float32
    sources: List[str]

    def scores(self, vec: np.ndarray, chars: set) -> Dict[str, float]:
        """Best similarity per requested character (max over its bank entries)."""
        sims = self.vectors @ vec.astype(np.float32)
        out: Dict[str, float] = {}
        for ch in chars:
            idx = [i for i, c in enumerate(self.chars) if c == ch]
            if idx:
                out[ch] = float(sims[idx].max())
        return out

    def topk(self, vec: np.ndarray, k: int = 3, allowed: Optional[set] = None) -> List[Tuple[str, float]]:
        sims = self.vectors @ vec.astype(np.float32)
        order = np.argsort(-sims)
        out: List[Tuple[str, float]] = []
        for i in order:
            ch = self.chars[i]
            if allowed and ch not in allowed:
                continue
            out.append((ch, float(sims[i])))
            if len(out) >= k:
                break
        return out

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            chars=np.array(self.chars),
            vectors=self.vectors,
            sources=np.array(self.sources),
        )

    @classmethod
    def load(cls, path: str) -> "Bank":
        d = np.load(path, allow_pickle=False)
        return cls(
            chars=[str(c) for c in d["chars"]],
            vectors=d["vectors"].astype(np.float32),
            sources=[str(s) for s in d["sources"]],
        )


def _bank_key(charset: str, fonts: Sequence[str], sizes: Sequence[int], angles: Sequence[float]) -> str:
    h = hashlib.sha1()
    h.update(charset.encode())
    h.update("|".join(fonts).encode())
    h.update(",".join(map(str, sizes)).encode())
    h.update(",".join(f"{a:.1f}" for a in angles).encode())
    h.update(f"cell{CELL}".encode())
    return h.hexdigest()[:12]


def build_bank(
    charset: str,
    fonts: Optional[Sequence[str]] = None,
    sizes: Sequence[int] = (28, 34, 40, 48),
    angles: Sequence[float] = (-12.0, 0.0, 12.0),
    use_cache: bool = True,
) -> Bank:
    """Synthesise glyph vectors for every charset character across fonts/sizes."""
    fonts = list(fonts) if fonts else discover_fonts()
    key = _bank_key(charset, fonts, sizes, angles)
    cache_path = os.path.join(CACHE_DIR, f"glyph_bank_{key}.npz")
    if use_cache and os.path.exists(cache_path):
        try:
            return Bank.load(cache_path)
        except Exception:
            pass

    chars: List[str] = []
    vecs: List[np.ndarray] = []
    sources: List[str] = []
    pad = 12
    for ch in charset:
        for font_path in fonts:
            for size in sizes:
                try:
                    font = ImageFont.truetype(font_path, size)
                except Exception:
                    continue
                canvas = Image.new("L", (size * 2 + pad * 2, size * 2 + pad * 2), 255)
                d = ImageDraw.Draw(canvas)
                d.text((pad, pad), ch, font=font, fill=0)
                for ang in angles:
                    g = canvas if abs(ang) < 0.01 else canvas.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
                    v = _norm_glyph(g)
                    if v is None:
                        continue
                    chars.append(ch)
                    vecs.append(v)
                    sources.append(f"{os.path.basename(font_path)}@{size}/{ang:+.0f}")
    if not vecs:
        raise RuntimeError("glyph bank empty — no usable fonts found")
    bank = Bank(chars=chars, vectors=np.stack(vecs).astype(np.float32), sources=sources)
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            bank.save(cache_path)
        except Exception:
            pass
    return bank


# --------------------------------------------------------------- solving ----
def glyph_patches(img: Image.Image, max_glyphs: int = 8, split_touching: bool = True) -> List[I.Component]:
    """Segment a text captcha into ordered glyph boxes.

    Components are kept as-is (captcha glyphs are normally separate), tiny
    fragments are glued only when they sit inside the same glyph cell, and
    merged multi-glyph blobs are split at vertical projection valleys.
    """
    gray = I.to_gray(img)
    ink = I.remove_lines(I.median3(I.binarize(gray, "otsu")))
    comps = I.components(ink, min_area=12)
    if not comps:
        return []
    comps = _merge_fragments(comps)
    comps = _select_glyph_band(comps, max_glyphs)
    if split_touching and len(comps) < max_glyphs:
        for _ in range(3):
            new = _split_wide(ink, comps, max_glyphs)
            if len(new) == len(comps):
                break
            comps = new
    comps.sort(key=lambda c: c.x0)
    return comps[:max_glyphs]


def _select_glyph_band(comps: Sequence[I.Component], max_glyphs: int) -> List[I.Component]:
    """Keep components whose height matches the dominant glyph height."""
    if not comps:
        return []
    big = sorted(comps, key=lambda c: -c.area)[: max(3, min(len(comps), max_glyphs))]
    ref = float(np.median([c.height for c in big]))
    band = [c for c in comps if ref * 0.62 <= c.height <= ref * 1.45 and c.width >= max(2, ref * 0.1)]
    if not band:
        band = list(big)
    if len(band) > max_glyphs:
        band = sorted(band, key=lambda c: -c.area)[:max_glyphs]
    return band


def _merge_fragments(comps: Sequence[I.Component]) -> List[I.Component]:
    """Glue small vertically-stacked fragments (dots, accents) into one glyph."""
    boxes = [I.Component(c.x0, c.y0, c.x1, c.y1, c.area) for c in comps]
    areas = np.array([c.area for c in boxes], dtype=np.float64)
    med = float(np.median(areas)) if areas.size else 0.0
    changed = True
    while changed and len(boxes) > 1:
        changed = False
        for i in range(len(boxes)):
            a = boxes[i]
            for j in range(i + 1, len(boxes)):
                b = boxes[j]
                if a.area > med * 0.6 and b.area > med * 0.6:
                    continue  # both look like real glyphs; leave them alone
                h_overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
                v_overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
                near = (v_overlap >= -2 and h_overlap >= 0) or (h_overlap >= -2 and v_overlap >= 0)
                if not near:
                    continue
                merged = I.Component(
                    min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1), a.area + b.area
                )
                boxes = [c for k, c in enumerate(boxes) if k not in (i, j)] + [merged]
                changed = True
                break
            if changed:
                break
    boxes.sort(key=lambda c: c.x0)
    return boxes


def _split_wide(ink: np.ndarray, comps: Sequence[I.Component], max_glyphs: int) -> List[I.Component]:
    """Split merged multi-glyph blobs at deep vertical projection valleys."""
    if not comps:
        return list(comps)
    widths = np.array([c.width for c in comps], dtype=np.float64)
    med_w = float(np.median(widths))
    out: List[I.Component] = []
    for c in comps:
        if c.width < med_w * 1.55 or c.width > ink.shape[1] * 0.9:
            out.append(c)
            continue
        sub = ink[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
        cols = sub.sum(axis=0).astype(np.float64)
        if cols.size < 10:
            out.append(c)
            continue
        k = max(3, c.width // 4)
        cols_s = I.smooth1d(cols, k)
        thr = cols_s.mean() * 0.35
        # candidate cut positions near the middle third
        lo, hi = int(c.width * 0.33), int(c.width * 0.67)
        if hi <= lo:
            out.append(c)
            continue
        cut = lo + int(np.argmin(cols_s[lo:hi]))
        if cols_s[cut] > thr:
            out.append(c)
            continue
        left = I.Component(c.x0, c.y0, c.x0 + cut - 1, c.y1, c.area // 2)
        right = I.Component(c.x0 + cut, c.y0, c.x1, c.y1, c.area // 2)
        out.extend([left, right])
    return out


def adjudicate(
    img: Image.Image,
    candidates: Sequence[str],
    charset: str,
    bank: Optional[Bank] = None,
) -> Optional[dict]:
    """Settle positions where whole-string candidates disagree, glyph by glyph.

    A flip needs three things: the glyph at that index must be isolated, the
    alternative must already be proposed by another candidate, and the bank must
    prefer it by a clear margin over the character currently chosen. Anything
    weaker is left alone — a wrong "correction" costs more than an open slot.
    """
    texts = [t for t in candidates if t]
    if len(texts) < 2:
        return None
    lengths = {len(t) for t in texts}
    if len(lengths) != 1:
        return None
    n = lengths.pop()
    if n == 0 or n > 8:
        return None
    comps = glyph_patches(img, max_glyphs=n)
    if len(comps) != n:
        return None
    bank = bank or build_bank(charset)
    base = texts[0]
    out = list(base)
    fixed: List[dict] = []
    for p in range(n):
        votes = [t[p] for t in texts]
        if len(set(votes)) < 2:
            continue
        patch = I.crop_ink(img.convert("L"), comps[p], pad=2)
        vec = _norm_glyph(patch)
        if vec is None:
            continue
        options = set(votes)
        sims = bank.scores(vec, options)
        cur = out[p]
        best_alt = max((c for c in options if c != cur), key=lambda c: sims.get(c, -1.0), default=None)
        if best_alt is None:
            continue
        margin = sims.get(best_alt, -1.0) - sims.get(cur, -1.0)
        if margin >= 0.05 and sims.get(best_alt, 0.0) >= 0.5:
            fixed.append(
                {"pos": p, "from": cur, "to": best_alt, "margin": round(margin, 3), "sim": round(sims[best_alt], 3)}
            )
            out[p] = best_alt
    if not fixed:
        return None
    return {"text": "".join(out), "fixed": fixed}


def recognize(
    img: Image.Image,
    charset: Optional[str] = None,
    length: Optional[int] = None,
    fonts: Optional[Sequence[str]] = None,
    bank: Optional[Bank] = None,
) -> dict:
    """Template-match every segmented glyph and return the ordered string."""
    charset = charset or ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    bank = bank or build_bank(charset, fonts=fonts)
    comps = glyph_patches(img, max_glyphs=max(8, length or 5))
    if not comps:
        return {"needed": False, "text": "", "confidence": 0.0, "glyphs": []}
    chars, scores, detail = [], [], []
    allowed = set(charset)
    for c in comps:
        patch = I.crop_ink(img.convert("L"), c, pad=2)
        v = _norm_glyph(patch)
        if v is None:
            continue
        top = bank.topk(v, k=3, allowed=allowed)
        if not top:
            continue
        ch, sim = top[0]
        margin = sim - (top[1][1] if len(top) > 1 else 0.0)
        chars.append(ch)
        # similarity is cosine of zero-mean unit vectors: 0.5 is already a good match
        scores.append(max(0.0, min(1.0, (sim - 0.35) / 0.55)) * (0.6 + 0.4 * min(1.0, margin * 8)))
        detail.append({"box": [c.x0, c.y0, c.width, c.height], "char": ch, "sim": round(sim, 3), "top": top})
    text = "".join(chars)
    conf = float(np.mean(scores)) * 100.0 if scores else 0.0
    if length and len(text) != length:
        conf *= 0.6
    return {
        "text": text,
        "confidence": round(conf, 1),
        "glyph_count": len(comps),
        "glyphs": detail,
        "bank_size": len(bank.chars),
    }
