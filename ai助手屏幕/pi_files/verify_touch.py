"""触摸实测：屏幕上实时显示 IRQ / 原始坐标 / 屏幕坐标，按一下就能看出来"""
import sys, time
sys.path.insert(0, "/home/pi")
import screen
from PIL import Image, ImageDraw

lcd = screen.open_display()
touch, irq = screen.open_touch()

img = Image.new("RGB", (screen.W, screen.H), (12, 16, 24))
d = ImageDraw.Draw(img)
d.text((16, 40), "TOUCH TEST", fill=(120, 220, 180))
d.text((16, 64), "press the screen", fill=(140, 160, 180))
lcd.show(img)

log = open("/tmp/vt.log", "w", buffering=1)
n = 0
last_draw = 0.0
t_end = time.time() + 90
while time.time() < t_end:
    if irq.is_active:
        x, y = touch.read_xy(samples=3)
        sx, sy = screen.to_screen(x, y)
        n += 1
        log.write(f"{time.time():.2f} raw=({x},{y}) scr=({sx},{sy})\n")
        if time.time() - last_draw > 0.15:
            last_draw = time.time()
            d.rectangle((0, 90, 240, 260), fill=(12, 16, 24))
            d.text((16, 100), f"raw  {x}, {y}", fill=(255, 220, 120))
            d.text((16, 126), f"scr  {sx}, {sy}", fill=(255, 220, 120))
            d.text((16, 152), f"count {n}", fill=(120, 220, 180))
            # 十字标记
            if 0 <= sx < 240 and 0 <= sy < 320:
                d.line((sx - 8, sy, sx + 8, sy), fill=(255, 90, 90), width=2)
                d.line((sx, sy - 8, sx, sy + 8), fill=(255, 90, 90), width=2)
            lcd.show(img)
        time.sleep(0.05)
    else:
        time.sleep(0.02)

log.write(f"end total={n}\n")
lcd.fill((0, 0, 0)); lcd.bl.off()
