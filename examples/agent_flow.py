"""Reference loop: browser -> captcha_solver -> submit, with escalating retries.

This is the shape an agent should follow. It talks to the HTTP API so it stays
framework-agnostic; the same calls exist as MCP tools (mcp__captcha__*).

    python examples/agent_flow.py --base http://127.0.0.1:8790 --image /tmp/cap.png
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from typing import Any, Dict, List, Optional

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def post(base: str, path: str, payload: Dict[str, Any], token: str = "") -> Dict[str, Any]:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA, **({"X-Api-Token": token} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def human_drag(page, handle_selector: str, distance: int, steps: Optional[int] = None) -> None:
    """Drag a slider handle like a person: fast start, ease-out, tiny settle-back.

    `page` is a Playwright/Selenium page; the body only uses mouse primitives so
    the same code works with either binding.
    """
    box = page.locator(handle_selector).bounding_box()
    x0, y0 = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    steps = steps or random.randint(24, 40)

    def ease(t: float) -> float:
        return 1 - (1 - t) ** 3

    page.mouse.move(x0, y0)
    page.mouse.down()
    prev = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        target = distance * ease(t) + random.uniform(-0.6, 0.6)
        page.mouse.move(x0 + target, y0 + random.uniform(-1.2, 1.2), steps=1)
        prev = target
        time.sleep(random.uniform(0.012, 0.035))
    # settle back for over-shoot, then release
    if prev > distance:
        page.mouse.move(x0 + distance, y0, steps=2)
    time.sleep(random.uniform(0.08, 0.2))
    page.mouse.up()


def solve_text(base: str, path: str, charset: str, length: Optional[int], token: str = "") -> Dict[str, Any]:
    res = post(base, "/solve", {"image_path": path, "kind": "text", "charset": charset, "length": length}, token)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--image", required=True)
    ap.add_argument("--charset", default="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    ap.add_argument("--length", type=int, default=None)
    ap.add_argument("--token", default="")
    a = ap.parse_args()

    res = solve_text(a.base, a.image, a.charset, a.length, a.token)
    print(json.dumps({k: res.get(k) for k in ("kind", "answer", "confidence", "recommendation")}, ensure_ascii=False))
    if float(res.get("confidence", 0)) < 55:
        print("low confidence -> refresh the captcha instead of submitting")
        return 1
    # submit: fill the input field and click, then verify the page accepted it
    # page.fill("#captcha-input", res["answer"]); page.click("#submit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
