#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小派触屏界面：纯 PIL 绘制，不碰任何硬件，所以也能在电脑上渲染预览图。

屏幕 240x320 竖屏，四个区域：
   0  .. 28   状态条：小派 / 状态 / 免提开关
  28  .. 170  宠物本体（会眨眼、会看你的手指、听你说话时竖耳朵）
 170  .. 176  音量条
 176 .. 250  对话气泡：上一句你说的话 + 小派现在的回答
 252 .. 312  三个按钮：静音 / 按住说话 / 重说
"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320

BAR_H = 28
LEVEL_Y = 158
BUBBLE = (12, 176, 228, 250)          # x0, y0, x1, y1
BTN_Y0, BTN_Y1 = 253, 309
BTN_MUTE = (10, BTN_Y0, 58, BTN_Y1)
BTN_PTT = (66, BTN_Y0, 174, BTN_Y1)
BTN_REPEAT = (182, BTN_Y0, 230, BTN_Y1)
PILL_AUTO = (150, 4, 230, 24)

FACE_CX, FACE_CY, FACE_R = 120, 96, 44

BG = (11, 17, 26)
BAR = (16, 24, 36)
CARD = (23, 32, 47)
CARD_EDGE = (36, 48, 68)
TEXT = (233, 239, 246)
MUTED = (138, 154, 174)
ACCENT = (94, 226, 196)
ACCENT_D = (32, 96, 86)
WARM = (250, 205, 110)
PINK = (240, 146, 158)
SKIN = (248, 231, 201)
SKIN_D = (214, 190, 156)
SKIN_LINE = (150, 124, 92)
INK = (42, 38, 48)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_FONTS = {}


def font(size):
    if size not in _FONTS:
        for path in _FONT_CANDIDATES:
            try:
                _FONTS[size] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def text_w(d, txt, fnt):
    return d.textlength(txt, font=fnt)


def center_text(d, x, y, txt, fnt, fill):
    left, top, right, bottom = d.textbbox((0, 0), txt, font=fnt)
    d.text((x - (left + right) / 2, y - (top + bottom) / 2), txt, font=fnt, fill=fill)


def lerp(c1, c2, t):
    t = min(max(t, 0.0), 1.0)
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


NO_START = "，。、；：？！）】》”’,.!?:;)]}"


def wrap(d, txt, fnt, max_w):
    lines, cur = [], ""
    for ch in txt:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and text_w(d, cur + ch, fnt) > max_w:
            if ch in NO_START:              # 标点不放行首，宁可让这一行稍长一点
                cur += ch
                lines.append(cur)
                cur = ""
                continue
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def clip_lines(d, lines, fnt, max_w, keep):
    """最多留 keep 行，超出的最后一行加省略号"""
    if len(lines) <= keep:
        return lines
    out = lines[:keep]
    last = out[-1]
    while last and text_w(d, last + "…", fnt) > max_w:
        last = last[:-1]
    out[-1] = last + "…"
    return out


def pill(d, box, fill, outline=None, radius=9, width=1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# ------------------------------------------------------------------ 图标
def icon_speaker(d, cx, cy, on, col):
    d.polygon([(cx - 7, cy - 3), (cx - 3, cy - 3), (cx + 1, cy - 7),
               (cx + 1, cy + 7), (cx - 3, cy + 3), (cx - 7, cy + 3)], fill=col)
    if on:
        for r in (5, 8):
            d.arc((cx - r + 1, cy - r, cx + r + 1, cy + r), -55, 55, fill=col, width=2)
    else:
        d.line((cx + 3, cy - 6, cx + 9, cy + 6), fill=col, width=2)
        d.line((cx + 9, cy - 6, cx + 3, cy + 6), fill=col, width=2)


def icon_repeat(d, cx, cy, col):
    d.arc((cx - 7, cy - 7, cx + 7, cy + 7), 40, 320, fill=col, width=2)
    d.polygon([(cx + 5, cy - 8), (cx + 10, cy - 3), (cx + 3, cy - 2)], fill=col)


def icon_ear(d, cx, cy, col, flip=1):
    d.arc((cx - 6, cy - 5, cx + 6, cy + 7), 0, 180, fill=col, width=2)


# ------------------------------------------------------------------ 脸
def _eye(d, cx, cy, w, h, look, open_ratio, blink):
    if blink:
        d.line((cx - w, cy, cx + w, cy), fill=INK, width=3)
        return
    hh = max(2, h * open_ratio)
    d.ellipse((cx - w, cy - hh, cx + w, cy + hh), fill=(252, 252, 250))
    pr = min(w * 0.62, hh * 0.9)
    px = cx + look[0] * (w - pr * 0.8)
    py = cy + look[1] * max(0, hh - pr * 0.9)
    d.ellipse((px - pr, py - pr, px + pr, py + pr), fill=INK)
    d.ellipse((px - pr * 0.36, py - pr * 0.5, px + pr * 0.18,
               py - pr * 0.02), fill=(255, 255, 255))


def _happy_eye(d, cx, cy, w, col):
    d.arc((cx - w, cy - w * 0.7, cx + w, cy + w * 0.9), 200, 340, fill=col, width=3)


def _mouth(d, cy, mode, level, t):
    cx = FACE_CX
    if mode == "happy":
        d.arc((cx - 12, cy - 6, cx + 12, cy + 12), 15, 165, fill=INK, width=3)
        d.ellipse((cx - 5, cy + 6, cx + 5, cy + 11), fill=PINK)
        return
    if mode == "listening":
        d.ellipse((cx - 4, cy - 3, cx + 4, cy + 5), fill=INK)
        return
    if mode == "thinking":
        d.line((cx - 7, cy + 2, cx + 7, cy + 2), fill=INK, width=3)
        return
    if mode == "speaking":
        h = 2 + 7 * level
        d.ellipse((cx - 7, cy - h, cx + 7, cy + h), fill=INK)
        d.ellipse((cx - 3, cy + h * 0.15, cx + 3, cy + h * 0.75), fill=PINK)
        return
    # idle / error：淡淡的笑
    d.arc((cx - 8, cy - 5, cx + 8, cy + 6), 20, 160,
          fill=INK if mode != "error" else MUTED, width=2)


def draw_face(d, st, t):
    mode = st.mode
    bob = math.sin(t * (5.0 if mode == "happy" else 2.2)) * (2.2 if mode == "happy" else 1.3)
    cx, cy = FACE_CX, FACE_CY + bob
    r = FACE_R

    # 光晕
    for i, alpha in ((16, 0.10), (10, 0.16)):
        col = lerp(BG, ACCENT if mode in ("listening", "speaking") else WARM, alpha)
        d.ellipse((cx - r - i, cy - r - i, cx + r + i, cy + r + i), fill=col)

    # 耳朵（听的时候竖起来）
    lift = -7 if mode == "listening" else 0
    ear_y = cy - r + 2 + lift
    for side in (-1, 1):
        ex = cx + side * (r - 9)
        d.ellipse((ex - 13, ear_y - 12, ex + 13, ear_y + 14), fill=SKIN, outline=SKIN_LINE)
        d.ellipse((ex - 6, ear_y - 5, ex + 6, ear_y + 8), fill=lerp(SKIN, PINK, 0.45))

    # 天线
    sway = math.sin(t * 3.1) * 3
    d.line((cx + 2, cy - r - 2, cx + sway, cy - r - 13), fill=SKIN_LINE, width=2)
    d.ellipse((cx + sway - 4, cy - r - 18, cx + sway + 4, cy - r - 10),
              fill=ACCENT if mode in ("listening", "thinking", "speaking") else WARM)

    # 头
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SKIN, outline=SKIN_LINE)

    # 长按摸头 -> 清空上下文：按住时外圈慢慢画满
    if getattr(st, "press_region", None) == "face":
        p = min(1.0, getattr(st, "press_seconds", 0.0) / 0.9)
        if p > 0.03:
            d.arc((cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9), -90,
                  -90 + 360 * p, fill=WARM, width=3)

    # 眼睛
    look = st.look if mode in ("idle", "listening", "happy") else (0.0, -0.35)
    blink = st.blink
    eye_w, eye_h = 8, 10
    if mode == "listening":
        eye_w, eye_h = 9, 11
    if mode == "happy":
        _happy_eye(d, cx - 16, cy - 6, eye_w, INK)
        _happy_eye(d, cx + 16, cy - 6, eye_w, INK)
    elif mode == "thinking":
        _eye(d, cx - 16, cy - 8, eye_w, eye_h, (0.7, -0.7), 1.0, False)
        _eye(d, cx + 16, cy - 8, eye_w, eye_h, (0.7, -0.7), 1.0, False)
    else:
        ratio = 0.16 if blink else 1.0
        _eye(d, cx - 16, cy - 6, eye_w, eye_h, look, ratio, blink)
        _eye(d, cx + 16, cy - 6, eye_w, eye_h, look, ratio, blink)

    # 腮红
    if mode in ("happy", "speaking"):
        for side in (-1, 1):
            d.ellipse((cx + side * 30 - 8, cy + 12, cx + side * 30 + 8, cy + 21),
                      fill=lerp(SKIN, PINK, 0.55))

    _mouth(d, cy + 20, mode, st.level, t)

    # 听你说话时，头两侧冒出声波弧
    if mode == "listening":
        for side in (-1, 1):
            for i in range(2):
                rad = 12 + i * 8 + (t * 26) % 8
                x0 = cx + side * (r + 2)
                d.arc((x0 - rad, cy - rad, x0 + rad, cy + rad),
                      -48 if side > 0 else 132, 48 if side > 0 else 228,
                      fill=ACCENT, width=2)

    if mode == "happy":
        for sx, sy, rr in ((cx - 52, cy - 40, 5), (cx + 54, cy - 26, 4), (cx + 40, cy + 34, 3)):
            k = 0.7 + 0.3 * math.sin(t * 9 + sx)
            d.line((sx - rr * k, sy, sx + rr * k, sy), fill=WARM, width=2)
            d.line((sx, sy - rr * k, sx, sy + rr * k), fill=WARM, width=2)

    if mode == "thinking":
        for i in range(3):
            a = 0.35 + 0.65 * max(0.0, math.sin(t * 4 - i * 0.7))
            d.ellipse((cx - 16 + i * 12 - 2, cy - r - 32, cx - 16 + i * 12 + 2,
                       cy - r - 28), fill=lerp(BG, TEXT, a))


# ------------------------------------------------------------------ 各区域
STATUS_TEXT = {
    "idle": "待机", "listening": "在听", "thinking": "在想",
    "speaking": "在说", "happy": "开心", "error": "出错",
}


def draw_bar(d, st):
    d.rectangle((0, 0, W - 1, BAR_H - 1), fill=BAR)
    col = {"idle": MUTED, "listening": ACCENT, "thinking": WARM,
           "speaking": ACCENT, "happy": PINK, "error": PINK}.get(st.mode, MUTED)

    # 作为控制中心的一个子页面时，左上角画返回箭头（对话页用；独立运行时没有）
    if getattr(st, "show_back", False):
        d.line((17, 9, 10, 14), fill=TEXT, width=2)
        d.line((10, 14, 17, 19), fill=TEXT, width=2)
        d.text((30, 6), "小派", font=font(14), fill=TEXT)
        d.text((65, 9), STATUS_TEXT.get(st.mode, ""), font=font(11), fill=MUTED)
    else:
        if st.mode in ("listening", "thinking", "speaking"):
            pulse = 3.4 + math.sin(st.t * 6) * 0.8
        else:
            pulse = 3.2
        d.ellipse((11 - pulse, 14 - pulse, 11 + pulse, 14 + pulse), fill=col)
        d.text((21, 6), "小派", font=font(14), fill=TEXT)
        d.text((56, 9), STATUS_TEXT.get(st.mode, ""), font=font(11), fill=MUTED)

    on = st.auto_listen
    pill(d, PILL_AUTO, ACCENT_D if on else (26, 34, 48),
         outline=ACCENT if on else CARD_EDGE, radius=10)
    label = "免提 开" if on else "免提 关"
    center_text(d, (PILL_AUTO[0] + PILL_AUTO[2]) / 2, 13.5, label, font(11),
                ACCENT if on else MUTED)
    d.line((0, BAR_H, W - 1, BAR_H), fill=(28, 38, 54))


def draw_level(d, st, t):
    n, bw, gap = 22, 4, 5
    total = n * bw + (n - 1) * gap
    x0 = (W - total) // 2
    y0 = LEVEL_Y
    active = st.mode in ("listening", "speaking")
    lvl = st.level
    if not active:
        d.line((W // 2 - 26, y0 + 3, W // 2 + 26, y0 + 3), fill=(34, 45, 62))
        return
    for i in range(n):
        # 中间高两边低的包络，看着像声波
        env = 1.0 - abs(i - (n - 1) / 2) / ((n - 1) / 2) * 0.75
        hgt = 2 + 7 * min(1.0, lvl * 1.6) * env
        hgt *= 0.75 + 0.25 * math.sin(t * 9 + i * 0.55)
        hgt = max(2.5, hgt)
        col = lerp((38, 52, 70), ACCENT, 0.35 + 0.65 * min(1.0, lvl * 1.4))
        cx = x0 + i * (bw + gap)
        d.rounded_rectangle((cx, y0 + 6 - hgt, cx + bw - 1, y0 + 6 + hgt),
                            radius=2, fill=col)


def draw_bubble(d, st, t):
    x0, y0, x1, y1 = BUBBLE
    pill(d, BUBBLE, CARD, outline=CARD_EDGE, radius=14)
    inner_w = x1 - x0 - 24

    user = st.user_text.strip()
    if st.mode == "listening":
        d.text((x0 + 12, y0 + 7), "录音中…", font=font(11), fill=ACCENT)
    elif user:
        uf = font(11)
        line = clip_lines(d, wrap(d, "你：" + user, uf, inner_w), uf, inner_w, 1)[0]
        d.text((x0 + 12, y0 + 7), line, font=uf, fill=MUTED)
    else:
        d.text((x0 + 12, y0 + 7), "点我摸头 · 按住下面说话", font=font(11), fill=(96, 112, 132))

    y = y0 + 22
    if st.mode == "listening":
        txt = st.partial or "说吧，我在听"
        for line in clip_lines(d, wrap(d, txt, font(15), inner_w), font(15), inner_w, 2):
            d.text((x0 + 12, y), line, font=font(15), fill=ACCENT)
            y += 18
        d.text((x0 + 12, y1 - 18), "松手就发送", font=font(10), fill=(96, 112, 132))
        return

    if st.mode == "thinking":
        txt = "让我想想" + "." * (1 + int(t * 3) % 3)
        d.text((x0 + 12, y), txt, font=font(15), fill=WARM)
        return

    reply = st.reply_text.strip()
    if not reply:
        reply = "你好呀，我是小派。按住下面的按钮跟我说话，或者戳我的脸。"
    lines = clip_lines(d, wrap(d, reply, font(15), inner_w), font(15), inner_w, 3)
    for line in lines:
        d.text((x0 + 12, y), line, font=font(15), fill=TEXT)
        y += 18


def draw_buttons(d, st, t):
    pressed = st.press_region

    # 静音 / 有声
    off = st.muted
    fill = (30, 40, 56) if pressed != "btn_mute" else (44, 58, 78)
    pill(d, BTN_MUTE, fill, outline=CARD_EDGE, radius=13)
    icon_speaker(d, 34, 273, not off, PINK if off else TEXT)
    center_text(d, 34, 294, "静音" if not off else "有声", font(10),
                PINK if off else MUTED)

    # 重说
    fill = (30, 40, 56) if pressed != "btn_repeat" else (44, 58, 78)
    pill(d, BTN_REPEAT, fill, outline=CARD_EDGE, radius=13)
    icon_repeat(d, 206, 273, TEXT if st.reply_text else (70, 84, 102))
    center_text(d, 206, 294, "重说", font(10), MUTED)

    # 按住说话
    listening = st.mode == "listening"
    if listening:
        fill, edge, fg = (52, 178, 152), ACCENT, (8, 26, 24)
        label = "松手发送"
    elif st.mode in ("thinking", "speaking"):
        fill, edge, fg = (36, 48, 66), CARD_EDGE, MUTED
        label = "按住说话"
    else:
        fill, edge, fg = ACCENT if pressed != "btn_ptt" else (78, 198, 172), ACCENT_D, (8, 26, 24)
        label = "按住说话"
    pill(d, BTN_PTT, fill, outline=edge, radius=18, width=2)
    cx = (BTN_PTT[0] + BTN_PTT[2]) / 2
    center_text(d, cx, 272, label, font(16), fg)
    if listening:
        center_text(d, cx, 292, f"已录 {int(st.ptt_seconds)}s", font(10), (10, 40, 34))
    elif st.mode == "speaking":
        center_text(d, cx, 291, "点这里可以打断", font(10), MUTED)
    elif st.mode == "thinking":
        center_text(d, cx, 291, "等一下", font(10), MUTED)
    else:
        center_text(d, cx, 291, "松手发送", font(9), (10, 44, 36))


def render(st):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_face(d, st, st.t)
    draw_bar(d, st)
    draw_level(d, st, st.t)
    draw_bubble(d, st, st.t)
    draw_buttons(d, st, st.t)
    return img


# ------------------------------------------------------------------ 命中判定
def hit(x, y):
    for name, box in (("btn_mute", BTN_MUTE), ("btn_ptt", BTN_PTT),
                      ("btn_repeat", BTN_REPEAT), ("auto", PILL_AUTO)):
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            return name
    if BAR_H <= y < LEVEL_Y + 6:
        return "face"
    if BUBBLE[1] <= y <= BUBBLE[3]:
        return "bubble"
    if (x - FACE_CX) ** 2 + (y - FACE_CY - 2) ** 2 <= (FACE_R + 26) ** 2:
        return "face"
    return None
