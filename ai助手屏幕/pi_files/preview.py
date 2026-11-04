#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑上渲染界面预览图（不碰硬件）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import petui  # noqa: E402


class S:
    def __init__(self, **kw):
        self.mode = "idle"
        self.user_text = ""
        self.reply_text = ""
        self.partial = ""
        self.level = 0.0
        self.muted = False
        self.auto_listen = False
        self.ptt_seconds = 0.0
        self.look = (0.0, 0.0)
        self.blink = False
        self.press_region = None
        self.press_seconds = 0.0
        self.t = 0.0
        self.__dict__.update(kw)


CASES = {
    "01_idle": S(mode="idle", t=0.8,
                 user_text="今天天气怎么样",
                 reply_text="外面阴沉沉的，可能下午会下雨，出门带把伞吧。"),
    "02_listening": S(mode="listening", t=1.4, level=0.55, ptt_seconds=1.8,
                      look=(0.4, 0.2), press_region="btn_ptt"),
    "03_thinking": S(mode="thinking", t=2.1, user_text="讲个冷笑话",
                     reply_text=""),
    "04_speaking": S(mode="speaking", t=3.2, level=0.8, muted=False,
                     user_text="你是谁呀", reply_text="我是小派，住在这块屏幕后面。"),
    "05_happy": S(mode="happy", t=2.6, user_text="摸摸头",
                  reply_text="嘿嘿，再摸一下嘛。", auto_listen=True),
    "06_mute": S(mode="idle", t=5.0, muted=True, auto_listen=False,
                 user_text="帮我记住明天要买牛奶",
                 reply_text="好，我记住了：明天买牛奶。还有别的吗？"),
    "07_longpress": S(mode="idle", t=1.2, press_region="face", press_seconds=0.62,
                      user_text="咱们换个话题",
                      reply_text="长按我的脸可以清空之前聊的东西。"),
}

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    for name, st in CASES.items():
        img = petui.render(st)
        path = os.path.join(out, f"preview_{name}.png")
        img.save(path)
        print("wrote", path)
    # 拼一张总览图（放大 1.5 倍）
    imgs = [petui.render(st) for st in CASES.values()]
    sw, sh = petui.W * 3, petui.H * 2
    sheet = __import__("PIL.Image", fromlist=["Image"]).new("RGB", (sw, sh), (0, 0, 0))
    for i, im in enumerate(imgs):
        sheet.paste(im, ((i % 3) * petui.W, (i // 3) * petui.H))
    sheet = sheet.resize((int(sw * 1.1), int(sh * 1.1)))
    sheet.save(os.path.join(out, "preview_sheet.png"))
    print("wrote sheet")
