#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preview_hub.py —— 在电脑上把小派控制中心的 6 个页面渲染成 PNG

不需要树莓派，也不用接屏幕：hubui/petui 都只依赖 PIL。
    python preview_hub.py            # 输出到 ../preview/
"""

import math
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import petui                                            # noqa: E402
import hubui                                            # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "preview")
os.makedirs(OUT, exist_ok=True)


def save(img, name):
    path = os.path.join(OUT, name)
    img.save(path)
    print(f"  {name}  {img.size[0]}x{img.size[1]}")
    return path


def home():
    v = types.SimpleNamespace(clock="12:39", date="10月30日 周五",
                              wtext="18° 多云", wkind="sun_cloud",
                              car_ok=True, net_ok=True, press=None)
    return hubui.render_home(v)


def weather():
    v = types.SimpleNamespace(city="资阳", temp="18°", desc="多云", kind="sun_cloud",
                              feels="17°", hilo="22° / 13°", pop="20%",
                              hum="68%", wind="3.2 m/s",
                              note=["更新 12:30 · open-meteo",
                                    "数据来源：免费公开接口，无需密钥"])
    return hubui.render_weather(v)


def env():
    temps = [21.4 + 0.9 * math.sin(i / 5.0) + 0.15 * math.sin(i / 1.7)
             for i in range(48)]
    v = types.SimpleNamespace(src="DHT22 · GPIO4", temp="23.4", hum="46",
                              trend=["实时", "实时"], temps=temps,
                              curve_title="最近 1 分 36 秒",
                              lo=f"{min(temps):.1f}°", hi=f"{max(temps):.1f}°",
                              notes=[("采样间隔 2 秒 · 共 48 点", petui.MUTED),
                                     ("DHT22 精度 ±0.5°C / ±2%RH", petui.MUTED),
                                     ("树莓派 CPU 44.3°C", petui.ACCENT)])
    return hubui.render_env(v)


def drive():
    v = types.SimpleNamespace(car_ok=True, connecting=False, angle="+1.2°",
                              speed="+0.00", yaw="+91°", flags=1, spd="1.4",
                              press="up", hint="行驶中，松手停车")
    return hubui.render_drive(v)


def voice():
    v = types.SimpleNamespace(car_ok=True, tel="俯仰 +1.2°   速度 +0.00   航向 +91°",
                              flags=2, said="右转九十度", action="右转 90°",
                              listening=False, secs=0, press=None, spd="1.4")
    return hubui.render_voice(v)


def voice_listening():
    v = types.SimpleNamespace(car_ok=True, tel="俯仰 +1.2°   速度 +0.00   航向 +91°",
                              flags=0, said="", action="", listening=True, secs=2.4,
                              press="talk", spd="1.4")
    return hubui.render_voice(v)


def sys_page():
    rows = [("CPU 温度", "44.3 °C", hubui.WARM),
            ("负载 (1/5/15)", "0.42 0.31 0.20", petui.TEXT),
            ("内存", "1.2/3.8 GB (33%)", petui.TEXT),
            ("磁盘", "12.4/29.0 GB (43%)", petui.TEXT),
            ("本机地址", "192.168.137.160", petui.ACCENT),
            ("运行时长", "3 小时 12 分", petui.TEXT),
            ("小车蓝牙", "已连接", hubui.GREEN),
            ("语音模型", "sherpa-onnx 就绪", petui.TEXT)]
    return hubui.render_sys(types.SimpleNamespace(clock="12:39", rows=rows))


def chat():
    st = petui
    s = types.SimpleNamespace(
        mode="speaking", t=1.0, look=(0.3, 0.2), blink=False, level=0.6,
        auto_listen=False, muted=False, press_region=None, press_seconds=0.0,
        user_text="今天天气怎么样", partial="", ptt_seconds=0.0,
        reply_text="外面阴沉沉的，可能下午会下雨，出门带把伞吧。",
        show_back=True)
    return petui.render(s)


def chat_listening():
    s = types.SimpleNamespace(
        mode="listening", t=2.0, look=(0.0, 0.1), blink=False, level=0.75,
        auto_listen=True, muted=False, press_region="btn_ptt", press_seconds=1.2,
        user_text="", partial="帮我看看", ptt_seconds=2.4,
        reply_text="", show_back=True)
    return petui.render(s)


PAGES = [
    ("hub_home.png", home),
    ("hub_weather.png", weather),
    ("hub_env.png", env),
    ("hub_drive.png", drive),
    ("hub_voice.png", voice),
    ("hub_voice_listen.png", voice_listening),
    ("hub_sys.png", sys_page),
    ("hub_chat.png", chat),
    ("hub_chat_listen.png", chat_listening),
]


def main():
    print(f"输出目录：{OUT}")
    imgs = []
    for name, fn in PAGES:
        img = fn()
        save(img, name)
        imgs.append(img)

    # 拼一张总览图，方便一眼看排版
    cols, rows = 5, 2
    sheet = petui.Image.new("RGB", (cols * 250 + 10, rows * 330 + 10), (20, 24, 32))
    for i, img in enumerate(imgs):
        r, c = divmod(i, cols)
        sheet.paste(img, (10 + c * 250, 10 + r * 330))
    save(sheet, "hub_all.png")


if __name__ == "__main__":
    main()
