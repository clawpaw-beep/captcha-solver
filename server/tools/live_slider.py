"""End-to-end slider run against a real site, with trusted CDP mouse input.

The point of this tool is the input path: coordinates come from `slider`, timing
comes from `behavior`, and the drag is played through `Input.dispatchMouseEvent`
so the page sees `isTrusted: true`. Synthetic `PointerEvent`s — everything the
page-JS route can offer — are rejected by GeeTest v4 no matter how good the
offset is.

    python -m tools.live_slider --url https://www.geetest.com/en/adaptive-captcha-demo
    python -m tools.live_slider --url ... --headed --keep-open
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Dict, Optional

from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from captcha_solver import behavior, cdp, imageops as I, slider  # noqa: E402

OUT = os.path.join(HERE, "out", "live")
SEED = 20261101


# --------------------------------------------------------------------- helpers
def _download(url: str, path: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": I.UA, "Referer": "https://www.geetest.com/"})
    with urllib.request.urlopen(req, timeout=20) as r, open(path, "wb") as fh:
        fh.write(r.read())
    return path


def _css_url(js_value: str) -> Optional[str]:
    """`url("https://…")` -> https://…"""
    if not js_value or "url(" not in js_value:
        return None
    return js_value.split("url(")[1].split(")")[0].strip('"\'')


GEETEST_PROBE = r"""
(() => {
  const vis = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect();
    return (r.width > 0 && r.height > 0) ? {x:r.x, y:r.y, w:r.width, h:r.height} : null; };
  const style = (s) => { const e = document.querySelector(s); return e ? getComputedStyle(e) : null; };
  const btn = vis('.geetest_btn');
  const bg = vis('[class*="geetest_bg"]');
  const slice = vis('[class*="geetest_slice_bg"]');
  const verify = vis('[class*="geetest_btn_click"]');
  return {
    btn, bg, slice, verify,
    bgUrl: style('[class*="geetest_bg"]') ? style('[class*="geetest_bg"]').backgroundImage : null,
    sliceUrl: style('[class*="geetest_slice_bg"]') ? style('[class*="geetest_slice_bg"]').backgroundImage : null,
    tips: (document.querySelector('[class*="geetest_tips_wrap"]') || {}).innerText || null,
    err: (document.querySelector('[class*="geetest_err_tips"]') || {}).innerText || null,
    result: (document.querySelector('[class*="geetest_result"]') || {}).innerText || null,
    dpr: devicePixelRatio,
  };
})()
"""

GEETEST_OUTCOME = r"""
(() => {
  const t = (s) => { const e = document.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  const cap = document.querySelector('[class*="geetest_captcha"]');
  return {
    tips: t('[class*="geetest_tips_wrap"]'),
    err: t('[class*="geetest_err_tips"]'),
    result: t('[class*="geetest_result"]'),
    panelVisible: !!document.querySelector('[class*="geetest_box_wrap"]') &&
                  document.querySelector('[class*="geetest_box_wrap"]').getBoundingClientRect().height > 20,
    cls: cap ? (cap.className || '').toString().slice(0, 90) : null,
    success: /success|verified|通过/i.test(document.body.innerText),
  };
})()
"""


# ------------------------------------------------------------------- geetest
def solve_geetest(driver: cdp.CDP, keep: bool = True) -> Dict:
    """Open the slide panel, solve it from the served PNGs, drag with real input."""
    os.makedirs(OUT, exist_ok=True)
    report: Dict = {"site": "geetest-v4"}

    # the demo page needs the "Slide CAPTCHA" tab, then the verify button
    driver.eval("""(() => {
      const b = [...document.querySelectorAll('button')].find(x => /Slide CAPTCHA/i.test(x.innerText||''));
      if (b) b.click(); return !!b; })()""")
    time.sleep(1.2)

    for attempt in range(3):
        probe = driver.eval(GEETEST_PROBE)
        if not probe.get("btn"):
            # panel closed → click the floating verify button with a real click
            v = probe.get("verify")
            if v:
                driver.click(v["x"] + v["w"] / 2, v["y"] + v["h"] / 2)
                time.sleep(2.0)
            probe = driver.eval(GEETEST_PROBE)
        if probe.get("btn") and probe.get("bgUrl"):
            break
        time.sleep(1.0)
    else:
        report["error"] = "slide panel never opened"
        return report

    bg_url = _css_url(probe["bgUrl"])
    slice_url = _css_url(probe["sliceUrl"])
    bg_path = _download(bg_url, os.path.join(OUT, "gt_bg.png"))
    slice_path = _download(slice_url, os.path.join(OUT, "gt_slice.png"))
    bg_img = Image.open(bg_path)
    slice_img = Image.open(slice_path)
    report.update({"bg": bg_url, "slice": slice_url, "bg_size": list(bg_img.size),
                   "slice_size": list(slice_img.size), "css_bg": probe["bg"]})

    # --- solve ------------------------------------------------------------
    solved = slider.solve_slider(background=bg_img, piece=slice_img)
    scale = probe["bg"]["w"] / float(bg_img.width)
    drag_css = float(solved["drag_distance"]) * scale
    report["solve"] = {
        "gap_x": solved.get("offset"),
        "piece_shape_dx": solved.get("piece_shape_dx"),
        "drag_img": solved.get("drag_distance"),
        "drag_css": round(drag_css, 2),
        "confidence": solved.get("confidence"),
        "score": solved.get("score"),
    }
    if drag_css <= 1:
        report["error"] = "solver returned no usable offset"
        return report

    # --- drag with trusted input -----------------------------------------
    track = behavior.drag_track(drag_css, seed=SEED)
    btn = probe["btn"]
    x0 = btn["x"] + btn["w"] * 0.35
    y0 = btn["y"] + btn["h"] * 0.5
    played = driver.drag(x0, y0, track)
    report["track"] = {"played": played, "check": behavior.humanize_check(track)["metrics"]}
    time.sleep(0.4)
    outcome: Dict = {}
    for _ in range(24):  # up to ~6s
        time.sleep(0.25)
        outcome = driver.eval(GEETEST_OUTCOME)
        if outcome.get("err") or outcome.get("result") or not outcome.get("panelVisible"):
            break
        if outcome.get("tips") and "verifying" not in outcome["tips"].lower():
            time.sleep(0.6)
            outcome = driver.eval(GEETEST_OUTCOME)
            break
    report["outcome"] = outcome
    path = os.path.join(OUT, f"geetest_after_{int(time.time())}.png")
    if keep:
        driver.screenshot(path)
        report["screenshot"] = path
    report["passed"] = bool(
        (not outcome.get("err"))
        and (not outcome.get("panelVisible") or re_search_ok(outcome.get("tips", "")))
    )
    return report


def re_search_ok(tips: str) -> bool:
    return bool(tips) and not any(k in tips.lower() for k in ("try again", "error", "fail"))


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.geetest.com/en/adaptive-captcha-demo")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    global SEED
    SEED = a.seed

    info = cdp.launch_chrome(port=a.port, headless=a.headless)
    driver = cdp.CDP.connect(port=a.port, timeout=20)
    try:
        driver.navigate(a.url, wait=4.5)
        report = solve_geetest(driver)
    finally:
        driver.close()
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: report.get(k) for k in ("passed", "solve", "outcome", "error")},
                         ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
