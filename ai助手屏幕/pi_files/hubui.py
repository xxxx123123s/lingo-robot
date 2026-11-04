#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hubui.py —— 小派控制中心的界面层

纯 PIL 绘制，不碰任何硬件，所以在电脑上也能渲染预览图。

屏幕 240x320，所有页面共用一条 28px 的顶部栏（左上角是返回箭头）：
    0 .. 28     顶部栏：‹ 返回 / 页面名 / 右侧状态
   28 .. 320    各页面自己的内容

页面清单：
    home     六个磁贴（对话 / 天气 / 温湿度 / 遥控 / 语音控车 / 系统）
    weather  实况 + 今日高低 + 降水概率
    env      室内温湿度 + 折线趋势 + 传感器状态
    drive    屏幕方向盘（按住行走、松手停车）+ 遥测
    voice    按住说话控车 + 识别结果 + 停车键
    sys      CPU / 内存 / 磁盘 / 网络 / 运行时长

约定：本文件只做「看」和「哪块被点到」，所有数据由 hub.py 填好放在 app 对象上。
"""

import math

from PIL import Image, ImageDraw

import petui
from petui import (ACCENT, ACCENT_D, BAR, BG, CARD, CARD_EDGE, MUTED, PINK,
                   SKIN, TEXT, WARM, center_text, clip_lines, font, lerp,
                   pill, text_w, wrap)

W, H = 240, 320
HEAD_H = 28
BACK_BOX = (0, 0, 36, HEAD_H)          # 返回热区，比箭头本身大一圈，手指好按

RED = (238, 108, 108)
RED_D = (120, 44, 48)
GREEN = (108, 214, 160)
BLUE = (120, 178, 240)


# ================================================================ 通用
def car_link(v):
    """车链路状态 → (文案, 颜色)。

    这里单独分出"掉线"一档。以前只有"已连接 / 未连接"两态，而状态位只在
    连接成功那一刻写一次，蓝牙断了以后页面还挂着绿色的"已连接"——
    用户对着一辆不会动的车按半天，根本不知道是自己没按住还是链路没了。
    掉线用暖色，和"从来没连过"的灰色区分开，一眼看出是断过。"""
    if getattr(v, "car_ok", False):
        return "已连接", GREEN
    if getattr(v, "car_lost", False):
        return "掉线", WARM
    if getattr(v, "connecting", False):
        return "连接中", MUTED
    return "未连接", MUTED


def card(d, box, fill=CARD, edge=CARD_EDGE, radius=12):
    pill(d, box, fill, outline=edge, radius=radius)


def dot(d, cx, cy, col, r=3):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)


def header(d, title, right="", right_col=MUTED, back=True):
    d.rectangle((0, 0, W - 1, HEAD_H - 1), fill=BAR)
    if back:
        d.line((17, 9, 10, 14), fill=TEXT, width=2)
        d.line((10, 14, 17, 19), fill=TEXT, width=2)
    d.text((30 if back else 12, 6), title, font=font(14), fill=TEXT)
    if right:
        f = font(11)
        d.text((W - 10 - text_w(d, right, f), 9), right, font=f, fill=right_col)
    d.line((0, HEAD_H, W - 1, HEAD_H), fill=(28, 38, 54))


def big_btn(d, box, label, sub="", fill=ACCENT, fg=ACCENT_D, radius=16,
            edge=None, pressed=False):
    f2 = lerp(fill, (255, 255, 255), 0.12) if pressed else fill
    pill(d, box, f2, outline=edge or f2, radius=radius, width=2)
    cx = (box[0] + box[2]) / 2
    if sub:
        center_text(d, cx, (box[1] + box[3]) / 2 - 9, label, font(17), fg)
        center_text(d, cx, (box[1] + box[3]) / 2 + 11, sub, font(10),
                    lerp(fg, fill, 0.35))
    else:
        center_text(d, cx, (box[1] + box[3]) / 2, label, font(17), fg)


def inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


# ================================================================ 图标
def icon_chat(d, cx, cy, col):
    pill(d, (cx - 11, cy - 9, cx + 11, cy + 5), col, radius=6)
    d.polygon([(cx - 5, cy + 4), (cx - 5, cy + 11), (cx + 3, cy + 4)], fill=col)
    for dx in (-4, 4):
        dot(d, cx + dx, cy - 2, BG, 1.8)


def icon_sun(d, cx, cy, col):
    dot(d, cx, cy, col, 6)
    for i in range(8):
        a = i * math.pi / 4
        d.line((cx + math.cos(a) * 8, cy + math.sin(a) * 8,
                cx + math.cos(a) * 11, cy + math.sin(a) * 11), fill=col, width=2)


def icon_cloud(d, cx, cy, col):
    dot(d, cx - 4, cy + 1, col, 5)
    dot(d, cx + 4, cy - 1, col, 6)
    dot(d, cx + 9, cy + 2, col, 4)
    d.rectangle((cx - 9, cy + 1, cx + 12, cy + 6), fill=col)


def icon_weather(d, cx, cy, col, kind="sun", r=1.0):
    if kind == "sun":
        icon_sun(d, cx, cy, col)
    elif kind == "sun_cloud":
        icon_sun(d, cx + 5, cy - 5, WARM)
        icon_cloud(d, cx - 2, cy + 3, col)
    elif kind == "rain":
        icon_cloud(d, cx, cy - 3, col)
        for dx in (-5, 2, 9):
            d.line((cx + dx, cy + 6, cx + dx - 2, cy + 11), fill=BLUE, width=2)
    elif kind == "snow":
        icon_cloud(d, cx, cy - 3, col)
        for dx in (-5, 2, 9):
            dot(d, cx + dx, cy + 9, (235, 245, 255), 1.5)
    elif kind == "fog":
        for i, y in enumerate((-4, 2, 8)):
            d.line((cx - 10 + i, cy + y, cx + 10 - i, cy + y), fill=col, width=2)
    elif kind == "thunder":
        icon_cloud(d, cx, cy - 4, col)
        d.polygon([(cx - 2, cy + 4), (cx + 4, cy + 4), (cx + 1, cy + 8),
                   (cx + 3, cy + 8), (cx - 3, cy + 13), (cx - 1, cy + 8),
                   (cx - 4, cy + 8)], fill=WARM)
    else:
        icon_cloud(d, cx, cy, col)


def icon_env(d, cx, cy, col):
    # 温度计
    d.rounded_rectangle((cx - 11, cy - 10, cx - 5, cy + 5), radius=3,
                        outline=col, width=2)
    dot(d, cx - 8, cy + 8, col, 5)
    d.line((cx - 8, cy + 2, cx - 8, cy + 6), fill=col, width=2)
    # 水滴
    d.polygon([(cx + 4, cy - 6), (cx + 9, cy + 2), (cx - 1, cy + 2)], fill=col)
    d.ellipse((cx - 1, cy - 1, cx + 9, cy + 7), fill=col)


def icon_car(d, cx, cy, col):
    d.rounded_rectangle((cx - 11, cy - 7, cx + 11, cy + 4), radius=5,
                        outline=col, width=2)
    d.line((cx - 6, cy + 4, cx - 6, cy + 7), fill=col, width=2)
    d.line((cx + 6, cy + 4, cx + 6, cy + 7), fill=col, width=2)
    dot(d, cx - 7, cy + 8, col, 3)
    dot(d, cx + 7, cy + 8, col, 3)
    d.line((cx - 4, cy - 7, cx + 4, cy - 7), fill=col, width=3)


def icon_mic(d, cx, cy, col):
    d.rounded_rectangle((cx - 4, cy - 12, cx + 4, cy + 1), radius=4, fill=col)
    d.arc((cx - 9, cy - 8, cx + 9, cy + 6), 0, 180, fill=col, width=2)
    d.line((cx, cy + 6, cx, cy + 11), fill=col, width=2)
    d.line((cx - 5, cy + 11, cx + 5, cy + 11), fill=col, width=2)


def icon_gear(d, cx, cy, col):
    for i in range(8):
        a = i * math.pi / 4
        d.line((cx + math.cos(a) * 6, cy + math.sin(a) * 6,
                cx + math.cos(a) * 11, cy + math.sin(a) * 11), fill=col, width=3)
    d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=col, width=2)
    dot(d, cx, cy, col, 2)


def icon_tiny(d, cx, cy, col, kind="sun"):
    """14px 的小图标，给时钟卡片那行天气用"""
    if kind in ("sun", "sun_cloud"):
        dot(d, cx, cy, WARM, 3.5)
        for i in range(4):
            a = i * math.pi / 2 + math.pi / 4
            d.line((cx + math.cos(a) * 5.5, cy + math.sin(a) * 5.5,
                    cx + math.cos(a) * 7.5, cy + math.sin(a) * 7.5),
                   fill=WARM, width=1)
        if kind == "sun_cloud":
            dot(d, cx + 2, cy + 4, col, 3)
            d.rectangle((cx - 1, cy + 4, cx + 8, cy + 7), fill=col)
    elif kind == "rain":
        dot(d, cx - 2, cy, col, 3)
        dot(d, cx + 3, cy - 1, col, 3.5)
        d.rectangle((cx - 5, cy, cx + 7, cy + 3), fill=col)
        d.line((cx, cy + 5, cx - 1, cy + 7), fill=BLUE, width=1)
    else:
        dot(d, cx - 2, cy + 1, col, 3)
        dot(d, cx + 3, cy, col, 3.5)
        d.rectangle((cx - 5, cy + 1, cx + 7, cy + 4), fill=col)


ICONS = {"chat": icon_chat, "weather": icon_weather, "env": icon_env,
         "drive": icon_car, "voice": icon_mic, "sys": icon_gear}

# ================================================================ 主页
TILE_W, TILE_H = 106, 56
TILE_X = (10, 124)
TILE_Y = (116, 178, 240)
CLOCK_BOX = (10, 30, 230, 112)

TILE_DEFS = [
    ("chat", "对话"), ("weather", "天气"),
    ("env", "温湿度"), ("drive", "遥控"),
    ("voice", "语音控车"), ("sys", "系统"),
]

TILES = []                       # [(key, label, box)]
for _i, (_k, _l) in enumerate(TILE_DEFS):
    _r, _c = divmod(_i, 2)
    TILES.append((_k, _l, (TILE_X[_c], TILE_Y[_r],
                           TILE_X[_c] + TILE_W, TILE_Y[_r] + TILE_H)))


def render_home(v):
    """v: clock date wtext wkind car_ok net_ok press"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "小派", getattr(v, "clock", ""), back=False)

    card(d, CLOCK_BOX)
    center_text(d, 120, 50, getattr(v, "clock", "--:--"), font(38), TEXT)
    center_text(d, 120, 78, getattr(v, "date", ""), font(11), MUTED)

    wtext = getattr(v, "wtext", "")
    if wtext:
        f = font(11)
        tw = text_w(d, wtext, f)
        x = 120 - tw / 2 + 8
        icon_tiny(d, x - 12, 97, (226, 234, 244), getattr(v, "wkind", "sun"))
        d.text((x, 91), wtext, font=f, fill=MUTED)

    press = getattr(v, "press", None)
    for key, label, box in TILES:
        card(d, box, (32, 43, 60) if press == key else CARD)
        cx, cy = (box[0] + box[2]) / 2, box[1] + 18
        col = ACCENT if press == key else TEXT
        if key == "weather":
            icon_weather(d, cx, cy - 1, col, getattr(v, "wkind", "sun"))
        else:
            ICONS[key](d, cx, cy, col)
        center_text(d, cx, box[1] + 41, label, font(12), TEXT)
        # 右上角状态点
        if key == "drive":
            dot(d, box[2] - 9, box[1] + 9, car_link(v)[1], 3)
        elif key == "weather":
            dot(d, box[2] - 9, box[1] + 9, GREEN if getattr(v, "net_ok", False) else (70, 82, 100), 3)

    center_text(d, 120, 306, "轻触磁贴进入 · 点左上角返回", font(9), (88, 102, 122))
    return img


def hit_home(x, y):
    for key, _label, box in TILES:
        if inside(box, x, y):
            return key
    return None


# ================================================================ 天气
def _cell(d, box, label, value, col=TEXT):
    card(d, box)
    d.text((box[0] + 8, box[1] + 5), label, font=font(9), fill=MUTED)
    d.text((box[0] + 8, box[1] + 17), value, font=font(15), fill=col)


def render_weather(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "天气", getattr(v, "city", ""), back=True)

    card(d, (10, 32, 230, 150))
    icon_weather(d, 58, 78, (222, 232, 244), getattr(v, "kind", "sun"))
    d.text((104, 42), getattr(v, "temp", "--"), font=font(44), fill=TEXT)
    d.text((106, 100), getattr(v, "desc", "—"), font=font(14), fill=ACCENT)
    d.text((106, 122), "体感 " + getattr(v, "feels", "--"), font=font(11), fill=MUTED)

    _cell(d, (10, 158, 116, 198), "最高 / 最低", getattr(v, "hilo", "--"), WARM)
    _cell(d, (124, 158, 230, 198), "降水概率", getattr(v, "pop", "--"), BLUE)
    _cell(d, (10, 204, 116, 244), "湿度", getattr(v, "hum", "--"), TEXT)
    _cell(d, (124, 204, 230, 244), "风速", getattr(v, "wind", "--"), TEXT)

    card(d, (10, 250, 230, 310))
    lines = getattr(v, "note", [])
    y = 257
    for i, line in enumerate(lines[:2]):
        d.text((18, y), line, font=font(10 if i == 0 else 9),
               fill=MUTED if i == 0 else (110, 126, 146))
        y += 16
    center_text(d, 120, 298, "轻触屏幕刷新", font(9), (88, 102, 122))
    return img


# ================================================================ 温湿度
def draw_curve(d, box, vals, col, lo=None, hi=None, empty="采集中…"):
    x0, y0, x1, y1 = box
    if len(vals) < 2:
        if empty:
            center_text(d, (x0 + x1) / 2, (y0 + y1) / 2, empty, font(10), MUTED)
        return
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 0.5:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.5, mid + 0.5
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = x0 + (x1 - x0) * i / (n - 1)
        y = y1 - (y1 - y0) * (v - lo) / (hi - lo)
        pts.append((x, y))
    d.polygon(pts + [(x1, y1), (x0, y1)], fill=lerp(CARD, col, 0.16))
    d.line(pts, fill=col, width=2)
    d.ellipse((pts[-1][0] - 2.5, pts[-1][1] - 2.5,
               pts[-1][0] + 2.5, pts[-1][1] + 2.5), fill=col)


def render_env(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "温湿度", getattr(v, "src", ""), back=True)

    for i, (label, val, unit, col) in enumerate((
            (getattr(v, "tlabel", "室内温度"), getattr(v, "temp", "--"), "°C", WARM),
            (getattr(v, "hlabel", "相对湿度"), getattr(v, "hum", "--"), "%", BLUE))):
        box = (10 + i * 114, 32, 116 + i * 114, 104)
        card(d, box)
        d.text((box[0] + 9, box[1] + 7), label, font=font(10), fill=MUTED)
        d.text((box[0] + 9, box[1] + 26), str(val), font=font(30), fill=col)
        tw = text_w(d, str(val), font(30))
        d.text((box[0] + 13 + tw, box[1] + 44), unit, font=font(11), fill=MUTED)
        d.text((box[0] + 9, box[1] + 62), getattr(v, "trend", ["", ""])[i],
               font=font(9), fill=(96, 112, 132))

    card(d, (10, 110, 230, 226))
    d.text((18, 116), getattr(v, "curve_title", "最近 2 分钟"), font=font(9), fill=MUTED)
    draw_curve(d, (18, 132, 222, 218), list(getattr(v, "temps", [])), WARM,
               empty=getattr(v, "curve_empty", "采集中…"))
    d.text((18, 200), getattr(v, "lo", ""), font=font(9), fill=(96, 112, 132))
    d.text((188, 200), getattr(v, "hi", ""), font=font(9), fill=(96, 112, 132))

    box = (10, 232, 230, 300)
    card(d, box)
    y = 240
    for line, col in getattr(v, "notes", []):
        d.text((18, y), line, font=font(10), fill=col)
        y += 16
    return img


# ================================================================ 遥控
DPAD = {
    "up": (98, 100, 142, 142),
    "down": (98, 200, 142, 242),
    "left": (50, 148, 90, 190),
    "right": (150, 148, 190, 190),
    "stop": (98, 150, 142, 192),
}
SPEED_MINUS = (14, 248, 58, 288)
SPEED_PLUS = (182, 248, 226, 288)
SPEED_BOX = (62, 248, 178, 288)

# 手势开关：放在「↑」左边那块空白。为什么不贴着「←」——
# 左边那条 4px 缝太窄，演示时手一抖就误触；这里离「↑」有 52px，
# 想按错都难。开着的时候描绿边，一瞥就知道摄像头在不在看。
GEST_BTN = (10, 100, 46, 146)


def _arrow(d, box, direction, col):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    s = 8
    if direction == "up":
        d.polygon([(cx, cy - s), (cx - s, cy + s * 0.7), (cx + s, cy + s * 0.7)], fill=col)
    elif direction == "down":
        d.polygon([(cx, cy + s), (cx - s, cy - s * 0.7), (cx + s, cy - s * 0.7)], fill=col)
    elif direction == "left":
        d.polygon([(cx - s, cy), (cx + s * 0.7, cy - s), (cx + s * 0.7, cy + s)], fill=col)
    else:
        d.polygon([(cx + s, cy), (cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s)], fill=col)


def render_drive(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ok = getattr(v, "car_ok", False)
    txt, col = car_link(v)
    header(d, "遥控", txt, right_col=col, back=True)

    card(d, (10, 32, 230, 96))
    for i, (label, val, col) in enumerate((
            ("俯仰", getattr(v, "angle", "--"), WARM),
            ("速度", getattr(v, "speed", "--"), TEXT),
            ("航向", getattr(v, "yaw", "--"), ACCENT))):
        cx = 48 + i * 72
        center_text(d, cx, 51, label, font(9), MUTED)
        center_text(d, cx, 68, str(val), font(15), col)
    flags = getattr(v, "flags", 0)
    tag, tcol = "运行中", GREEN
    if flags & 4:
        tag, tcol = "跌倒锁定·按Key1解", RED
    elif flags & 8:
        # ★这一位以前没人显示，用户只看到"运行中"，可车端正拒绝一切转向，
        # 表现就是"按键没反应"，完全查不出原因。
        tag, tcol = "陀螺没标好·断电静止再上电", RED
    elif flags & 2:
        tag, tcol = "转向中", BLUE
    elif not (flags & 1):
        tag, tcol = "待机", MUTED
    center_text(d, 120, 87, tag, font(9), tcol)

    press = getattr(v, "press", None)
    for name, box in DPAD.items():
        on = press == name
        fill = (46, 60, 82) if on else (28, 38, 52)
        edge = ACCENT if on else CARD_EDGE
        pill(d, box, fill, outline=edge, radius=12, width=2 if on else 1)
        if name == "stop":
            center_text(d, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, "停",
                        font(20), RED if not on else (255, 200, 200))
        else:
            _arrow(d, box, name, ACCENT if on else TEXT)

    # 手势开关
    gon = getattr(v, "gest", False)
    pill(d, GEST_BTN, (30, 52, 42) if gon else (28, 38, 52),
         outline=GREEN if gon else CARD_EDGE, radius=12, width=2 if gon else 1)
    gcx = (GEST_BTN[0] + GEST_BTN[2]) / 2
    center_text(d, gcx, 115, "手势", font(10), GREEN if gon else MUTED)
    center_text(d, gcx, 132, "开" if gon else "关", font(11),
                GREEN if gon else (88, 102, 122))

    # 速度档
    for box, sign in ((SPEED_MINUS, "-"), (SPEED_PLUS, "+")):
        on = press == ("slower" if sign == "-" else "faster")
        pill(d, box, (46, 60, 82) if on else (28, 38, 52), outline=CARD_EDGE, radius=12)
        center_text(d, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, sign, font(20), TEXT)
    card(d, SPEED_BOX)
    center_text(d, 120, 255, "速度", font(9), MUTED)
    center_text(d, 120, 267, getattr(v, "spd", "1.4"), font(16), ACCENT)

    center_text(d, 120, 300, "按住方向键行驶 · 松手即停", font(9), (88, 102, 122))
    center_text(d, 120, 311, getattr(v, "hint", ""), font(9), ACCENT)
    return img


def hit_drive(x, y):
    for name, box in DPAD.items():
        if inside(box, x, y):
            return name
    if inside(SPEED_MINUS, x, y):
        return "slower"
    if inside(SPEED_PLUS, x, y):
        return "faster"
    if inside(GEST_BTN, x, y):
        return "gesture"
    return None


# ================================================================ 语音控车
BTN_TALK = (10, 156, 230, 246)
BTN_STOP = (10, 250, 230, 294)


def render_voice(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ok = getattr(v, "car_ok", False)
    txt, col = car_link(v)
    header(d, "语音控车", txt, right_col=col, back=True)

    card(d, (10, 30, 230, 68))
    d.text((16, 36), getattr(v, "tel", ""), font=font(10), fill=MUTED)
    flags = getattr(v, "flags", 0)
    tag = ("陀螺没标好" if flags & 8 else
           "转向中" if flags & 2 else
           "跌倒锁定" if flags & 4 else
           ("运行中" if flags & 1 else "待机"))
    spd = getattr(v, "spd", "")
    d.text((16, 52), f"状态 {tag}" + (f" · 速度档 {spd}" if spd else ""), font=font(10),
           fill=RED if flags & 4 else ACCENT)

    card(d, (10, 76, 230, 150))
    d.text((18, 82), "你说", font=font(9), fill=MUTED)
    tg = getattr(v, "teach", "")
    if tg:
        d.text((226, 82), tg, font=font(9), fill=RED, anchor="ra")
    uf = font(13)
    say = getattr(v, "said", "")
    if say:
        line = clip_lines(d, wrap(d, say, uf, 196), uf, 196, 1)[0]
        d.text((18, 94), line, font=uf, fill=TEXT)
    else:
        d.text((18, 94), "前进 / 右转九十度 / 记住这条路", font=font(11),
               fill=(96, 112, 132))
    act = getattr(v, "action", "")
    if act:
        d.text((18, 116), act, font=font(20), fill=ACCENT)
    else:
        d.text((18, 118), "按住下面说话", font=font(13), fill=(96, 112, 132))

    listening = getattr(v, "listening", False)
    if listening:
        big_btn(d, BTN_TALK, "松手发送", f"已录 {int(getattr(v, 'secs', 0))}s",
                fill=(52, 178, 152), fg=(8, 26, 24))
    else:
        big_btn(d, BTN_TALK, "按住说话",
                "说完松手，立刻执行" if ok else
                ("掉线了，正在自动重连" if getattr(v, "car_lost", False) else "车没连上，先给车上电"),
                fill=ACCENT if ok else (44, 56, 74),
                fg=ACCENT_D if ok else MUTED, pressed=getattr(v, "press", None) == "talk")

    big_btn(d, BTN_STOP, "停车", fill=RED_D, fg=(255, 214, 214), edge=RED, radius=14,
            pressed=getattr(v, "press", None) == "stop")
    center_text(d, 120, 302, "示教：记住这条路 → 指挥走 → 记好了", font(9), (88, 102, 122))
    return img


def hit_voice(x, y):
    if inside(BTN_TALK, x, y):
        return "talk"
    if inside(BTN_STOP, x, y):
        return "stop"
    return None


# ================================================================ 系统
SYS_FAN_BOX = (10, 36, 230, 64)        # 第一行是风扇开关，和 hub.py 共用这个热区


def render_sys(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    header(d, "系统", getattr(v, "clock", ""), back=True)

    rows = getattr(v, "rows", [])
    y = 36
    f = font(11)
    for i, (label, value, col) in enumerate(rows):
        box = (10, y, 230, y + 28)
        if i == 0:
            # 风扇这一行可以点，所以描边 + 右侧留出箭头的空
            pill(d, box, (26, 36, 53), outline=ACCENT_D, radius=8, width=1)
            d.text((18, y + 7), label, font=f, fill=MUTED)
            tw = text_w(d, value, f)
            d.text((210 - tw, y + 7), value, font=f, fill=col)
            d.line((219, y + 10, 223, y + 14), fill=MUTED, width=2)
            d.line((223, y + 14, 219, y + 18), fill=MUTED, width=2)
        else:
            card(d, box, (23, 32, 47) if i % 2 == 0 else (27, 37, 53), radius=8)
            d.text((18, y + 7), label, font=f, fill=MUTED)
            d.text((222 - text_w(d, value, f), y + 7), value, font=f, fill=col)
        y += 32

    center_text(d, 120, 304, "点风扇行切换 自动/开/关", font(9), (88, 102, 122))
    return img


# ================================================================ 人脸验证（开机门禁）
LOCK_RETRY = (24, 228, 116, 264)
LOCK_SKIP = (124, 228, 216, 264)


def _face_hint(d, cx, cy):
    """框里画一个很淡的头肩轮廓，告诉人「把脸放这儿」。

    比一行字管用：摄像头挂在机身侧面，人很容易对着屏幕找镜头。
    """
    col = (52, 68, 92)
    d.ellipse((cx - 17, cy - 24, cx + 17, cy + 10), outline=col, width=2)
    d.arc((cx - 30, cy + 6, cx + 30, cy + 56), 180, 360, fill=col, width=2)


def render_lock(v):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    st = getattr(v, "state", "checking")
    col = {"checking": ACCENT, "ok": GREEN, "fail": RED,
           "wait": WARM}.get(st, ACCENT)

    center_text(d, 120, 46, "人脸验证", font(17), TEXT)
    center_text(d, 120, 68, getattr(v, "sub", "小派只认主人"), font(10), MUTED)

    cx, cy, r = 120, 140, 46
    box = (cx - r, cy - r, cx + r, cy + r)

    if st in ("idle", "checking"):
        _face_hint(d, cx, cy)      # 只有"正在认人"时才画头肩，配问号会显乱
    d.ellipse(box, outline=(34, 46, 66), width=2)
    if st in ("idle", "checking"):
        a = (getattr(v, "phase", 0.0) * 360.0) % 360.0
        d.arc(box, a, a + 100, fill=ACCENT, width=3)
        d.arc(box, a + 180, a + 280, fill=ACCENT_D, width=3)
    elif st == "ok":
        d.ellipse(box, outline=GREEN, width=3)
        d.line((cx - 17, cy, cx - 4, cy + 13), fill=GREEN, width=4)
        d.line((cx - 4, cy + 13, cx + 19, cy - 13), fill=GREEN, width=4)
    elif st == "fail":
        d.ellipse(box, outline=RED, width=3)
        d.line((cx - 13, cy - 13, cx + 13, cy + 13), fill=RED, width=4)
        d.line((cx + 13, cy - 13, cx - 13, cy + 13), fill=RED, width=4)
    elif st == "wait":
        d.ellipse(box, outline=WARM, width=2)
        center_text(d, cx, cy, "?", font(38), WARM)

    center_text(d, 120, 204, getattr(v, "msg", ""), font(12), col)

    hold = float(getattr(v, "skip_hold", 0.0) or 0.0)
    for box_, key, label in ((LOCK_RETRY, "retry", "重试"),
                             (LOCK_SKIP, "skip", "长按跳过")):
        press = getattr(v, "press", None) == key
        hot = key == "skip" and hold > 0
        fill = lerp(CARD, ACCENT_D, 0.55) if press else CARD
        if hot:
            fill = lerp(CARD, RED_D, min(1.0, hold) * 0.85)
        pill(d, box_, fill, outline=CARD_EDGE, radius=12)
        txt = f"{int(hold * 100)}%" if hot else label
        center_text(d, (box_[0] + box_[2]) / 2, (box_[1] + box_[3]) / 2,
                    txt, font(12), (255, 220, 220) if hot else TEXT)

    tip = "正在进入…" if st == "ok" else "认出主人自动进入"
    center_text(d, 120, 288, tip, font(9), (88, 102, 122))
    return img


def hit_lock(x, y):
    if inside(LOCK_RETRY, x, y):
        return "retry"
    if inside(LOCK_SKIP, x, y):
        return "skip"
    return None


def render_splash(text="正在启动……"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    center_text(d, 120, 130, "小派", font(30), TEXT)
    center_text(d, 120, 168, text, font(12), MUTED)
    return img
