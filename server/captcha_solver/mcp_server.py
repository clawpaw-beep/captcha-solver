"""MCP stdio server: the captcha solver as agent-facing tools.

    .venv/bin/python -m captcha_solver.mcp_server

Speaks newline-delimited JSON-RPC 2.0 (MCP 2024-11-05 / 2025-06-18 style) and
exposes captcha_solve / captcha_slider / captcha_click / captcha_trajectory /
captcha_status / captcha_bench. Any MCP-capable agent gets these as native tools.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from typing import Any, Dict, List

from . import __version__, engine, imageops as I, pipeline, text_solver

SERVER_INFO = {"name": "captcha-solver", "version": __version__}
PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

_IMAGE_PROPS = {
    "image_path": {"type": "string", "description": "Absolute path to the captcha image."},
    "image_b64": {"type": "string", "description": "Base64 image payload (data: prefix allowed)."},
    "image_url": {"type": "string", "description": "HTTP(S) URL of the captcha image."},
}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "captcha_solve",
        "description": (
            "Solve one captcha image locally and return the answer to submit. "
            "Handles text captchas, arithmetic (a+b=?), jigsaw sliders and "
            "click-order captchas. Returns {answer, confidence, kind, candidates}: "
            "submit `answer`; when confidence < 60 refresh the captcha and retry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_IMAGE_PROPS,
                "kind": {
                    "type": "string",
                    "enum": ["auto", "text", "arithmetic", "arith_multi", "slider", "click", "rotate", "segment"],
                    "description": "Captcha family; `auto` routes from image structure.",
                },
                "charset": {
                    "type": "string",
                    "description": "Allowed characters, e.g. '0123456789' or '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'.",
                },
                "length": {"type": "integer", "description": "Expected answer length, when the page states it."},
                "prompt": {
                    "type": "string",
                    "description": "On-screen instruction text for click captchas, e.g. '请依次点击：明 天 快 乐'.",
                },
                "case_insensitive": {"type": "boolean", "description": "Case-fold the read onto the charset."},
                "piece_path": {"type": "string", "description": "Slider piece image path (jigsaw)."},
                "background_path": {"type": "string", "description": "Slider background image path."},
                "piece_width": {"type": "integer", "description": "Rendered piece width in page pixels."},
                "handle_x": {"type": "integer", "description": "Slider handle's current x; drag = offset - handle_x."},
                "overlay_path": {"type": "string", "description": "Write a debug overlay PNG here."},
                "escalate": {
                    "type": "boolean",
                    "description": "Forward to the configured remote solver when local confidence is low.",
                },
            },
        },
    },
    {
        "name": "captcha_slider",
        "description": (
            "Locate a jigsaw/slider gap and return drag_distance. Pass background+piece "
            "for accurate matching, or a single composite screenshot when only that exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "background_path": {"type": "string"},
                "piece_path": {"type": "string"},
                "image_path": {"type": "string", "description": "Composite screenshot fallback."},
                "piece_width": {"type": "integer"},
                "handle_x": {"type": "integer"},
            },
        },
    },
    {
        "name": "captcha_click",
        "description": (
            "Resolve a click-order captcha: returns ordered [x, y] points to click. "
            "Pass the on-screen instruction text as `prompt` so the order can be matched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_IMAGE_PROPS,
                "prompt": {"type": "string"},
                "charset": {"type": "string"},
            },
        },
    },
    {
        "name": "captcha_trajectory",
        "description": (
            "Turn a solved captcha answer into a human-like pointer gesture. Slider: pass "
            "`distance` -> points/durations_ms/total_ms; drive the handle with those instead of a "
            "constant-step drag (a correct distance with uniform 12 ms steps is rejected by "
            "Aliyun PUZZLE). Click-order: pass `points` -> clicks with hold_ms/move_ms/offset/micro. "
            "Every response also carries humanize_check (ok/issues/metrics); fix the issues before "
            "sending the gesture."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "distance": {
                    "type": "number",
                    "description": "Slider travel in CSS px (> 0). Omit when sending `points`.",
                },
                "points": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "Click-order targets [[x, y], ...] in the order to click.",
                },
                "steps": {
                    "type": "integer",
                    "description": "Trajectory point count; default max(12, min(40, round(distance/8))).",
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "Wanted total gesture length; each step stays inside 8-26 ms.",
                },
                "easing": {
                    "type": "string",
                    "enum": ["ease_out_quart", "ease_out_cubic", "ease_in_out_sine"],
                    "description": "Speed curve; default ease_out_quart (fast start, slow finish).",
                },
                "jitter": {
                    "type": "number",
                    "description": "Sub-pixel horizontal noise amplitude in px; default 0.6.",
                },
                "overshoot": {
                    "type": "boolean",
                    "description": "Overshoot 0.5-2.5 px then rebound onto the target; default true.",
                },
                "seed": {"type": "integer", "description": "Seed so a gesture is reproducible."},
            },
        },
    },
    {
        "name": "captcha_status",
        "description": "Report which recognition engines are live (neural model, tesseract, template bank) and their versions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "captcha_bench",
        "description": "Run the synthetic accuracy benchmark and return per-family accuracy and latency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dir": {"type": "string", "description": "Corpus directory (defaults to the bundled fixtures)."},
                "count": {"type": "integer", "description": "Regenerate the corpus with this many samples per family."},
            },
        },
    },
]


def _load(payload: Dict[str, Any], prefix: str = "image"):
    if payload.get(f"{prefix}_path") or payload.get(f"{prefix}_b64") or payload.get(f"{prefix}_url"):
        return I.load_image(
            path=payload.get(f"{prefix}_path"),
            b64=payload.get(f"{prefix}_b64"),
            url=payload.get(f"{prefix}_url"),
            keep_alpha=(prefix == "piece"),
        )
    return None


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "captcha_solve":
        img = _load(args, "image")
        if img is None:
            raise ValueError("image_path, image_b64 or image_url is required")
        res = pipeline.solve(
            img,
            kind=args.get("kind", "auto"),
            charset=args.get("charset"),
            length=args.get("length"),
            prompt=args.get("prompt"),
            case_sensitive=not args.get("case_insensitive", False),
            piece=_load(args, "piece"),
            background=_load(args, "background"),
            piece_width=args.get("piece_width"),
            handle_x=int(args.get("handle_x", 0)),
            escalate=bool(args.get("escalate", False)),
        )
        if args.get("overlay_path"):
            pipeline._overlay(res, img).save(args["overlay_path"])
            res["overlay_path"] = args["overlay_path"]
        res.pop("overlay", None)
        return res
    if name == "captcha_slider":
        from . import slider

        bg = _load(args, "background")
        piece = _load(args, "piece")
        comp = _load(args, "image") or bg
        if comp is None:
            raise ValueError("background_path+piece_path or image_path is required")
        res = slider.solve_slider(
            composite=comp,
            background=bg,
            piece=piece,
            piece_width=args.get("piece_width"),
            handle_x=int(args.get("handle_x", 0)),
        )
        if args.get("escalate"):
            from . import providers

            res = providers.maybe_escalate(comp, res, threshold=45.0)
        return res
    if name == "captcha_click":
        from . import click

        img = _load(args, "image")
        if img is None:
            raise ValueError("image_path, image_b64 or image_url is required")
        return click.solve_click(img, prompt=args.get("prompt"), charset=args.get("charset"))
    if name == "captcha_trajectory":
        from . import behavior

        return behavior.build_track(args)
    if name == "captcha_status":
        try:
            import subprocess

            ver = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5)
            tess = (ver.stdout or "").splitlines()[0].strip() or None
        except Exception:
            tess = None
        return {
            "ok": True,
            "version": __version__,
            "engines": {
                "neural": engine.available(),
                "tesseract": tess,
                "template_bank": True,
            },
            "engine_error": engine.status().get("error"),
            "python": sys.executable,
            "default_charset": text_solver.DEFAULT_CHARSET,
            "capabilities": ["text", "arithmetic", "arith_multi", "slider", "click", "rotate", "trajectory"],
        }
    if name == "captcha_bench":
        from bench.run_bench import run as bench_run

        out = bench_run(args.get("dir"), int(args.get("count", 6) or 6))
        out.pop("rows", None)
        return out
    raise ValueError(f"unknown tool: {name}")


def _text_result(payload: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def handle(msg: Dict[str, Any]) -> Dict[str, Any] | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:
        return None  # a response, not a request
    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        version = client_ver if client_ver in PROTOCOL_VERSIONS else "2025-06-18"
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Local captcha solver. Call captcha_solve with the captcha image; submit the "
                    "returned `answer`. Sliders: captcha_slider -> drag_distance, then "
                    "captcha_trajectory with that distance to get a human-like drag track. "
                    "Click captchas: captcha_click with the on-screen prompt -> ordered points, "
                    "then captcha_trajectory with those points for humanised click timing."
                ),
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            out = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": mid, "result": _text_result(out)}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)}
                    ],
                    "isError": True,
                },
            }
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(msg)
        except Exception:
            resp = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": traceback.format_exc(limit=2)},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
