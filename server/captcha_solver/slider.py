"""Slider / jigsaw captcha: gap offset detection.

Two input shapes are supported:

  * ``background`` + ``piece``  — the classic two-image jigsaw. Edge profiles of
    the piece are matched against the background at several scales; the best
    NCC peak is the gap left edge.
  * ``composite`` alone         — a screenshot where the piece is already
    rendered on the image. The notch is located by scanning for a vertically
    uniform column run (a hole through the background) and cross-checked
    against a piece-edge match.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import imageops as I


def _piece_profile(piece: Image.Image) -> Tuple[np.ndarray, int, int]:
    """Edge column profile of the piece's opaque content, plus its bbox."""
    arr = I.to_array(piece)
    alpha = None
    if piece.mode in ("RGBA", "LA") or (piece.mode == "P" and "transparency" in piece.info):
        alpha = np.asarray(piece.convert("RGBA"))[:, :, 3]
    gray = I.to_gray(piece)
    if alpha is not None and (alpha < 250).any():
        mask = alpha > 40
    else:
        mask = gray < 245
    if not mask.any():
        mask = np.ones_like(gray, dtype=bool)
    ys, xs = np.where(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    crop = piece.crop((x0, y0, x1 + 1, y1 + 1))
    prof = I.profile_edges(I.to_gray(crop), axis=1)
    return I.smooth1d(prof, 3), crop.width, crop.height


def _match(bg_prof: np.ndarray, piece_prof: np.ndarray, lo: float = 0.6, hi: float = 1.45) -> Tuple[int, float, List[dict]]:
    """Multi-scale NCC match of a piece profile inside the background profile."""
    W = bg_prof.size
    best = (0, -2.0)
    trail: List[dict] = []
    for scale in np.arange(lo, hi + 1e-9, 0.05):
        n = int(round(piece_prof.size * scale))
        if n < 8 or n > W - 2:
            continue
        resized = np.interp(
            np.linspace(0, piece_prof.size - 1, n), np.arange(piece_prof.size), piece_prof
        )
        off, score, _ = I.ncc_track(resized, bg_prof)
        trail.append({"offset": int(off), "scale": round(float(scale), 3), "score": round(score, 4)})
        if score > best[1]:
            best = (int(off), float(score))
    trail.sort(key=lambda d: -d["score"])
    return best[0], best[1], trail[:8]


def _uniform_runs(composite: Image.Image) -> List[dict]:
    """Contiguous column runs that are much flatter than the page background."""
    gray = I.to_gray(composite)
    col_std = gray.std(axis=0)
    med = float(np.median(col_std)) + 1e-6
    flat = col_std < 0.35 * med
    W = flat.size
    runs: List[dict] = []
    i = 0
    while i < W:
        if not flat[i]:
            i += 1
            continue
        j = i
        while j + 1 < W and flat[j + 1]:
            j += 1
        width = j - i + 1
        if 0.02 * W <= width <= 0.55 * W:
            runs.append(
                {
                    "x0": int(i),
                    "x1": int(j),
                    "width": int(width),
                    "flatness": round(1.0 - float(col_std[i : j + 1].mean()) / med, 4),
                }
            )
        i = j + 1
    runs.sort(key=lambda r: -r["flatness"])
    return runs


def _flat_blocks(img: Image.Image, min_w: int = 24, max_w: int = 140) -> List[dict]:
    """Uniform rectangles inside a busy image — the cut-out a piece left behind.

    A notch is flat *locally*: its columns still carry the picture above and
    below it, so a whole-column variance test cannot find it. Local 5x5 variance
    plus a connected-component shape filter can.
    """
    gray = I.to_gray(img)
    m = I.box_blur(gray, 5)
    m2 = I.box_blur(gray * gray, 5)
    local_std = np.sqrt(np.maximum(m2 - m * m, 0.0))
    flat = local_std < 4.0
    if not flat.any():
        return []
    comps = I.components(flat, min_area=min_w * 12)
    h, w = gray.shape
    out: List[dict] = []
    for c in comps:
        if not (min_w <= c.width <= max_w and min_w * 0.5 <= c.height <= h * 0.95):
            continue
        sub = gray[c.y0 : c.y1 + 1, c.x0 : c.x1 + 1]
        out.append(
            {
                "x0": c.x0,
                "x1": c.x1,
                "y0": c.y0,
                "y1": c.y1,
                "width": c.width,
                "height": c.height,
                "std": round(float(sub.std()), 3),
                "rectangularity": round(c.width * c.height / max(1, c.area), 3),
            }
        )
    out.sort(key=lambda d: (d["std"], -d["width"] * d["height"]))
    return out


def _vertical_edge_pairs(composite: Image.Image) -> List[dict]:
    """Long straight vertical edges: the two sides of a rectangular notch."""
    gray = I.to_gray(composite)
    dx = np.abs(np.diff(gray, axis=1)).sum(axis=0)
    dx = I.smooth1d(dx, 3)
    if dx.size < 8:
        return []
    thr = float(np.percentile(dx, 97.0))
    xs = np.flatnonzero(dx >= thr)
    groups: List[List[int]] = []
    for x in xs:
        if groups and x - groups[-1][-1] <= 3:
            groups[-1].append(int(x))
        else:
            groups.append([int(x)])
    peaks = [int(np.mean(g)) for g in groups if dx[g].max() > 0]
    out: List[dict] = []
    for a, b in zip(peaks, peaks[1:]):
        w = b - a
        if 8 <= w <= gray.shape[1] * 0.6:
            out.append({"x": int(a), "width": int(w), "edge": round(float(dx[a] + dx[b]), 1)})
    out.sort(key=lambda d: -d["edge"])
    return out


def _piece_gray(piece: Image.Image) -> Tuple[np.ndarray, Optional[np.ndarray], Tuple[int, int]]:
    """Grayscale piece content, its alpha mask, and the crop origin.

    The crop origin matters: matching runs on the *trimmed* content, so the
    reported x is where the silhouette belongs, not where the piece image's own
    top-left corner goes. Vendors pad the piece with transparent margin (GeeTest
    v4 uses 11-14px), and a slider dragged to the untrimmed x lands exactly that
    far off.
    """
    gray = I.to_gray(piece)
    mask = None
    dx = dy = 0
    if piece.mode in ("RGBA", "LA") or "transparency" in piece.info:
        alpha = np.asarray(piece.convert("RGBA"))[:, :, 3]
        if (alpha < 250).any():
            mask = alpha > 40
    if mask is None:
        mask = gray < 245 if (gray < 245).any() else None
    if mask is not None:
        ys, xs = np.where(mask)
        if ys.size:
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            dx, dy = int(x0), int(y0)
            gray = gray[y0:y1, x0:x1]
            mask = mask[y0:y1, x0:x1]
    return gray, mask, (dx, dy)


def _match_2d(bg_edges: np.ndarray, piece_gray: np.ndarray, scales: Sequence[float]) -> Tuple[int, int, float, int, int, List[dict]]:
    """Multi-scale 2-D edge NCC: returns (x, y, score, width, height, trail)."""
    best = (0, 0, -2.0, 0, 0)
    trail: List[dict] = []
    h, w = piece_gray.shape
    for s in scales:
        ww, hh = int(round(w * s)), int(round(h * s))
        if ww < 12 or hh < 12 or ww >= bg_edges.shape[1] or hh >= bg_edges.shape[0]:
            continue
        scaled = np.asarray(Image.fromarray(piece_gray.astype(np.uint8)).resize((ww, hh), Image.LANCZOS), dtype=np.float32)
        x, y, score, _ = I.match_template_2d(bg_edges, I.edge_map(scaled))
        # scale prior: a piece is normally 1:1 with its background, so a
        # rescaled match must be clearly better to win
        adj = score - 0.45 * abs(float(np.log(max(s, 1e-3))))
        if x < 20:
            adj -= 0.06  # the piece sits at its start position, never the gap
        trail.append(
            {
                "x": int(x),
                "y": int(y),
                "scale": round(float(s), 3),
                "score": round(score, 4),
                "adjusted": round(adj, 4),
            }
        )
        if adj > best[2]:
            best = (int(x), int(y), float(adj), ww, hh)
    trail.sort(key=lambda d: -d["score"])
    return best[0], best[1], best[2], best[3], best[4], trail[:8]


def shape_offset(background: Image.Image, piece: Image.Image, exclude_left: int = 0) -> Optional[dict]:
    """Align the piece's *outline* with the lightened cut-out in the picture.

    Vendors like Aliyun render the gap as a semi-transparent light patch of the
    piece's exact shape, and the piece itself carries the photo content — so the
    content does not match the gap, but the silhouette does. Matching alpha
    against a "lighter than its surroundings and low-saturation" map is what
    survives textured backgrounds where content NCC locks onto a tree trunk.
    """
    try:
        import numpy as np
        from PIL import Image as PILImage

        alpha = np.asarray(piece.convert("RGBA"))[:, :, 3]
        if (alpha < 250).mean() < 0.05:
            return None
        mask = alpha > 60
        ys, xs = np.where(mask)
        if ys.size < 100:
            return None
        crop = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        gray = I.to_gray(background)
        rgb = I.to_array(background) / 255.0
        mx = rgb.max(axis=2)
        mn = rgb.min(axis=2)
        sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
        base = I.box_blur(gray, 61)
        light = ((gray > base + 8) & (sat < 0.35)).astype(np.float32)
        # Aliyun PUZZLE keeps a light copy of the piece at its resting place on
        # the left edge; without this guard the matcher locks onto its own
        # starting slot and reports gap ~= 0.
        if exclude_left > 2:
            light[:, : int(exclude_left)] = 0.0
        x, y, score, _ = I.match_template_2d(light, crop.astype(np.float32))
        # where the silhouette sits *inside* the piece image: vendors pad the
        # piece with transparent margin, so a gap-aligned x is not the same as
        # the distance the slider has to travel
        return {
            "x": int(x),
            "y": int(y),
            "score": float(score),
            "mask": (int(crop.shape[1]), int(crop.shape[0])),
            "piece_offset": (int(xs.min()), int(ys.min())),
        }
    except Exception:
        return None


def solve_slider(
    composite: Image.Image | None = None,
    background: Image.Image | None = None,
    piece: Image.Image | None = None,
    piece_width: int | None = None,
    mode: str = "auto",
    scale_hint: float = 1.0,
    handle_x: int = 0,
) -> dict:
    """Locate the gap: 2-D edge matching when a piece is supplied, notch scan otherwise."""
    if background is None and composite is None:
        raise ValueError("need composite or background image")

    result: dict = {"kind": "slider", "mode": mode}
    evidence: List[dict] = []
    shape_dx = 0  # silhouette left margin inside the piece image

    if piece is not None:
        bg_img = background if background is not None else composite
        assert bg_img is not None
        piece_gray, _mask, _piece_origin = _piece_gray(piece)
        # a real transparent margin means the trimmed-content x is not the drag
        # distance; sub-2px padding is just antialiasing noise and is ignored
        origin_dx = _piece_origin[0] if _piece_origin[0] > 2 else 0
        base_w = piece_gray.shape[1]
        lo = hi = 1.0
        if piece_width:
            base = piece_width / max(base_w, 1)
            scales = [base * k for k in (0.94, 0.97, 1.0, 1.03, 1.06)]
        else:
            c = 1.0 * scale_hint
            scales = [c * k for k in (0.8, 0.9, 1.0, 1.1, 1.2)]
        _bg_edges = I.edge_map(I.to_gray(bg_img))
        # the resting-slot guard is a fraction of the piece's own width, not the
        # transparent margin: Aliyun's piece is nearly full-width, so a 2px
        # margin would guard nothing
        rest_guard = min(piece_gray.shape[1], int(0.25 * _bg_edges.shape[1]))
        if rest_guard > 4:
            _bg_edges[:, :rest_guard] = 0.0
        x, y, score, w, h, trail = _match_2d(_bg_edges, piece_gray, scales)
        evidence.append({"source": "edge_ncc_2d", "candidates": trail})

        # optional neural cross-check
        try:
            from . import engine

            got = engine.slide_offset(piece, bg_img)
        except Exception:
            got = None
        # silhouette alignment: wins when the picture is textured (a trunk, a
        # window frame) and content NCC has to guess
        shape = shape_offset(bg_img, piece, exclude_left=locals().get('rest_guard', 0))
        if shape:
            evidence.append({"source": "shape_ncc", **{k: v for k, v in shape.items() if k != "mask"}})
            if shape["score"] > max(score, 0.0) + 0.05:
                px0, _py0 = shape.get("piece_offset", (0, 0))
                evidence.append({"source": "shape_override", "from": int(x), "to": shape["x"],
                                 "piece_offset": [int(px0), int(_py0)]})
                x, y, w, h = shape["x"], shape["y"], shape["mask"][0], shape["mask"][1]
                score = shape["score"]
                shape_dx = int(px0)

        if got:
            evidence.append({"source": "engine_slide_match", **got})
            # the neural matcher only breaks ties for very weak 2-D matches
            if abs(got["x"] - x) > max(6, w * 0.15) and got.get("confidence", 0) >= 0.9 and score < 0.18:
                evidence.append({"source": "engine_override", "from": x, "to": got["x"]})
                x = int(got["x"])

        drag = max(0, int(x) - max(shape_dx, origin_dx) - int(handle_x))
        result.update(
            {
                "offset": int(x),
                "offset_y": int(y),
                "piece_width": int(w),
                "piece_height": int(h),
                "drag_distance": drag,
                "handle_x": int(handle_x),
                "piece_shape_dx": int(max(shape_dx, origin_dx)),
                "score": round(float(score), 4),
                "gap_box": [int(x), int(y), int(w), int(h)],
                "confidence": round(min(100.0, max(0.0, (float(score) - 0.10) / 0.55 * 100.0)), 1),
            }
        )
        result["evidence"] = evidence
        result["hint"] = f"drag the piece by {drag}px (target x={int(x)}, handle starts at {int(handle_x)})"
        return result

    img = composite
    assert img is not None
    blocks = _flat_blocks(img)
    if blocks:
        best = blocks[0]
        # a 5x5 local-variance window erodes the flat region by ~2px on each side
        gap_x, width = max(0, int(best["x0"]) - 2), int(best["width"]) + 4
        conf = max(0.05, min(1.0, 1.0 - float(best["std"]) / 12.0))
        evidence.append({"source": "flat_block", "block": best, "others": blocks[1:4]})
        result.update(
            {
                "offset": gap_x,
                "piece_width": width,
                "piece_height": int(best["height"]),
                "drag_distance": max(0, gap_x - int(handle_x)),
                "score": round(conf, 4),
                "gap_box": [gap_x, int(best["y0"]), width, int(best["height"])],
                "confidence": round(min(100.0, conf * 100.0), 1),
                "evidence": evidence,
                "hint": f"drag the piece to x={gap_x}" + (f" ({gap_x - int(handle_x)}px)" if handle_x else ""),
                "note": "composite-only read: pass background+piece for a much stronger match",
            }
        )
        return result
    runs = _uniform_runs(img)
    edges = _vertical_edge_pairs(img)
    if runs:
        evidence.append({"source": "flat_run", "runs": runs[:4]})
    if edges:
        evidence.append({"source": "vertical_edges", "pairs": edges[:4]})
    if not runs and not edges:
        return {
            "kind": "slider",
            "offset": None,
            "confidence": 0.0,
            "evidence": evidence,
            "note": "no notch detected — pass background+piece for reliable matching",
        }
    best = runs[0] if runs else {"x0": edges[0]["x"], "width": edges[0]["width"], "flatness": 0.5}
    gap_x = int(best["x0"])
    width = int(best.get("width", piece_width or img.width // 8))
    bonus = 0.0
    for e in edges:
        if abs(e["x"] - gap_x) <= max(4, width * 0.15):
            bonus = 0.25
            gap_x = int(round((gap_x + e["x"]) / 2))
            width = int(round((width + e["width"]) / 2)) if e["width"] > 4 else width
            break
    conf = min(1.0, float(best.get("flatness", 0.4)) + bonus)
    result.update(
        {
            "offset": gap_x,
            "piece_width": width,
            "drag_distance": max(0, gap_x - int(handle_x)),
            "score": round(conf, 4),
            "gap_box": [gap_x, 0, width, img.height],
            "confidence": round(min(100.0, max(0.0, conf * 100.0)), 1),
        }
    )
    result["evidence"] = evidence
    result["hint"] = (
        "drag the slider to x=offset (add the handle's own x if the handle starts off 0)"
    )
    return result


def debug_overlay(composite: Image.Image, box) -> Image.Image:
    return I.overlay_box(composite, box)
