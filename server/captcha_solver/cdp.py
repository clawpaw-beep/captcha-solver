"""Minimal Chrome DevTools Protocol driver — real (trusted) input, not synthetic.

Why this exists: `PointerEvent`/`MouseEvent` dispatched from page JavaScript carry
`isTrusted: false`, and captcha vendors (GeeTest v4, ByteDance cap, AliCloud) check
that flag. Everything else — coordinates, timing, easing — can be perfect and the
verification still fails. `Input.dispatchMouseEvent` goes through the browser's own
input pipeline, so `isTrusted` is true and the page cannot tell it from a hand.

Only the handful of commands a captcha loop needs are implemented; the socket is
plain `websocket-client`, no Playwright / Puppeteer dependency.

    driver = CDP.connect(port=9222)          # attach to an existing Chrome
    driver.navigate("https://example.com")
    driver.eval("document.title")
    driver.drag(x, y, behavior.drag_track(137))
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


class CDPError(RuntimeError):
    pass


def _http_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


class CDP:
    """One page target, one websocket."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        import websocket  # websocket-client

        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=64 * 1024 * 1024)
        self._id = 0
        self._events: List[dict] = []
        for domain in ("Page", "Runtime", "Input"):
            try:
                self.send(f"{domain}.enable")
            except CDPError:
                pass

    # ---------------------------------------------------------------- plumbing
    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self._events.append(msg)
        raise CDPError(f"{method}: timeout after {timeout}s")

    def eval(self, expression: str, await_promise: bool = False) -> Any:
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
                "allowUnsafeEvalBlockedByCSP": False,
            },
        )
        if res.get("exceptionDetails"):
            raise CDPError(f"JS error: {res['exceptionDetails'].get('text')} :: {expression[:120]}")
        return res.get("result", {}).get("value")

    def drain_events(self, method: Optional[str] = None) -> List[dict]:
        out = [e for e in self._events if method is None or e.get("method") == method]
        if method is None:
            self._events.clear()
        else:
            self._events = [e for e in self._events if e.get("method") != method]
        return out

    # ----------------------------------------------------------------- control
    def navigate(self, url: str, wait: float = 3.0) -> None:
        self.send("Page.navigate", {"url": url})
        time.sleep(wait)

    def wait_for(self, expression: str, timeout: float = 15.0, interval: float = 0.25) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return True
            except CDPError:
                pass
            time.sleep(interval)
        return False

    # ------------------------------------------------------------------- input
    def mouse(self, kind: str, x: float, y: float, button: str = "left", buttons: int = 0,
              click_count: int = 0, modifiers: int = 0) -> None:
        self.send(
            "Input.dispatchMouseEvent",
            {
                "type": kind,
                "x": round(float(x), 2),
                "y": round(float(y), 2),
                "button": button,
                "buttons": buttons,
                "clickCount": click_count,
                "modifiers": modifiers,
                "pointerType": "mouse",
            },
        )

    def click(self, x: float, y: float, hold_ms: float = 80.0) -> None:
        self.mouse("mouseMoved", x, y, buttons=0)
        time.sleep(0.03)
        self.mouse("mousePressed", x, y, button="left", buttons=1, click_count=1)
        time.sleep(hold_ms / 1000.0)
        self.mouse("mouseReleased", x, y, button="left", buttons=0, click_count=1)

    def drag(self, x0: float, y0: float, track: dict, settle_ms: float = 120.0) -> dict:
        """Play a `behavior.drag_track` result through the real input pipeline."""
        pts = track.get("points") or []
        durations = track.get("durations_ms") or []
        if not pts:
            raise ValueError("track has no points")
        # approach the handle like a hand would, then press
        self.mouse("mouseMoved", x0 - 7, y0 + 4, buttons=0)
        time.sleep(0.035)
        self.mouse("mouseMoved", x0, y0, buttons=0)
        time.sleep(0.045)
        self.mouse("mousePressed", x0, y0, button="left", buttons=1, click_count=1)
        time.sleep(0.06)
        last = (x0, y0)
        def _xy(p):
            if isinstance(p, dict):
                return float(p.get("x", 0.0)), float(p.get("y", 0.0))
            return float(p[0]), float(p[1])

        for i in range(1, len(pts)):
            dx, dy = _xy(pts[i])
            last = (x0 + dx, y0 + dy)
            self.mouse("mouseMoved", last[0], last[1], button="left", buttons=1)
            dt = durations[i] if i < len(durations) else 14
            time.sleep(max(0.004, dt / 1000.0))
        time.sleep(settle_ms / 1000.0)
        self.mouse("mouseReleased", last[0], last[1], button="left", buttons=0, click_count=1)
        return {"from": [round(x0, 2), round(y0, 2)], "to": [round(last[0], 2), round(last[1], 2)],
                "steps": len(pts), "total_ms": track.get("total_ms")}

    # ----------------------------------------------------------------- screens
    def screenshot(self, path: str) -> str:
        import base64

        res = self.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(res["data"])
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- attaching
    @classmethod
    def connect(cls, port: int = 9222, url_contains: Optional[str] = None, timeout: float = 10.0) -> "CDP":
        """Attach to the first page target (optionally matching a URL fragment)."""
        deadline = time.time() + timeout
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                targets = _http_json(f"http://127.0.0.1:{port}/json/list")
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if url_contains:
                    pages = [t for t in pages if url_contains in (t.get("url") or "")] or pages
                if pages:
                    return cls(pages[0]["webSocketDebuggerUrl"])
            except Exception as exc:  # browser still starting
                last = exc
            time.sleep(0.3)
        raise CDPError(f"no debuggable page on port {port}: {last}")

    @classmethod
    def new_tab(cls, port: int = 9222, url: str = "about:blank") -> "CDP":
        target = _http_json(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?&=')}")
        if "webSocketDebuggerUrl" not in target:
            raise CDPError(f"could not open tab: {target}")
        return cls(target["webSocketDebuggerUrl"])


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def launch_chrome(
    port: int = 9222,
    user_data_dir: str = "/tmp/captcha-cdp-profile",
    headless: bool = False,
    extra_args: Optional[Sequence[str]] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Start a Chrome with a TCP debug port (the MCP-launched one only has a pipe)."""
    if _port_open(port):
        return {"launched": False, "port": port, "reason": "already listening"}
    chrome = next((p for p in CHROME_CANDIDATES if p and os.path.exists(p)), None)
    if not chrome:
        raise CDPError("no Chrome binary found")
    os.makedirs(user_data_dir, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--window-size=1280,900",
        "--window-position=40,40",
    ]
    if headless:
        args.append("--headless=new")
    args.extend(list(extra_args or []))
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            try:
                ver = _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
                return {"launched": True, "pid": proc.pid, "port": port,
                        "browser": ver.get("Browser"), "webSocketDebuggerUrl": ver.get("webSocketDebuggerUrl")}
            except Exception:
                pass
        time.sleep(0.3)
    raise CDPError(f"chrome did not open debug port {port} within {timeout}s")
