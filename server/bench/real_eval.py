"""Score the solver on a folder of real captchas with hand-checked labels.

    python -m bench.real_eval --dir out/real --labels bench/real_labels.json

Labels are graded visually (the `?` marker excludes genuinely ambiguous shots).
Reports exact-match accuracy, confidence buckets, and the miss list so the gap
between "looks right" and "is right" stays visible.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from captcha_solver import imageops as I  # noqa: E402
from captcha_solver import pipeline  # noqa: E402


def run(dir_path: str, labels_path: str, kind: str = "text") -> Dict:
    labels = json.load(open(labels_path, encoding="utf-8"))
    rows: List[dict] = []
    for name, truth in labels.items():
        if name.startswith("_") or truth == "?":
            continue
        path = os.path.join(dir_path, name)
        if not os.path.exists(path):
            continue
        # arithmetic labels carry both the expression and its value, so the run
        # can report "read the right digits but evaluated wrong" separately
        expr = None
        if isinstance(truth, dict):
            expr = truth.get("expr")
            truth = truth.get("answer", "")
        img = I.load_image(path=path)
        res = pipeline.solve(img, kind=kind)
        got = (res.get("answer") or "").upper()
        rows.append(
            {
                "file": name,
                "truth": str(truth).upper(),
                "expr": expr,
                "read": res.get("expr") or res.get("text"),
                "got": got,
                "hit": got == str(truth).upper(),
                "conf": res.get("confidence", 0.0),
                "size": list(img.size),
            }
        )
    if not rows:
        raise SystemExit("no labelled samples found")
    hit = sum(1 for r in rows if r["hit"])
    buckets = {"conf<60": [], "60-80": [], ">80": []}
    for r in rows:
        key = "conf<60" if r["conf"] < 60 else "60-80" if r["conf"] < 80 else ">80"
        buckets[key].append(r["hit"])
    return {
        "dir": dir_path,
        "n": len(rows),
        "hit": hit,
        "acc": round(100.0 * hit / len(rows), 1),
        "mean_ms": None,
        "buckets": {k: [len(v), sum(1 for x in v if x)] for k, v in buckets.items()},
        "rows": rows,
    }


def main(dir_path: str = None, labels: str = None, args_kind: str = "text") -> int:
    dir_path = dir_path or os.path.join(ROOT, "out", "real")
    labels = labels or os.path.join(HERE, "real_labels.json")
    out = run(dir_path, labels, kind=args_kind)
    print(f'real captchas: {out["n"]} graded')
    print(f'accuracy: {out["hit"]}/{out["n"]} = {out["acc"]}%')
    for k, (n, ok) in out["buckets"].items():
        if n:
            print(f'  confidence {k:<7} n={n:<3} correct={ok} ({100.0*ok/n:.0f}%)')
    print("\nmisses:")
    for r in out["rows"]:
        if not r["hit"]:
            extra = f' read={r.get("read")!r} expr={r.get("expr")!r}' if r.get("expr") else ""
            print(f'  {r["file"]:<13} got {r["got"]:<8} truth {r["truth"]:<8} conf {r["conf"]}{extra}')
    report = os.path.join(ROOT, "out", "real_report.json")
    json.dump(out, open(report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nreport -> {report}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--kind", default="text", choices=["text", "arithmetic", "click", "slider", "rotate"])
    a = ap.parse_args()
    default_labels = (
        os.path.join(HERE, "real_labels_arith.json") if a.kind == "arithmetic" else None
    )
    raise SystemExit(main(a.dir, a.labels or default_labels, a.kind))
