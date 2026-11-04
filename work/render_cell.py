# -*- coding: utf-8 -*-
"""把某个单元格的代码与真实运行输出渲染成一张清晰的结果图（中英文字体混排）。

用法：python render_cell.py <标题> <ipynb> <输出txt> <png>
"""
import io
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

F_CJK = r"C:\Windows\Fonts\msyh.ttc"
F_MONO = r"C:\Windows\Fonts\consola.ttf"
F_SYM = r"C:\Users\xuxin\AppData\Local\Programs\Python\Python313\Lib\site-packages\matplotlib\mpl-data\fonts\ttf\DejaVuSans.ttf"

title, nb_path, out_path, png_path = sys.argv[1:5]
student = sys.argv[5] if len(sys.argv) > 5 else ""

with io.open(nb_path, encoding="utf-8") as f:
    nb = json.load(f)
code_lines = None
for cell in nb["cells"]:
    if cell["cell_type"] == "code":
        code_lines = "".join(cell["source"]).rstrip("\n").split("\n")
        break
code = code_lines
with io.open(out_path, encoding="utf-8") as f:
    out = f.read().rstrip("\n").split("\n")

SIZE = 19
f_cjk = ImageFont.truetype(F_CJK, SIZE)
f_mono = ImageFont.truetype(F_MONO, SIZE)
f_sym = ImageFont.truetype(F_SYM, SIZE)
f_title = ImageFont.truetype(F_CJK, 23)
f_tag = ImageFont.truetype(F_CJK, 15)
f_tag2 = ImageFont.truetype(F_CJK, 18)

tmp = Image.new("RGB", (10, 10))
d = ImageDraw.Draw(tmp)
LINE = 28
PAD = 34
SYM = "\u00a7\u2011"


def use_cjk(ch):
    o = ord(ch)
    return o >= 0x2E80 or 0x2460 <= o <= 0x27BF


def font_of(ch):
    if ch in SYM:
        return f_sym
    if use_cjk(ch):
        return f_cjk
    return f_mono


MONO_ADV = d.textlength("0", font=f_mono)


def char_span(ch):
    """按等宽网格排版：半角 1 格，全角（中文/符号）2 格，保证数组列对齐。"""
    font = font_of(ch)
    width = 2 * MONO_ADV if use_cjk(ch) else MONO_ADV
    glyph_w = d.textlength(ch, font=font)
    return ch, font, width, (width - glyph_w) / 2


def spans(line):
    return [char_span(ch) for ch in line]


def line_width(line):
    return sum(w for _, _, w, _ in spans(line))


def draw_line(dr, x, y, line):
    for ch, font, w, off in spans(line):
        dr.text((x + off, y), ch, font=font, fill="#1F2328")
        x += w
    return x


maxw = max([line_width(l) for l in code + out] + [line_width("运行输出   Out [1]:")])
WIDTH = int(maxw) + 2 * PAD + 96
if student:
    head = d.textlength(title, font=f_title) + d.textlength("姓名：" + student, font=f_tag2) + 80
    WIDTH = max(WIDTH, int(head) + 2 * PAD)

height = PAD + 42 + 36 + 28 + len(code) * LINE + 18 + 44 + 28 + len(out) * LINE + 18 + PAD
img = Image.new("RGB", (WIDTH, height), "#FFFFFF")
dr = ImageDraw.Draw(img)

dr.text((PAD, PAD), title, font=f_title, fill="#1F2328")
if student:
    label = "姓名：" + student
    lw = d.textlength(label, font=f_title)
    dr.text((WIDTH - PAD - lw, PAD + 4), label, font=f_tag2, fill="#57606A")
dr.line([(PAD, PAD + 40), (WIDTH - PAD, PAD + 40)], fill="#D0D7DE", width=2)

y = PAD + 40 + 36


def block(label, lines, y, bar="#4C9A5E", bg="#F6F8FA"):
    dr.text((PAD, y), label, font=f_tag, fill="#57606A")
    y += 28
    box_h = len(lines) * LINE + 16
    dr.rectangle([PAD, y, WIDTH - PAD, y + box_h], fill=bg, outline="#D0D7DE")
    dr.rectangle([PAD, y, PAD + 5, y + box_h], fill=bar)
    ty = y + 8
    for l in lines:
        draw_line(dr, PAD + 22, ty, l)
        ty += LINE
    return y + box_h + 36


y = block("代码单元格   In [1]:", code, y)
y = block("运行输出   Out [1]:", out, y, bar="#3B7DD8")

img = img.crop((0, 0, WIDTH, y - 36 + PAD - 8))
img.save(png_path)
print("saved", png_path, img.size)
