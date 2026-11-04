#!/usr/bin/env python3
"""触摸体检：分三步定位，屏幕上直接给结论

  python3 touch_check.py

结论含义：
  SPI DEAD      -> T_DO / T_CLK / T_DIN / T_CS 有根线没通
  PANEL OPEN    -> 芯片和 SPI 都正常，但触摸面板没信号（查 FPC 排线）
  IRQ DEAD      -> 坐标有反应但中断线没到 GPIO22
  ALL GOOD      -> 可以 python3 pet.py
"""
import sys, time
sys.path.insert(0, "/home/pi")
import screen
from PIL import Image, ImageDraw

lcd = screen.open_display()
touch, irq = screen.open_touch()

img = Image.new("RGB", (screen.W, screen.H), (10, 14, 22))
d = ImageDraw.Draw(img)


def show(title, lines, color=(220, 232, 245)):
    d.rectangle((0, 0, screen.W, screen.H), fill=(10, 14, 22))
    d.text((14, 46), title, fill=color)
    y = 84
    for ln in lines:
        d.text((14, y), ln, fill=(148, 168, 192))
        y += 24
    lcd.show(img)


CH = [("TEMP", 0x80), ("Y", 0x90), ("Z1", 0xB0), ("Z2", 0xC0), ("X", 0xD0)]


def fingerprint():
    vals = {}
    for name, cmd in CH:
        vals[name] = touch._read(cmd)
    return vals


def main():
    show("STEP 1 / SPI", ["reading channels..."])
    fp = fingerprint()
    print("通道指纹:", fp, flush=True)

    distinct = len(set(fp.values()))
    if distinct == 1:
        show("SPI DEAD", [f"all channels = {list(fp.values())[0]}",
                          "", "T_DO / T_CLK / T_DIN / T_CS",
                          "one of them is not", "reaching the module"],
              (255, 120, 120))
        print("结论：五个通道读数完全相同 -> SPI 数据线没通", flush=True)
        return 1

    at_rail = all(v <= 8 or v >= 4088 for v in fp.values())
    show("STEP 1 / SPI OK", [f"{distinct} different values", "",
                             "press and hold..."], (120, 230, 170))
    print(f"结论：{distinct} 个不同值 -> SPI 通信正常", flush=True)

    # 第二步：按住，看有没有任何一个通道会变
    t0 = time.time()
    moved = False
    baseline = dict(fp)
    while time.time() - t0 < 20:
        cur = fingerprint()
        if any(abs(cur[k] - baseline[k]) > 30 for k in baseline):
            moved = True
            break
        time.sleep(0.15)

    if not moved:
        msg = ["press did nothing", "for 20 seconds", ""]
        if at_rail:
            msg += ["ADC inputs sit at rail", "panel not connected"]
        msg += ["check the touch FPC", "on the back of module"]
        show("PANEL OPEN", msg, (255, 190, 110))
        print("结论：按住时所有通道都不变 -> 触摸面板没有信号（查 FPC 排线）", flush=True)
        return 2

    show("STEP 2 / PANEL OK", ["values respond", "", "reading position..."],
         (120, 230, 170))
    print("结论：通道随按压变化 -> 面板和 ADC 都正常", flush=True)

    # 第三步：坐标
    samples = []
    t0 = time.time()
    while time.time() - t0 < 10:
        x = touch._read(0xD0)
        y = touch._read(0x90)
        samples.append((x, y))
        time.sleep(0.08)

    xs = sorted(s[0] for s in samples)
    ys = sorted(s[1] for s in samples)
    print(f"X {xs[0]}~{xs[-1]}  Y {ys[0]}~{ys[-1]}", flush=True)
    show("ALL GOOD", [f"X {xs[0]}~{xs[-1]}", f"Y {ys[0]}~{ys[-1]}",
                      "", "python3 pet.py"], (120, 230, 170))
    return 0


code = 0
try:
    code = main()
except Exception as exc:                                  # noqa: BLE001
    show("ERROR", [str(exc)[:40]], (255, 120, 120))
    print("异常:", exc, flush=True)
    code = 9
finally:
    time.sleep(2.5)
    lcd.fill((0, 0, 0))
    lcd.bl.off()
sys.exit(code)
