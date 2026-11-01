"""Accuracy benchmark over the synthetic corpus.

    python -m bench.run_bench [--dir server/out/corpus] [--count 6]

Reports exact-match accuracy per captcha family plus mean latency, so a change
to preprocessing or engine weights can be judged instead of guessed.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from captcha_solver import imageops as I  # noqa: E402
from captcha_solver import engine, pipeline  # noqa: E402


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def run(dir_path: str | None = None, count: int = 0) -> Dict:
    dir_path = dir_path or os.path.join(ROOT, "out", "corpus")
    labels_path = os.path.join(dir_path, "labels.json")
    if not os.path.exists(labels_path):
        from bench import gen_corpus

        gen_corpus.build(dir_path, count=count or 6)
    labels = json.load(open(labels_path, encoding="utf-8"))

    rows: List[dict] = []
    for kind, items in labels.items():
        for item in items:
            img = I.load_image(path=os.path.join(dir_path, item["file"]))
            t0 = time.time()
            kwargs: dict = {"kind": kind}
            if kind == "text":
                kwargs.update(charset="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", length=item.get("length"))
            elif kind == "click":
                kwargs.update(prompt=item.get("prompt"))
            elif kind == "slider":
                kwargs.update(
                    piece=I.load_image(path=os.path.join(dir_path, item["piece_file"])),
                    background=I.load_image(path=os.path.join(dir_path, item["bg_file"])),
                )
            res = pipeline.solve(img, **kwargs)
            dt = (time.time() - t0) * 1000

            hit = False
            detail = ""
            if kind in ("text", "rotate"):
                hit = (res.get("answer") or "").upper() == item["answer"].upper()
                detail = f'{res.get("answer")} vs {item["answer"]}'
            elif kind == "arithmetic":
                hit = str(res.get("answer")) == str(item["answer"])
                detail = f'{res.get("text")} -> {res.get("answer")} vs {item["answer"]}'
            elif kind == "slider":
                off = res.get("offset")
                hit = off is not None and abs(int(off) - int(item["gap_x"])) <= 6
                detail = f'{off} vs {item["gap_x"]}'
            elif kind == "click":
                pts = res.get("points_xy") or []
                want = item["points"]
                hit = len(pts) == len(want) and all(_dist(a, b) <= 12 for a, b in zip(pts, want))
                detail = f"{pts} vs {want}"
            rows.append(
                {"kind": kind, "file": item["file"], "hit": hit, "ms": round(dt), "detail": detail, "conf": res.get("confidence")}
            )

    summary: Dict[str, dict] = {}
    for kind in {r["kind"] for r in rows}:
        sub = [r for r in rows if r["kind"] == kind]
        summary[kind] = {
            "n": len(sub),
            "hit": sum(1 for r in sub if r["hit"]),
            "acc": round(100.0 * sum(1 for r in sub if r["hit"]) / max(len(sub), 1), 1),
            "mean_ms": round(statistics.mean(r["ms"] for r in sub)),
        }
    overall = {
        "n": len(rows),
        "hit": sum(1 for r in rows if r["hit"]),
        "acc": round(100.0 * sum(1 for r in rows if r["hit"]) / max(len(rows), 1), 1),
        "mean_ms": round(statistics.mean(r["ms"] for r in rows)),
    }
    return {"dir": dir_path, "engines": engine.status(), "summary": summary, "overall": overall, "rows": rows}


def main(dir_path: str | None = None, count: int = 8) -> int:
    out = run(dir_path, count)
    print(f'dir: {out["dir"]}')
    print(f'ddddocr: {out["engines"]["ddddocr"]}  {out["engines"]["error"] or ""}')
    print(f'{"kind":<12}{"acc":>8}{"n":>6}{"ms":>8}')
    for kind, s in sorted(out["summary"].items()):
        print(f'{kind:<12}{s["acc"]:>7}%{s["n"]:>6}{s["mean_ms"]:>8}')
    o = out["overall"]
    print(f'{"OVERALL":<12}{o["acc"]:>7}%{o["n"]:>6}{o["mean_ms"]:>8}')
    misses = [r for r in out["rows"] if not r["hit"]]
    if misses:
        print("\nmisses:")
        for r in misses[:12]:
            print(f'  {r["kind"]:<11}{r["file"]:<22}{r["detail"]}')
    report = os.path.join(ROOT, "out", "bench_report.json")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    json.dump(out, open(report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nreport -> {report}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--count", type=int, default=8)
    a = ap.parse_args()
    raise SystemExit(main(a.dir, a.count))
