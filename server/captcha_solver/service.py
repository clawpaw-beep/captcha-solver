"""HTTP API for the captcha solver (stdlib only, no framework dependency).

    python -m captcha_solver.service --host 127.0.0.1 --port 8790

Endpoints
    GET  /health                     engine + tesseract status
    POST /solve                      {"kind": "...", "image_path"|"image_b64"|"image_url", ...}
    POST /slider                      {"background_path", "piece_path", "piece_width"}
    POST /click                      {"image_path", "prompt"}
    POST /track                      {"distance": 185, "seed": 7}     slider drag
                                     {"points": [[x, y], ...]}         click order
    POST /bench                      {"count": 6}

Any agent framework can call this with plain HTTP; the MCP transport in
`mcp_server.py` exposes the same operations as agent tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from . import engine, imageops as I, pipeline, text_solver

API_TOKEN = os.environ.get("CAPTCHA_API_TOKEN", "").strip()


def _load(payload: Dict[str, Any], prefix: str = "image"):
    path = payload.get(f"{prefix}_path")
    b64 = payload.get(f"{prefix}_b64")
    url = payload.get(f"{prefix}_url")
    if not (path or b64 or url):
        return None
    return I.load_image(path=path, b64=b64, url=url, keep_alpha=(prefix == "piece"))


def handle_solve(payload: Dict[str, Any]) -> Dict[str, Any]:
    img = _load(payload, "image")
    if img is None:
        raise ValueError("image_path / image_b64 / image_url is required")
    piece = _load(payload, "piece")
    bg = _load(payload, "background")
    kwargs = {
        "kind": payload.get("kind", "auto"),
        "charset": payload.get("charset"),
        "length": payload.get("length"),
        "prompt": payload.get("prompt"),
        "case_sensitive": not payload.get("case_insensitive", False),
        "piece": piece,
        "background": bg,
        "piece_width": payload.get("piece_width"),
        "rounds": int(payload.get("rounds", 2)),
        "handle_x": int(payload.get("handle_x", 0)),
        "escalate": bool(payload.get("escalate", False)),
    }
    res = pipeline.solve(img, **kwargs)
    out_path = payload.get("overlay_path")
    if out_path:
        overlay = pipeline._overlay(res, img)
        overlay.save(out_path)
        res["overlay_path"] = out_path
    res.pop("overlay", None)
    return res


def handle_slider(payload: Dict[str, Any]) -> Dict[str, Any]:
    from . import slider

    bg = _load(payload, "background")
    piece = _load(payload, "piece")
    comp = _load(payload, "image") or bg
    if comp is None:
        raise ValueError("background_path / piece_path is required")
    return slider.solve_slider(
        composite=comp,
        background=bg,
        piece=piece,
        piece_width=payload.get("piece_width"),
        handle_x=int(payload.get("handle_x", 0)),
    )


def handle_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    from . import click

    img = _load(payload, "image")
    if img is None:
        raise ValueError("image_path / image_b64 / image_url is required")
    return click.solve_click(img, prompt=payload.get("prompt"), charset=payload.get("charset"))


def handle_track(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Human-like pointer trajectory: slider drag (`distance`) or click order (`points`)."""
    from . import behavior

    return behavior.build_track(payload)


def handle_bench(payload: Dict[str, Any]) -> Dict[str, Any]:
    from bench.run_bench import run as bench_run

    out = bench_run(payload.get("dir"), int(payload.get("count", 6)))
    out.pop("rows", None)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "captcha-solver/1.0"
    routes = {
        "/solve": handle_solve,
        "/slider": handle_slider,
        "/click": handle_click,
        "/track": handle_track,
        "/bench": handle_bench,
    }

    def log_message(self, fmt, *args):  # keep the MCP/agent transcript quiet
        pass

    def _send(self, code: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] != "/health":
            self._send(404, {"ok": False, "error": "not found"})
            return
        self._send(
            200,
            {
                "ok": True,
                "ddddocr": engine.available(),
                "engine_error": engine.status().get("error"),
                "tesseract": shutil.which("tesseract"),
                "tesseract_version": _tesseract_version(),
                "charset_default": text_solver.DEFAULT_CHARSET,
            },
        )

    def do_POST(self):
        route = self.path.split("?")[0]
        fn = self.routes.get(route)
        if fn is None:
            self._send(404, {"ok": False, "error": "not found"})
            return
        if API_TOKEN and self.headers.get("X-Api-Token", "") != API_TOKEN:
            self._send(401, {"ok": False, "error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            self._send(200, fn(payload))
        except Exception as exc:  # surfaced to the caller, never swallowed
            self._send(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _tesseract_version() -> str | None:
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.splitlines()[0].strip() if out.stdout else None
    except Exception:
        return None


def serve(host: str = "127.0.0.1", port: int = 8790) -> int:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"captcha-solver listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    raise SystemExit(serve(a.host, a.port))
