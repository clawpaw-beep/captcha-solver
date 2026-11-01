"""Optional high-accuracy engines (ddddocr / onnxruntime) behind one adapter.

The bundled DSH Python is hardened and refuses to load onnxruntime's native
library, so the plugin ships a dedicated virtualenv (`.venv`) built from a
non-hardened interpreter. When that import works it is the strongest text
engine; every other engine still works without it.
"""
from __future__ import annotations

import io
import os
import threading
from typing import List

import numpy as np
from PIL import Image
from typing import List, Optional, Sequence

from PIL import Image

_state: dict = {"tried": False, "ok": False, "err": None, "cls": None, "beta": None, "det": None, "slide": None}
_lock = threading.Lock()


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def available() -> bool:
    """Import ddddocr once; report whether it can be used."""
    if _state["tried"]:
        return bool(_state["ok"])
    with _lock:
        if _state["tried"]:
            return bool(_state["ok"])
        _state["tried"] = True
        try:
            import ddddocr  # type: ignore

            _state["cls"] = ddddocr.DdddOcr(show_ad=False)
            try:
                _state["beta"] = ddddocr.DdddOcr(show_ad=False, beta=True)
            except Exception:
                _state["beta"] = None
            _state["ok"] = True
        except Exception as exc:  # pragma: no cover - depends on host python
            _state["err"] = f"{type(exc).__name__}: {exc}"
            _state["ok"] = False
        return bool(_state["ok"])


def status() -> dict:
    ok = available()
    return {
        "ddddocr": ok,
        "error": _state["err"],
        "interpreter": os.path.realpath(_state.get("python", "")) or None,
    }


def classify_text(img: Image.Image, charset: Optional[str] = None) -> Optional[tuple[str, float]]:
    """Run ddddocr classification; returns (text, pseudo-confidence 0..100)."""
    if not available():
        return None
    try:
        text = _state["cls"].classification(_png(img))
    except Exception as exc:
        _state["err"] = f"classify: {type(exc).__name__}: {exc}"
        return None
    text = (text or "").strip()
    if not text:
        return None
    return text, 82.0


def classify_variants(img: Image.Image, fast: bool = False) -> list:
    """Run the neural classifier over several renderings of the same captcha.

    Real captchas disagree between renderings (a glyph recovered by inversion is
    invisible to the raw pass), so the variant set is the evidence pool for
    voting — one read is not.
    """
    if not available():
        return []
    from PIL import ImageEnhance, ImageOps

    from . import imageops as ops

    gray = img.convert("L")
    variants = [("raw", img), ("l", gray)]
    if not fast:
        variants += [
            ("inv", ImageOps.invert(gray)),
            ("auto", ImageOps.autocontrast(gray, cutoff=2)),
            ("sharp2x", ImageEnhance.Sharpness(ops.upscale(gray, 2.0)).enhance(1.6)),
            ("up3", ops.upscale(gray, 3.0)),
        ]
        ink = ops.binarize(ops.to_gray(img), "otsu")
        variants += [
            ("otsu", ops.to_pil(np.where(ink, 0, 255))),
            ("otsu_inv", ops.to_pil(np.where(ink, 255, 0))),
        ]
    out = []
    for name, v in variants:
        got = classify_text(v)
        if got and got[0]:
            out.append((name, got[0]))
    beta = classify_beta(img)
    if beta:
        out.append(("beta", beta))
    return out


def classify_beta(img: Image.Image) -> Optional[str]:
    """Second neural model (ddddocr beta): a different network for the same image."""
    if not available() or _state.get("beta") is None:
        return None
    try:
        text = _state["beta"].classification(_png(img))
    except Exception as exc:
        _state["err"] = f"beta: {type(exc).__name__}: {exc}"
        return None
    text = (text or "").strip()
    return text or None


def detect_boxes(img: Image.Image) -> List[List[int]]:
    """Glyph boxes via ddddocr's detection model: [[x0, y0, x1, y1], ...]."""
    if not available():
        return []
    with _lock:
        if _state["det"] is None:
            try:
                import ddddocr  # type: ignore

                _state["det"] = ddddocr.DdddOcr(det=True, show_ad=False)
            except Exception as exc:
                _state["err"] = f"det: {type(exc).__name__}: {exc}"
                return []
        try:
            boxes = _state["det"].detection(_png(img))
        except Exception as exc:
            _state["err"] = f"det-run: {type(exc).__name__}: {exc}"
            return []
    out: List[List[int]] = []
    for b in boxes or []:
        try:
            out.append([int(v) for v in b])
        except Exception:
            continue
    out.sort(key=lambda b: (b[1] // 40, b[0]))
    return out


def slide_offset(piece: Image.Image, background: Image.Image) -> Optional[dict]:
    """ddddocr's jigsaw matcher, used as a cross-check for the slider solver."""
    if not available():
        return None
    with _lock:
        if _state["slide"] is None:
            try:
                import ddddocr  # type: ignore

                _state["slide"] = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            except Exception as exc:
                _state["err"] = f"slide: {type(exc).__name__}: {exc}"
                return None
        try:
            res = _state["slide"].slide_match(_png(piece), _png(background), simple_target=True)
        except Exception as exc:
            _state["err"] = f"slide-run: {type(exc).__name__}: {exc}"
            return None
    try:
        x = int(res.get("target", [res.get("target_x", 0)])[0])
        return {"x": x, "confidence": float(res.get("confidence", 0.5))}
    except Exception:
        return None
