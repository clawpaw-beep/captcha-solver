"""OS-level mouse driver — real `CGEvent`s, not protocol injection.

Every browser-level route (`JS PointerEvent`, CDP `Input.dispatchMouseEvent`,
`Input.dispatchTouchEvent`) produces an event the page can still profile: the
pointer sampling cadence, the missing hardware fields, the fact that the whole
gesture arrives through one channel. Aliyun silently resets the challenge on all
of them while a human hand passes on the same page — the difference is that a
hand produces `NSEvent`s from the window server.

This module posts events with `CGEventPost(kCGHIDEventTap, …)`, so they travel
through the same path as physical mouse movement.

Requires macOS Accessibility permission for the posting process (Terminal / the
Python binary). Without it `CGEventPost` silently does nothing — `probe()` tells
you which side of that you are on.

    m = OSMouse()
    m.probe()                       # raises if permission is missing
    m.move(x, y)
    m.drag(screen_x, screen_y, behavior.drag_track(210))
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

# the venv is isolated; pyobjc lives in the Homebrew interpreter's site-packages
for _p in ("/opt/homebrew/lib/python3.14/site-packages", "/opt/homebrew/lib/python3/site-packages"):
    if _p not in sys.path:
        sys.path.append(_p)

try:
    import Quartz  # type: ignore
except ImportError as exc:  # pragma: no cover - platform guard
    Quartz = None
    _import_error = exc
else:
    _import_error = None


class OSMouseError(RuntimeError):
    pass


def _require():
    if Quartz is None:
        raise OSMouseError(f"pyobjc Quartz unavailable: {_import_error}")


class OSMouse:
    """Thin, stateful wrapper over CGEvent mouse posts."""

    def __init__(self, tap: Optional[int] = None):
        _require()
        self.tap = Quartz.kCGHIDEventTap if tap is None else tap
        self._down = False

    # ------------------------------------------------------------------ state
    def location(self) -> Tuple[float, float]:
        _require()
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(p.x), float(p.y)

    def probe(self, timeout: float = 1.0) -> Dict:
        """Move the cursor 1px and back; proves Accessibility permission works."""
        _require()
        start = self.location()
        probe_to = (start[0] + 1.0, start[1])
        self._post(Quartz.kCGEventMouseMoved, probe_to)
        deadline = time.time() + timeout
        moved = False
        while time.time() < deadline:
            if abs(self.location()[0] - probe_to[0]) < 2.0:
                moved = True
                break
            time.sleep(0.02)
        self._post(Quartz.kCGEventMouseMoved, start)
        if not moved:
            raise OSMouseError(
                "CGEventPost had no effect — grant Accessibility permission to the "
                "posting process (System Settings → Privacy & Security → Accessibility)"
            )
        return {"ok": True, "origin": [round(start[0], 1), round(start[1], 1)]}

    # ------------------------------------------------------------------ posts
    def _post(self, kind: int, pos: Tuple[float, float], button: int = None) -> None:
        if button is None:
            button = Quartz.kCGMouseButtonLeft
        ev = Quartz.CGEventCreateMouseEvent(None, kind, pos, button)
        Quartz.CGEventPost(self.tap, ev)

    def move(self, x: float, y: float) -> None:
        # while the button is held this must be a *dragged* event: a plain
        # MouseMoved reaches the page as pointermove with buttons=0, and every
        # slider listener treats that as "not dragging"
        kind = Quartz.kCGEventLeftMouseDragged if self._down else Quartz.kCGEventMouseMoved
        self._post(kind, (x, y))

    def down(self, x: float, y: float) -> None:
        self._post(Quartz.kCGEventLeftMouseDown, (x, y))
        self._down = True

    def up(self, x: float, y: float) -> None:
        self._post(Quartz.kCGEventLeftMouseUp, (x, y))
        self._down = False

    # ------------------------------------------------------------- keyboard
    def type_text(self, text: str, gap_ms: float = 45.0, rng=None) -> None:
        """Type through real key events (unicode string, layout independent)."""
        import random as _random

        rng = rng or _random.Random(len(text))
        for ch in text:
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                Quartz.CGEventPost(self.tap, ev)
            # humans don't type at a metronome
            time.sleep(max(0.012, (gap_ms + rng.uniform(-18, 26)) / 1000.0))

    def click(self, x: float, y: float, hold_ms: float = 80.0) -> None:
        self.move(x, y)
        time.sleep(0.04)
        self.down(x, y)
        time.sleep(hold_ms / 1000.0)
        self.up(x, y)

    # ------------------------------------------------------------------- drag
    def drag(self, x0: float, y0: float, track: dict, hover_ms: Optional[float] = None,
             approach: bool = True, micro_pauses: bool = True) -> Dict:
        """Play a `behavior.drag_track` gesture as real system mouse events."""
        import random

        pts = track.get("points") or []
        durations = track.get("durations_ms") or []
        if not pts:
            raise ValueError("track has no points")
        rng = random.Random(int(track.get("distance", 0) or 0))

        def _xy(p):
            if isinstance(p, dict):
                return float(p.get("x", 0.0)), float(p.get("y", 0.0))
            return float(p[0]), float(p[1])

        if approach:
            for dx, dy, dt in ((-19, 13, 46), (-11, 7, 34), (-5, 3, 27), (-2, 1, 21)):
                self.move(x0 + dx, y0 + dy)
                time.sleep(dt / 1000.0)
        self.move(x0, y0)
        time.sleep((hover_ms if hover_ms is not None else rng.uniform(120, 260)) / 1000.0)

        self.down(x0, y0)
        time.sleep(rng.uniform(0.055, 0.095))

        n = len(pts)
        pause_at = {max(2, n // 3), max(3, (2 * n) // 3)} if micro_pauses else set()
        last = (x0, y0)
        for i in range(1, n):
            dx, dy = _xy(pts[i])
            last = (x0 + dx, y0 + dy)
            self.move(last[0], last[1])
            dt = durations[i] if i < len(durations) else 14
            time.sleep(max(0.004, dt / 1000.0))
            if i in pause_at and rng.random() < 0.7:
                time.sleep(rng.uniform(0.018, 0.05))
        time.sleep(0.09)
        self.up(last[0], last[1])
        return {"from": [round(x0, 1), round(y0, 1)], "to": [round(last[0], 1), round(last[1], 1)],
                "steps": n, "total_ms": track.get("total_ms"), "backend": "cgevent"}


# --------------------------------------------------------------------- helpers
ACTIVATE_CHROME = """
tell application "Google Chrome" to activate
delay 0.25
tell application "System Events" to tell process "Google Chrome"
    set frontmost to true
end tell
"""


def activate_chrome(timeout: float = 6.0) -> bool:
    """Bring Chrome to the front — CGEvent posts go to whatever is focused."""
    try:
        subprocess.run(["osascript", "-e", ACTIVATE_CHROME], check=False,
                       capture_output=True, timeout=timeout)
        return True
    except Exception:
        return False


def viewport_origin(js_eval) -> Dict:
    """Screen coordinates of the page viewport's top-left corner.

    Chrome's `screenX/screenY` is the *window* origin (title bar included), so the
    viewport starts `outerHeight - innerHeight` lower. CSS pixels and CGEvent
    points are 1:1 on macOS, so no DPR maths is needed.
    """
    info = js_eval(
        """(() => ({sx: window.screenX, sy: window.screenY,
                    outerH: window.outerHeight, innerH: window.innerHeight,
                    outerW: window.outerWidth, innerW: window.innerWidth,
                    dpr: devicePixelRatio}))()"""
    )
    chrome_h = max(0, info["outerH"] - info["innerH"])
    return {
        "x": info["sx"],
        "y": info["sy"] + chrome_h,
        "chrome_h": chrome_h,
        "dpr": info["dpr"],
    }
