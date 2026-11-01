"""Synthetic captcha corpus generator — local fixtures for accuracy benchmarking.

Produces five families (text / arithmetic / slider / click / rotate) with a
labels.json per family so `run_bench.py` can score the solver without touching
any live site.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = "/System/Library/Fonts/Supplemental"
FONTS = [
    "Arial.ttf",
    "Arial Bold.ttf",
    "Courier New.ttf",
    "Georgia.ttf",
    "Times New Roman.ttf",
    "Trebuchet MS.ttf",
    "Verdana.ttf",
]
DIGITS = string.digits
ALNUM = string.ascii_uppercase + string.digits


def _font(size: int, rng: random.Random) -> ImageFont.FreeTypeFont:
    name = rng.choice(FONTS)
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def _noise_bg(draw: ImageDraw.ImageDraw, w: int, h: int, rng: random.Random) -> None:
    for _ in range(int(w * h * 0.008)):
        x, y = rng.randrange(w), rng.randrange(h)
        g = rng.randrange(140, 255)
        draw.point((x, y), fill=(g, g, g))


def _interference(draw: ImageDraw.ImageDraw, w: int, h: int, rng: random.Random, n: int = 3) -> None:
    for _ in range(n):
        pts = [(rng.randrange(w), rng.randrange(h)) for _ in range(3)]
        col = (rng.randrange(150, 215),) * 3
        draw.line(pts, fill=col, width=1)


def gen_text(out: str, idx: int, rng: random.Random, label_charset: str = ALNUM) -> Dict:
    w, h = 220, 78
    img = Image.new("RGB", (w, h), (rng.randrange(238, 256),) * 3)
    d = ImageDraw.Draw(img)
    _noise_bg(d, w, h, rng)
    n = rng.choice([4, 5])
    text = "".join(rng.choice(label_charset) for _ in range(n))
    x = rng.randrange(8, 14)
    for ch in text:
        size = rng.randrange(26, 33)
        f = _font(size, rng)
        glyph = Image.new("RGBA", (size + 20, size + 20), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glyph)
        gd.text((6, 6), ch, font=f, fill=(rng.randrange(0, 80),) * 3 + (255,))
        glyph = glyph.rotate(rng.uniform(-16, 16), resample=Image.BICUBIC, expand=False)
        img.paste(glyph, (x, rng.randrange(6, 18)), glyph)
        x += size + rng.randrange(2, 8)
    _interference(d, w, h, rng, n=rng.choice([1, 2]))
    img = img.filter(ImageFilter.GaussianBlur(rng.choice([0, 0.3, 0.5])))
    name = f"text_{idx:03d}.png"
    img.save(os.path.join(out, name))
    return {"file": name, "kind": "text", "answer": text, "length": len(text)}


def gen_arithmetic(out: str, idx: int, rng: random.Random) -> Dict:
    w, h = 220, 70
    img = Image.new("RGB", (w, h), (rng.randrange(242, 256),) * 3)
    d = ImageDraw.Draw(img)
    _noise_bg(d, w, h, rng)
    a, b = rng.randrange(1, 30), rng.randrange(1, 20)
    op = rng.choice(["+", "-", "x"])
    expr = f"{a}{op}{b}=?"
    x = 12
    for ch in expr:
        f = _font(30, rng)
        d.text((x, rng.randrange(10, 16)), ch, font=f, fill=(rng.randrange(0, 50),) * 3)
        x += 30
    _interference(d, w, h, rng, n=1)
    val = a + b if op == "+" else a - b if op == "-" else a * b
    name = f"arith_{idx:03d}.png"
    img.save(os.path.join(out, name))
    return {"file": name, "kind": "arithmetic", "answer": str(val), "expr": f"{a}{op}{b}"}


def gen_slider(out: str, idx: int, rng: random.Random) -> Dict:
    w, h = 320, 170
    bg = Image.new("RGB", (w, h), (rng.randrange(60, 160), rng.randrange(60, 160), rng.randrange(60, 160)))
    d = ImageDraw.Draw(bg)
    for _ in range(26):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        d.ellipse(
            [x0, y0, x0 + rng.randrange(20, 120), y0 + rng.randrange(20, 120)],
            fill=(rng.randrange(0, 255), rng.randrange(0, 255), rng.randrange(0, 255)),
        )
    bg = bg.filter(ImageFilter.GaussianBlur(0.6))
    pw = rng.choice([52, 58, 64, 70])
    gap_x = rng.randrange(70, w - pw - 12)
    y0 = rng.randrange(20, 60)
    piece_box = (gap_x, y0, gap_x + pw, y0 + pw)
    piece = bg.crop(piece_box).convert("RGBA")

    mask = Image.new("L", (pw, pw), 0)
    md = ImageDraw.Draw(mask)
    md.rectangle([0, 0, pw - 1, pw - 1], fill=255)
    # knock out a little jigsaw nub so edges are not a plain square
    md.ellipse([pw // 2 - 9, -8, pw // 2 + 9, 10], fill=0)
    piece.putalpha(mask)
    comp = bg.copy()
    cd = ImageDraw.Draw(comp)
    cd.rectangle(piece_box, fill=(228, 228, 232))
    comp.paste(piece, (8, piece_box[1]), piece)

    bg_name, comp_name, piece_name = f"slider_{idx:03d}_bg.png", f"slider_{idx:03d}.png", f"slider_{idx:03d}_piece.png"
    bg.save(os.path.join(out, bg_name))
    comp.save(os.path.join(out, comp_name))
    piece.save(os.path.join(out, piece_name))
    return {
        "file": comp_name,
        "bg_file": bg_name,
        "piece_file": piece_name,
        "kind": "slider",
        "answer": str(gap_x - 8),
        "gap_x": gap_x,
        "piece_width": pw,
    }


def gen_click(out: str, idx: int, rng: random.Random) -> Dict:
    w, h = 300, 150
    img = Image.new("RGB", (w, h), (rng.randrange(238, 256),) * 3)
    d = ImageDraw.Draw(img)
    _noise_bg(d, w, h, rng)
    chars = rng.sample("天地人山水日月", 4)
    order = list(range(4))
    rng.shuffle(order)
    slots: List[Tuple[int, int]] = []
    for i, ch in enumerate(chars):
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 40) if os.path.exists(
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
        ) else ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 40)
        cx = 20 + (i % 2) * 150 + rng.randrange(0, 40)
        cy = 12 + (i // 2) * 70 + rng.randrange(0, 18)
        d.text((cx, cy), ch, font=f, fill=(rng.randrange(0, 70),) * 3)
        slots.append((cx + 22, cy + 26))
    prompt = "请依次点击：" + "".join(chars[p] for p in order)
    name = f"click_{idx:03d}.png"
    img.save(os.path.join(out, name))
    return {
        "file": name,
        "kind": "click",
        "prompt": prompt,
        "answer": "|".join(f"{slots[p][0]},{slots[p][1]}" for p in order),
        "points": [list(slots[p]) for p in order],
    }


def gen_rotate(out: str, idx: int, rng: random.Random) -> Dict:
    w, h = 260, 150
    img = Image.new("RGB", (w, h), (250, 250, 252))
    d = ImageDraw.Draw(img)
    text = "".join(rng.choice(ALNUM) for _ in range(4))
    x = 40
    for ch in text:
        d.text((x, 58), ch, font=_font(34, rng), fill=(30, 30, 30))
        x += 44
    ang = rng.uniform(-38, 38)
    img = img.rotate(ang, resample=Image.BICUBIC, fillcolor=(250, 250, 252))
    name = f"rotate_{idx:03d}.png"
    img.save(os.path.join(out, name))
    return {"file": name, "kind": "rotate", "answer": text, "angle": round(ang, 2)}


# ------------------------------------------------- multi-operator arithmetic ----
# Deliberately rendered with fonts that are NOT in arith.SERIF_FONTS, so the
# template bank has to generalise instead of matching its own renderings.
MULTI_FONTS = ["Verdana.ttf", "Trebuchet MS.ttf", "Baskerville.ttc", "Tahoma.ttf", "Arial Narrow.ttf"]
CJK_FONTS = ["Arial Unicode.ttf", "PingFang.ttc", "Hiragino Sans GB.ttc", "STHeiti Light.ttc"]


def _cjk_font(size: int) -> ImageFont.FreeTypeFont:
    for name in CJK_FONTS:
        for d in (os.path.join(FONT_DIR, "Supplemental"), "/System/Library/Fonts", "/Library/Fonts"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
    return ImageFont.truetype(os.path.join(FONT_DIR, "Arial.ttf"), size)


def _draw_star(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, width: int = 0) -> None:
    import math

    pts = []
    for k in range(12):
        ang = math.pi * k / 6 - math.pi / 2
        rad = r if k % 2 == 0 else r * 0.42
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=(20, 20, 20))


def gen_arith_multi(out: str, idx: int, rng: random.Random, with_prompt: bool = True) -> Dict:
    """3-4 operand arithmetic with a drawn star, CJK prompt, speckle and scribbles."""
    w, h = 220, 70
    img = Image.new("RGB", (w, h), (rng.randrange(246, 256),) * 3)
    d = ImageDraw.Draw(img)
    n_ops = rng.choice([3, 3, 4])
    operands = [rng.randrange(0, 11) if i else rng.randrange(1, 11) for i in range(n_ops)]
    ops = [rng.choice(["+", "-", "*"]) for _ in range(n_ops - 1)]
    size = rng.randrange(26, 33)
    font_name = rng.choice(MULTI_FONTS)
    font_path = os.path.join(FONT_DIR, font_name)
    if not os.path.exists(font_path):
        font_name, font_path = "Arial.ttf", os.path.join(FONT_DIR, "Arial.ttf")
    font = ImageFont.truetype(font_path, size)

    x = rng.randrange(6, 12)
    baseline = rng.randrange(22, 30)
    expr_parts: List[str] = []
    for i, num in enumerate(operands):
        txt = str(num)
        d.text((x, baseline), txt, font=font, fill=(rng.randrange(0, 60),) * 3)
        x += int(d.textlength(txt, font=font)) + rng.randrange(1, 3)
        expr_parts.append(txt)
        if i < len(ops):
            op = ops[i]
            if op == "*":
                _draw_star(d, x + size * 0.28, baseline + size * 0.38, size * 0.30)
                x += int(size * 0.62) + rng.randrange(1, 3)
            else:
                d.text((x, baseline), op, font=font, fill=(rng.randrange(0, 60),) * 3)
                x += int(d.textlength(op, font=font)) + rng.randrange(1, 3)
            expr_parts.append(op)

    prompt = ""
    if with_prompt:
        prompt = "答案是?"
        cf = _cjk_font(max(14, int(size * 0.62)))
        d.text((x + 2, baseline + int(size * 0.18)), prompt, font=cf, fill=(rng.randrange(0, 60),) * 3)

    # salt & pepper
    for _ in range(rng.randrange(420, 900)):
        px, py = rng.randrange(w), rng.randrange(h)
        s = rng.choice([1, 1, 2])
        d.rectangle([px, py, px + s, py + s], fill=(rng.randrange(0, 110),) * 3)
    # scribbled interference
    for _ in range(rng.randrange(1, 3)):
        x0, y0 = rng.randrange(0, w), rng.randrange(0, h)
        x1 = x0 + rng.randrange(30, 90) * rng.choice([-1, 1])
        y1 = y0 + rng.randrange(10, 40) * rng.choice([-1, 1])
        d.line([(x0, y0), (x1, y1)], fill=(rng.randrange(0, 120),) * 3, width=rng.choice([1, 2]))
    img = img.filter(ImageFilter.GaussianBlur(rng.choice([0, 0.4, 0.7])))

    expr = "".join(expr_parts)
    from captcha_solver import expression as E  # local import keeps bench standalone

    parsed = E.parse(expr)
    name = f"arith_multi_{idx:03d}.png"
    img.save(os.path.join(out, name))
    return {
        "file": name,
        "kind": "arith_multi",
        "expr": expr,
        "answer": parsed["answer"] if parsed else None,
        "font": font_name,
        "prompt": prompt,
    }


def build(out_dir: str, count: int = 8, seed: int = 7) -> Dict[str, List[dict]]:
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    corpus: Dict[str, List[dict]] = {
        "text": [], "arithmetic": [], "slider": [], "click": [], "rotate": [], "arith_multi": []
    }
    for i in range(count):
        corpus["text"].append(gen_text(out_dir, i, rng))
        corpus["arithmetic"].append(gen_arithmetic(out_dir, i, rng))
        corpus["slider"].append(gen_slider(out_dir, i, rng))
        corpus["click"].append(gen_click(out_dir, i, rng))
        corpus["rotate"].append(gen_rotate(out_dir, i, rng))
        corpus["arith_multi"].append(gen_arith_multi(out_dir, i, rng))
    with open(os.path.join(out_dir, "labels.json"), "w", encoding="utf-8") as fh:
        json.dump(corpus, fh, ensure_ascii=False, indent=2)
    return corpus


def main(out_dir: str | None = None, count: int = 8) -> int:
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "corpus")
    corpus = build(out_dir, count=count)
    total = sum(len(v) for v in corpus.values())
    print(f"wrote {total} fixtures to {out_dir}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--count", type=int, default=8)
    a = ap.parse_args()
    raise SystemExit(main(a.out, a.count))
