"""Aliyun PUZZLE: the slider-to-piece transmission curve, and a closed loop.

Why this module exists
----------------------
Every earlier attempt at an Aliyun slider dragged the *handle* by the gap width
and assumed the piece followed 1:1. It does not. Measured on
business.uniagency.net (2026-11-01), with the panel's own
`#aliyunCaptcha-puzzle` style.left as ground truth:

    slider.left   50    170    204    219    240
    puzzle.left  12.72 115.68 163.44 187.12 222.96

A quadratic fits all of it to within a pixel:

    puzzle = A·slider² + B·slider        A = 0.003550, B = 0.07692

That is why dragging `gap` px never worked: at gap≈200 the piece only travels
~160px, i.e. it stops ~80% of the way and the challenge answers
"验证失败，请重试".  (The piece also stops at 260, so the whole travel range is
used on wide gaps.)

Reading `#aliyunCaptcha-puzzle`'s own `left` is a much better feedback signal
than any assumption: it is what the page actually renders, so a few correction
steps land sub-pixel accurate.

Status
------
Sub-pixel placement (0.01px measured) is **still rejected** by the server for
scripted input — the remaining judgement lives in behaviour data that a
DevTools-injected gesture does not reproduce. So treat this module as the
geometric half of the problem, solved and reusable, not as a guaranteed pass.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

# measured on business.uniagency.net, 2026-11-01 (three independent fits agreed)
CURVE_A = 0.003550
CURVE_B = 0.07692
PUZZLE_MAX = 260.0  # piece travel saturates here (track width - handle width)


def puzzle_left(slider_left: float, a: float = CURVE_A, b: float = CURVE_B,
                cap: float = PUZZLE_MAX) -> float:
    """Where `#aliyunCaptcha-puzzle` ends up for a given handle offset."""
    s = max(0.0, min(cap, float(slider_left)))
    return min(cap, a * s * s + b * s)


def slider_for_puzzle(target: float, a: float = CURVE_A, b: float = CURVE_B,
                      cap: float = PUZZLE_MAX) -> float:
    """Handle offset that puts the piece at `target` (inverse of the curve)."""
    t = max(0.0, float(target))
    if abs(a) < 1e-9:
        s = t / b if b else t
    else:
        disc = b * b + 4 * a * t
        s = (-b + math.sqrt(max(disc, 0.0))) / (2 * a)
    return max(0.0, min(cap, s))


def local_slope(slider_left: float, a: float = CURVE_A, b: float = CURVE_B) -> float:
    """d(puzzle)/d(slider) — used to size a correction step from a pixel error."""
    return max(0.20, 2 * a * max(slider_left, 1.0) + b)


def plan(target_puzzle: float) -> Dict:
    """What to drag, and what to expect at the end."""
    s = slider_for_puzzle(target_puzzle)
    return {
        "target_puzzle": round(float(target_puzzle), 2),
        "slider": round(s, 2),
        "expected_puzzle": round(puzzle_left(s), 2),
        "slope_at_end": round(local_slope(s), 3),
        "clamped": abs(puzzle_left(s) - target_puzzle) > 0.5,
    }


def drag_aliyun(driver, x0: float, y0: float, target_puzzle: float,
                read_state, *, steps: int = 30, correct: int = 2,
                sleep=None, log: Optional[list] = None) -> Dict:
    """Drag the handle so the piece lands on `target_puzzle`, with correction.

    `driver` needs `mouse(kind, x, y, button=…, buttons=…, force=…)`;
    `read_state()` must return at least `{"puzzleLeft": float, "sliderLeft": float}`.
    Two correction reads are enough in practice (measured: 1.4px → 0.01px).
    """
    import time as _time

    sleep = sleep or (lambda s: _time.sleep(s))
    plan_ = plan(target_puzzle)
    need = plan_["slider"]

    driver.mouse("mouseMoved", x0 - 9, y0 + 6, buttons=0)
    sleep(0.14)
    driver.mouse("mouseMoved", x0, y0, buttons=0)
    sleep(0.18)
    driver.mouse("mousePressed", x0, y0, button="left", buttons=1, click_count=1, force=0.5)
    sleep(0.09)

    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)
        driver.mouse("mouseMoved", x0 + need * eased, y0 + (0.4 if i % 3 else -0.3),
                     button="left", buttons=1, force=0.5)
        sleep(0.009 + (0.011 if t > 0.75 else 0.006))
    sleep(0.16)

    report = {"planned": plan_, "corrections": []}
    for k in range(max(0, correct)):
        st = read_state()
        err = target_puzzle - float(st.get("puzzleLeft", 0.0))
        if abs(err) <= 0.35:
            break
        need += err / local_slope(float(st.get("sliderLeft", need)))
        need = max(0.0, min(PUZZLE_MAX, need))
        driver.mouse("mouseMoved", x0 + need, y0 + 0.1, button="left", buttons=1, force=0.5)
        sleep(0.26)
        report["corrections"].append({"err": round(err, 2), "slider": round(need, 2)})

    st = read_state()
    report.update({
        "slider_used": round(need, 2),
        "puzzle_reached": round(float(st.get("puzzleLeft", 0.0)), 2),
        "final_error": round(abs(target_puzzle - float(st.get("puzzleLeft", 0.0))), 3),
    })
    driver.mouse("mouseReleased", x0 + need, y0, button="left", buttons=0, click_count=1, force=0.0)
    if log is not None:
        log.append(report)
    return report
