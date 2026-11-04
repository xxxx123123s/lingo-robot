#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音控车 car.py —— 树莓派麦克风 -> VAD -> sherpa 识别 -> 意图 -> 蓝牙串口 -> 平衡车

和固件（main.c 6-代码优化 版）配套的协议
  Pi -> 车   [go,速度,转向]    速度单位约 -5~+5，正数前进；转向正数右转
             [turn,角度]       相对当前航向转多少度，正数右转，车端闭环
             [stop]            立即停车
  车 -> Pi   [s,俯仰角x10,速度x100,航向角]

设计原则
  * 快通道：意图靠本地关键词表匹配，不联网、不调大模型
  * 慢通道：匹配不上才问 DeepSeek，且只允许它从白名单里挑一个动作，绝不下发 PWM
  * 闭环在车上：Pi 只负责"说清楚要什么"，转到多少度由车自己保证，
    Pi 用遥测只为判断"做完了没有"，不参与控制，因此蓝牙延迟不影响稳定性
  * 所有前进都自带时长上限，说"前进"默认走 2 秒自动停

用法
  python3 car.py --monitor           # 只看遥测，先确认蓝牙通了（建议第一步）
  python3 car.py --dry               # 跑完整语音流程，但不发串口
  python3 car.py                     # 正式语音控车
  python3 car.py --text "右转九十度"  # 不开麦，单测一句的解析结果
  python3 car.py --keys              # 键盘遥控（WASD），排除语音问题用
  python3 car.py --no-llm            # 纯本地，不调大模型

退出：Ctrl+C（退出时一定发 [stop]）
"""
import argparse
import json
import os
import re
import select
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, "/home/pi")
from talk import make_rec, make_vad, speech_seconds, RATE  # noqa: E402

CHUNK = 1600
CONFIG = "/home/pi/car.json"
ROUTE_DB = "/home/pi/routes.json"

# 遥测黑匣子：存在 /tmp/telem.on 才记录，写 /tmp/telem.log。
# 调参（速度环、直行平衡）时全靠它 —— 肉眼看到的"快一点""有点摆"没有数值，
# 落成曲线才能分清是稳态误差还是振荡、周期多少、幅度多大。
_TELEM_SW = "/tmp/telem.on"
_TELEM_LOG = "/tmp/telem.log"

DEFAULT_CFG = {
    "mac": "",                # 车上的蓝牙模块地址。填了就直连蓝牙，不用 rfcomm
    "channel": 1,             # SPP 通道，HC-04/HC-05 都是 1
    "port": "/dev/rfcomm0",   # 没填 mac 时走串口
    "baud": 9600,
    "speed": 1.4,             # 默认前进速度（对应固件 SpeedPID.Target）
    "speed_step": 0.4,
    "speed_min": 0.5,
    "speed_max": 3.2,
    "drive_seconds": 2.0,     # 说"前进"默认走多久
    "hold_max": 8.0,          # 说"一直走"最多走多久，安全上限
    "turn_timeout": 12.0,     # 等车转完的最长时间。360° 约 6.5 秒，转得更大会自动放宽
    "llm_json": "/home/pi/llm.json",
}


# ==================== 串口 / 蓝牙链路 ====================
class Link:
    """后台读线程 + 裸设备读写。
    两种接法：
      * mac   -> 直接用 Python 蓝牙 socket 连（推荐，不用 rfcomm、不占终端、断线能自己重连）
      * port  -> 走 /dev/rfcomm0 或 /dev/ttyAMA0（备用）
    socket 和普通文件描述符都能被 os.read/os.write 操作，所以下面读写代码是共用的。"""

    def __init__(self, port, baud=9600, dry=False, mac=None, channel=1, tries=20, timeout=6):
        self.dry = dry
        self.fd = None
        self.sock = None
        self.mac = mac
        self.channel = channel
        self.tries = tries        # 首次连接最多试几次。触屏端调小，免得车没上电时界面卡太久
        self.timeout = timeout    # 单次 connect 的超时（秒）
        self.alive = False
        self.buf = b""
        self.got = False          # 收到过至少一条遥测（没收过就不判超时，兼容旧固件）
        self.lock = threading.Lock()
        self.rec_lock = threading.Lock()
        self.state = {"angle": 0.0, "speed": 0.0, "yaw": 0.0, "flags": 0,
                      "dif": 0.0, "dpwm": 0, "apwm": 0,
                      "atgt": 0.0, "sint": 0, "aint": 0, "trim": 0.0, "t": 0.0}
        if dry:
            print("[dry] 不连蓝牙/串口，指令只打印")
            return
        if mac:
            self._open_bt(mac, channel)
        else:
            self._open(port, baud)

    def _open_bt(self, mac, channel):
        import socket
        wait = self.tries * (self.timeout + 1.5)
        print(f"正在连蓝牙 {mac} …（车没上电会在这儿等，最多 {wait:.0f} 秒）", flush=True)
        err = None
        for _ in range(self.tries):
            try:
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                  socket.BTPROTO_RFCOMM)
                s.settimeout(self.timeout)
                s.connect((mac, channel))
                s.settimeout(None)
            except OSError as e:
                err = e
                time.sleep(1.5)
                continue
            self.sock = s
            self.fd = s.fileno()
            self.alive = True
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._watch, daemon=True).start()
            print(f"蓝牙已连 {mac} (channel {channel})")
            return
        sys.exit(
            f"蓝牙连不上 {mac}（channel {channel}）：{err}\n"
            f"  1) 车通电了吗？模块的灯在闪吗？\n"
            f"  2) 手机蓝牙关掉了吗？SPP 只允许一个连接\n"
            f"  3) 用 bluetoothctl info {mac} 看还在不在附近\n"
            f"  4) 先用 --dry 跑通语音链路"
        )

    def _reconnect(self):
        """蓝牙断了自己接回来。演示时手机干扰、走远了、车断电重上都会掉线，
        不该整个程序崩掉。只要有电就一直重试，直到连回来或 Ctrl+C。"""
        import socket
        if not self.mac:
            return False
        # 只允许一个线程在重连（读线程和看门狗都可能发现断线）
        if not self.rec_lock.acquire(blocking=False):
            return False
        try:
            try:
                if self.sock:
                    self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.fd = None
            print("\n  [蓝牙断开] 重连中…（车重新上电后会自动连上，Ctrl+C 退出）", flush=True)
            n = 0
            while self.alive:
                n += 1
                try:
                    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                      socket.BTPROTO_RFCOMM)
                    s.settimeout(6)
                    s.connect((self.mac, self.channel))
                    s.settimeout(None)
                except OSError as e:
                    if n % 15 == 0:
                        print(f"  [蓝牙] 仍在尝试（第 {n} 次，约 {n * 2}s）：{e}", flush=True)
                    time.sleep(2)
                    continue
                self.sock = s
                self.fd = s.fileno()
                self.buf = b""
                with self.lock:
                    self.state["t"] = time.time()
                print("  [蓝牙] 重连成功", flush=True)
                return True
            return False
        finally:
            self.rec_lock.release()

    def _watch(self):
        """看门狗：车断电时 RFCOMM socket 常常「假活着」——read 不报错也不来数据，
        读线程根本没机会触发重连。所以用遥测断流来判断链路死活。"""
        while self.alive:
            time.sleep(1.0)
            if not self.got:
                continue                      # 从没收到过数据就不判（可能是旧固件）
            with self.lock:
                age = time.time() - self.state["t"]
            if age > 4.0:
                print(f"\n  [遥测中断 {age:.0f}s] 判断链路已断，尝试重连", flush=True)
                self._reconnect()

    def _open(self, port, baud):
        import termios
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        except OSError as e:
            sys.exit(
                f"打不开 {port}: {e}\n"
                f"  1) 先确认蓝牙已连接：sudo rfcomm connect hci0 <车上的MAC> 1\n"
                f"  2) 或用 --dry / --monitor 先跑通语音链路\n"
                f"  3) 有线接法可 -p /dev/ttyAMA0"
            )
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            spd = getattr(termios, f"B{baud}", termios.B9600)
            attrs[4] = spd
            attrs[5] = spd
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception as e:
            print(f"  [提示] 串口参数设置跳过：{e}")
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        print(f"串口已连 {port} @ {baud}")

    def _reader(self):
        while self.alive:
            try:
                if self.fd is None:
                    time.sleep(0.1)
                    continue
                r, _, _ = select.select([self.fd], [], [], 0.2)
                if not r:
                    continue
                data = os.read(self.fd, 256)
                if not data:
                    # 读不到东西 = 对端断开。蓝牙模式就自己接回来
                    if self.sock is not None:
                        if self._reconnect():
                            continue
                    time.sleep(0.3)
                    continue
                self.buf += data
                while b"[" in self.buf and b"]" in self.buf:
                    s = self.buf.index(b"[")
                    e = self.buf.index(b"]")
                    if e < s:
                        self.buf = self.buf[e + 1:]
                        continue
                    pkt = self.buf[s + 1:e].decode("ascii", "replace")
                    self.buf = self.buf[e + 1:]
                    self._parse(pkt)
            except ValueError:
                time.sleep(0.2)
            except OSError:
                # socket 出错（车断电、走远）也要走重连，不能只是睡一觉再读死 socket
                if self.sock is not None:
                    if self._reconnect():
                        continue
                time.sleep(0.2)
            except Exception:
                time.sleep(0.05)

    def _parse(self, pkt):
        parts = pkt.split(",")
        if not parts:
            return
        if parts[0] == "s" and len(parts) >= 4:
            try:
                with self.lock:
                    self.state["angle"] = int(parts[1]) / 10.0
                    self.state["speed"] = int(parts[2]) / 100.0
                    self.state["yaw"] = float(parts[3])
                    if len(parts) >= 5:
                        self.state["flags"] = int(parts[4])   # 运行1 + 转向2 + 跌倒4
                    if len(parts) >= 6:
                        self.state["dif"] = int(parts[5]) / 100.0   # 左右轮速度差
                    if len(parts) >= 7:
                        self.state["dpwm"] = int(parts[6])          # 差速 PWM
                    if len(parts) >= 8:
                        self.state["apwm"] = int(parts[7])          # 平均 PWM
                    if len(parts) >= 9:
                        self.state["atgt"] = int(parts[8]) / 10.0      # 角度目标（速度环输出）
                    if len(parts) >= 10:
                        self.state["sint"] = int(parts[9])            # 速度环积分
                    if len(parts) >= 11:
                        self.state["aint"] = int(parts[10])           # 角度环积分
                    if len(parts) >= 12:
                        self.state["trim"] = int(parts[11]) / 10.0    # 直行开环偏置
                    # 黑匣子：时间戳 + 全部原始字段，不写死格式。
                    # 固件以后再加字段也不用改这里 —— 之前写死 7 个，
                    # 加了 avePWM 之后日志就少一列，白丢一次采集。
                    if len(parts) >= 5 and os.path.exists(_TELEM_SW):
                        try:
                            with open(_TELEM_LOG, "a") as f:
                                f.write("%.3f %s\n" % (time.time(),
                                                       " ".join(parts[1:])))
                        except Exception:
                            pass
                    self.state["t"] = time.time()
                    self.got = True
            except ValueError:
                pass

    def send(self, s):
        if self.dry:
            print(f"  -> 车: {s}")
            return
        if self.fd is None:
            return
        try:
            os.write(self.fd, s.encode("ascii"))
        except OSError as e:
            print(f"  [串口写失败] {e}")

    def snap(self):
        with self.lock:
            return dict(self.state)

    def fresh(self, max_age=1.0):
        s = self.snap()
        return s if (time.time() - s["t"]) < max_age else None

    def close(self):
        self.alive = False
        if self.sock is not None:
            try:
                self.sock.close()      # socket 的 fd 由它自己关闭，别再 os.close
            except OSError:
                pass
            self.sock = None
            self.fd = None
        elif self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ==================== 车的高级动作 ====================
class Car:
    def __init__(self, link, cfg):
        self.link = link
        self.cfg = cfg
        self.speed = cfg["speed"]
        self.moving = False
        self.timer = None
        self.lock = threading.Lock()

    def go(self, seconds=None, hold=False):
        sec = self.cfg["hold_max"] if hold else (
            self.cfg["drive_seconds"] if seconds is None else seconds)
        sec = max(0.2, min(sec, self.cfg["hold_max"]))
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.link.send(f"[go,{self.speed:.2f},0]")
            self.moving = True
            self.timer = threading.Timer(sec, self.stop)
            self.timer.daemon = True
            self.timer.start()
        print(f"  前进 {sec:g}s @ {self.speed:.2f}")
        return sec

    def back(self, seconds=None, hold=False):
        sec = self.cfg["drive_seconds"] if seconds is None else seconds
        sec = max(0.2, min(sec, self.cfg["hold_max"]))
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.link.send(f"[go,{-self.speed:.2f},0]")
            self.moving = True
            self.timer = threading.Timer(sec, self.stop)
            self.timer.daemon = True
            self.timer.start()
        print(f"  后退 {sec:g}s")
        return sec

    def stop(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.link.send("[stop]")
            self.moving = False
        print("  停车")

    def turn(self, deg):
        """相对转向，闭环在车上，Pi 只等它做完。超过 200° 拆两段"""
        deg = max(-360, min(360, deg))
        segs = turn_plan(deg)
        ok = True
        for i, seg in enumerate(segs):
            if len(segs) > 1:
                print(f"  第 {i + 1}/{len(segs)} 段 {seg:+.0f}°")
            ok = self._turn_once(seg) and ok
            if not ok:
                break
        return ok

    def _turn_once(self, deg):
        with self.lock:                       # 转向前必须掐掉行进计时器，否则会在转向途中被 stop 打断
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.moving = False
        if self.link.dry:
            print(f"  转向 {deg:+.0f}°（dry 模式直接返回）")
            time.sleep(0.4)
            return True
        start = self.link.snap()["yaw"]
        target = start + deg
        self.link.send(f"[turn,{deg:.0f}]")
        print(f"  转向 {deg:+.0f}°（目标航向 {target:.0f}°）")
        t0 = time.time()
        time.sleep(0.25)
        # 角度越大等得越久：56°/s 是实测转速，+3 秒留给起步和收尾。
        # 这样即使 car.json 里的 turn_timeout 没跟着改，360° 也不会被误判成超时。
        timeout = max(float(self.cfg["turn_timeout"]), abs(deg) / 56.0 + 3.0)
        while time.time() - t0 < timeout:
            s = self.link.fresh(max_age=0.6)
            if s is not None and abs(s["yaw"] - target) <= 4.0:
                # 再确认 0.3 秒没继续转，避免路过目标点时误判
                a = s["yaw"]
                time.sleep(0.3)
                b = self.link.snap()["yaw"]
                if abs(b - a) < 2.0:
                    print(f"  -> 到位 {b:.0f}°（用时 {time.time() - t0:.1f}s）")
                    return True
            time.sleep(0.1)
        print(f"  -> 转向超时，已停车（当前 {self.link.snap()['yaw']:.0f}°）")
        self.link.send("[stop]")
        return False

    def faster(self):
        self.speed = min(self.speed + self.cfg["speed_step"], self.cfg["speed_max"])
        print(f"  速度档 -> {self.speed:.2f}")
        if self.moving:
            self.link.send(f"[go,{self.speed:.2f},0]")

    def slower(self):
        self.speed = max(self.speed - self.cfg["speed_step"], self.cfg["speed_min"])
        print(f"  速度档 -> {self.speed:.2f}")
        if self.moving:
            self.link.send(f"[go,{self.speed:.2f},0]")


# ==================== 意图识别 ====================
_CN = {c: i for i, c in enumerate("零一二三四五六七八九")}
_CN["两"] = 2
_UNITS = {"十": 10, "百": 100}


def cn2num(s):
    """中文数字转数值，支持 0~999：十/二十/一百八十/三百六十"""
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN:
            num = _CN[ch]
        elif ch in _UNITS:
            total += (num if num else 1) * _UNITS[ch]
            num = 0
        else:
            return None
    return float(total + num)


def norm(text):
    return re.sub(r"[\s，。、！？,.!?~～]+", "", text)


def parse_seconds(t):
    m = re.search(r"(\d+(?:\.\d+)?)\s*秒", t)
    if m:
        return float(m.group(1))
    m = re.search(r"([零一二两三四五六七八九十百]+)\s*秒", t)
    if m:
        return cn2num(m.group(1))
    return None


def parse_degree(t):
    m = re.search(r"(\d+(?:\.\d+)?)\s*度", t)
    if m:
        return float(m.group(1))
    m = re.search(r"([零一二两三四五六七八九十百]+)\s*度", t)
    if m:
        return cn2num(m.group(1))
    m = re.search(r"([零一二两三四五六七八九十]+)\s*[个]?直角", t)
    if m:
        v = cn2num(m.group(1))
        return v * 90 if v else 90
    if "半圈" in t:
        return 180.0
    if "一圈" in t or "转圈" in t or "整圈" in t:
        return 360.0
    return None


def turn_plan(deg):
    """一次转向要拆成几段发给车端。

    老固件里车端是死板的 `YawTicks > 200`（20Hz，即 10 秒）就收手，360° 单段
    要 6~7 秒，起步一慢就卡在半路，所以在 Pi 端拆成两段 180°。代价是两段之间
    要停一次、重新闭环，走起来能看出一个"顿"。

    现在车端超时改成按角度算（`TURN_TIMEOUT_TICKS`），360° 能一口气转完，
    这里就不再拆了。函数保留，以后真要拆只改这一处。
    """
    deg = max(-360.0, min(360.0, float(deg)))
    return [deg]


STOP_W = ["停下", "停止", "停车", "站住", "别动", "不要动", "别走", "别跑", "刹车",
          "别转", "不要转", "停"]
FWD_W = ["前进", "往前", "向前", "直走", "往前走", "向前走", "过来", "走"]
BACK_W = ["后退", "往后", "向后", "倒车", "往后退", "回来", "回去", "退"]
LEFT_W = ["左转", "往左", "向左", "左边", "左"]
RIGHT_W = ["右转", "往右", "向右", "右边", "右"]
HOLD_W = ["一直", "继续", "别停", "不要停", "不停"]
FAST_W = ["快一点", "快点", "加速", "快些"]
SLOW_W = ["慢一点", "慢点", "减速", "慢些"]
STATUS_W = ["多少度", "歪", "倾斜", "姿态", "状态", "稳不稳", "正不正"]


def parse_intent(text, cfg):
    """返回 dict 或 None"""
    t = norm(text)
    if not t:
        return None

    # "别停/不要停" 必须先于 STOP_W 判断，否则会被当成停车
    if any(w in t for w in ("别停", "不要停", "不停")):
        return {"act": "forward", "hold": True, "sec": None}
    if any(w in t for w in STOP_W):
        return {"act": "stop"}

    if any(w in t for w in ("一直", "继续")):
        for w, act in ((FWD_W, "forward"), (BACK_W, "back")):
            if any(k in t for k in w):
                return {"act": act, "hold": True, "sec": None}

    for words, act in ((FWD_W, "forward"), (BACK_W, "back")):
        if any(k in t for k in words):
            return {"act": act, "sec": parse_seconds(t), "hold": False}

    for words, sign in ((LEFT_W, -1), (RIGHT_W, +1)):
        if any(k in t for k in words):
            deg = parse_degree(t)
            if deg is None:
                deg = 90.0
            return {"act": "turn", "deg": sign * abs(deg)}

    # 光说"转九十度"没给方向，默认向右
    if "转" in t:
        deg = parse_degree(t)
        if deg:
            return {"act": "turn", "deg": abs(deg)}

    if any(k in t for k in FAST_W):
        return {"act": "faster"}
    if any(k in t for k in SLOW_W):
        return {"act": "slower"}
    if any(k in t for k in STATUS_W):
        return {"act": "status"}
    return None


# ==================== 示教再现 ====================
class Routes:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self.rec = None
        self.last = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        # 重启后"走一遍"要能找到最近那条，所以默认指到最后一条
        self.last = list(self.data)[-1] if self.data else None

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def start(self):
        self.rec = []

    def add(self, step):
        if self.rec is not None:
            self.rec.append(step)

    def finish(self, name=None):
        if not self.rec:
            self.rec = None
            return None
        name = name or f"路线{len(self.data) + 1}"
        self.data[name] = self.rec
        self.last = name
        self.rec = None
        self._save()
        return name

    def rename_last(self, name):
        if not self.last or self.last not in self.data:
            return None
        self.data[name] = self.data.pop(self.last)
        self.last = name
        self._save()
        return name

    def find(self, name):
        if not name:
            return None, None
        if name in self.data:
            return name, self.data[name]
        for k in self.data:
            if name in k or k in name:
                return k, self.data[k]
        return None, None


def teach_intent(text):
    """示教相关的口语 -> 动作。不是示教就返回 None。

    单独抽出来是因为这句话跟开车指令混在一起，而且 hub.py（触屏）也要用同一套判断，
    以前只在 main() 的 handle() 里写了，触屏那边说"记住这条路"就永远"没听懂"。
    """
    t = norm(text)
    if not t:
        return None

    # 结束在前：说"记好了"时不能再被当成"开始记"
    if any(k in t for k in ("记好了", "记完了", "记完", "结束记录", "结束记路", "不用记了")):
        return {"act": "teach_stop"}
    if any(k in t for k in ("有哪些路", "几条路", "什么路", "记得几条", "记了什么")):
        return {"act": "teach_list"}
    if any(k in t for k in ("走一遍", "再来一遍", "重走", "原路返回",
                            "走刚才", "走上次", "重复一遍", "刚才那条")) \
            or ("回放" in t and "路" not in t):
        return {"act": "teach_replay", "name": None}

    # "走厨房那条路" / "回放路线2"：必须带"路"字才算回放，否则"走路"会被误当成回放
    if "路" in t and re.search(r"^(?:走|去|回放|执行|再来)", t):
        nm = re.sub(r"^(?:走|去|回放|执行|再来)", "", t)
        nm = nm.replace("路线", "").replace("路", "")
        nm = re.sub(r"(?:这|那)?条$", "", nm).strip()
        if nm in ("这", "那", "刚才", "上次", "原"):
            nm = ""
        if nm:
            return {"act": "teach_replay", "name": nm}
        if any(k in t for k in ("这条路", "那条路", "刚才", "上次", "原路")):
            return {"act": "teach_replay", "name": None}

    # "记住这条路" 常被识别成 "记住这条路/记录路线/记一下路/记着这条路"，
    # 只要同时出现"记"和"路/路线/记录"就当开始记，别抠字眼
    if "记" in t and any(k in t for k in ("这条路", "这条", "路", "路线", "记录")):
        if not any(k in t for k in ("记好", "记完")):
            return {"act": "teach_start"}
    return None


def do_replay(car, seq):
    for i, st in enumerate(seq):
        print(f"  [{i + 1}/{len(seq)}] ", end="")
        if st["act"] in ("forward", "back"):
            if st["act"] == "forward":
                sec = car.go(st.get("sec"))
            else:
                sec = car.back(st.get("sec"))
            time.sleep(sec + 0.35)      # 必须等这一步走完，否则后面会被立刻盖掉
            car.stop()
            time.sleep(0.2)
        elif st["act"] == "turn":
            car.turn(st["deg"])
        elif st["act"] == "stop":
            car.stop()
    car.stop()


# ==================== 大模型慢通道 ====================
def llm_pick(text, cfg):
    import urllib.request

    system = (
        "你是平衡车的指令解析器。用户的话来自语音识别，可能有同音错字，按最合理的意思理解。"
        "只能输出一个 JSON 对象，不要任何其他文字："
        '{"action":"forward|back|left|right|stop|faster|slower|chat",'
        '"seconds":数字或null,"degree":数字或null,"say":"给用户的一句话"}'
        "left/right 表示转向，degree 是转多少度，没说就填 90。"
        "绝对不要输出速度、PWM、占空比等任何底层参数。"
    )
    body = {
        "model": cfg.get("model", "deepseek-chat"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 80,
        "stream": False,
    }
    req = urllib.request.Request(
        cfg.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            c = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", c, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"  [模型不可用] {e}")
        return None


# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--device", default="plughw:3,0")
    ap.add_argument("-o", "--out", default="plughw:2,0")
    ap.add_argument("-p", "--port")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--keys", action="store_true", help="键盘遥控 WASD")
    ap.add_argument("--monitor", action="store_true", help="只看遥测")
    ap.add_argument("--text")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--ack", action="store_true", help="每条指令都语音回一句（会变慢）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("-t", "--threads", type=int, default=2)
    args = ap.parse_args()

    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if args.port:
        cfg["port"] = args.port

    if args.text:
        r = parse_intent(args.text, cfg)
        print(f"[{args.text}] -> {r}")
        return

    # 指定 -p 就走串口；否则只要配了 mac 就直连蓝牙
    mac = None if args.port else (cfg.get("mac") or None)
    link = Link(cfg["port"], cfg["baud"], dry=args.dry,
                mac=mac, channel=cfg.get("channel", 1))
    car = Car(link, cfg)
    routes = Routes(ROUTE_DB)

    if args.monitor:
        print("只读遥测中，Ctrl+C 退出。正常应看到 [s,...] 每秒约 5 条")
        try:
            while True:
                time.sleep(1.0)
                s = link.fresh(max_age=2.0)
                if s is None:
                    print("  没有遥测 —— 检查蓝牙是否连上、车是否上电")
                else:
                    st = []
                    if s.get("flags", 0) & 1:
                        st.append("运行中")
                    if s.get("flags", 0) & 2:
                        st.append("转向中")
                    if s.get("flags", 0) & 4:
                        st.append("跌倒锁定(按Key1解)")
                    if abs(s["angle"]) > 50:
                        st.append("躺着/倒了")
                    print(f"  俯仰 {s['angle']:+.1f}°   速度 {s['speed']:+.2f}   "
                          f"航向 {s['yaw']:+.0f}°   {' '.join(st) or '待机'}")
        except KeyboardInterrupt:
            pass
        link.close()
        return

    if args.keys:
        print("键盘遥控：W前进 S后退 A左转 D右转 R原地转一圈 空格停 Q退出（WASD 马上返回，R 会等它转完）")
        import termios
        import tty

        def _status():
            """边控边看航向，用来判断转向到底转了多少度"""
            last = ""
            while True:
                time.sleep(0.5)
                s = link.snap()
                if not s["t"]:
                    continue
                f = s.get("flags", 0)
                tag = "转向中" if f & 2 else ("跌倒锁定" if f & 4 else ("运行" if f & 1 else "待机"))
                line = f"    俯仰{s['angle']:+5.1f}°  航向{s['yaw']:+6.0f}°  {tag}"
                if line != last:
                    print(line, flush=True)
                    last = line

        threading.Thread(target=_status, daemon=True).start()
        fd_in = sys.stdin.fileno()
        old = termios.tcgetattr(fd_in)
        try:
            tty.setcbreak(fd_in)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q"):
                    break
                if ch == "w":
                    car.go(1.5)
                elif ch == "s":
                    car.back(1.5)
                elif ch == "a":
                    link.send("[turn,-90]")
                    print("  左转 90°")
                elif ch == "d":
                    link.send("[turn,90]")
                    print("  右转 90°")
                elif ch == "r":
                    car.turn(360)
                    print("  原地转一圈")
                elif ch == " ":
                    car.stop()
        finally:
            termios.tcsetattr(fd_in, termios.TCSADRAIN, old)
            car.stop()
            link.close()
        return

    print("加载语音模型...", end="", flush=True)
    t0 = time.time()
    rec = make_rec(args.threads, "/home/pi/hotwords.txt")
    vad, window = make_vad()
    print(f" 完成 ({time.time() - t0:.1f}s)")

    llm_cfg = None
    if not args.no_llm and os.path.exists(cfg["llm_json"]):
        try:
            with open(cfg["llm_json"], encoding="utf-8") as f:
                llm_cfg = json.load(f)
            if not llm_cfg.get("api_key"):
                llm_cfg = None
        except Exception:
            llm_cfg = None
    print(f"慢通道：{'DeepSeek' if llm_cfg else '关闭（纯本地）'}")

    # ---- TTS：先探一次 PC 上的 SAPI 服务，不通就直接用本机 espeak ----
    # 原来固定写死 engine="sapi"，PC 服务没开时 urlopen 要等 15 秒，再退到 edge 联网又失败，
    # 合计 17 秒主循环全卡住，arecord 缓冲区直接溢出（就是那句 Overrun!!!）。
    tts_engine = "sapi"
    if not args.quiet:
        try:
            import urllib.error
            import urllib.request

            from tts import SAPI_URL
            try:
                urllib.request.urlopen(SAPI_URL + "/", timeout=2)
            except urllib.error.HTTPError:
                pass                       # 404 也说明服务活着
        except Exception:
            tts_engine = "espeak"
            print("TTS：PC 端 SAPI 没开，改用本机 espeak（机器人音，能听懂）")
            print("     想要自然的声音：PC 上双击 pc\\启动语音服务.bat，再重启本脚本")

    speak_until = {"t": 0.0}               # 播报到什么时候，用来屏蔽自己的声音

    def speak(text):
        """非阻塞播报：TTS 放后台线程，慢也不会卡住麦克风采集"""
        if args.quiet or not text:
            return

        def worker():
            speak_until["t"] = time.time() + 0.5
            try:
                from tts import speak as tts_speak
                tts_speak(text, engine=tts_engine, voice="huihui", out=args.out,
                          espeak_voice="cmn+f3", espeak_speed=165)
            except Exception as e:
                print(f"  [播报失败] {e}")
            finally:
                speak_until["t"] = time.time() + 0.6   # 播完再压 0.6 秒，免得收录尾音

        threading.Thread(target=worker, daemon=True).start()

    def report_status():
        s = link.fresh(max_age=1.5)
        if s is None:
            print("  （没收到车的回传，蓝牙可能断了）")
            speak("我看不到车了")
            return
        tilt = s["angle"]
        if abs(tilt) < 3:
            msg = f"站得挺稳，歪了{tilt:+.0f}度"
        elif abs(tilt) < 15:
            msg = f"有点歪，{tilt:+.0f}度"
        else:
            msg = f"快倒了，{tilt:+.0f}度"
        print(f"  {msg}（航向 {s['yaw']:+.0f}°）")
        speak(msg)

    def run(intent):
        a = intent["act"]
        if a in ("forward", "back"):
            if a == "forward":
                sec = car.go(intent.get("sec"), hold=intent.get("hold", False))
            else:
                sec = car.back(intent.get("sec"), hold=intent.get("hold", False))
            if routes.rec is not None:
                routes.add({"act": a, "sec": sec})
            if args.ack:
                speak("走")
            return True
        if a == "turn":
            car.turn(intent["deg"])
            if routes.rec is not None:
                routes.add({"act": "turn", "deg": intent["deg"]})
            return True
        if a == "stop":
            car.stop()
            if routes.rec is not None:
                routes.add({"act": "stop"})
            return True
        if a == "faster":
            car.faster()
            return True
        if a == "slower":
            car.slower()
            return True
        if a == "status":
            report_status()
            return True
        return False

    def handle(text):
        if not text:
            print("(没识别出内容)")
            return
        t = norm(text)
        print(f"你: {text}")

        # 示教相关优先，避免被"走"之类的词抢走
        ti = teach_intent(text)
        if ti:
            a = ti["act"]
            if a == "teach_start":
                routes.start()
                print("  [示教] 开始记录")
                speak("好，你指挥，我跟着记")
                return
            if a == "teach_stop":
                name = routes.finish()
                if name:
                    n = len(routes.data[name])
                    print(f"  [示教] 已存为「{name}」，{n} 步")
                    speak(f"记好了，{n}步")
                else:
                    print("  [示教] 还没记东西")
                    speak("还没记东西")
                return
            if a == "teach_list":
                print("  [示教] 已存:", "、".join(routes.data) or "（空）")
                speak(f"记了{len(routes.data)}条路" if routes.data else "还没记过路")
                return
            if a == "teach_replay":
                key = ti.get("name") or routes.last or ""
                name, seq = routes.find(key)
                if seq:
                    print(f"  [示教] 回放「{name}」，{len(seq)} 步")
                    speak("这就走")
                    do_replay(car, seq)
                    return
                print(f"  [示教] 没找到「{key}」，现有：", "、".join(routes.data) or "（空）")
                speak("我没有记过这条路")
                return

        m = re.search(r"叫(.+)$", t)
        if m and routes.last:
            nm = routes.rename_last(m.group(1))
            print(f"  [示教] 改名为「{nm}」")
            speak("行")
            return

        it = parse_intent(text, cfg)
        if it and run(it):
            return

        if llm_cfg:
            got = llm_pick(text, llm_cfg)
            if got:
                a = got.get("action")
                if a in ("forward", "back"):
                    run({"act": a, "sec": got.get("seconds"), "hold": False})
                    return
                if a in ("left", "right"):
                    d = abs(float(got.get("degree") or 90))
                    run({"act": "turn", "deg": -d if a == "left" else d})
                    return
                if a in ("stop", "faster", "slower"):
                    run({"act": a})
                    return
                if a == "chat" and got.get("say"):
                    print(f"小派: {got['say']}")
                    speak(got["say"])
                    return
        print("  （没听懂：前进 / 后退 / 左转 / 右转九十度 / 停）")

    cmd = ["arecord", "-D", args.device, "-f", "S16_LE", "-r", str(RATE),
           "-c", "1", "-t", "raw", "-q",
           "--period-size=1600", "--buffer-size=16000"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    fd = proc.stdout.fileno()
    print("就绪。说：'前进'、'右转九十度'、'停'、'记住这条路'", flush=True)

    carry = np.zeros(0, dtype=np.float32)
    hist = np.zeros(0, dtype=np.float32)
    preroll = int(0.35 * RATE)
    buf = None

    try:
        while True:
            r_, _, _ = select.select([fd], [], [], 0.5)
            if not r_:
                continue
            data = os.read(fd, CHUNK * 2)
            if not data:
                break
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

            carry = np.concatenate([carry, samples]) if len(carry) else samples
            while len(carry) >= window:
                vad.accept_waveform(carry[:window])
                carry = carry[window:]

            hist = np.concatenate([hist, samples])
            if len(hist) > preroll:
                hist = hist[-preroll:]

            started = False
            if buf is None and vad.is_speech_detected():
                buf = hist.copy()
                started = True
            if buf is not None and not started:
                buf = np.concatenate([buf, samples])

            if not vad.empty():
                seg = buf if buf is not None else vad.front.samples
                while not vad.empty():
                    vad.pop()
                buf = None
                hist = np.zeros(0, dtype=np.float32)
                if speech_seconds(seg) >= 0.3:
                    if time.time() < speak_until["t"]:
                        print("（自己刚说完话，这一句丢掉）")
                        continue
                    st = rec.create_stream()
                    st.accept_waveform(RATE, seg)
                    st.input_finished()
                    while rec.is_ready(st):
                        rec.decode_stream(st)
                    handle(rec.get_result(st).strip())
                    # 处理期间麦克风积压的数据丢掉，免得下一句被污染
                    while select.select([fd], [], [], 0)[0]:
                        if not os.read(fd, 65536):
                            break
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        car.stop()
        proc.terminate()
        proc.wait()
        link.close()


if __name__ == "__main__":
    main()
