"""Rotate captcha: estimate the correction angle, then read the text."""
from __future__ import annotations

from typing import List

import numpy as np
from PIL import Image

from . import imageops as I
from . import ocr, text_solver


def estimate_angle(img: Image.Image, limit: float = 60.0, coarse: float = 3.0) -> dict:
    """Find the angle whose horizontal projection variance is maximal."""
    gray = I.to_gray(img)
    ink = I.binarize(gray, "otsu")
    if not ink.any():
        return {"angle": 0.0, "score": 0.0, "scores": []}
    canvas = I.to_pil(np.where(ink, 0, 255))
    scores: List[dict] = []
    best_ang, best_score = 0.0, -1.0
    for ang in np.arange(-limit, limit + 1e-9, coarse):
        rot = np.asarray(canvas.rotate(float(ang), resample=Image.BILINEAR, fillcolor=255), dtype=np.float32)
        rows = (rot < 128).sum(axis=1).astype(np.float64)
        score = float(np.var(rows))
        scores.append({"angle": round(float(ang), 2), "score": round(score, 1)})
        if score > best_score:
            best_ang, best_score = float(ang), score
    for ang in np.arange(best_ang - coarse, best_ang + coarse + 1e-9, 0.5):
        rot = np.asarray(canvas.rotate(float(ang), resample=Image.BILINEAR, fillcolor=255), dtype=np.float32)
        rows = (rot < 128).sum(axis=1).astype(np.float64)
        score = float(np.var(rows))
        scores.append({"angle": round(float(ang), 2), "score": round(score, 1)})
        if score > best_score:
            best_ang, best_score = float(ang), score
    scores.sort(key=lambda d: -d["score"])
    return {"angle": round(best_ang, 2), "score": round(best_score, 1), "scores": scores[:6]}


def _angle_quality(img: Image.Image, ang: float, charset: str, length: int | None, bank=None) -> tuple[float, str]:
    """Cheap angle score from glyph segmentation + font templates.

    Tesseract confidence is far too noisy to rank angles (it often reads
    nothing at every angle); glyph shape statistics are stable and run in
    milliseconds, so the sweep stays fast.
    """
    from . import template as tpl

    corrected = img if abs(ang) < 0.4 else I.rotate_pil(img, ang)
    corrected = I.trim(corrected, pad=6)
    if bank is not None:
        try:
            res = tpl.recognize(corrected, charset=charset, length=length, bank=bank)
            text = res.get("text", "")
            n = int(res.get("glyph_count", 0) or 0)
            score = float(res.get("confidence", 0.0))
            if length:
                score -= abs(n - length) * 14.0
            return max(0.0, score), text
        except Exception:
            pass
    r = ocr.tesseract(corrected, psm=7, whitelist=charset)
    text = ocr.normalize(r.text, charset)
    score = r.conf
    if length:
        score -= abs(len(text) - length) * 20.0
    return max(0.0, float(score)), text


def solve_rotate(
    img: Image.Image,
    limit: float = 60.0,
    charset: str | None = None,
    length: int | None = None,
    case_sensitive: bool = True,
) -> dict:
    """Cancel the rotation, then OCR.

    The projection estimate is only used for reporting: it can lock onto the
    mirrored angle, so the sweep is global and ranked by cheap glyph-shape
    quality. The three most distinct angles are read with the full text
    pipeline and their readings vote — a single read can be confidently wrong
    (X -> I1, 0 -> O), three reads rarely agree on the same mistake.
    """
    from . import template as tpl

    if charset is not None and not case_sensitive:
        charset = charset.upper()
    est = estimate_angle(img, limit=limit)
    bank = None
    try:
        bank = tpl.build_bank(charset or "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    except Exception:
        bank = None

    # angle grid: the projection estimate and its mirror bracket the truth,
    # plus a coarse spread in case the estimate locked onto a local maximum
    base = float(est["angle"])
    grid: List[float] = []
    for ang in (-base - 10.0, -base, -base + 10.0, base - 10.0, base, base + 10.0):
        ang = max(-limit, min(limit, ang))
        if all(abs(ang - g) >= 4.0 for g in grid):
            grid.append(round(ang, 1))
    grid = grid[:6]

    reads: List[dict] = []
    scored: List[dict] = []
    for ang in grid:
        q, qtext = _angle_quality(img, ang, charset, length, bank=bank)
        scored.append({"angle": round(ang, 1), "score": round(q, 1), "text": qtext})
        corrected = I.trim(img if abs(ang) < 0.4 else I.rotate_pil(img, ang), pad=8)
        read = text_solver.solve_text(
            corrected,
            charset=charset,
            length=length,
            case_sensitive=case_sensitive,
            max_passes=4,
            fast=True,
        )
        reads.append(
            {
                "angle": round(float(ang), 1),
                "text": read.get("text", ""),
                "conf": float(read.get("confidence", 0.0)),
            }
        )

    target_len = length
    if target_len is None:
        # confidence-weighted length vote: garbage reads are short but can be
        # confident, so count weight, not votes
        counts: dict = {}
        for r in reads:
            if r["text"]:
                counts[len(r["text"])] = counts.get(len(r["text"]), 0.0) + max(r["conf"], 12.0)
        target_len = max(counts, key=counts.get) if counts else 0
    usable = [r for r in reads if r["text"] and len(r["text"]) == target_len]

    if usable:
        cols: List[dict] = [{} for _ in range(target_len)]
        for r in usable:
            w = max(r["conf"], 10.0)
            for i, ch in enumerate(r["text"]):
                cols[i][ch] = cols[i].get(ch, 0.0) + w
        chars, ratios = [], []
        for col in cols:
            total = sum(col.values())
            ch = max(col, key=col.get)
            chars.append(ch)
            ratios.append(col[ch] / total if total else 0.0)
        winner = "".join(chars)
        vote_conf = float(np.mean(ratios)) * 100.0 if ratios else 0.0
        support = [r for r in usable if r["text"] == winner]
        final = max(support, key=lambda r: r["conf"]) if support else max(usable, key=lambda r: r["conf"])
    else:
        final = max(reads, key=lambda r: r["conf"])
        winner = final["text"]
        vote_conf = 0.0

    # one full-quality read at the winning angle: templates and the aggressive
    # variants clean up single-glyph slips the fast screening cannot see
    corrected = I.trim(img if abs(final["angle"]) < 0.4 else I.rotate_pil(img, final["angle"]), pad=8)
    full = text_solver.solve_text(
        corrected, charset=charset, length=length, case_sensitive=case_sensitive, max_passes=6
    )
    full_text = full.get("text") or ""
    full_conf = float(full.get("confidence", 0.0))
    pool = list(usable)
    if full_text and len(full_text) == target_len:
        pool.append({"angle": final["angle"], "text": full_text, "conf": full_conf})

    if pool:
        cols = [{} for _ in range(target_len)]
        for r in pool:
            w = max(r["conf"], 10.0)
            for i, ch in enumerate(r["text"]):
                cols[i][ch] = cols[i].get(ch, 0.0) + w
        chars, ratios = [], []
        for col in cols:
            total = sum(col.values())
            ch = max(col, key=col.get)
            chars.append(ch)
            ratios.append(col[ch] / total if total else 0.0)
        merged = "".join(chars)
        conf = float(np.mean(ratios)) * 100.0 if ratios else 0.0
        support = [r for r in pool if r["text"] == merged]
        if len(support) > 1:
            conf = min(100.0, conf + 6.0)
        if merged != winner:
            winner = merged
    else:
        conf = vote_conf

    return {
        "kind": "rotate",
        "angle": final["angle"],
        "projection_angle": est["angle"],
        "text": winner,
        "confidence": round(conf, 1),
        "candidates": scored[:6],
        "angle_reads": reads,
        "corrected_size": list(I.trim(I.rotate_pil(img, final["angle"]), pad=8).size),
        "note": "apply `angle` to the input, or use the corrected image directly",
    }
