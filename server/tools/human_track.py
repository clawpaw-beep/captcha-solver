"""Record one human drag, replay it as many times as needed.

Aliyun's decision is made server-side from the behaviour data the page uploass —
there is no front-end rule to satisfy. The only thing that reliably matches a
human is a human's own event stream, so: capture one real drag (all pointer
fields + real timestamps), then replay that recording through CDP with the x
axis scaled to whatever gap the solver found this time.

One recording, unlimited automatic runs.

    # 1) record (user drags once in the browser)
    python -m tools.human_track record

    # 2) replay (fully automatic)
    python -m tools.human_track replay --gap 182
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from captcha_solver import cdp  # noqa: E402

OUT = os.path.join(HERE, "out", "live")
TRACK_FILE = os.path.join(OUT, "human_track.json")

RECORDER_JS = r"""
(() => {
  if (window.__humanRec) return 'already';
  window.__humanRec = {events: [], active: false, done: false, t0: 0};
  const push = (kind, e) => {
    const rec = window.__humanRec;
    if (rec.done) return;
    if (kind === 'down' && !rec.active) { rec.active = true; rec.t0 = e.timeStamp; rec.events = []; }
    if (!rec.active) return;
    rec.events.push({
      kind, t: Math.round((e.timeStamp - rec.t0) * 100) / 100,
      x: e.clientX, y: e.clientY,
      pressure: e.pressure, buttons: e.buttons, pointerType: e.pointerType,
      tiltX: e.tiltX, tiltY: e.tiltY, twist: e.twist,
      target: (e.target && (e.target.id || e.target.className || '')) + ''
    });
    if (kind === 'up') { rec.active = false; rec.done = true; }
  };
  const opts = {capture: true, passive: true};
  document.addEventListener('pointerdown', e => push('down', e), opts);
  document.addEventListener('pointermove', e => push('move', e), opts);
  document.addEventListener('pointerup', e => push('up', e), opts);
  return 'hooked';
})()
"""


def record(port: int = 9222, timeout: float = 120.0, wait: bool = True) -> Dict:
    d = cdp.CDP.connect(port=port, timeout=15)
    print("注入记录器:", d.eval(RECORDER_JS))
    d.eval("window.__humanRec = {events: [], active: false, done: false, t0: 0};")
    print("→ 现在请在浏览器里用手拖一次滑块（我不碰鼠标）。等待中…")
    start = time.time()
    last_n = 0
    while time.time() - start < timeout:
        st = d.eval("(() => ({done: window.__humanRec.done, n: window.__humanRec.events.length}))()")
        if st["n"] != last_n:
            last_n = st["n"]
        if st["done"] and st["n"] > 5:
            break
        time.sleep(0.4)
    rec = d.eval("window.__humanRec")
    d.close()
    if not rec.get("done"):
        print("没有记录到完整拖动（超时或未操作）")
        return {"ok": False, "events": len(rec.get("events") or [])}
    evs = rec["events"]
    moves = [e for e in evs if e["kind"] == "move"]
    span = moves[-1]["x"] - moves[0]["x"] if moves else 0
    track = {
        "ok": True,
        "recorded_at": int(time.time()),
        "events": evs,
        "stats": {"events": len(evs), "moves": len(moves), "duration_ms": evs[-1]["t"],
                  "span_x": round(span, 1), "pressure_range": [min(e["pressure"] for e in moves),
                                                               max(e["pressure"] for e in moves)]},
    }
    os.makedirs(OUT, exist_ok=True)
    with open(TRACK_FILE, "w", encoding="utf-8") as fh:
        json.dump(track, fh, ensure_ascii=False, indent=1)
    print(f"已保存 {TRACK_FILE}")
    print("  统计:", json.dumps(track["stats"], ensure_ascii=False))
    return track


def load() -> Optional[Dict]:
    if not os.path.exists(TRACK_FILE):
        return None
    with open(TRACK_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def replay(d: "cdp.CDP", x0: float, y0: float, target_dx: float, rec: Optional[Dict] = None,
           time_scale: float = 1.0) -> Dict:
    """Replay the recording scaled to `target_dx`, around the press point (x0, y0)."""
    rec = rec or load()
    if not rec or not rec.get("ok"):
        raise RuntimeError("no human track recorded — run `record` first")
    evs = rec["events"]
    moves = [e for e in evs if e["kind"] == "move"]
    if len(moves) < 4:
        raise RuntimeError("recorded track has too few moves")
    src_dx = moves[-1]["x"] - moves[0]["x"]
    if abs(src_dx) < 1:
        raise RuntimeError("recorded track has no horizontal travel")
    k = target_dx / src_dx

    def tx(x):  # scale horizontal travel around the start
        return x0 + (x - moves[0]["x"]) * k

    def ty(y):
        return y0 + (y - moves[0]["y"])

    sent = 0
    prev_t = 0.0
    for e in evs:
        dt = max(0.0, (e["t"] - prev_t) / 1000.0 / max(time_scale, 0.2))
        if dt:
            time.sleep(dt)
        prev_t = e["t"]
        kind = e["kind"]
        x, y = tx(e["x"]), ty(e["y"])
        if kind == "down":
            d.mouse("mouseMoved", x, y, buttons=0, force=float(e.get("pressure") or 0))
            d.mouse("mousePressed", x, y, button="left", buttons=1, click_count=1,
                    force=float(e.get("pressure") or 0.5))
        elif kind == "move":
            d.mouse("mouseMoved", x, y, button="left", buttons=int(e.get("buttons") or 1),
                    force=float(e.get("pressure") or 0.5))
        else:
            d.mouse("mouseReleased", x, y, button="left", buttons=0, click_count=1, force=0.0)
        sent += 1
    return {"events": sent, "scale": round(k, 4), "target_dx": round(target_dx, 1),
            "final": [round(tx(moves[-1]["x"]), 1), round(ty(moves[-1]["y"]), 1)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "show"])
    ap.add_argument("--port", type=int, default=9222)
    a = ap.parse_args()
    if a.cmd == "record":
        record(a.port)
    else:
        rec = load()
        print(json.dumps(rec["stats"] if rec else {"ok": False}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
