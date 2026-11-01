"""Escalation providers for captchas the local engines cannot settle.

Local engines are free, offline and fast; this module is the escape hatch for
the long tail (odd characters, heavy distortion, interactive challenges). Every
provider speaks the same interface and is configured through environment
variables, so an agent can call `captcha_solve` with `escalate=true` and the
plugin will forward the image upstream only when local confidence is low.

Configure with:
    CAPTCHA_PROVIDER=none|http|twocaptcha
    CAPTCHA_PROVIDER_URL=https://solver.internal/api/solve   # http preset
    CAPTCHA_PROVIDER_KEY=YOUR_KEY                             # twocaptcha preset
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from PIL import Image

from . import imageops as I

UA = "captcha-solver/1.0"


def provider_name() -> str:
    return (os.environ.get("CAPTCHA_PROVIDER") or "none").strip().lower()


def enabled() -> bool:
    return provider_name() not in ("", "none", "off")


def _png_b64(img: Image.Image) -> str:
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _post(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def solve_remote(img: Image.Image, kind: str = "text", hints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Forward one image to the configured provider and normalise its answer.

    Returns {"ok", "answer", "confidence", "provider", "raw"} — never raises for
    a provider error; the caller decides whether to keep retrying.
    """
    name = provider_name()
    if name in ("", "none", "off"):
        return {"ok": False, "error": "no provider configured", "provider": "none"}

    payload = {
        "image_b64": _png_b64(img),
        "kind": kind,
        "hints": hints or {},
    }

    if name == "http":
        url = os.environ.get("CAPTCHA_PROVIDER_URL", "")
        if not url:
            return {"ok": False, "error": "CAPTCHA_PROVIDER_URL missing", "provider": name}
        key = os.environ.get("CAPTCHA_PROVIDER_KEY", "")
        try:
            res = _post(url, payload, {"Authorization": f"Bearer {key}"} if key else None)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "provider": name}
        answer = str(res.get("answer") or res.get("text") or res.get("solution") or "")
        return {
            "ok": bool(answer),
            "answer": answer,
            "confidence": float(res.get("confidence", 0.0) or 0.0),
            "provider": name,
            "raw": res,
        }

    if name == "twocaptcha":
        key = os.environ.get("CAPTCHA_PROVIDER_KEY", "")
        if not key:
            return {"ok": False, "error": "CAPTCHA_PROVIDER_KEY missing", "provider": name}
        base = os.environ.get("CAPTCHA_PROVIDER_URL", "https://2captcha.com")
        form = {
            "key": key,
            "method": "base64",
            "body": _png_b64(img),
            "json": 1,
        }
        try:
            data = urllib.parse.urlencode(form).encode()
            req = urllib.request.Request(
                f"{base}/in.php", data=data, headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                created = json.loads(resp.read().decode("utf-8", "replace"))
            if str(created.get("status")) != "1":
                return {"ok": False, "error": str(created.get("request")), "provider": name, "raw": created}
            import time

            request_id = created.get("request")
            for _ in range(24):
                time.sleep(5)
                query = urllib.parse.urlencode({"key": key, "action": "get", "id": request_id, "json": 1})
                with urllib.request.urlopen(f"{base}/res.php?{query}", timeout=30) as resp:
                    got = json.loads(resp.read().decode("utf-8", "replace"))
                if str(got.get("status")) == "1":
                    return {
                        "ok": True,
                        "answer": str(got.get("request") or ""),
                        "confidence": 100.0,
                        "provider": name,
                        "raw": got,
                    }
                if got.get("request") != "CAPCHA_NOT_READY":
                    return {"ok": False, "error": str(got.get("request")), "provider": name, "raw": got}
            return {"ok": False, "error": "timeout waiting for solution", "provider": name}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "provider": name}

    return {"ok": False, "error": f"unknown provider: {name}", "provider": name}


def maybe_escalate(img: Image.Image, local: Dict[str, Any], threshold: float = 55.0) -> Dict[str, Any]:
    """Escalate when local confidence is below `threshold`, keeping local as fallback."""
    if not enabled():
        return local
    if float(local.get("confidence", 0.0)) >= threshold:
        return local
    remote = solve_remote(img, local.get("kind", "text"), {"charset": local.get("charset")})
    if remote.get("ok"):
        merged = dict(local)
        merged.update(
            {
                "answer": remote["answer"],
                "confidence": remote.get("confidence", 100.0),
                "escalated": True,
                "provider": remote.get("provider"),
                "local": {"answer": local.get("answer"), "confidence": local.get("confidence")},
            }
        )
        return merged
    local = dict(local)
    local["escalation_error"] = remote.get("error")
    return local
