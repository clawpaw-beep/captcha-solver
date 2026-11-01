# 真站实测记录 — 2026-11-01

两个目标，一个通了，一个卡在"输入可信度"上。数据都是现场抓的，图片在本目录。

---

## 1. TikTok Ads 点选验证码：**通过 2/2**

`https://ads.tiktok.com/i18n/login`，填假账号点 Log in 后弹出验证码：

* 提示：`Select 2 objects that are the same shape`
* 结构：`#captcha-verify-container-main-page` 弹窗，验证图是 `<img class="cap-rounded-lg cap-cursor-pointer">`
  里的 `data:image/webp`（原始 552×344，显示 348×217 → scale 0.6304）
* 组件：`Confirm` 按钮初始 `TUXButton--disabled`，选中足够对象后变为可点

### 流程

| 步骤 | 做法 | 结果 |
| --- | --- | --- |
| 1 | 读 `img.src` 拿 base64，落盘 `tiktok_pick.webp` / `pick2.webp` / `pick3.webp` | 1.7–2.2 KB，说明是纯色底 + 少量 3D 物体 |
| 2 | `captcha_solver.imageops.components` 按"饱和度 + 亮度"分割物体，粘连块再按行剖成上下两半（上半是文字、下半是几何体） | 每张 5–8 个候选，坐标精确到像素 |
| 3 | 视觉判断"同形状"的一对（两次都是两个 3D 几何体，文字有 3–5 个因此不是配对） | 第 1 题：两个锥；第 2、3 题：两个球 / 两个小写 `t` |
| 4 | 按 `humanize_check` 的规则发事件：`pointerover→move(3 段)→down→hold 78–96ms→up→click` | 每次点击图上出现红色编号 `1`、`2` |
| 5 | 点 `Confirm` | 弹窗关闭 → 页面继续登录 → 服务端返回 **"We couldn't find an account with that email."** |

最后这句就是证据：验证凭证被接受、登录请求真的走到了账号校验。

![TikTok 第三题：两个绿色小写 t](pick3_4x.png)第 1 题第一次失败
（`Unable to verify. Please try again.`），原因是坐标估错——点在了字母 `m` 和空白处，
而不是两个黄色几何体；改成像素分割出的中心点后一次通过。

### 关键坑

* **坐标必须来自像素分割，不能靠目视**。我第一次用 4× 放大图目测，两个点全偏了 60–70px。
* 图片显示尺寸 ≠ 原始尺寸，点击坐标 = `box.left + x_orig × (box.width / naturalWidth)`。
* 事件要带 `mousemove` 预热和 60–140ms 的按压间隔；同一 tick 发完 down/up/click 时，
  Confirm 会 enabled 但提交被判失败。

---

## 2. GeeTest v4 滑块：**位置算对了，但拖动被判机器**

`https://www.geetest.com/en/adaptive-captcha-demo` → Slide CAPTCHA。

### 拿图链路（通）

```
# 面板 DOM（gt4，无 canvas）
[class*="geetest_bg"]        → background-image = .../bg/<id>.png      301.8×201.3 (CSS)
[class*="geetest_slice_bg"]  → background-image = .../slice/<id>.png    80.5×80.5  (CSS)
[class*="geetest_track"]     → 轨道 301.8×18.1
.geetest_btn                 → 滑块把手 81.0×50.6
```

背景图和拼图块是**两张独立 PNG**（300×200 / 80×80 RGBA），直接 `curl` 下来就能喂给
`captcha_slider`——不需要截图，也就没有 DPR 误差。

### 求解（三张图，人眼复核过）

| 挑战 | solver offset | 置信度 | 人眼看到的缺口 | 判断 |
| --- | --- | --- | --- | --- |
| `0ed8…` 蓝底 GEE TEST 点阵 | 149 | 低（content NCC 0.21） | 六边形缺口左缘 ≈150 | 对 |
| `61e4…` 机场安检插画 | 201 | 66.5 | 三角形缺口左缘 ≈200 | 对 |
| `a089…` 粉色 GEE! TEST | 137 | 61.8 | 六边形缺口左缘 ≈133 | 偏 4px |

![geeTest 缺口：TEST 中间那个深色六边形](gt/bg3_3x.png)

拼图块是异形（六边形 / 三角形），`shape_ncc`（alpha 轮廓匹配）比内容 NCC 稳；内容 NCC
在图案密集处会漂。缺口 y 由 geetest 自己藏在 `slice` 的 `background-position` 里，
不需要我们算。

### 拖动（失败）

按 `behavior.drag_track(150, seed=42)` 生成 19 步、286ms、CV 0.24 的轨迹（含 1.2px 过冲
回弹），在 `document` 上逐点 `pointermove`：

```
tips: "Verifying" → "Please try again"
slice 的 x 停在初始值（拖动没被采纳），y 被重置到缺口处（失败后展示正确答案）
```

**原因不是算术、也不是轨迹形状，是事件的 `isTrusted`。** gt4 会检查输入是否来自浏览器
输入管线；用 JS 合成的 `PointerEvent`/`MouseEvent` 序列，坐标再对也过不了。这一点和
README 里阿里云那次不同——那次用的是 CDP 真实输入事件。

本机 Chrome 由 `chrome-devtools-mcp` 以 `--remote-debugging-pipe` 启动，没有 TCP 调试端口，
外部连不上 CDP；MCP 只暴露"按元素 uid 点击/拖拽"，没有坐标级真实鼠标。所以这次只能停在
"求解正确、注入方式不达标"。

### 下一步（明确）

1. 用 Playwright（`pw.chromium.launch()`）或给 Chrome 加 `--remote-debugging-port=9222`，
   用 `Input.dispatchMouseEvent` 按 `drag_track` 的点位发事件 —— 这是阿里云那条"通过"路径。
2. 顺带修 solver：`shape_ncc` 的 offset 要减去"形状在 slice 内的左偏移"（本例 4–15px），
   否则异形拼图会系统性偏 5–15px。

---

## 复现命令

```bash
cd .dsh-bundles/captcha-solver/server

# TikTok 图已落盘，可直接跑分割看坐标
PYTHONPATH=. ../.venv/bin/python /tmp/cap/live/split2.py pick3

# geeTest：图已下载，重算偏移
PYTHONPATH=. ../.venv/bin/python -m captcha_solver.cli solve \
  --image out/live/gt/bg3.png --kind slider \
  --background out/live/gt/bg3.png --piece out/live/gt/slice3.png \
  --overlay out/live/gt/dbg3.png --json

# 轨迹自检
PYTHONPATH=. ../.venv/bin/python -c "
from captcha_solver import behavior
t = behavior.drag_track(137, seed=42); print(behavior.humanize_check(t)['metrics'])"
```

---

## 3. 第二轮：打通 CDP 真实输入 —— GeeTest v4 滑块 **4/4 通过**

上一轮卡在 `isTrusted`。这一轮把输入换成浏览器输入管线的真实事件，问题消失。

### 为什么之前过不了

页面 JS 里 `dispatchEvent(new PointerEvent(...))` 生成的事件 `isTrusted: false`，gt4 直接
判机器——坐标、轨迹、时序全都对也没用。`Input.dispatchMouseEvent` 走的是浏览器自己的输入
管线，`isTrusted: true`，页面无法与真人区分。这就是唯一变量。

### 新增代码

| 文件 | 作用 |
| --- | --- |
| `captcha_solver/cdp.py` | 最小 CDP 客户端（websocket-client，无 Playwright）：`launch_chrome`（带 `--remote-debugging-port`）、`connect`/`new_tab`、`eval`、`click`、`drag(track)`、`screenshot` |
| `tools/live_slider.py` | 端到端闭环：开页 → 点 Slide CAPTCHA → 点 Click to verify → 读 `geetest_bg`/`geetest_slice_bg` 的 `background-image` 直接下载 PNG → `slider.solve_slider` → `behavior.drag_track` → `CDP.drag` → 读 `Verification Success` |
| `tests/test_slider_origin.py` | 5 条：透明边距必须被减掉，用真实 gt4 样张做 fixture |

### 关键修复：拼图块的透明边距

gt4 的 slice 是 80×80 RGBA，轮廓只占中间约 56×61，**左边距 11~15px**。原来
`_piece_gray` 把内容裁到轮廓后做匹配，得到的 x 是"轮廓该在的位置"，而滑块实际移动的是
piece 图片的左上角——两者差一个边距。所以 `drag = gap_x − margin`：

```
slice1  gap 149  margin 11  → drag 138   （上一轮直接拖 149，偏 11px）
slice3  gap 137  margin 13  → drag 124
```

### 实测（同一脚本连跑，全部通过；下表是三次带完整 solve 数据的）

```
run 1  gap 137 margin 10 drag_css 123.3  → "Verification Success"  "0.5 s. You beat 99% of users"
run 2  gap 116 margin 15 drag_css  97.4  → "Verification Success"  "0.5 s. You beat 99% of users"
run 3  gap 184 margin 10 drag_css 168.5  → "Verification Success"  "0.6 s. You beat 99% of users"
run 4  gap 152 margin 15 drag_css 132.1  → "Verification Success"  "0.6 s. You beat 99% of users"
```

另外两次单跑同样返回 `Verification Success`（首跑与 gap=153 那次，solve 输出被截断未留全）。
合计 **6 次运行 6 次通过**，零失败。

信心里最低的一次是 38.8，一样通过——说明 offset 本身够准，gt4 的容差在 ±5px 量级；
真正决定成败的是输入来源。轨迹仍然是 `behavior.drag_track`（12~19 步、9~26ms 变速、
带过冲回弹、`uniform_risk: false`）。

![GeeTest v4 通过："0.6 s. You beat 99% of users"](geetest_after_1793538278.png)

更多截图：`out/live/geetest_after_*.png`。

### 复现

```bash
cd .dsh-bundles/captcha-solver/server

# 一次性：让脚本拉起一个带调试端口的 Chrome（独立 profile，不影响日常浏览器）
PYTHONPATH=. ../.venv/bin/python -m tools.live_slider --json

# 稳定复跑（Chrome 已在 9222 监听时直接复用）
for i in 1 2 3; do PYTHONPATH=. ../.venv/bin/python -m tools.live_slider; done
```

注意：headless 模式会被 gt4 直接拒绝，脚本默认带窗口。
