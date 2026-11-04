# -*- coding: utf-8 -*-
"""把任务2-1的代码与真实运行输出渲染成一张清晰的结果图（中英文字体混排）。"""
import io
import json
import os

from PIL import Image, ImageDraw, ImageFont

WORK = r"D:\智能桌宠\work"
F_CJK = r"C:\Windows\Fonts\msyh.ttc"
F_MONO = r"C:\Windows\Fonts\consola.ttf"
F_SYM = r"C:\Users\xuxin\AppData\Local\Programs\Python\Python313\Lib\site-packages\matplotlib\mpl-data\fonts\ttf\DejaVuSans.ttf"

with io.open(os.path.join(WORK, "task2_1_board.ipynb"), encoding="utf-8") as f:
    nb = json.load(f)
code = "".join(nb["cells"][1]["source"]).rstrip("\n").split("\n")
with io.open(os.path.join(WORK, "task2_1_output.txt"), encoding="utf-8") as f:
    out = f.read().rstrip("\n").split("\n")

with io.open(os.path.join(WORK, "task2_1_cell_code.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(code))

SIZE = 19
f_cjk = ImageFont.truetype(F_CJK, SIZE)
f_mono = ImageFont.truetype(F_MONO, SIZE)
f_sym = ImageFont.truetype(F_SYM, SIZE)
f_title = ImageFont.truetype(F_CJK, 23)
f_tag = ImageFont.truetype(F_CJK, 15)

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


def spans(line):
    res = []
    for ch in line:
        font = font_of(ch)
        res.append((ch, font, d.textlength(ch, font=font)))
    return res


def line_width(line):
    return sum(w for _, _, w in spans(line))


def draw_line(dr, x, y, line):
    for ch, font, w in spans(line):
        dr.text((x, y), ch, font=font, fill="#1F2328")
        x += w
    return x


maxw = max([line_width(l) for l in code + out] + [line_width("运行输出   Out [1]:")])
WIDTH = int(maxw) + 2 * PAD + 64

height = PAD + 42 + 36 + 28 + len(code) * LINE + 18 + 44 + 28 + len(out) * LINE + 18 + PAD
img = Image.new("RGB", (WIDTH, height), "#FFFFFF")
dr = ImageDraw.Draw(img)

dr.text((PAD, PAD), "拓展任务2-1　创建 19×19 围棋棋盘", font=f_title, fill="#1F2328")
dr.line([(PAD, PAD + 40), (WIDTH - PAD, PAD + 40)], fill="#D0D7DE", width=2)

y = PAD + 40 + 36


def block(title, lines, y, bar="#4C9A5E", bg="#F6F8FA"):
    dr.text((PAD, y), title, font=f_tag, fill="#57606A")
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
img.save(os.path.join(WORK, "task2_1_result.png"))
print("saved", img.size)
