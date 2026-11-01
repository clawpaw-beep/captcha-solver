"""Numpy-only image primitives for captcha pre-processing.

No OpenCV / scipy dependency: everything here works with numpy + Pillow so the
solver can run inside any bundled Python runtime.
"""
from __future__ import annotations

import base64
import io
import urllib.request
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageOps

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------- loading ----
def load_image(
    path: str | None = None,
    b64: str | None = None,
    url: str | None = None,
    raw: bytes | None = None,
    timeout: float = 15.0,
    keep_alpha: bool = False,
) -> Image.Image:
    """Load a captcha image from a path, base64 payload, raw bytes or URL."""
    if path:
        img = Image.open(path)
    elif b64:
        if "," in b64 and b64.strip().lower().startswith("data:"):
            b64 = b64.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
    elif raw is not None:
        img = Image.open(io.BytesIO(raw))
    elif url:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            img = Image.open(io.BytesIO(resp.read()))
    else:
        raise ValueError("one of path / b64 / url / raw is required")
    if keep_alpha:
        # slider pieces carry their outline in the alpha channel; flattening
        # them onto white would destroy the only usable signal
        return img if img.mode == "RGBA" else img.convert("RGBA")
    return normalize_mode(img)


def normalize_mode(img: Image.Image) -> Image.Image:
    """Flatten palette / alpha images onto white, keep RGB otherwise."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def to_gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def to_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"), dtype=np.float32)


def to_pil(arr: np.ndarray) -> Image.Image:
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    return Image.fromarray(arr, mode="RGB")


# ------------------------------------------------------------ enhancement ----
def otsu(gray: np.ndarray) -> float:
    hist = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    idx = np.arange(256, dtype=np.float64)
    w_bg = np.cumsum(hist)
    w_fg = total - w_bg
    sum_all = float(np.dot(idx, hist))
    cum = np.cumsum(hist * idx)
    with np.errstate(divide="ignore", invalid="ignore"):
        m_bg = np.where(w_bg > 0, cum / np.maximum(w_bg, 1), 0.0)
        m_fg = np.where(w_fg > 0, (sum_all - cum) / np.maximum(w_fg, 1), 0.0)
    between = w_bg * w_fg * (m_bg - m_fg) ** 2
    return float(np.argmax(between))


def binarize(gray: np.ndarray, mode: str = "otsu", invert_hint: bool | None = None) -> np.ndarray:
    """Return a bool array where True = ink.

    mode: otsu | adaptive | fixed
    """
    g = gray.astype(np.float32)
    if mode == "fixed":
        thr = 128.0
    elif mode == "adaptive":
        local = box_blur(g, 31)
        thr_map = local - 8.0
        ink = g < thr_map
        return _orient_ink(ink, g, invert_hint)
    else:
        thr = otsu(g)
    ink = g < thr
    return _orient_ink(ink, g, invert_hint)


def _orient_ink(ink: np.ndarray, gray: np.ndarray, invert_hint: bool | None) -> np.ndarray:
    """Ensure the minority class is ink (captcha text is usually the sparser class)."""
    ratio = ink.mean()
    if invert_hint is True:
        return ~ink if ratio > 0.5 else ink
    if invert_hint is False:
        return ink
    # captcha glyph coverage normally sits between 2% and 45%
    if ratio > 0.5 or ratio < 0.015:
        return ~ink
    return ink


def box_blur(gray: np.ndarray, k: int = 3) -> np.ndarray:
    """Separable box blur implemented with cumulative sums."""
    k = max(1, int(k) | 1)
    r = k // 2
    p = np.pad(gray.astype(np.float64), r, mode="edge")
    cs = np.cumsum(p, axis=0)
    cs = np.vstack([np.zeros((1, p.shape[1])), cs])
    v = (cs[k:, :] - cs[:-k, :]) / k
    cs2 = np.cumsum(v, axis=1)
    cs2 = np.hstack([np.zeros((v.shape[0], 1)), cs2])
    out = (cs2[:, k:] - cs2[:, :-k]) / k
    return out.astype(np.float32)


def median3(ink: np.ndarray) -> np.ndarray:
    """3x3 median on a bool mask (despeckle)."""
    g = ink.astype(np.uint8)
    p = np.pad(g, 1, mode="edge")
    h, w = g.shape
    stack = np.stack(
        [p[dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3)], axis=0
    )
    return np.median(stack, axis=0) > 0.5


def _run_extreme(mask: np.ndarray, k: int, axis: int, kind: str) -> np.ndarray:
    """1-D erode/dilate along an axis with a k-length structuring element."""
    k = max(1, int(k))
    if k == 1:
        return mask.copy()
    m = mask.astype(np.uint8)
    if axis == 1:
        p = np.pad(m, ((0, 0), (k // 2, k - 1 - k // 2)), mode="constant")
        wins = np.stack([p[:, i : i + m.shape[1]] for i in range(k)], axis=0)
    else:
        p = np.pad(m, ((k // 2, k - 1 - k // 2), (0, 0)), mode="constant")
        wins = np.stack([p[i : i + m.shape[0], :] for i in range(k)], axis=0)
    out = wins.max(axis=0) if kind == "dilate" else wins.min(axis=0)
    return out.astype(bool)


def erode2(mask: np.ndarray) -> np.ndarray:
    """2x2 erosion: kills 1px-thick strokes (interference lines, noise)."""
    m = mask.astype(np.uint8)
    h, w = m.shape
    if h < 2 or w < 2:
        return mask.copy()
    out = np.zeros_like(m)
    out[:-1, :-1] = m[:-1, :-1] & m[1:, :-1] & m[:-1, 1:] & m[1:, 1:]
    return out.astype(bool)


def dilate2(mask: np.ndarray) -> np.ndarray:
    """2x2 dilation, the dual of :func:`erode2`."""
    m = mask.astype(np.uint8)
    h, w = m.shape
    out = m.copy()
    out[1:, :] |= m[:-1, :]
    out[:, 1:] |= m[:, :-1]
    out[1:, 1:] |= m[:-1, :-1]
    return out.astype(bool)


def remove_lines(ink: np.ndarray, min_len_ratio: float = 6.0, keep_glyphs: bool = True) -> np.ndarray:
    """Strip long horizontal / vertical strokes (interference lines)."""
    h, w = ink.shape
    kh = max(3, int(w / min_len_ratio) | 1)
    kv = max(3, int(h / min_len_ratio) | 1)
    hor = _run_extreme(_run_extreme(ink, kh, 1, "erode"), kh, 1, "dilate")
    ver = _run_extreme(_run_extreme(ink, kv, 0, "erode"), kv, 0, "dilate")
    lines = hor | ver
    if not keep_glyphs:
        return ink & ~lines
    # only remove line pixels that are not dense glyph cores
    dense = _run_extreme(_run_extreme(ink, 3, 0, "erode"), 3, 1, "erode")
    return ink & ~(lines & ~dense)


def deskew_angle(ink: np.ndarray, limit: float = 12.0, step: float = 1.0) -> float:
    """Estimate the rotation that maximises horizontal projection variance."""
    if ink.mean() < 0.005:
        return 0.0
    img = to_pil(np.where(ink, 0, 255).astype(np.uint8))
    best, best_score = 0.0, -1.0
    angles = np.arange(-limit, limit + 1e-6, step)
    for ang in angles:
        rot = np.asarray(img.rotate(ang, resample=Image.BILINEAR, fillcolor=255), dtype=np.float32)
        rows = (rot < 128).sum(axis=1).astype(np.float64)
        score = float(np.var(rows))
        if score > best_score:
            best, best_score = float(ang), score
    if step < 1.0:
        return best
    coarse = best
    for ang in np.arange(coarse - step, coarse + step + 1e-6, 0.25):
        rot = np.asarray(img.rotate(float(ang), resample=Image.BILINEAR, fillcolor=255), dtype=np.float32)
        rows = (rot < 128).sum(axis=1).astype(np.float64)
        score = float(np.var(rows))
        if score > best_score:
            best, best_score = float(ang), score
    return best


def rotate_pil(img: Image.Image, angle: float) -> Image.Image:
    if abs(angle) < 0.05:
        return img
    fill = 255 if img.mode == "L" else (255, 255, 255)
    return img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)


def trim(img: Image.Image, pad: int = 6, ink_thr: int = 200) -> Image.Image:
    """Crop surrounding blank margin, then re-pad with white."""
    g = to_gray(img)
    mask = g < ink_thr
    if not mask.any():
        return img
    ys, xs = np.where(mask)
    y0, y1 = max(0, ys.min() - pad), min(g.shape[0], ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(g.shape[1], xs.max() + 1 + pad)
    return img.crop((x0, y0, x1, y1))


def upscale(img: Image.Image, factor: float) -> Image.Image:
    if factor <= 1.01:
        return img
    w, h = img.size
    return img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)


def sharpen(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=170, threshold=2))


def denoise_gray(gray: np.ndarray, strength: int = 1) -> np.ndarray:
    out = gray
    for _ in range(max(1, strength)):
        out = np.asarray(
            to_pil(out).filter(ImageFilter.MedianFilter(3)), dtype=np.float32
        )
    return out


# ------------------------------------------------------------- components ----
@dataclass
class Component:
    x0: int
    y0: int
    x1: int
    y1: int
    area: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def as_dict(self, shape: Tuple[int, int] | None = None) -> dict:
        d = {
            "box": [self.x0, self.y0, self.width, self.height],
            "center": [round(self.cx, 1), round(self.cy, 1)],
            "area": self.area,
        }
        if shape:
            h, w = shape
            d["center_norm"] = [round(self.cx / max(w - 1, 1), 4), round(self.cy / max(h - 1, 1), 4)]
        return d


def components(ink: np.ndarray, min_area: int = 8) -> List[Component]:
    """8-connected component labelling via run-length union-find."""
    h, w = ink.shape
    parent: List[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    runs: List[Tuple[int, int, int, int]] = []  # (y, x0, x1, label)
    prev: List[Tuple[int, int, int]] = []
    for y in range(h):
        row = ink[y]
        if not row.any():
            prev = []
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        cur: List[Tuple[int, int, int]] = []
        for s, e in zip(idx[0::2], idx[1::2]):
            lbl = len(parent)
            parent.append(lbl)
            runs.append((y, int(s), int(e) - 1, lbl))
            for (py, ps, pe, plbl) in prev:
                if y - py <= 1 and not (pe < s - 1 or ps > e):
                    union(lbl, plbl)
            cur.append((y, int(s), int(e) - 1, lbl))
        prev = cur

    stats: dict[int, list] = {}
    for (y, s, e, lbl) in runs:
        r = find(lbl)
        s0 = stats.get(r)
        if s0 is None:
            stats[r] = [s, y, e, y, e - s + 1]
        else:
            s0[0] = min(s0[0], s)
            s0[1] = min(s0[1], y)
            s0[2] = max(s0[2], e)
            s0[3] = max(s0[3], y)
            s0[4] += e - s + 1

    out = [
        Component(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4]))
        for v in stats.values()
        if v[4] >= min_area
    ]
    out.sort(key=lambda c: c.x0)
    return out


def crop_ink(img: Image.Image, comp: Component, pad: int = 3) -> Image.Image:
    w, h = img.size
    return img.crop(
        (
            max(0, comp.x0 - pad),
            max(0, comp.y0 - pad),
            min(w, comp.x1 + 1 + pad),
            min(h, comp.y1 + 1 + pad),
        )
    )


# ------------------------------------------------------------ projections ----
def profile_edges(gray: np.ndarray, axis: int = 1) -> np.ndarray:
    """Column (axis=1) or row (axis=0) edge-energy profile."""
    dx = np.abs(np.diff(gray.astype(np.float32), axis=1)).sum(axis=0)
    dy = np.abs(np.diff(gray.astype(np.float32), axis=0)).sum(axis=1)
    return dx if axis == 1 else dy


def ncc_track(template: np.ndarray, signal: np.ndarray) -> Tuple[int, float, np.ndarray]:
    """Sliding zero-mean normalised cross-correlation of two 1-D signals.

    Returns (best_offset, best_score, score_array) where score_array[i] is the
    correlation of template against signal[i:i+len(template)].
    """
    t = np.asarray(template, dtype=np.float64).ravel()
    s = np.asarray(signal, dtype=np.float64).ravel()
    n = t.size
    if n == 0 or s.size < n:
        return 0, 0.0, np.zeros(0)
    t0 = t - t.mean()
    tn = float(np.sqrt((t0 ** 2).sum()))
    if tn <= 1e-9:
        return 0, 0.0, np.zeros(s.size - n + 1)
    c = np.correlate(s, t0, mode="valid")
    cs = np.concatenate(([0.0], np.cumsum(s)))
    cs2 = np.concatenate(([0.0], np.cumsum(s * s)))
    win_sum = cs[n:] - cs[:-n]
    win_sq = cs2[n:] - cs2[:-n]
    var = win_sq - (win_sum ** 2) / n
    denom = np.sqrt(np.maximum(var, 1e-9)) * tn
    score = c / denom
    i = int(np.argmax(score))
    return i, float(score[i]), score


def smooth1d(x: np.ndarray, k: int = 3) -> np.ndarray:
    k = max(1, int(k) | 1)
    if k == 1:
        return x
    pad = k // 2
    p = np.pad(x.astype(np.float64), pad, mode="edge")
    ker = np.ones(k) / k
    return np.convolve(p, ker, mode="valid")


def match_template_2d(image: np.ndarray, template: np.ndarray) -> Tuple[int, int, float, np.ndarray]:
    """2-D normalised cross-correlation by FFT (numpy only).

    Returns (best_x, best_y, best_score, score_map) where score_map has shape
    (H - h + 1, W - w + 1). This is the discriminative matcher used for jigsaw
    sliders: a 1-D edge profile is too weak for small pieces.
    """
    img = np.asarray(image, dtype=np.float64)
    tpl = np.asarray(template, dtype=np.float64)
    H, W = img.shape
    h, w = tpl.shape
    if h >= H or w >= W:
        return 0, 0, -1.0, np.zeros((0, 0))
    t0 = tpl - tpl.mean()
    tn = float(np.sqrt((t0 ** 2).sum()))
    if tn <= 1e-9:
        return 0, 0, -1.0, np.zeros((0, 0))

    fh, fw = H, W
    num = np.fft.irfft2(np.fft.rfft2(img, (fh, fw)) * np.conj(np.fft.rfft2(t0, (fh, fw))), (fh, fw))
    # valid-position sums via integral images
    valid = num[: H - h + 1, : W - w + 1]

    def integral(a: np.ndarray) -> np.ndarray:
        c = np.cumsum(np.cumsum(a, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)), mode="constant")
        return c

    ii = integral(img)
    ii2 = integral(img * img)
    win_sum = ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]
    win_sq = ii2[h:, w:] - ii2[:-h, w:] - ii2[h:, :-w] + ii2[:-h, :-w]
    n = float(h * w)
    var = win_sq - (win_sum ** 2) / n
    denom = np.sqrt(np.maximum(var, 1e-9)) * tn
    score = valid[: win_sum.shape[0], : win_sum.shape[1]] / denom
    idx = int(np.argmax(score))
    by, bx = divmod(idx, score.shape[1])
    return int(bx), int(by), float(score[by, bx]), score


def edge_map(img: np.ndarray) -> np.ndarray:
    """Simple gradient magnitude image (Sobel-free, numpy only)."""
    g = img.astype(np.float32)
    dx = np.zeros_like(g)
    dy = np.zeros_like(g)
    dx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    dy[1:-1, :] = g[2:, :] - g[:-2, :]
    return np.sqrt(dx ** 2 + dy ** 2)


def overlay_points(img: Image.Image, points: Iterable[Sequence[float]], radius: int = 8, color=(255, 0, 0)) -> Image.Image:
    from PIL import ImageDraw

    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for i, p in enumerate(points):
        x, y = float(p[0]), float(p[1])
        d.ellipse([x - radius, y - radius, x + radius, y + radius], outline=color, width=2)
        d.text((x + radius + 2, y - radius), str(i + 1), fill=color)
    return out


def overlay_box(img: Image.Image, box: Sequence[int], color=(0, 200, 0)) -> Image.Image:
    from PIL import ImageDraw

    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    x, y, w, h = box
    d.rectangle([x, y, x + w, y + h], outline=color, width=2)
    return out


# ------------------------------------------------- morph filtering blocks ----
def erode3(mask: np.ndarray) -> np.ndarray:
    """3x3 erosion — keeps strokes at least 2px thick in both directions."""
    return _run_extreme(_run_extreme(mask, 3, 0, "erode"), 3, 1, "erode")


def reconstruct(marker: np.ndarray, mask: np.ndarray, iters: int = 96) -> np.ndarray:
    """Morphological reconstruction: grow `marker` inside `mask` until stable.

    The thick core of every glyph (marker) regrows its thin strokes and serifs
    through the mask, while 1-2px interference lines and speckle — which have no
    thick core — simply never appear. It beats an opening because an opening
    would clip exactly the thin strokes we want to keep.
    """
    cur = marker & mask
    for _ in range(max(1, iters)):
        nxt = dilate2(cur) & mask
        if np.array_equal(nxt, cur):
            break
        cur = nxt
    return cur


def drop_small_components(mask: np.ndarray, min_area: int = 40, min_extent: int = 9) -> np.ndarray:
    """Keep only components big enough to be glyph parts (kills salt/pepper)."""
    out = np.zeros_like(mask)
    for c in components(mask, min_area=max(4, min_area // 3)):
        if c.area >= min_area and max(c.width, c.height) >= min_extent:
            out[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1] |= mask[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
    return out


def line_masks(ink: np.ndarray, min_len: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """Long horizontal / vertical strokes (1-3px wide), as two boolean masks."""
    k = max(3, int(min_len) | 1)
    hor = _run_extreme(_run_extreme(ink, k, 1, "erode"), k, 1, "dilate")
    ver = _run_extreme(_run_extreme(ink, k, 0, "erode"), k, 0, "dilate")
    # a bar inset of the glyph body: keep only pixels whose extent is line-like
    hor &= ~_run_extreme(_run_extreme(ink, 5, 0, "erode"), 5, 0, "dilate")
    ver &= ~_run_extreme(_run_extreme(ink, 5, 1, "erode"), 5, 1, "dilate")
    return hor, ver


def slant_mask(
    ink: np.ndarray,
    angles: Iterable[float] | None = None,
    min_len: int = 14,
) -> np.ndarray:
    """Detect long straight strokes at arbitrary angles (captcha scribbles).

    The image is rotated by -θ, eroded+dilated along the horizontal axis with a
    `min_len` structuring element (which only keeps runs that long), then
    rotated back and accumulated. Glyph strokes are shorter than `min_len` in
    every direction, so only the drawn lines survive.
    """
    if angles is None:
        angles = [a for a in range(-80, 81, 5) if abs(a) >= 10]
    h, w = ink.shape
    acc = np.zeros_like(ink)
    base = to_pil(np.where(ink, 0, 255))
    for deg in angles:
        rot = rotate_pil(base, float(deg))
        arr = np.asarray(rot, dtype=np.uint8) < 128
        if arr.shape[0] < min_len or arr.shape[1] < min_len:
            continue
        sr = _run_extreme(_run_extreme(arr, int(min_len) | 1, 1, "erode"), int(min_len) | 1, 1, "dilate")
        if not sr.any():
            continue
        back = rotate_pil(to_pil(np.where(sr, 0, 255)), -float(deg)).resize((w, h), Image.BILINEAR)
        acc |= np.asarray(back, dtype=np.uint8) < 128
    return acc


def row_band(ink: np.ndarray, min_height: int = 8) -> tuple[int, int]:
    """Vertical extent of the text line: the densest run of rows."""
    rows = ink.sum(axis=1).astype(np.float64)
    if rows.max() <= 0:
        return 0, ink.shape[0] - 1
    idx = np.flatnonzero(rows >= max(2.0, 0.22 * rows.max()))
    if idx.size == 0:
        return 0, ink.shape[0] - 1
    groups: List[List[int]] = []
    for y in idx:
        if groups and y - groups[-1][-1] <= 2:
            groups[-1].append(int(y))
        else:
            groups.append([int(y)])
    best = max(groups, key=lambda g: (g[-1] - g[0] + 1, int(rows[g[0] : g[-1] + 1].sum())))
    y0, y1 = best[0], best[-1]
    if y1 - y0 + 1 < min_height:
        pad = (min_height - (y1 - y0 + 1)) // 2 + 1
        y0, y1 = max(0, y0 - pad), min(ink.shape[0] - 1, y1 + pad)
    return y0, y1


def column_runs(
    ink: np.ndarray,
    thr: float,
    smooth: int = 3,
    min_width: int = 2,
    merge_gap: int = 1,
) -> List[tuple[int, int]]:
    """Column-projection segmentation: contiguous runs above a density threshold."""
    cols = smooth1d(ink.sum(axis=0).astype(np.float64), smooth)
    segs: List[List[int]] = []
    for x in np.flatnonzero(cols >= thr):
        x = int(x)
        if segs and x - segs[-1][1] <= merge_gap:
            segs[-1][1] = x
        else:
            segs.append([x, x])
    return [(a, b) for a, b in segs if b - a + 1 >= min_width]
