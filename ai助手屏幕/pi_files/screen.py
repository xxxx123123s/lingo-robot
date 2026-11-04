#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ST7789 240x320 + XPT2046 触摸的硬件层（树莓派 SPI0）

只做三件事：
  1. 初始化屏幕、整帧推送（240x320 RGB565，约 30ms 一帧）
  2. 读触摸原始值，按校准参数换算成屏幕像素
  3. 把"按下 / 拖动 / 抬起"变成事件对象，扔进队列

接线（BCM / 物理引脚），和打地鼠那份完全一致：
  显示：SCLK=GPIO11(23) MOSI=GPIO10(19) CS=CE0/GPIO8(24)
        DC=GPIO25(22)   RST=GPIO24(18)   BL=GPIO18(12)
  触摸：T_CLK=GPIO11  T_DIN=GPIO10  T_DO=GPIO9(21)
        T_CS=GPIO6(31)  T_IRQ=GPIO22(15)
"""

import json
import os
import queue
import statistics
import threading
import time

import numpy as np
from PIL import Image

try:
    import spidev
    from gpiozero import DigitalInputDevice, DigitalOutputDevice
    HAVE_HW = True
except Exception:                                   # 电脑上也能 import，方便预览界面
    spidev = None
    DigitalInputDevice = DigitalOutputDevice = None
    HAVE_HW = False

W, H = 240, 320

PIN_DC, PIN_RST, PIN_BL = 25, 24, 18
# 接线和这张图不一样时，用环境变量改，不用改代码
PIN_TOUCH_CS = int(os.environ.get("LCD_TOUCH_CS", "6"))
PIN_TOUCH_IRQ = int(os.environ.get("LCD_TOUCH_IRQ", "22"))

SPI_HZ_DISPLAY = int(os.environ.get("LCD_SPI_HZ", "40000000"))
SPI_HZ_TOUCH = int(os.environ.get("LCD_TOUCH_SPI_HZ", "500000"))
# 一帧拆成几条传。整帧 153600 字节要连续跑 30ms，而 ST7789 的像素传输
# 没有超时机制：中间只要被 udev、内核枚举之类的事抢掉一瞬，字节流就断了，
# 它以为还在收像素，后面本该是命令的字节会被当数据吃掉，窗口坐标全乱。
# 每次 writebytes 结束时 spidev 会拉高 CS，这正好是 ST7789 的同步点 ——
# 拆成几条，最坏情况也只是某一条花掉，下一条就能对上，不会整屏发白。
FRAME_CHUNK = int(os.environ.get("LCD_CHUNK", "80"))
MADCTL = int(os.environ.get("LCD_MADCTL", "0"))      # 镜像/红蓝反了就改这里
INVERT = os.environ.get("LCD_INVERT", "1") != "0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(BASE_DIR, "touch_cal.json")

# 显示和触摸共用一条 SPI 总线：读触摸时不能被刷屏插进来，否则坐标会花
BUS_LOCK = threading.Lock()

# 默认值就是这台机器上打地鼠校准出来的结果，缺 touch_cal.json 也能用
CAL = {
    "swap": True,
    "xfac": 0.06931207762952694,
    "xoff": -13.99757407728297,
    "yfac": 0.09172809172809172,
    "yoff": -20.497952497952497,
}


def load_cal(path=CAL_FILE):
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return False
    if not all(k in data for k in CAL):
        return False
    CAL.update({k: data[k] for k in CAL})
    return True


def save_cal(path=CAL_FILE):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(CAL, fp, ensure_ascii=False, indent=2)


def to_screen(raw_x, raw_y):
    """触摸原始值 -> 屏幕像素 (0..W-1, 0..H-1)"""
    if CAL["swap"]:
        raw_x, raw_y = raw_y, raw_x
    sx = CAL["xfac"] * raw_x + CAL["xoff"]
    sy = CAL["yfac"] * raw_y + CAL["yoff"]
    return (round(min(max(sx, 0.0), W - 1)),
            round(min(max(sy, 0.0), H - 1)))


# ---------------------------------------------------------------- 屏幕
INIT_SEQ = (
    (0x01, None, 0.15),                                  # SWRESET
    (0x11, None, 0.12),                                  # SLPOUT
    (0x3A, b"\x55", 0.0),                                # COLMOD 16bit
    (0x36, None, 0.0),                                   # MADCTL（下面填）
    (0xB2, bytes((0x0C, 0x0C, 0x00, 0x33, 0x33)), 0.0),  # PORCTRL
    (0xB7, b"\x35", 0.0),                                # GCTRL
    (0xBB, b"\x19", 0.0),                                # VCOMS
    (0xC0, b"\x2C", 0.0),                                # LCMCTRL
    (0xC2, b"\x01", 0.0),                                # VDVVRHEN
    (0xC3, b"\x12", 0.0),                                # VRHS
    (0xC4, b"\x20", 0.0),                                # VDVS
    (0xC6, b"\x0F", 0.0),                                # FRCTRL2
    (0xD0, bytes((0xA4, 0xA1)), 0.0),                    # PWCTRL1
    (0xE0, bytes((0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F,
                  0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23)), 0.0),
    (0xE1, bytes((0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F,
                  0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23)), 0.0),
)


def rgb565(img):
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    buf = ((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)
    return buf.astype(">u2").tobytes()


class ST7789:
    def __init__(self, spi, dc, rst, bl):
        self.spi = spi
        self.dc = dc
        self.rst = rst
        self.bl = bl
        self.last_frame_ms = 0.0

    def init(self):
        self.rst.on()
        time.sleep(0.05)
        self.rst.off()
        time.sleep(0.02)
        self.rst.on()
        time.sleep(0.12)
        for cmd, data, delay in INIT_SEQ:
            if cmd == 0x36:
                data = bytes((MADCTL,))
            self._cmd(cmd, data)
            if delay:
                time.sleep(delay)
        self._cmd(0x21 if INVERT else 0x20)          # INVON / INVOFF
        self._cmd(0x13)                              # NORON
        time.sleep(0.01)
        self._cmd(0x29)                              # DISPON
        time.sleep(0.05)
        self.bl.on()

    def _cmd(self, cmd, data=None):
        self.dc.off()
        self.spi.writebytes([cmd])
        if data:
            self.dc.on()
            self.spi.writebytes(list(data))

    def _window(self, x0, y0, x1, y1):
        self._cmd(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._cmd(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self._cmd(0x2C)

    def show(self, img, y0=0, y1=None):
        """推送一帧；y0/y1 只推其中的横条（局部刷新），返回耗时毫秒"""
        y1 = H if y1 is None else y1
        if FRAME_CHUNK > 0 and (y1 - y0) > FRAME_CHUNK:
            total = 0.0
            for top in range(y0, y1, FRAME_CHUNK):
                total += self.show(img, top, min(top + FRAME_CHUNK, y1))
            self.last_frame_ms = total
            return total
        part = img if (y0 == 0 and y1 == H) else img.crop((0, y0, W, y1))
        buf = rgb565(part)
        t0 = time.perf_counter()
        with BUS_LOCK:
            self._window(0, y0, W - 1, y1 - 1)
            self.dc.on()
            if hasattr(self.spi, "writebytes2"):
                self.spi.writebytes2(buf)
            else:                                    # 老版本 spidev 一次最多 4KB
                for i in range(0, len(buf), 4096):
                    self.spi.writebytes(buf[i:i + 4096])
        self.last_frame_ms = (time.perf_counter() - t0) * 1000
        return self.last_frame_ms

    def fill(self, color):
        self.show(Image.new("RGB", (W, H), color))


# ---------------------------------------------------------------- 触摸
class XPT2046:
    CMD_X, CMD_Y, CMD_Z1 = 0xD0, 0x90, 0xB0

    def __init__(self, spi, cs):
        self.spi = spi
        self.cs = cs
        self._lock = threading.Lock()

    def _read(self, cmd):
        with BUS_LOCK:                               # 命令→等待→读，整段不被打断
            self.cs.off()
            try:
                self.spi.writebytes([cmd])
                time.sleep(0.000008)                 # 等转换完成（最长 6µs）
                rx = self.spi.xfer2([0x00, 0x00])    # 1 位 BUSY + 12 位数据 + 3 个 0
            finally:
                self.cs.on()
        return ((rx[0] << 8) | rx[1]) >> 3

    def read_xy(self, samples=3):
        with self._lock:
            xs, ys = [], []
            for _ in range(samples):
                xs.append(self._read(self.CMD_X))
                ys.append(self._read(self.CMD_Y))
        return int(statistics.median(xs)), int(statistics.median(ys))

    def read_stable(self, timeout=0.15, tol=60):
        """连读两次差值够小才算稳，避免按下的瞬间采到抖动值"""
        prev = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            x, y = self.read_xy()
            if prev and abs(x - prev[0]) < tol and abs(y - prev[1]) < tol:
                return (x + prev[0]) // 2, (y + prev[1]) // 2
            prev = (x, y)
        return prev if prev else (0, 0)


class TouchEvent:
    """kind: down / move / up；x,y 已经是屏幕像素"""
    __slots__ = ("kind", "x", "y", "t", "dur")

    def __init__(self, kind, x, y, t, dur=0.0):
        self.kind, self.x, self.y, self.t, self.dur = kind, x, y, t, dur

    def __repr__(self):
        return f"TouchEvent({self.kind}, {self.x}, {self.y}, {self.dur:.2f}s)"


class TouchPoller(threading.Thread):
    """独立的触摸线程：按下、拖动、抬起都变成事件，主循环只管消费"""

    def __init__(self, touch, irq, events, drag_hz=25, debounce=0.05):
        super().__init__(daemon=True)
        self.touch = touch
        self.irq = irq
        self.events = events
        self.drag_dt = 1.0 / drag_hz
        self.debounce = debounce
        self.running = True
        self.presses = 0

    def run(self):
        pressed = False
        down_t = 0.0
        last_move = 0.0
        last_xy = (0, 0)
        release_t = 0.0
        while self.running:
            active = self.irq.is_active
            now = time.monotonic()
            if active and not pressed and now - release_t > self.debounce:
                raw = self.touch.read_stable(0.12)
                last_xy = to_screen(*raw)
                pressed = True
                down_t = now
                last_move = now
                self.presses += 1
                self.events.put(TouchEvent("down", *last_xy, now))
            elif active and pressed:
                if now - last_move >= self.drag_dt:
                    last_move = now
                    raw = self.touch.read_xy(samples=2)
                    xy = to_screen(*raw)
                    if xy != last_xy:
                        last_xy = xy
                        self.events.put(TouchEvent("move", *xy, now))
            elif not active and pressed:
                pressed = False
                release_t = now
                self.events.put(TouchEvent("up", *last_xy, now, now - down_t))
            time.sleep(0.008)

    def stop(self):
        self.running = False


# ---------------------------------------------------------------- 初始化
def open_display():
    if not HAVE_HW:
        raise SystemExit("缺少硬件库：sudo apt install python3-spidev python3-gpiozero")
    spi_d = spidev.SpiDev()
    spi_d.open(0, 0)
    spi_d.max_speed_hz = SPI_HZ_DISPLAY
    spi_d.mode = 0
    spi_d.bits_per_word = 8

    dc = DigitalOutputDevice(PIN_DC)
    rst = DigitalOutputDevice(PIN_RST)
    bl = DigitalOutputDevice(PIN_BL, initial_value=False)
    lcd = ST7789(spi_d, dc, rst, bl)
    lcd.init()
    return lcd


def open_touch():
    try:
        spi_t = spidev.SpiDev()
        spi_t.open(0, 1)
    except OSError:
        spi_t = spidev.SpiDev()
        spi_t.open(0, 0)
        print("[警告] 没有 /dev/spidev0.1，触摸改用 0.0（会和刷屏抢总线）")
    spi_t.max_speed_hz = SPI_HZ_TOUCH
    spi_t.mode = 0
    spi_t.bits_per_word = 8

    cs = DigitalOutputDevice(PIN_TOUCH_CS, initial_value=True)
    irq = DigitalInputDevice(PIN_TOUCH_IRQ, pull_up=True)
    if irq.is_active:
        print("[警告] 启动时 T_IRQ 已经是低电平，检查 GPIO22 接线")
    return XPT2046(spi_t, cs), irq


def touch_queue():
    return queue.Queue(maxsize=64)
