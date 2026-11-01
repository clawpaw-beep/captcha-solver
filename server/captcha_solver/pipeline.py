"""Unified captcha pipeline: routing, escalation rounds, unified answer schema."""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np
from PIL import Image

from . import click as click_solver
from . import imageops as I
from . import ocr
from . import rotate as rotate_solver
from . import slider as slider_solver
from . import text_solver

KINDS = ("auto", "text", "arithmetic", "arith_multi", "slider", "click", "rotate", "segment")


def _slider_signature(img: Image.Image) -> float:
    """0..1 confidence that the image has a rectangular notch cut into it.

    A text captcha on a white page has plenty of flat *columns* (the gaps
    between glyphs), so page whiteness and notch width are both required
    before a slider route is even considered.
    """
    try:
        gray = I.to_gray(img)
        if float((gray > 240).mean()) > 0.45:
            return 0.0
    except Exception:
        return 0.0
    try:
        blocks = slider_solver._flat_blocks(img)
    except Exception:
        return 0.0
    if not blocks:
        return 0.0
    best = blocks[0]
    width_ratio = best["width"] / max(img.width, 1)
    if not 0.03 <= width_ratio <= 0.45:
        return 0.0
    # a flat notch has almost no internal texture; a busy patch does
    return max(0.0, min(1.0, 1.0 - best["std"] / 12.0))


def _click_signature(img: Image.Image) -> dict:
    comps, ink = click_solver._glyph_candidates(img)
    return {"glyph_like": len(comps), "aspect": round(img.height / max(img.width, 1), 2)}


def _merge_text_reads(reads: List[dict], charset: str, length: Optional[int]) -> tuple[str, float, List[dict]]:
    """Re-vote across the candidate trails of several text passes."""
    fake = []
    for r in reads:
        for cand in r.get("candidates", []):
            fake.append(
                ocr.OcrResult(text=cand["text"], conf=float(cand.get("conf", 0.0)), psm=7, variant="merged")
            )
    if not fake:
        return "", 0.0, []
    return ocr.vote(fake, charset, length)


def solve(
    image: Image.Image,
    kind: str = "auto",
    charset: str | None = None,
    length: int | None = None,
    prompt: str | None = None,
    case_sensitive: bool = True,
    piece: Image.Image | None = None,
    background: Image.Image | None = None,
    piece_width: int | None = None,
    rounds: int = 2,
    want_overlay: bool = False,
    handle_x: int = 0,
    escalate: bool = False,
) -> Dict:
    """Dispatch to the right solver and return one unified result object."""
    started = time.time()
    kind = (kind or "auto").lower()
    # keep charset=None so the solver can infer the alphabet (uppercase default)
    attempts: List[dict] = []

    if kind == "auto":
        if piece is not None or background is not None:
            kind = "slider"
        elif prompt:
            kind = "click"
        else:
            # read as text first: it is the common case and its confidence is
            # a better router than any structural heuristic
            probe = text_solver.solve_text(
                image, charset=charset, length=length, case_sensitive=case_sensitive, max_passes=6
            )
            sig = _slider_signature(image)
            if probe.get("confidence", 0.0) >= 62 or sig < 0.7:
                kind = "text"
                attempts.append({"route": "auto", "probe": probe.get("text"), "conf": probe.get("confidence")})
            else:
                kind = "slider"
                attempts.append({"route": "auto", "probe_conf": probe.get("confidence"), "slider_sig": round(sig, 3)})
        attempts.append({"route": "auto", "resolved_kind": kind})

    result: Dict
    if kind == "slider":
        result = slider_solver.solve_slider(
            composite=image,
            background=background,
            piece=piece,
            piece_width=piece_width,
            handle_x=handle_x,
        )
        result["answer"] = str(result.get("offset")) if result.get("offset") is not None else ""
    elif kind == "click":
        result = click_solver.solve_click(image, prompt=prompt, charset=charset, expected=length)
        result["answer"] = "|".join(f'{p["x"]},{p["y"]}' for p in result.get("points", []))
    elif kind == "rotate":
        result = rotate_solver.solve_rotate(image, charset=charset, length=length, case_sensitive=case_sensitive)
        result["answer"] = result.get("text", "")
    elif kind == "arith_multi":
        # Experimental structural engine for chained expressions (see arith.py);
        # opt-in only — it reports its read string and per-route evidence so a
        # caller can see that it failed instead of trusting a fake confidence.
        from . import arith as arith_engine

        result = arith_engine.solve(image)
        result["kind"] = "arith_multi"
        attempts.append({"engine": "multi_operator", "read": result.get("text"),
                         "threshold": result.get("threshold"), "routes": result.get("routes")})
    elif kind == "arithmetic":
        result = text_solver.solve_arithmetic(image)
        result["answer"] = result.get("answer") or ""
        if result.get("answer") is None:
            alt = text_solver.solve_text(image, charset=charset, length=length, aggressive=True)
            attempts.append({"escalation": "aggressive_text", "read": alt.get("text"), "conf": alt.get("confidence")})
            parsed = ocr.eval_arithmetic(alt.get("text", ""))
            if parsed:
                result.update({"answer": parsed["answer"], "expr": parsed["expr"], "text": alt["text"]})
    elif kind == "segment":
        seg = text_solver._segment_ocr(image, charset)
        result = {
            "kind": "segment",
            "answer": seg.text if seg else "",
            "confidence": round(seg.conf, 1) if seg else 0.0,
        }
    else:
        read = text_solver.solve_text(
            image, charset=charset, length=length, case_sensitive=case_sensitive, max_passes=8
        )
        attempts.append({"pass": 1, "read": read.get("text"), "conf": read.get("confidence")})
        best = read
        for r in range(1, max(1, rounds)):
            if best.get("confidence", 0.0) >= 72.0:
                break
            hard = text_solver.solve_text(
                image,
                charset=charset,
                length=length,
                case_sensitive=case_sensitive,
                aggressive=True,
                max_passes=10,
            )
            attempts.append({"pass": r + 1, "read": hard.get("text"), "conf": hard.get("confidence")})
            merged_text, merged_conf, trail = _merge_text_reads([best, hard], charset, length)
            # a merge must earn the replacement: equal-confidence merges mostly
            # reshuffle glyphs and lose the evidence metadata
            if merged_text and merged_conf > best.get("confidence", 0.0) + 8.0:
                best = {
                    **best,
                    "text": merged_text,
                    "confidence": round(min(100.0, merged_conf), 1),
                    "candidates": trail or best.get("candidates", []),
                }
            if hard.get("confidence", 0.0) > best.get("confidence", 0.0) + 5.0:
                best = hard
        result = dict(best)
        result["answer"] = result.get("text", "")
        parsed = ocr.eval_arithmetic(result.get("answer", ""))
        if parsed and kind == "text":
            result["arithmetic"] = parsed

    result.setdefault("kind", kind)
    result.setdefault("answer", "")
    result["confidence"] = float(result.get("confidence", 0.0))
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    result["attempts"] = attempts
    result["recommendation"] = _recommend(result)
    if escalate:
        from . import providers

        result = providers.maybe_escalate(image, result)
    if want_overlay:
        result["overlay"] = _overlay(result, image).copy()
    return result


def _recommend(result: Dict) -> str:
    conf = result.get("confidence", 0.0)
    kind = result.get("kind")
    if kind == "slider" and result.get("offset") is not None:
        target = result.get("drag_distance", result.get("offset"))
        if conf >= 55:
            return f"drag the piece/slider by {target}px, then release"
        return f"weak match at x={result.get('offset')} — verify the gap visually before releasing"
    if kind == "click" and result.get("points"):
        return "click the returned points in order, then confirm the submission"
    if kind in ("arithmetic", "arith_multi") and result.get("answer"):
        if conf >= 60:
            return "submit the numeric answer"
        return "expression read is ambiguous — retry with a fresh captcha"
    if result.get("kind") == "click" and not result.get("points"):
        return "pass prompt='<instruction text>' so the click order can be resolved"
    if result.get("kind") == "slider" and result.get("offset") is None:
        return "pass background+piece (or piece_width) — notch detection alone found nothing"
    if conf >= 80:
        return "submit the answer"
    if conf >= 55:
        return "submit, but be ready to retry with a fresh captcha"
    return "low confidence: refresh the captcha, or retry with explicit charset/length hints"


def _overlay(result: Dict, image: Image.Image) -> Image.Image:
    kind = result.get("kind")
    if kind == "slider" and result.get("gap_box"):
        return I.overlay_box(image, result["gap_box"])
    if kind == "click" and result.get("points"):
        return I.overlay_points(image, [[p["x"], p["y"]] for p in result["points"]])
    return image
