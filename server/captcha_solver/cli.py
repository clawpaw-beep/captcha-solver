"""Command line entry point: `python -m captcha_solver.cli solve --image x.png`."""
from __future__ import annotations

import argparse
import json
import sys

from . import imageops as I
from . import pipeline, text_solver


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="captcha-solver", description="Local captcha solving toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("solve", help="solve one captcha image")
    s.add_argument("--image", help="image path")
    s.add_argument("--b64", help="base64 image payload (data: prefix allowed)")
    s.add_argument("--url", help="image URL")
    s.add_argument("--kind", default="auto", choices=list(pipeline.KINDS))
    s.add_argument("--charset", default=None)
    s.add_argument("--length", type=int, default=None)
    s.add_argument("--prompt", default=None, help="instruction text for click captchas")
    s.add_argument("--case-insensitive", action="store_true")
    s.add_argument("--piece", help="slider piece image path")
    s.add_argument("--background", help="slider background image path")
    s.add_argument("--piece-width", type=int, default=None)
    s.add_argument("--rounds", type=int, default=2)
    s.add_argument("--overlay", help="write a debug overlay PNG here")
    s.add_argument("--json", action="store_true", help="machine-readable output")

    q = sub.add_parser("serve", help="run the HTTP API")
    q.add_argument("--host", default="127.0.0.1")
    q.add_argument("--port", type=int, default=8790)

    b = sub.add_parser("bench", help="run the synthetic accuracy benchmark")
    b.add_argument("--dir", default=None)
    b.add_argument("--count", type=int, default=12)

    m = sub.add_parser("mcp", help="run the MCP stdio server")
    m.add_argument("--transport", default="stdio")
    return p


def cmd_solve(args) -> int:
    img = I.load_image(path=args.image, b64=args.b64, url=args.url)
    piece = I.load_image(path=args.piece, keep_alpha=True) if args.piece else None
    background = I.load_image(path=args.background) if args.background else None
    res = pipeline.solve(
        img,
        kind=args.kind,
        charset=args.charset,
        length=args.length,
        prompt=args.prompt,
        case_sensitive=not args.case_insensitive,
        piece=piece,
        background=background,
        piece_width=args.piece_width,
        rounds=args.rounds,
        want_overlay=bool(args.overlay),
    )
    if args.overlay and "overlay" in res:
        res["overlay"].save(args.overlay)
        res.pop("overlay", None)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(f'kind={res["kind"]} answer={res["answer"]!r} conf={res["confidence"]} '
              f'({res["elapsed_ms"]}ms) -> {res["recommendation"]}')
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "solve":
        return cmd_solve(args)
    if args.cmd == "serve":
        from .service import serve

        return serve(args.host, args.port)
    if args.cmd == "bench":
        from bench.run_bench import main as bench_main

        return bench_main(args.dir, args.count)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main()
    return 2


if __name__ == "__main__":
    sys.exit(main())
