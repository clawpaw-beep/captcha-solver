"""Human-like pointer trajectories for captcha challenges (stdlib + numpy only).

Why this module exists: on the Aliyun PUZZLE slider the *distance* was right
(233 px vs a true ~235) but the drag was driven as 28 steps of a constant 12 ms
with zero jitter, and the challenge answered "Verification failed". The exact
same distance driven by an easing curve + sub-pixel jitter + 8-26 ms variable
step timing passed. Position is not the whole signal - the trajectory itself is
scored, so scripted dragging has to carry human kinematics:

    drag_track(185, seed=7)   -> {"points": [...], "durations_ms": [...], ...}
    click_track([[120, 44]])  -> {"clicks": [...], "total_ms": ...}
    humanize_check(track)     -> {"ok": ..., "issues": [...], "metrics": {...}}

All randomness comes from a seeded ``numpy.random.Generator``, so a given seed
reproduces the same gesture byte for byte.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

SPEED_MIN_MS = 8
SPEED_MAX_MS = 26
HOLD_RANGE_MS = (60, 140)
MOVE_RANGE_MS = (150, 400)
OFFSET_MAX_PX = 1.2
Y_MAX_PX = 0.8

EASINGS = {
    "ease_out_quart": lambda u: 1.0 - (1.0 - u) ** 4,
    "ease_out_cubic": lambda u: 1.0 - (1.0 - u) ** 3,
    "ease_in_out_sine": lambda u: -(np.cos(np.pi * u) - 1.0) / 2.0,
}
DEFAULT_EASING = "ease_out_quart"


def _f(value: Any, digits: int = 3) -> float:
    """Round to `digits` and return a plain float (JSON friendly)."""
    return float(round(float(value), digits))


def _resolve_steps(distance: float, steps: Optional[int]) -> int:
    if steps is None:
        n = max(12, min(40, round(distance / 8.0)))
    else:
        try:
            n = int(steps)
        except (TypeError, ValueError):
            raise ValueError(f"steps must be an integer, got {steps!r}") from None
    if n < 3:
        raise ValueError(f"steps must be >= 3, got {n}")
    return min(200, n)


def _timings(
    weights: np.ndarray,
    rng: np.random.Generator,
    total_ms: Optional[int],
    lo: int = SPEED_MIN_MS,
    hi: int = SPEED_MAX_MS,
) -> np.ndarray:
    """Turn relative step weights into integer ms inside [lo, hi].

    The weights already carry the front-fast / tail-slow shape; here we add
    per-step noise, then (optionally) rescale so the gesture lasts `total_ms` -
    clipped to the human band, so an impossible request lands on the nearest
    realisable total and the caller sees the truth in `profile`.
    """
    w = weights * rng.uniform(0.84, 1.16, size=weights.size)
    w = np.clip(w, 1e-6, None)
    target = None if total_ms is None else int(total_ms)
    scale = (15.0 / float(w.mean())) if target is None else (target / float(w.sum()))
    # the +-0.9 ms draw breaks the integer ties the shape alone would produce
    v = np.round(np.clip(w * scale, lo, hi) + rng.uniform(-0.9, 0.9, size=w.size)).astype(int)
    if target is not None:
        # spend the residual one ms at a time so a feasible request lands exactly
        for _ in range(64):
            diff = target - int(v.sum())
            if diff == 0:
                break
            if diff > 0:
                idx = np.flatnonzero(v < hi)
                if idx.size == 0:
                    break
                order = idx[np.argsort(v[idx], kind="stable")]
                v[order[: min(idx.size, diff)]] += 1
            else:
                idx = np.flatnonzero(v > lo)
                if idx.size == 0:
                    break
                order = idx[np.argsort(-v[idx], kind="stable")]
                v[order[: min(idx.size, -diff)]] -= 1
    return np.clip(v, lo, hi).astype(int)


def _drift_y(rng: np.random.Generator, u: np.ndarray) -> np.ndarray:
    """Slow, smooth hand tremor in [-0.8, 0.8] px - two low frequencies, no white noise."""
    f1 = float(rng.uniform(0.55, 1.35))
    f2 = float(rng.uniform(2.1, 3.0))
    p1 = float(rng.uniform(0.0, 2.0 * math.pi))
    p2 = float(rng.uniform(0.0, 2.0 * math.pi))
    y = 0.65 * np.sin(2.0 * math.pi * f1 * u + p1) + 0.35 * np.sin(2.0 * math.pi * f2 * u + p2)
    y = y - y[0]
    peak = float(np.abs(y).max())
    if peak > 1e-9:
        y = y / peak * float(rng.uniform(0.45, Y_MAX_PX))
    return np.clip(y, -Y_MAX_PX, Y_MAX_PX)


def drag_track(
    distance: float,
    *,
    steps: Optional[int] = None,
    duration_ms: Optional[int] = None,
    easing: str = DEFAULT_EASING,
    jitter: float = 0.6,
    overshoot: bool = True,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a humanised slider drag of `distance` CSS pixels.

    Returns points relative to the press position (`x` cumulative, `y` tremor),
    one per-step dwell time, the total gesture length and the generation profile.
    The x axis is strictly non-decreasing up to the overshoot peak and settles
    back to exactly `distance` on the final point.

    ``durations_ms[i]`` is the dwell that ends on ``points[i]`` (index 0 is the
    press before the first move) and ``total_ms`` is their sum, so it includes
    the settle on the landing point. Feed the pairs to
    ``Input.dispatchMouseEvent`` (mouseMoved + sleep(durations_ms[i])).
    """
    try:
        d = float(distance)
    except (TypeError, ValueError):
        raise ValueError(f"distance must be a number, got {distance!r}") from None
    if not math.isfinite(d) or d <= 0:
        raise ValueError("distance must be a positive number of CSS pixels")
    if easing not in EASINGS:
        raise ValueError(f"unknown easing {easing!r}; choose one of {sorted(EASINGS)}")
    try:
        jit = float(jitter)
    except (TypeError, ValueError):
        raise ValueError(f"jitter must be a number, got {jitter!r}") from None
    if not math.isfinite(jit) or jit < 0:
        raise ValueError("jitter must be >= 0 pixels")

    n = _resolve_steps(d, steps)
    rng = np.random.default_rng(seed)

    # Tail = 1 overshoot peak + 2..3 rebound points, only when it fits.
    tail = 0
    if bool(overshoot) and n >= 8:
        tail = 1 + int(rng.integers(2, 4))
    m = max(3, n - tail)
    tail = n - m

    # --- main segment: easing shape + sub-pixel noise on each increment ------
    u = np.linspace(0.0, 1.0, m)
    p = EASINGS[easing](u)
    p[0], p[-1] = 0.0, 1.0
    deltas = np.diff(p) * d + rng.uniform(-jit, jit, size=m - 1)
    deltas = np.clip(deltas, 0.0, None)
    total = float(deltas.sum())
    deltas = np.full(m - 1, d / (m - 1)) if total <= 1e-9 else deltas * (d / total)
    x_main = np.concatenate([[0.0], np.cumsum(deltas)])
    x_main[-1] = d

    # one continuous tremor over the whole gesture, so the rebound tail does not
    # jump when it takes over from the main segment
    y_all = _drift_y(rng, np.linspace(0.0, 1.0, n))
    y_main = y_all[:m]
    y_tail = y_all[m:]

    # --- overshoot + rebound tail -------------------------------------------
    ox = 0.0
    x_tail = np.empty(0)
    if tail:
        ox = float(rng.uniform(0.5, 2.5))
        reb = max(1, tail - 1)  # rebound points after the peak
        frac = np.linspace(0.0, 1.0, reb + 1)[1:]  # ends at exactly 1.0 -> distance
        x_tail = np.concatenate([[d + ox], (d + ox) - ox * frac])
        x_tail = x_tail[:tail]
        if x_tail.size > 2:
            x_tail[1:-1] = x_tail[1:-1] + rng.uniform(-0.15, 0.15, size=x_tail.size - 2)
        x_tail = np.minimum.accumulate(x_tail)
        x_tail[-1] = d

    xs = np.concatenate([x_main, x_tail]) if tail else x_main
    ys = np.concatenate([y_main, y_tail]) if tail else y_main
    # only the main segment is forced non-decreasing; the tail must rebound
    xs = np.concatenate([np.maximum.accumulate(xs[:m]), xs[m:]])
    xs[0] = 0.0
    xs[-1] = d
    ys[0] = 0.0

    # --- step timing: front fast, tail slow, jittered, 8..26 ms -------------
    uu = np.linspace(0.0, 1.0, n)
    weights = 0.68 + 0.85 * np.power(uu, 1.6)
    if tail:
        weights[-tail:] *= 0.86  # the rebound flick is quicker, then settles
    durs = _timings(weights, rng, None if duration_ms is None else int(duration_ms))
    durs = np.clip(durs, SPEED_MIN_MS, SPEED_MAX_MS).astype(int)

    t = np.concatenate([[0], np.cumsum(durs)[:-1]]).astype(int)
    points = [
        {"x": _f(xs[i]), "y": _f(ys[i]), "t": int(t[i])}
        for i in range(n)
    ]
    total_ms = int(durs.sum())

    return {
        "distance": _f(d),
        "points": points,
        "durations_ms": [int(v) for v in durs],
        "total_ms": total_ms,
        "profile": {
            "easing": easing,
            "steps": n,
            "speed_range_ms": [SPEED_MIN_MS, SPEED_MAX_MS],
            "step_range_ms": [int(durs.min()), int(durs.max())],
            "overshoot_px": _f(ox, 3) if tail else 0.0,
            "jitter": _f(jit, 3),
            "y_drift_px": _f(float(np.abs(ys).max()), 3),
            "seed": None if seed is None else int(seed),
            "duration_ms_requested": None if duration_ms is None else int(duration_ms),
            "endpoint_exact": bool(abs(float(points[-1]["x"]) - d) < 1e-9),
        },
    }


def _normalise_points(points: Sequence[Any]) -> List[tuple]:
    if points is None or (isinstance(points, (list, tuple)) and len(points) == 0):
        raise ValueError("points must be a non-empty list of [x, y] pairs")
    if not isinstance(points, (list, tuple)):
        raise ValueError("points must be a non-empty list of [x, y] pairs")
    out: List[tuple] = []
    for idx, item in enumerate(points):
        if isinstance(item, dict):
            x, y = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            x, y = item[0], item[1]
        else:
            raise ValueError(f"points[{idx}] must be [x, y] or {{'x': .., 'y': ..}}")
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(f"points[{idx}] must contain numbers, got {item!r}") from None
        if not (math.isfinite(fx) and math.isfinite(fy)):
            raise ValueError(f"points[{idx}] must be finite, got {item!r}")
        out.append((fx, fy))
    return out


def click_track(points: Sequence[Any], *, seed: Optional[int] = None) -> Dict[str, Any]:
    """Build a humanised click sequence for a click-order captcha.

    `points` are the ordered targets in CSS/screen pixels. Each click gets a
    60-140 ms press, a distance-scaled 150-400 ms travel, a +/-1.2 px landing
    offset, and a two-stage micro-move over the final 2 px.
    """
    pts = _normalise_points(points)
    rng = np.random.default_rng(seed)

    clicks: List[Dict[str, Any]] = []
    total_ms = 0
    prev: Optional[tuple] = None
    for tx, ty in pts:
        if prev is None:
            travel = float(rng.uniform(40.0, 120.0))  # approach from wherever the pointer sat
            ux, uy = float(math.cos(rng.uniform(0, 2 * math.pi))), float(math.sin(rng.uniform(0, 2 * math.pi)))
        else:
            dx, dy = tx - prev[0], ty - prev[1]
            travel = float(math.hypot(dx, dy))
            ux, uy = (dx / travel, dy / travel) if travel > 1e-9 else (1.0, 0.0)
        move_ms = int(round(np.clip(MOVE_RANGE_MS[0] + travel * 1.6 + float(rng.uniform(-16, 16)), *MOVE_RANGE_MS)))
        hold_ms = int(round(float(rng.uniform(*HOLD_RANGE_MS))))

        off = np.clip(rng.normal(0.0, OFFSET_MAX_PX / 2.2, size=2), -OFFSET_MAX_PX, OFFSET_MAX_PX)
        lx, ly = tx + float(off[0]), ty + float(off[1])

        # last 2 px resolved as two micro-steps so the landing is not a teleport
        micro = []
        for back in (2.0, 1.0):
            micro.append(
                {
                    "x": _f(lx - ux * back),
                    "y": _f(ly - uy * back),
                    "move_ms": int(round(np.clip(move_ms * (0.20 if back == 2.0 else 0.10), 30, 90))),
                }
            )

        clicks.append(
            {
                "x": _f(lx),
                "y": _f(ly),
                "target": [_f(tx), _f(ty)],
                "hold_ms": hold_ms,
                "move_ms": move_ms,
                "offset": [_f(float(off[0]), 3), _f(float(off[1]), 3)],
                "micro": micro,
            }
        )
        total_ms += move_ms + hold_ms
        prev = (lx, ly)

    return {
        "clicks": clicks,
        "total_ms": int(total_ms),
        "profile": {
            "seed": None if seed is None else int(seed),
            "count": len(clicks),
            "hold_range_ms": [HOLD_RANGE_MS[0], HOLD_RANGE_MS[1]],
            "move_range_ms": [MOVE_RANGE_MS[0], MOVE_RANGE_MS[1]],
            "offset_max_px": OFFSET_MAX_PX,
            "micro_move_px": 2.0,
        },
    }


def humanize_check(track: Dict[str, Any]) -> Dict[str, Any]:
    """Score a generated (or hand-written) track for machine-obvious patterns."""
    if not isinstance(track, dict):
        raise ValueError("track must be a dict from drag_track()/click_track()")

    issues: List[str] = []
    pts = track.get("points") or []
    clicks = track.get("clicks") or []
    durs_in = track.get("durations_ms") or []

    kind = "drag" if pts else ("click" if clicks else "unknown")
    x_monotonic = True
    duration_ms: List[int] = []

    if kind == "drag":
        xs = [float(p.get("x", 0.0)) for p in pts]
        peak = int(np.argmax(xs)) if xs else 0
        rising = all(xs[i + 1] >= xs[i] - 1e-6 for i in range(peak))
        settling = all(xs[i + 1] <= xs[i] + 1e-6 for i in range(peak, len(xs) - 1))
        x_monotonic = bool(rising and settling)
        if not x_monotonic:
            issues.append("x is not monotonic up to the peak, or the rebound does not settle")
        if xs and abs(xs[-1]) <= 1e-9:
            issues.append("last point has x == 0")
        duration_ms = [int(round(float(v))) for v in durs_in]
        if len(duration_ms) != len(pts):
            issues.append(f"durations_ms length {len(duration_ms)} != points {len(pts)}")
    elif kind == "click":
        duration_ms = [int(c.get("hold_ms", 0)) + int(c.get("move_ms", 0)) for c in clicks]
        for idx, c in enumerate(clicks):
            hold = int(c.get("hold_ms", 0))
            move = int(c.get("move_ms", 0))
            if not HOLD_RANGE_MS[0] <= hold <= HOLD_RANGE_MS[1]:
                issues.append(f"clicks[{idx}].hold_ms {hold} outside {HOLD_RANGE_MS[0]}-{HOLD_RANGE_MS[1]}")
            if not MOVE_RANGE_MS[0] <= move <= MOVE_RANGE_MS[1]:
                issues.append(f"clicks[{idx}].move_ms {move} outside {MOVE_RANGE_MS[0]}-{MOVE_RANGE_MS[1]}")
            off = c.get("offset") or [0.0, 0.0]
            if max(abs(float(off[0])), abs(float(off[1]))) > OFFSET_MAX_PX + 1e-6:
                issues.append(f"clicks[{idx}].offset beyond +/-{OFFSET_MAX_PX} px")
    else:
        issues.append("track has neither points nor clicks")

    steps = len(pts) if kind == "drag" else len(clicks)
    mean_ms = float(np.mean(duration_ms)) if duration_ms else 0.0
    std_ms = float(np.std(duration_ms)) if duration_ms else 0.0
    duration_cv = (std_ms / mean_ms) if mean_ms > 0 else 0.0

    uniform_risk = False
    if len(duration_ms) > 1:
        uniform_risk = len(set(duration_ms)) <= 1 or duration_cv < 0.05
        if uniform_risk:
            issues.append(
                f"uniform step timing (mean {mean_ms:.1f} ms, cv {duration_cv:.3f}) - machine-like drag"
            )
    if kind == "drag" and steps < 12:
        issues.append(f"only {steps} points; human drags of a slider emit ~12+ move events")
    if kind == "drag" and duration_ms and (mean_ms < SPEED_MIN_MS * 0.75 or mean_ms > 60.0):
        issues.append(f"mean step time {mean_ms:.1f} ms is outside the human band")

    return {
        "ok": not issues,
        "issues": issues,
        "metrics": {
            "kind": kind,
            "steps": steps,
            "x_monotonic": bool(x_monotonic),
            "duration_cv": _f(duration_cv, 3),
            "mean_ms": _f(mean_ms, 1),
            "std_ms": _f(std_ms, 2),
            "min_ms": int(min(duration_ms)) if duration_ms else 0,
            "max_ms": int(max(duration_ms)) if duration_ms else 0,
            "distinct_durations": len(set(duration_ms)),
            "uniform_risk": bool(uniform_risk),
            "total_ms": int(track.get("total_ms", 0) or 0),
        },
    }


def build_track(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a JSON request ({distance|points, ...}) and attach the self-check.

    Shared by the HTTP `/track` route and the MCP `captcha_trajectory` tool so
    both surfaces accept exactly the same body.
    """
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    seed = request.get("seed")
    seed = None if seed is None else int(seed)

    if request.get("points") is not None:
        track = click_track(request["points"], seed=seed)
    elif request.get("distance") is not None:
        easing = request.get("easing") or DEFAULT_EASING
        steps = request.get("steps")
        duration_ms = request.get("duration_ms")
        jitter = request.get("jitter", 0.6)
        track = drag_track(
            float(request["distance"]),
            steps=None if steps is None else int(steps),
            duration_ms=None if duration_ms is None else int(duration_ms),
            easing=str(easing),
            jitter=float(jitter),
            overshoot=bool(request.get("overshoot", True)),
            seed=seed,
        )
    else:
        raise ValueError("distance (slider drag) or points (click order) is required")

    track["humanize_check"] = humanize_check(track)
    return track
