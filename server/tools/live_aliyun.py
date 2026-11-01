"""钛动 UniAgency 登录：全自动过阿里云 PUZZLE 滑块。

这是唯一一条实测通过的路径（2026-11-01，业务接口返回 `verifyResult: true`、
页面显示「验证通过!」）。三个条件缺一不可，都是踩出来的：

1. **整段会话走系统级输入**（`CGEventPost` 鼠标 + 键盘）。只把最后那一拖换成系统
   鼠标不管用 —— 阿里云按会话行为画像判定，CDP 注入的会话太干净。
2. **传动曲线**：拖把手不等于拼图块位移，`puzzle = 0.00355·s² + 0.0769·s`。
   拖 gap 像素时拼图块只走 ~80%，停在缺口前面。
3. **亚像素闭环**：松手前拼图块必须与缺口差 <0.5px，否则阿里云连 `verifyCaptcha`
   都不发，直接静默重置（实测 0.27px 通过、1.56px 被本地丢弃）。

拖动节奏用录制的真人轨迹（`tools/human_track.py record` 产出的模板）按位移缩放、
时间同比拉长以保持原速；没有模板时退回 `behavior.drag_track` 也能到位，但未验证过
是否能过判定。

    python -m tools.live_aliyun --phone 173xxxx --password '***'
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.request
from typing import Dict, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

from captcha_solver import aliyun_slide, cdp, imageops as I, os_mouse, slider  # noqa: E402
from tools import human_track  # noqa: E402

OUT = os.path.join(HERE, "out", "live")
LOGIN_URL = "https://business.uniagency.net/login"

STATE_JS = """(() => {
  const e = document.querySelector('#aliyunCaptcha-img');
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  const pz = document.querySelector('#aliyunCaptcha-puzzle');
  const g = (x) => x ? (({x,y,width,height}) => ({x,y,w:width,h:height}))(x.getBoundingClientRect()) : null;
  return {img: g(e), slider: g(s),
          puzzleLeft: pz ? parseFloat(getComputedStyle(pz).left) || 0 : null,
          sliderLeft: s ? parseFloat(getComputedStyle(s).left) || 0 : null,
          natW: e ? e.naturalWidth : 0, natH: e ? e.naturalHeight : 0,
          text: (document.querySelector('#aliyunCaptcha-sliding-text') || {}).innerText || null};
})()"""

HOOK_JS = """(() => {
  window.__vr = [];
  const xo = XMLHttpRequest.prototype.open, xs = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m,u,...r){ this.__u=u; return xo.call(this,m,u,...r); };
  XMLHttpRequest.prototype.send = function(b){
    const rec = {url: String(this.__u).slice(0, 60)};
    if (/verifyCaptcha/.test(rec.url)) window.__vr.push(rec);
    this.addEventListener('load', () => {
      if (/verifyCaptcha/.test(rec.url)) rec.resp = String(this.responseText || '').slice(0, 400);
    });
    return xs.call(this, b);
  };
  return true;
})()"""


def _fetch_piece(d, path: str) -> Image.Image:
    src = d.eval("(() => (document.querySelector('#aliyunCaptcha-puzzle')||{}).src)()")
    if src.startswith("data:"):
        open(path, "wb").write(base64.b64decode(src.split(",", 1)[1]))
    else:
        req = urllib.request.Request(src, headers={"User-Agent": I.UA, "Referer": LOGIN_URL})
        with urllib.request.urlopen(req, timeout=20) as r, open(path, "wb") as fh:
            fh.write(r.read())
    return Image.open(path)


def run(phone: str, password: str, port: int = 9224, headless: bool = False,
        track_file: Optional[str] = None, max_puzzle_err: float = 0.08) -> Dict:
    os.makedirs(OUT, exist_ok=True)
    info = cdp.launch_chrome(port=port, user_data_dir="/tmp/uni-os-profile",
                             extra_args=["--window-position=120,120", "--window-size=1280,900"])
    d = cdp.CDP.connect(port=port, timeout=25)
    m = os_mouse.OSMouse()
    home = m.location()
    report: Dict = {"phone": phone[:3] + "****" + phone[-4:], "steps": []}
    try:
        m.probe()
        d.navigate(LOGIN_URL, wait=5.5)
        d.send("Page.bringToFront")
        time.sleep(1.2)
        d.eval(HOOK_JS)

        def box(js):
            return d.eval("""(() => { const e = %s; if (!e) return null; const r = e.getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height}; })()""" % js)

        def scr(b):
            o = os_mouse.viewport_origin(d.eval)
            return o["x"] + b["x"] + b["w"] / 2, o["y"] + b["y"] + b["h"] / 2

        def click(js, label):
            b = box(js)
            if not b or b["w"] < 2:
                raise RuntimeError(f"{label} not found")
            x, y = scr(b)
            m.click(x, y, hold_ms=90)
            time.sleep(0.35)
            report["steps"].append(label)

        # ---- 登录表单：真点击 + 真键盘
        click("document.querySelectorAll('input')[0]", "phone field")
        m.type_text(phone, gap_ms=52)
        time.sleep(0.2)
        click("document.querySelectorAll('input')[1]", "password field")
        m.type_text(password, gap_ms=48)
        time.sleep(0.25)
        d.eval("""(() => { const c = document.querySelector('input[type=checkbox]');
            if (c && !c.checked) c.click(); return true; })()""")
        time.sleep(0.25)
        click("[...document.querySelectorAll('button')].find(b => /登录/.test(b.innerText||''))", "login")
        time.sleep(3.2)
        report["after_login"] = d.eval("document.body.innerText.replace(/\\s+/g,' ').slice(-80)")

        # ---- 获取验证码 → 滑块
        for _ in range(2):
            click("[...document.querySelectorAll('button,.v-btn')].find(b => /获取验证码|重新获取/.test(b.innerText||''))",
                  "get-code")
            time.sleep(2.6)
            if d.eval("!!document.querySelector('#aliyunCaptcha-img')"):
                break
        p = d.eval(STATE_JS)
        if not (p.get("img") and p["img"]["w"] > 50):
            raise RuntimeError(f"slider panel not shown: {p.get('text')}")

        # ---- 求解
        res = d.send("Page.captureScreenshot", {"format": "png", "clip": {
            "x": p["img"]["x"], "y": p["img"]["y"], "width": p["img"]["w"], "height": p["img"]["h"], "scale": 1}})
        bg = Image.open(io.BytesIO(base64.b64decode(res["data"]))).resize((p["natW"], p["natH"]), Image.LANCZOS)
        piece = _fetch_piece(d, os.path.join(OUT, "_live_piece.png"))
        sol = slider.solve_slider(background=bg, piece=piece)
        disp = p["img"]["w"] / float(p["natW"])
        target = (sol["offset"] - sol.get("piece_shape_dx", 0)) * disp
        plan = aliyun_slide.plan(target)
        report["solve"] = {"gap": sol["offset"], "target": round(target, 1),
                           "slider": plan["slider"], "confidence": sol["confidence"]}

        # ---- 系统鼠标拖动 + 亚像素闭环
        s = p["slider"]
        sx, sy = scr(s)
        d.eval("window.__hv = null; document.addEventListener('mousemove', e => { window.__hv = e.target; }, {capture:true});")
        m.move(sx - 14, sy + 8)
        time.sleep(0.12)
        m.move(sx, sy)
        time.sleep(0.22)
        hover = d.eval("(window.__hv && window.__hv.id) || null")
        if hover != "aliyunCaptcha-sliding-slider":
            raise RuntimeError(f"cursor not on handle (hover={hover})")

        # 节奏是必要条件：用录制的真人轨迹，位移按曲线缩放、时间同比拉长保持原速
        rec = human_track.load()
        m.down(sx, sy)
        time.sleep(0.09)
        cur_x = sx
        if rec and rec.get("ok"):
            moves = [e for e in rec["events"] if e["kind"] == "move"]
            span = max(rec["stats"]["span_x"], 1.0)
            k = plan["slider"] / span
            x00, y00 = moves[0]["x"], moves[0]["y"]
            prev = 0.0
            for e in moves:
                dt = max(0.0, (e["t"] - prev) / 1000.0) * k
                if dt:
                    time.sleep(dt)
                prev = e["t"]
                cur_x = sx + (e["x"] - x00) * k
                m.move(cur_x, sy + (e["y"] - y00) * 0.6)
            report["pace"] = f"human template x{k:.3f} ({rec['stats']['duration_ms']}ms)"
        else:
            steps = 30
            for i in range(1, steps + 1):
                t = i / steps
                cur_x = sx + plan["slider"] * (t * t * (3 - 2 * t))
                m.move(cur_x, sy + (0.4 if i % 3 else -0.3))
                time.sleep(0.010 + (0.012 if t > 0.72 else 0.005))
            report["pace"] = "smoothstep (no human template — judgement not verified)"
        time.sleep(0.18)

        for i in range(10):
            st = d.eval(STATE_JS)
            cur = st.get("puzzleLeft") or 0.0
            err = target - cur
            report["steps"].append(f"close[{i}] err={err:+.2f}")
            if abs(err) <= max_puzzle_err:
                break
            # local_slope() 的输入是 slider 位置，不是 puzzle 位置（曾经混用导致越修越偏）
            cur_x += err / max(aliyun_slide.local_slope(st.get("sliderLeft") or 0.0), 0.4)
            m.move(cur_x, sy + 0.2)
            time.sleep(0.3)

        st = d.eval(STATE_JS)
        report["final_error"] = round(abs((st.get("puzzleLeft") or 0.0) - target), 3)
        m.up(cur_x, sy)

        # ---- 结果
        verdict = None
        for _ in range(9):
            time.sleep(0.85)
            vr = d.eval("window.__vr || []")
            if vr and vr[0].get("resp"):
                try:
                    payload = json.loads(vr[0]["resp"])
                    verdict = payload.get("result") or payload.get("data", {}).get("result")
                    report["verify_response"] = payload
                except Exception:
                    report["verify_response_raw"] = vr[0]["resp"]
                break
            txt = d.eval(STATE_JS).get("text")
            if txt and "通过" in str(txt):
                report["ui"] = txt
                break
        report["passed"] = bool(verdict and verdict.get("verifyResult") is True)
        report["screenshot"] = os.path.join(OUT, f"uni_auto_{int(time.time())}.png")
        d.screenshot(report["screenshot"])
    finally:
        try:
            m.move(home[0], home[1])
        except Exception:
            pass
        d.close()
    return report


def run_with_retry(phone: str, password: str, attempts: int = 4, port: int = 9224) -> Dict:
    """单次成功率约 1/5（实测 2/10），失败就刷新重试 —— 服务端对轨迹的整体评判有波动。"""
    tries = []
    for i in range(max(1, attempts)):
        rep = run(phone, password, port=port)
        tries.append({"attempt": i + 1, "passed": rep.get("passed"),
                      "final_error": rep.get("final_error"),
                      "verify": (rep.get("verify_response") or {}).get("data", {}).get("result")})
        print(f"  attempt {i+1}: passed={rep.get('passed')} err={rep.get('final_error')}")
        if rep.get("passed"):
            rep["attempts"] = tries
            return rep
        time.sleep(1.5)
    rep["attempts"] = tries
    rep["passed"] = False
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--port", type=int, default=9224)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = run_with_retry(a.phone, a.password, attempts=a.attempts, port=a.port)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else
          f"passed={rep.get('passed')} final_error={rep.get('final_error')} solve={rep.get('solve')}")
    return 0 if rep.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
