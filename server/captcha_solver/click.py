"""Click-order captcha: locate glyphs, read each, then order them by the prompt."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import imageops as I
from . import ocr

CJK = re.compile(r"[\u4e00-\u9fff]")

CJK_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]
_PROMPT_BANKS: dict = {}


def _prompt_bank(chars: str):
    """Glyph bank built from the prompt characters themselves."""
    from . import template

    key = chars
    if key in _PROMPT_BANKS:
        return _PROMPT_BANKS[key]
    fonts = [p for p in CJK_FONTS if __import__("os").path.exists(p)]
    try:
        bank = template.build_bank(chars, fonts=fonts, sizes=(24, 30, 36, 44, 52), angles=(-8.0, 0.0, 8.0))
    except Exception:
        bank = None
    _PROMPT_BANKS[key] = bank
    return bank


def _template_glyph(img, comp, chars: str):
    """Template-match one glyph against the prompt alphabet."""
    from . import template

    bank = _prompt_bank(chars)
    if bank is None:
        return ("", 0.0)
    patch = I.crop_ink(img.convert("L"), comp, pad=2)
    vec = template._norm_glyph(patch)
    if vec is None:
        return ("", 0.0)
    top = bank.topk(vec, k=2, allowed=set(chars))
    if not top:
        return ("", 0.0)
    ch, sim = top[0]
    margin = sim - (top[1][1] if len(top) > 1 else 0.0)
    conf = max(0.0, min(1.0, (sim - 0.30) / 0.62)) * 100.0 * (0.85 + 0.15 * min(1.0, margin * 6))
    return (ch, conf)


def merge_boxes(comps: Sequence[I.Component], gray_shape: Tuple[int, int]) -> List[I.Component]:
    """Glue vertically stacked fragments (dots, split strokes) into one glyph box."""
    boxes = [I.Component(c.x0, c.y0, c.x1, c.y1, c.area) for c in comps]
    changed = True
    while changed:
        changed = False
        out: List[I.Component] = []
        used = [False] * len(boxes)
        for i, a in enumerate(boxes):
            if used[i]:
                continue
            cur = a
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                b = boxes[j]
                hx = max(2, int(0.35 * min(cur.width, b.width)))
                near = not (b.x0 > cur.x1 + hx or cur.x0 > b.x1 + hx)
                vert = not (b.y0 > cur.y1 + 2 or cur.y0 > b.y1 + 2)
                if near and vert:
                    nx0, ny0 = min(cur.x0, b.x0), min(cur.y0, b.y0)
                    nx1, ny1 = max(cur.x1, b.x1), max(cur.y1, b.y1)
                    cur = I.Component(nx0, ny0, nx1, ny1, cur.area + b.area)
                    used[j] = True
                    changed = True
            used[i] = True
            out.append(cur)
        boxes = out
    boxes.sort(key=lambda c: c.x0)
    return boxes


def _detector_candidates(img: Image.Image) -> List[I.Component]:
    """Glyph boxes from the neural detector (much better than projection on busy art)."""
    from . import engine

    boxes = engine.detect_boxes(img)
    comps: List[I.Component] = []
    for (x0, y0, x1, y1) in boxes:
        if x1 <= x0 or y1 <= y0:
            continue
        comps.append(I.Component(x0, y0, x1 - 1, y1 - 1, (x1 - x0) * (y1 - y0)))
    return comps


def _glyph_candidates(img: Image.Image) -> Tuple[List[I.Component], np.ndarray]:
    """Detected glyph boxes: neural detector first, projection fallback."""
    det = _detector_candidates(img)
    if len(det) >= 2:
        det.sort(key=lambda c: c.x0)
        return det, np.zeros((0, 0), dtype=bool)
    gray = I.to_gray(img)
    ink = I.binarize(I.median3(I.binarize(gray, "otsu")), "otsu")
    ink = I.remove_lines(ink)
    comps = I.components(ink, min_area=max(18, int(ink.shape[0] * ink.shape[1] * 0.0012)))
    comps = merge_boxes(comps, ink.shape)
    if not comps:
        return [], ink
    hs = np.array([c.height for c in comps], dtype=np.float64)
    med = float(np.median(hs))
    keep = [
        c
        for c in comps
        if c.height >= med * 0.55
        and c.width >= med * 0.18
        and c.area >= 0.10 * (med ** 2)
        and c.height <= ink.shape[0] * 0.95
    ]
    keep.sort(key=lambda c: c.x0)
    return keep, ink


def _ocr_glyph(img: Image.Image, comp: I.Component, charset: str) -> Tuple[str, float]:
    patch = I.crop_ink(img.convert("L"), comp, pad=3)
    if patch.height < 12:
        patch = I.upscale(patch, 2.5)
    else:
        patch = I.upscale(patch, max(1.0, 40.0 / patch.height))
    has_cjk = bool(CJK.search(charset))
    lang = "chi_sim+eng" if has_cjk else "eng"
    best: Tuple[str, float] = ("", 0.0)
    for psm in (10, 8):
        r = ocr.tesseract(patch, psm=psm, whitelist=charset if not has_cjk else None, lang=lang)
        t = ocr.clean(r.text)
        if has_cjk:
            t = "".join(ch for ch in t if CJK.match(ch) or ch in charset)
        t = t[:1]
        if t and r.conf > best[1]:
            best = (t, r.conf)
    return best


def _shape_key(img: Image.Image, comp: I.Component, size: int = 24) -> np.ndarray:
    patch = I.crop_ink(img.convert("L"), comp, pad=1).resize((size, size), Image.BILINEAR)
    a = np.asarray(patch, dtype=np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    return a.ravel()


def parse_prompt(prompt: str | None) -> Optional[str]:
    """Extract the ordered characters a click captcha asks for."""
    if not prompt:
        return None
    cleaned = re.sub(r"(请|依次|点击|选择|下列|图片中|文字|字符|顺序|按|图中|的|：|:|,|，|。|!|！)", "", prompt)
    chars = [c for c in cleaned if CJK.match(c) or c.isalnum()]
    return "".join(chars) if chars else None


def solve_click(
    img: Image.Image,
    prompt: str | None = None,
    charset: str | None = None,
    expected: int | None = None,
    max_points: int = 6,
) -> dict:
    """Return ordered click points for a click-order captcha.

    ``prompt`` is the instruction text ("请依次点击：明 天 快 乐"); when it is
    absent only the detected glyphs and their readings are returned.
    """
    comps, ink = _glyph_candidates(img)
    wanted = parse_prompt(prompt)
    if charset is None:
        charset = wanted or "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    comps = comps[: max(4, max_points * 2)]

    reads: List[dict] = []
    for c in comps:
        ch, conf = _ocr_glyph(img, c, charset)
        src = "ocr"
        if wanted and (not ch or conf < 55):
            tch, tconf = _template_glyph(img, c, wanted)
            if tch and tconf > conf:
                ch, conf, src = tch, tconf, "template"
        reads.append({"box": [c.x0, c.y0, c.width, c.height], "char": ch, "conf": round(conf, 1), "src": src})

    order: List[int] = []
    assignment: List[dict] = []
    if wanted and reads:
        # global greedy assignment over (position, glyph) pairs: an OCR-exact hit
        # beats a template hit, and both beat shape clustering.
        tsim: dict = {}
        if wanted:
            from . import template

            bank = _prompt_bank(wanted)
            vecs = {}
            for i, r in enumerate(reads):
                if i not in vecs:
                    patch = I.crop_ink(img.convert("L"), comps[i], pad=2)
                    vecs[i] = template._norm_glyph(patch)
            if bank is not None:
                for pi, want in enumerate(wanted):
                    for i in range(len(reads)):
                        vec = vecs.get(i)
                        if vec is None:
                            continue
                        top = bank.topk(vec, k=len(set(wanted)) + 1, allowed=set(wanted))
                        for ch, sim in top:
                            if ch == want:
                                tsim[(pi, i)] = max(0.0, min(1.0, (sim - 0.32) / 0.58))
                                break
        pairs = []
        for pi, want in enumerate(wanted):
            for i, r in enumerate(reads):
                score = 0.0
                how = ""
                if r["char"] == want and r["conf"] > 0:
                    score = 60.0 + r["conf"]
                    how = "ocr_exact"
                t = tsim.get((pi, i), 0.0) * 100.0
                if t > score:
                    score, how = t, "template"
                if score > 0:
                    pairs.append((score, pi, i, how))
        pairs.sort(key=lambda x: -x[0])
        used_p, used_i = set(), set()
        for score, pi, i, how in pairs:
            if pi in used_p or i in used_i:
                continue
            used_p.add(pi)
            used_i.add(i)
            order.append(i)
            assignment.append(
                {"pos": pi, "char": wanted[pi], "index": i, "how": how, "score": round(score, 1)}
            )

        # every requested position needs a point: fill the leftovers by the best
        # remaining template similarity rather than dropping a click
        missing = [p for p in range(len(wanted)) if p not in used_p]
        free = [i for i in range(len(reads)) if i not in used_i]
        while missing and free:
            best = None
            for pi in missing:
                for i in free:
                    sc = tsim.get((pi, i), 0.0)
                    if best is None or sc > best[0]:
                        best = (sc, pi, i)
            if best is None:
                break
            sc, pi, i = best
            missing.remove(pi)
            free.remove(i)
            used_p.add(pi)
            used_i.add(i)
            order.append(i)
            assignment.append(
                {"pos": pi, "char": wanted[pi], "index": i, "how": "forced_fill", "score": round(sc * 100, 1)}
            )

    assignment.sort(key=lambda a: a["pos"])
    points = []
    for a in assignment:
        c = comps[a["index"]]
        points.append(
            {
                "seq": a["pos"] + 1,
                "char": a["char"],
                "x": int(round(c.cx)),
                "y": int(round(c.cy)),
                "box": [c.x0, c.y0, c.width, c.height],
                "how": a["how"],
            }
        )

    conf = 0.0
    if points:
        matched = [r["conf"] for r in reads if r["char"]]
        char_conf = float(np.mean(matched)) if matched else 40.0
        how_bonus = 1.0 if all(p["how"] == "ocr_exact" for p in points) else 0.72
        # a reading that also matched the prompt alphabet is worth more than shape clustering
        srcs = {r["src"] for r in reads if r["char"]}
        if "template" in srcs:
            how_bonus *= 0.95
        conf = min(100.0, char_conf * how_bonus)

    return {
        "kind": "click",
        "prompt": prompt,
        "wanted": wanted,
        "points": points,
        "points_xy": [[p["x"], p["y"]] for p in points],
        "glyphs": reads,
        "confidence": round(conf, 1),
        "note": None if points else "no ordered points resolved — pass prompt='<the on-screen instruction>'",
    }
