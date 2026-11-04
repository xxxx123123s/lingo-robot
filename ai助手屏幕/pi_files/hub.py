#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hub.py —— 小派控制中心（触屏多应用）

把原来单一的「触屏对话」扩成一个可以点来点去的控制中心：
主页六个磁贴，点进去是独立的应用，左上角 ‹ 返回主页。

    对话      原 pet.py 那套：按住说话 → DeepSeek → 朗读，脸会动
    天气      实况 / 今日高低 / 降水概率（open-meteo，免密钥）
    温湿度    DHT22 读数 + 折线趋势（没接传感器时显示室外值，并提示怎么接）
    遥控      屏幕方向盘，按住走、松手停，实时遥测
    语音控车  按住说「右转九十度」，本地意图表直接发车，不走大模型
    系统      CPU / 内存 / 磁盘 / 网络 / 运行时长

一条总线给所有应用共用：麦克风只有一个、识别模型只加载一次、TTS 只有一套。
应用之间互斥（同一时刻只有一个在跑），所以不用担心抢设备。

用法:
  python3 hub.py                  # 正常启动
  python3 hub.py --demo           # 不接麦克风，只跑界面（看效果）
  python3 hub.py --mute           # 启动时静音
  python3 hub.py --spi-hz 62500000
  python3 hub.py --calibrate      # 触摸校准（同 pet.py）
  python3 hub.py --check          # 触摸自检
退出: Ctrl+C

依赖：python3-spidev python3-gpiozero python3-pil python3-numpy fonts-wqy-zenhei
可选：adafruit-circuitpython-dht（接 DHT22 才需要）
"""

import argparse
import collections
import json
import os
import queue
import random
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import types
import urllib.request

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, "/home/pi")

import hubui                                             # noqa: E402
import petui                                             # noqa: E402
import screen                                            # noqa: E402
import tts                                               # noqa: E402
from chat import (HISTORY_MAX, ask_llm, handle_memo_cmd, is_deep,  # noqa: E402
                  live_context, load_cfg, remember_from)
from talk import RATE, make_asr, make_vad, speech_seconds  # noqa: E402

# 视觉三件事（手势停车 / 人脸认主人 / 拍照问模型）。摄像头没接、
# cv2 没装时这里会失败，但只是关掉视觉，其它功能照常 —— 不能因为
# 一个可选模块把整块屏幕带崩。
try:
    import vision_intent                                   # noqa: E402
except Exception as _vex:                                  # noqa: BLE001
    vision_intent = None
    print(f"[视觉] 未启用：{_vex}")

# 识别结果的拼音纠正。只在"按原意解析不出动作"时才用，所以缺了它
# 也不会影响正常功能，最多是错别字没人兜底。
try:
    import asr_fix                                         # noqa: E402
except Exception as _aex:                                  # noqa: BLE001
    asr_fix = None
    print(f"[语音] 纠正层未启用：{_aex}")

CHUNK = 1600                       # 0.1 秒一块
MIN_PTT = 0.45                     # 短于这个不送
MAX_PTT = 20.0
FPS = 15
CFG_FILE = "/home/pi/hub.json"      # 可选：{"lat":30.1,"lon":104.6,"city":"资阳"}
PET_LINES = [
    "嘿嘿，痒。", "再摸一下嘛。", "我在呢，随时待命。",
    "别摸啦，我要认真工作了。", "唔……好舒服。", "你今天心情怎么样？",
]
WEEK = "一二三四五六日"


# ================================================================ 小工具
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def city_label(name):
    """ip-api 返回的城市是英文（Ziyang），中文界面上不好看 → 统一显示「本地」"""
    if not name:
        return "本地"
    if any("\u4e00" <= ch <= "\u9fff" for ch in name):
        return name
    return "本地"


def cpu_temp():
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(p) as f:
                return int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            pass
    return None


def mem_info():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0])       # kB
        total = info["MemTotal"] / 1024 / 1024
        avail = info.get("MemAvailable", info["MemFree"]) / 1024 / 1024
        return total, total - avail
    except (OSError, KeyError, ValueError):
        return None, None


def disk_info():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize / 1024 ** 3
        free = st.f_bavail * st.f_frsize / 1024 ** 3
        return total, total - free
    except OSError:
        return None, None


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "—"


def uptime_text():
    try:
        with open("/proc/uptime") as f:
            sec = float(f.read().split()[0])
    except (OSError, ValueError):
        return "—"
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分"


def fmt_clock():
    t = time.localtime()
    return time.strftime("%H:%M", t)


def fmt_date():
    t = time.localtime()
    return f"{t.tm_mon}月{t.tm_mday}日 周{WEEK[t.tm_wday]}"


# ================================================================ 声卡解析
# 插拔 USB 麦克风会让 ALSA 重新分配卡号（这台机器已经飘到 card 3 了），
# 写死 plughw:3,0 换个口或者重插一次就可能失效。所以按卡名现查，
# /proc/asound/cards 很便宜，每次开始录音前读一遍就够。
_PCM_FALLBACK = {"mic": "plughw:3,0", "spk": "plughw:2,0"}
_PCM_KEY = {"mic": "usb-audio", "spk": "headphone"}


def pcm(kind="mic", want=""):
    if want and want != "auto":
        return want
    key = _PCM_KEY[kind]
    try:
        with open("/proc/asound/cards", encoding="utf-8") as fp:
            for line in fp:
                s = line.strip()
                if "[" not in s or key not in s.lower():
                    continue
                head = s.split()[0]
                if head.isdigit():
                    return f"plughw:{head},0"
    except OSError:
        pass
    return _PCM_FALLBACK[kind]


# ================================================================ 屏幕自愈
# 插拔 USB 麦克风时，内核要重新枚举设备、跑 udev 规则，CPU 会抖几百毫秒。
# 而一整帧是 153600 字节、要连续传 30ms，ST7789 的像素传输又没有超时机制：
# 字节流一断，它以为还在收像素，后面本该是命令的字节被当数据吃掉，
# 窗口坐标全乱，大片没更新过的区域就停在 ST7789 的上电默认白色。
# 屏幕自己不会恢复，spidev 也只管把数据丢进 FIFO，收不到任何错误。
# 所以只能主动兜：USB 设备表变了、或某一帧耗时明显超标，就重走一遍 init。
def usb_sig():
    """USB 和声卡设备表的指纹。两处都看：只看 USB 目录的话，
    设备只是被 deauthorize 时目录还在，指纹不变，白屏就漏过去了。"""
    out = []
    for d in ("/sys/bus/usb/devices", "/dev/snd"):
        try:
            out.append(tuple(sorted(os.listdir(d))))
        except OSError:
            out.append(())
    return tuple(out)


# ================================================================ 风扇开关
# fan.py 是个独立的 root 服务，占着 GPIO12/23/27，屏幕这边不能直接碰引脚，
# 只能通过 /tmp 下的一对文件跟它对话：
#   写 /tmp/fan.cmd   → auto / on / off
#   读 /tmp/fan.state → "<模式> <占空比%> <温度>"
FAN_CMD = "/tmp/fan.cmd"
FAN_STATE = "/tmp/fan.state"
FAN_NAMES = {"auto": "自动", "on": "手动开", "off": "手动关"}

# 调参用的原始指令通道（见我下面 _poll_carcmd 的说明）。生产逻辑不用它，
# 只在"车跑偏/速度不对"这类要扫参数的场合临时写文件用。
CAR_CMD = "/tmp/car.cmd"

_fan_cache = {"t": 0.0, "v": ("auto", 0, None)}


def fan_state():
    """(模式, 占空比整数, 温度)。0.5 秒缓存 —— 系统页每帧都调，
    没必要为了几个数字反复读盘，但也不能太久，点完得跟手。"""
    now = time.monotonic()
    if now - _fan_cache["t"] < 0.5:
        return _fan_cache["v"]
    mode, duty, temp = "auto", 0, None
    try:
        with open(FAN_STATE) as f:
            parts = f.read().split()
        if len(parts) >= 3 and parts[0] in FAN_NAMES:
            mode = parts[0]
            duty = int(float(parts[1]))
            temp = float(parts[2])
    except (OSError, ValueError):
        pass
    _fan_cache["t"] = now
    _fan_cache["v"] = (mode, duty, temp)
    return _fan_cache["v"]


def fan_alive():
    """fan.state 每 0.4 秒刷新一次，超过 6 秒没动说明服务已死。
    这时候还显示「自动 68%」就是在骗人。"""
    try:
        return time.time() - os.stat(FAN_STATE).st_mtime < 6
    except OSError:
        return False


def fan_set(mode):
    """写模式给 fan.py。返回是否写成功"""
    try:
        with open(FAN_CMD, "w") as f:
            f.write(mode + "\n")
    except OSError:
        return False
    _fan_cache["t"] = 0.0                  # 立刻重读，别等缓存过期
    return True


def fan_next(mode):
    """三态循环：手动关 → 手动开 → 自动 → 手动关。

    起点是"手动关"——因为 fan.py 开机/重启后一律先关着（见那边的 BOOT_MODE），
    所以最常走的是 **关 → 点一下 → 手动开**，一步到位。
    再点一下给"自动"（按温度自己跑），第三下回到关。"""
    return {"off": "on", "on": "auto", "auto": "off"}.get(mode, "on")


# ================================================================ 天气
WMO = {
    0: ("sun", "晴"), 1: ("sun_cloud", "少云"), 2: ("sun_cloud", "多云"),
    3: ("cloud", "阴"), 45: ("fog", "有雾"), 48: ("fog", "雾凇"),
    51: ("rain", "小雨"), 53: ("rain", "小雨"), 55: ("rain", "中雨"),
    56: ("rain", "冻雨"), 57: ("rain", "冻雨"),
    61: ("rain", "小雨"), 63: ("rain", "中雨"), 65: ("rain", "大雨"),
    66: ("rain", "冻雨"), 67: ("rain", "冻雨"),
    71: ("snow", "小雪"), 73: ("snow", "中雪"), 75: ("snow", "大雪"),
    77: ("snow", "雪粒"), 80: ("rain", "阵雨"), 81: ("rain", "阵雨"),
    82: ("rain", "强阵雨"), 85: ("snow", "阵雪"), 86: ("snow", "阵雪"),
    95: ("thunder", "雷阵雨"), 96: ("thunder", "雷阵雨"), 99: ("thunder", "雷暴"),
}


def wmo(code):
    return WMO.get(int(code), ("cloud", "未知"))


class WeatherSvc:
    """后台每 10 分钟拉一次；界面永远读缓存，不会因为断网卡住"""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.err = ""
        self.lat = None
        self.lon = None
        self.city = ""
        self.kind = "sun"
        self.desc = "—"
        self.busy = False

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            self.refresh()
            time.sleep(600)

    def _locate(self):
        cfg = load_json(CFG_FILE, {})
        if cfg.get("lat") and cfg.get("lon"):
            self.lat, self.lon = float(cfg["lat"]), float(cfg["lon"])
            self.city = cfg.get("city", "")
            return
        try:
            url = ("http://ip-api.com/json/?fields=status,city,regionName,lat,lon")
            with urllib.request.urlopen(url, timeout=5) as r:
                j = json.loads(r.read().decode("utf-8"))
            if j.get("status") == "success":
                self.lat, self.lon = float(j["lat"]), float(j["lon"])
                self.city = j.get("city") or j.get("regionName") or ""
                return
        except Exception as exc:                          # noqa: BLE001
            print(f"  [定位失败] {exc}")
        self.lat, self.lon = 30.66, 104.06                # 兜底：成都
        self.city = ""

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        try:
            if self.lat is None:
                self._locate()
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={self.lat:.4f}&longitude={self.lon:.4f}"
                "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
                "&daily=temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
                "&timezone=auto&forecast_days=1"
            )
            with urllib.request.urlopen(url, timeout=8) as r:
                j = json.loads(r.read().decode("utf-8"))
            cur, day = j["current"], j["daily"]
            kind, desc = wmo(cur.get("weather_code", 0))
            d = {
                "temp": round(float(cur["temperature_2m"])),
                "feels": round(float(cur["apparent_temperature"])),
                "hum": int(cur.get("relative_humidity_2m", 0)),
                "wind": float(cur.get("wind_speed_10m", 0)),
                "hi": round(float(day["temperature_2m_max"][0])),
                "lo": round(float(day["temperature_2m_min"][0])),
                "pop": int((day.get("precipitation_probability_max") or [0])[0] or 0),
                "code": cur.get("weather_code", 0),
                "ts": time.time(),
            }
            with self.lock:
                self.data = d
                self.kind, self.desc = kind, desc
                self.err = ""
        except Exception as exc:                          # noqa: BLE001
            with self.lock:
                self.err = str(exc)[:60]
            print(f"  [天气失败] {exc}")
        finally:
            self.busy = False


# ================================================================ 温湿度
class EnvSvc:
    """有 DHT22 就读 DHT22，没有就只报 CPU 温度，界面照常显示"""

    def __init__(self):
        self.lock = threading.Lock()
        self.temp = None
        self.hum = None
        self.hist = collections.deque(maxlen=60)          # 60 点，每 2 秒一点 = 2 分钟
        self.src = "未接传感器"
        self.err = ""
        self.cpu = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        try:
            import adafruit_dht
            import board
            return adafruit_dht.DHT22(board.D4, use_pulseio=False)
        except Exception as exc:                          # noqa: BLE001
            self.err = str(exc)[:50]
            return None

    def _loop(self):
        dht = self._open()
        if dht is not None:
            self.src = "DHT22 · GPIO4"
            print("[环境] DHT22 已就绪")
        else:
            self.src = "未接传感器"
            print("[环境] 没有 DHT22，温湿度显示室外值（pip install adafruit-circuitpython-dht 可启用）")
        while True:
            self.cpu = cpu_temp()
            if dht is not None:
                try:
                    t, h = dht.temperature, dht.humidity
                    if t is not None and h is not None:
                        with self.lock:
                            self.temp, self.hum = float(t), float(h)
                            self.hist.append(float(t))
                        self.src = "DHT22 · GPIO4"
                except Exception:                          # noqa: BLE001
                    pass                                   # DHT 读失败是常态，跳过这一轮
            time.sleep(2.0)


# ================================================================ 车控链路
class CarCtl:
    """懒连接：第一次进遥控页才去连，连不上也不会把主程序带崩"""

    def __init__(self):
        self.cfg = {}
        self.link = None
        self.status = "idle"        # idle / connecting / ok / lost / fail
        self.err = ""
        self._born = 0.0            # 连通时刻，排查"连上多久断的"用
        self._last_check = 0.0      # tick 节流
        self._retry_at = 0.0        # 上次连接失败的时刻，控制自动重试间隔
        self._gest_cancel = threading.Event()   # 手势前进中途被张手叫停

    def tick(self):
        """每帧调用（内部自己节流到 0.5 秒一次），把链路死活实时反映到 status。

        以前 status 只在 `_connect()` 成功那一刻写一次 "ok"，之后再没人动它 ——
        蓝牙断了（车断电、走远了、手机抢连），遥控页和语音页仍然挂着绿色的
        "已连接"，用户对着一条死链按半天，还以为是自己没按住。

        判据用遥测新鲜度而不是 socket 状态：RFCOMM 的 socket 断线时经常
        "假活着"，`recv` 既不报错也不来数据，只有看数据有没有按时到达才准。
        车端 5Hz 上报，3 秒没数据就是断了；重连成功后遥测恢复，自动变回 ok。"""
        now = time.time()
        if now - self._last_check < 0.5:
            return
        self._last_check = now
        self._dump_tel()
        self._poll_vision()
        self._poll_carcmd()
        if self.status == "fail":
            # 启动时车还没上电 → 连接失败。之后每 8 秒自己再试一次，
            # 不然用户给车上了电，还得离开页面再点回来才能连上
            if now - self._retry_at > 8.0:
                print("[车] 上次没连上，自动重试")
                self.ensure()
            return
        if self.link is None:
            return
        if not self.link.got:
            return                      # 这辆车一条遥测都没发过（老固件），没有判据，别乱判
        age = now - self.link.snap()["t"]
        if age <= 3.0:
            if self.status != "ok":
                print("[车] 遥测恢复，重新判定为已连接")
                self.status = "ok"
        elif self.status == "ok":
            print(f"[车] 遥测断了 {age:.1f}s，标记为掉线")
            self.status = "lost"

    def _dump_tel(self):
        """把最新遥测落到 /tmp/car.tel，一行四个数：倾角,速度,航向,状态位。

        为什么要这个：调方向符号时隔着屏幕问「你点右它往哪转」太慢，
        而且人眼容易看成想要的结果。落盘之后可以直接从 PC 侧 ssh 读数，
        "手拿车往右转 90°，航向是变大还是变小"这种问题一秒就有客观答案。
        /tmp 是内存盘，0.5 秒写一行几十字节，开销可以忽略。"""
        if self.link is None:
            return
        try:
            s = self.snap()
            with open("/tmp/car.tel", "w", encoding="utf-8") as f:
                f.write(f"{s['angle']:.2f},{s['speed']:.3f},{s['yaw']:.1f},{s.get('flags', 0)}\n")
        except Exception:                                  # noqa: BLE001
            pass

    def _poll_carcmd(self):
        """调参用的原始指令通道：/tmp/car.cmd 里每行一条，原样发给车。

        为什么绕这么一圈：HC-04 的 SPP 同一时刻只允许一路连接，链路在主程序手里。
        在树莓派上用脚本单开一条连接会把主程序的链路挤掉，所以改成"写文件，
        主程序代为转发"—— 和风扇 /tmp/fan.cmd、手势 /tmp/vision.cmd 一个套路。

        用途是扫参数（例如 [slider,StraightTrim,-6]）：以前每调一个值都要
        改代码、编译、烧录、跑一趟，一轮下来十几分钟；现在一条 echo 就够，
        一轮可以扫十个值。发完立刻删文件，不会重复下发。
        """
        if self.link is None:
            return
        try:
            if not os.path.exists(CAR_CMD):
                return
            with open(CAR_CMD, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            os.unlink(CAR_CMD)
        except Exception:                                  # noqa: BLE001
            return
        for ln in lines:
            body = ln[1:-1] if ln.startswith("[") and ln.endswith("]") else ln
            try:
                self.link.send("[" + body + "]")
                print(f"[调试] → [{body}]")
            except Exception as exc:                       # noqa: BLE001
                print(f"[调试] 发送失败：{exc}")

    def _poll_vision(self):
        """手势进程只往 /tmp/vision.cmd 写一行指令，不自己连蓝牙 ——
        HC-04 的 SPP 同一时刻只允许一路连接，链路在主程序手里。
        这里读到就借用同一条链路发出去，和风扇用 /tmp/fan.cmd 是一个套路。

        指令有两种：`stop`（张开手掌）和 `forward 4`（握拳走 4 秒）。
        秒数由手势进程写在指令里，主程序照着执行 —— 以后改时长只动一边。
        """
        if vision_intent is None or self.link is None:
            return
        try:
            cmd = vision_intent.pending_cmd()
            if not cmd:
                return
            vision_intent.clear_cmd()          # 先清再发，发失败也不该反复触发
            if cmd == "stop":
                self._gest_cancel.set()
                self.link.send("[stop]")
                print("[视觉] 手势停车 → 已发 [stop]")
            elif cmd.startswith("forward"):
                parts = cmd.split()
                try:
                    sec = float(parts[1]) if len(parts) > 1 else 4.0
                except ValueError:
                    sec = 4.0
                # tick() 是屏幕主循环调的，这里一睡屏幕就定住了 4 秒，
                # 所以发车交给线程，主循环照常刷屏。
                # clear 放在启动线程之前：万一上一轮刚被张手叫停、
                # 事件还停在 set 状态，线程里的 wait 会立刻返回，白跑一次。
                self._gest_cancel.clear()
                threading.Thread(target=self._gest_drive,
                                 args=(sec,), daemon=True).start()
            else:
                print(f"[视觉] 看不懂的手势指令：{cmd!r}")
        except Exception as exc:                           # noqa: BLE001
            print(f"[视觉] 手势发车失败：{exc}")

    def _gest_drive(self, sec: float):
        """握拳前进 sec 秒，到点自己停。

        为什么要自己掐表而不是让车端记时：车端只认 [go]/[stop]，
        时长在 Pi 这边控制，和语音、示教走同一条路，行为一致。

        中途张手（写 stop）会 set 取消事件，最多 50 毫秒就收手 ——
        这一条必须在，否则"想停停不住"是这套东西最不能接受的失败。
        """
        v = float((self.cfg or {}).get("speed", 1.4))
        try:
            self.link.send(f"[go,{v:.2f},0]")
            print(f"[视觉] 手势前进 {sec:g}s 开始（v={v:.2f}）")
            if self._gest_cancel.wait(sec):
                print("[视觉] 手势前进被打断")
        except Exception as exc:                           # noqa: BLE001
            print(f"[视觉] 手势前进出错：{exc}")
        finally:
            try:
                self.link.send("[stop]")
                print("[视觉] 手势前进结束 → [stop]")
            except Exception as exc:                       # noqa: BLE001
                print(f"[视觉] 收尾 [stop] 失败：{exc}")

    def ensure(self):
        if self.link is not None or self.status == "connecting":
            return
        self.status = "connecting"
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            import car
            cfg = dict(car.DEFAULT_CFG)
            cfg.update(load_json("/home/pi/car.json", {}))
            self.cfg = cfg
            if not cfg.get("mac"):
                print("[车] car.json 里没有 mac，改用串口")
            link = car.Link(cfg.get("port") or "/dev/rfcomm0",
                            baud=int(cfg.get("baud", 9600)),
                            mac=cfg.get("mac") or None,
                            channel=int(cfg.get("channel", 1)),
                            tries=2, timeout=3)   # 车没上电时约 9 秒就放弃，别让界面一直卡在"连接中"
            self.link = link
            self._born = time.time()
            self.status = "ok"
            print(f"[车] 已连接 {cfg.get('mac')}")
        except SystemExit as exc:                          # car.py 连不上会 sys.exit
            self.status = "fail"
            self._retry_at = time.time()
            self.err = str(exc).splitlines()[0]
            print(f"[车] 连接失败：{self.err}")
        except Exception as exc:                           # noqa: BLE001
            self.status = "fail"
            self._retry_at = time.time()
            self.err = str(exc)
            traceback.print_exc()

    def send(self, s):
        if self.link is None:
            return False
        try:
            self.link.send(s)
            return True
        except Exception:                                  # noqa: BLE001
            return False

    def snap(self):
        if self.link is None:
            return {"angle": 0.0, "speed": 0.0, "yaw": 0.0, "flags": 0}
        try:
            return self.link.snap()
        except Exception:                                  # noqa: BLE001
            return {"angle": 0.0, "speed": 0.0, "yaw": 0.0, "flags": 0}

    def close(self):
        try:
            if self.link is not None:
                self.link.send("[stop]")
                self.link.close()
        except Exception:                                  # noqa: BLE001
            pass


# ================================================================ 共享服务
class State:
    """给 petui 用的界面状态（字段和 pet.py 保持一致）"""

    def __init__(self):
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
        self.show_back = True


class Services:
    """麦克风、识别模型、TTS —— 所有应用共用这一套"""

    def __init__(self, args):
        self.args = args
        self.rec = None
        self.vad = None
        self.window = 0
        self.llm_cfg = None
        self.llm_err = ""

        self.audio = collections.deque(maxlen=240)
        self.audio_lock = threading.Lock()
        self.level = 0.0
        self.muted = args.mute

        self.spk = None
        self.speak_lock = threading.Lock()
        self.speaking = False
        self.engine = args.tts

        self.carry = np.zeros(0, dtype=np.float32)
        self.hist = np.zeros(0, dtype=np.float32)
        self.buf = None
        self.preroll = int(0.35 * RATE)
        self.proc = None

    # ---------------- 加载 ----------------
    def load(self):
        print("加载语音模型…", end="", flush=True)
        t0 = time.time()
        hw = "" if self.args.no_hotwords else "/home/pi/hotwords.txt"
        backend = "streaming" if getattr(self.args, "streaming", False) else None
        try:
            self.rec = make_asr(2, hw, backend)
        except Exception as exc:                           # noqa: BLE001
            print(f"\n[警告] 识别模型加载失败({exc})，回退流式小模型", end="")
            self.rec = make_asr(2, "", "streaming")
        self.vad, self.window = make_vad()
        print(f" 完成 ({time.time() - t0:.1f}s)")

        try:
            self.llm_cfg = load_cfg(types.SimpleNamespace(model=self.args.model))
        except SystemExit as exc:
            self.llm_err = str(exc)
            print(f"[大模型] 不可用：{exc}")

        if self.engine == "sapi":
            try:
                tts._sapi_wav("你好", tts.VOICES.get(self.args.voice, "huihui"),
                              180, timeout=2)
                print("[TTS] PC 端 SAPI 在线")
            except Exception as exc:                       # noqa: BLE001
                self.engine = "espeak"
                print(f"[TTS] PC 端 SAPI 没开（{exc}），改用本机 espeak")

    # ---------------- 采集 ----------------
    def start_capture(self):
        # 上一路先收尸：插拔几次就会攒一堆僵尸 arecord，还占着设备不放
        old = self.proc
        if old is not None:
            try:
                if old.poll() is None:
                    old.terminate()                # 先 SIGTERM，对着 USB 音频没必要上 SIGKILL
                    try:
                        old.wait(timeout=0.6)
                    except subprocess.TimeoutExpired:
                        old.kill()
                old.wait(timeout=1.0)              # 收尸，不然攒僵尸
            except Exception:                              # noqa: BLE001
                pass
        cmd = ["arecord", "-D", pcm("mic", self.args.device), "-f", "S16_LE", "-r", str(RATE),
               "-c", "1", "-t", "raw", "-q",
               "--period-size=1600", "--buffer-size=16000"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            print(f"[采集] 启动失败：{exc}", flush=True)
            self.proc = None
            return
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def capture_ok(self):
        p = self.proc
        return p is not None and p.poll() is None

    def ensure_capture(self):
        """插拔 USB 会让常驻的 arecord 报 No such device 直接退出，
        而这边收不到任何通知 —— 不重建的话，界面看着一切正常，
        其实已经听不见了，按住说话永远是「太短 0.00s」。"""
        if self.capture_ok():
            return
        self._revive = getattr(self, "_revive", 0) + 1
        if self._revive <= 3 or self._revive % 50 == 0:
            print(f"[采集] 重建录音进程（第 {self._revive} 次）", flush=True)
        self.start_capture()

    def _capture_loop(self):
        proc = self.proc                       # 用局部引用：self.proc 随时会被重建换掉
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    continue
                data = os.read(fd, CHUNK * 2)
            except (OSError, ValueError):
                return
            if not data:
                return
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2)))
            with self.audio_lock:
                self.audio.append((samples, rms))

    def take_audio(self):
        with self.audio_lock:
            items = list(self.audio)
            self.audio.clear()
        return items

    def clear_audio(self):
        with self.audio_lock:
            self.audio.clear()

    def recognize(self, samples):
        return self.rec.recognize(samples)

    # ---------------- VAD ----------------
    def reset_vad(self):
        try:
            while not self.vad.empty():
                self.vad.pop()
        except Exception:                                  # noqa: BLE001
            pass
        self.carry = np.zeros(0, dtype=np.float32)
        self.hist = np.zeros(0, dtype=np.float32)
        self.buf = None

    def feed_vad(self, samples):
        self.carry = np.concatenate([self.carry, samples]) if len(self.carry) else samples
        while len(self.carry) >= self.window:
            self.vad.accept_waveform(self.carry[:self.window])
            self.carry = self.carry[self.window:]

        self.hist = np.concatenate([self.hist, samples])
        if len(self.hist) > self.preroll:
            self.hist = self.hist[-self.preroll:]

        started = False
        if self.buf is None and self.vad.is_speech_detected():
            self.buf = self.hist.copy()
            started = True
        if self.buf is not None and not started:
            self.buf = np.concatenate([self.buf, samples])

        if not self.vad.empty():
            seg = self.buf if self.buf is not None else self.vad.front.samples
            while not self.vad.empty():
                self.vad.pop()
            self.buf = None
            self.hist = np.zeros(0, dtype=np.float32)
            if speech_seconds(seg) >= 0.3:
                return seg
        return None

    # ---------------- 朗读 ----------------
    def speak(self, text):
        if self.muted or not text:
            return
        text = tts.clean(text)
        if text:
            threading.Thread(target=self._speak, args=(text,), daemon=True).start()

    def _speak(self, text):
        with self.speak_lock:
            self.stop_speak()
            wav = None
            if self.engine == "sapi":
                try:
                    wav = tts._sapi_wav(text, tts.VOICES.get(self.args.voice, "huihui"),
                                        self.args.rate, timeout=6)
                except Exception as exc:                   # noqa: BLE001
                    print(f"  [SAPI 不可用 {exc}]")
                    self.engine = "espeak"
            self.speaking = True
            try:
                if wav and len(wav) > 1000:
                    path = "/tmp/hub_tts.wav"
                    with open(path, "wb") as f:
                        f.write(wav)
                    self.spk = subprocess.Popen(["aplay", "-D", pcm("spk", self.args.out), "-q", path])
                    t0 = time.monotonic()
                    while self.spk.poll() is None:
                        self.level = 0.45 + 0.35 * abs(np.sin((time.monotonic() - t0) * 9))
                        time.sleep(0.03)
                    self.spk = None
                else:
                    tts._espeak(text, self.args.espeak_voice, self.args.speed, pcm("spk", self.args.out))
            except Exception as exc:                       # noqa: BLE001
                print(f"  [朗读失败 {exc}]")
            finally:
                self.speaking = False
                self.level = 0.0

    def stop_speak(self):
        if self.spk is not None and self.spk.poll() is None:
            try:
                self.spk.terminate()
                self.spk.wait(timeout=1)
            except Exception:                              # noqa: BLE001
                try:
                    self.spk.kill()
                except Exception:                          # noqa: BLE001
                    pass
        self.spk = None

    def flush_input(self):
        self.clear_audio()
        try:
            self.reset_vad()
        except Exception:                                  # noqa: BLE001
            pass


# ================================================================ 门禁策略
# 进这些页面之前要先过一次人脸验证。默认只有 sys ——「系统」页里能改风扇、
# 看磁盘和网络，属于"碰机器"的操作，值得多验一次；聊天、天气这些没必要。
# 想加别的页：HUB_FACE_APPS=sys,drive（逗号分隔，key 见 hubui.TILE_DEFS）。
FACE_APPS = {k.strip() for k in
             os.environ.get("HUB_FACE_APPS", "sys").split(",") if k.strip()}
# 验证通过后多少秒内不再重复验。默认 0 = 每次点都验（用户要求）。
# 演示嫌慢可以设 HUB_FACE_TTL=60：一分钟内连着点就不重复认了。
FACE_TTL = float(os.environ.get("HUB_FACE_TTL", 0))


# ================================================================ 应用基类
class App:
    SPLIT = None                       # 有值 = 上半屏每帧刷、下半屏变化才刷
    key = ""
    title = ""

    def __init__(self, hub):
        self.hub = hub
        self.sv = hub.sv

    def enter(self):
        pass

    def leave(self):
        pass

    def tick(self, now):
        pass

    def draw(self, now):
        return hubui.render_splash("…")

    def sig(self, now):
        return ()

    def hit(self, x, y):
        return None

    def on_down(self, region):
        pass

    def on_up(self, region, x, y, dur):
        pass

    def on_move(self, x, y):
        pass

    def on_audio(self, items):
        pass

    def on_cmd(self, cmd):
        pass


# ================================================================ 人脸验证
class LockApp(App):
    """开机门禁：认出主人才放到主页。

    几个设计取舍：
    - 识别放子线程 + 子进程。vision_service 一次要 3~5 秒（要起 python、
      开摄像头、等自动曝光稳），在主循环里同步调会把触摸和刷新全卡住，
      屏幕看起来像死机。
    - 自动重试三次就停。人不在跟前时它不该一直空转 —— 一直转着同样的字，
      用户只会以为程序卡了。停下来等人点「重试」。
    - 跳过必须长按 2 秒。留后路是因为这套东西要上答辩台，摄像头歪掉不能让人
      进不去系统；但普通点一下不算，免得演示时手一抖就"验证通过"了。
    """
    key = "lock"
    title = "人脸验证"

    RETRY_GAP = 1.2       # 两次尝试之间的间隔
    AUTO_TRIES = 3        # 自动试几次后停下等人
    SKIP_HOLD = 2.0       # 长按多久算跳过
    OK_HOLD = 0.9         # 显示对勾多久再进主页

    def __init__(self, hub):
        super().__init__(hub)
        self.state = "idle"
        self.msg = ""
        self.phase = 0.0
        self.press = None
        self.target = "home"          # 解锁后进哪一页
        self.tries = 0
        self.next_at = 0.0
        self.ok_at = 0.0
        self.skip_from = 0.0
        self.worker = None
        self.result = None

    # ---------------- 识别 ----------------
    def enter(self):
        self.tries = 0
        self.result = None
        self.ok_at = 0.0
        self.skip_from = 0.0
        self.msg = "把脸对准摄像头"
        print("[锁屏] 开始人脸验证", flush=True)
        self._spawn()

    def _spawn(self):
        if self.worker is not None and self.worker.is_alive():
            return
        self.state = "checking"
        self.msg = "正在认人…"
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _work(self):
        try:
            import vision_intent as vi
            self.result = vi.who_check(wait=2.5)
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            self.result = {"ok": False, "name": "",
                           "why": f"{type(exc).__name__}: {exc}"}

    # ---------------- 状态机 ----------------
    def tick(self, now):
        self.phase = (now * 1.6) % 1.0

        if self.state == "ok":
            if now - self.ok_at >= self.OK_HOLD:
                self.hub.unlocked(self.target)
            return

        if self.state == "checking":
            r = self.result
            if r is None:
                return
            self.result = None
            self.worker = None
            if r.get("ok"):
                self.state = "ok"
                self.msg = r.get("why") or "已通过"
                self.ok_at = now
                print(f"[锁屏] 认到 {r.get('name')}"
                      f"（{float(r.get('score', -1)):.3f}），放行", flush=True)
            else:
                self.tries += 1
                self.state = "fail"
                self.msg = r.get("why") or "没认出你"
                self.next_at = now + self.RETRY_GAP
                print(f"[锁屏] 第 {self.tries} 次没过：{self.msg}", flush=True)
            return

        if self.state == "fail" and now >= self.next_at:
            if self.tries < self.AUTO_TRIES:
                self._spawn()
            else:
                self.state = "wait"
                self.msg = "没认出来，点重试或长按跳过"
                print("[锁屏] 自动重试结束，等人操作", flush=True)

    # ---------------- 触摸 ----------------
    def hit(self, x, y):
        return hubui.hit_lock(x, y)

    def on_down(self, region):
        self.press = region
        if region == "skip":
            self.skip_from = time.monotonic()

    def on_up(self, region, x, y, dur):
        held = time.monotonic() - self.skip_from if self.skip_from else 0.0
        self.press = None
        self.skip_from = 0.0
        r = hubui.hit_lock(x, y)
        if not r or r != region:
            return
        if r == "retry":
            self.tries = 0
            self.result = None
            self.msg = "再看一次…"
            self._spawn()
        elif held >= self.SKIP_HOLD:
            print(f"[锁屏] 长按 {held:.1f}s，跳过验证", flush=True)
            self.hub.unlocked(self.target, skipped=True)

    # ---------------- 渲染 ----------------
    def _hold(self):
        if not self.skip_from:
            return 0.0
        return min(1.0, (time.monotonic() - self.skip_from) / self.SKIP_HOLD)

    def draw(self, now):
        # 副标题写清楚"这一关是为什么弹出来的"：开机锁和"进系统页"
        # 用的是同一个页面，不说明的话会显得莫名其妙。
        sub = "小派只认主人"
        if self.target and self.target != "home":
            t = self.hub.apps.get(self.target)
            if t is not None and getattr(t, "title", ""):
                sub = f"进入「{t.title}」"
        return hubui.render_lock(types.SimpleNamespace(
            state=self.state, msg=self.msg, phase=self.phase,
            press=self.press, skip_hold=self._hold(), sub=sub))

    def sig(self, now):
        return (self.state, self.msg, int(self.phase * 12), self.press,
                round(self._hold(), 1), self.target)


# ================================================================ 主页
class HomeApp(App):
    key = "home"
    title = "主页"

    def __init__(self, hub):
        super().__init__(hub)
        self.press = None

    def hit(self, x, y):
        return hubui.hit_home(x, y)

    def on_down(self, region):
        self.press = region

    def on_up(self, region, x, y, dur):
        self.press = None
        if region and hubui.hit_home(x, y) == region and dur < 1.5:
            self.hub.open(region)

    def _view(self):
        w = self.hub.weather
        with w.lock:
            d = dict(w.data) if w.data else None
            kind = w.kind
        wtext = f"{d['temp']}° {self.hub.weather.desc}" if d else (
            "天气不可用" if w.err else "获取中…")
        return types.SimpleNamespace(
            clock=fmt_clock(), date=fmt_date(), wtext=wtext, wkind=kind if d else "cloud",
            car_ok=self.hub.cars.status == "ok",
            car_lost=self.hub.cars.status == "lost",
            net_ok=bool(d), press=self.press)

    def draw(self, now):
        return hubui.render_home(self._view())

    def sig(self, now):
        v = self._view()
        return (v.clock, v.wtext, v.wkind, v.car_ok, v.net_ok, v.press)


# ================================================================ 对话
class ChatApp(App):
    key = "chat"
    title = "对话"
    SPLIT = 176

    def __init__(self, hub):
        super().__init__(hub)
        self.st = State()
        self.st.history = []
        self.st.reply_text = "你好呀，我是小派。按住下面说话，或者戳我的脸。"
        self.ptt_active = False
        self.ptt_buf = []
        self.ptt_t0 = 0.0
        self.next_blink = time.monotonic() + 2.5
        self.blink_until = 0.0
        self.last_gaze = time.monotonic()

    def enter(self):
        self.st.user_text = ""
        self.st.partial = ""
        self.st.mode = "idle"

    def leave(self):
        self.ptt_active = False
        self.ptt_buf = []
        self.st.press_region = None
        self.sv.stop_speak()

    def tick(self, now):
        self.st.t = now
        self.st.muted = self.sv.muted
        self.st.level = max(self.sv.level, 0.0)
        if now >= self.next_blink:
            self.blink_until = now + 0.12
            self.next_blink = now + random.uniform(2.8, 5.5)
        self.st.blink = now < self.blink_until
        if now - self.last_gaze > 4.0 and self.st.mode == "idle":
            self.st.look = (random.uniform(-0.6, 0.6), random.uniform(-0.4, 0.4))
            self.last_gaze = now
        if self.ptt_active:
            self.st.ptt_seconds = time.monotonic() - self.ptt_t0

    def draw(self, now):
        return petui.render(self.st)

    def sig(self, now):
        st = self.st
        return (st.mode, st.user_text, st.reply_text, st.muted,
                st.press_region, st.auto_listen, int(st.ptt_seconds))

    # ---- 触摸 ----
    def hit(self, x, y):
        self.st.press_region = None
        return petui.hit(x, y)

    def on_down(self, region):
        self.st.press_region = region
        if region == "btn_ptt":
            self.hub.cmdq.put(("ptt_down",))

    def on_up(self, region, x, y, dur):
        self.st.press_region = None
        if region == "btn_ptt":
            self.hub.cmdq.put(("ptt_up",))
        elif region == "btn_mute":
            self.hub.cmdq.put(("toggle_mute",))
        elif region in ("btn_repeat", "bubble"):
            self.hub.cmdq.put(("repeat",))
        elif region == "auto":
            self.hub.cmdq.put(("toggle_auto",))
        elif region == "face":
            self.hub.cmdq.put(("reset",) if dur >= 0.9 else ("pet",))

    def on_move(self, x, y):
        dx = (x - petui.FACE_CX) / 70.0
        dy = (y - petui.FACE_CY) / 90.0
        n = max(1.0, (dx * dx + dy * dy) ** 0.5)
        self.st.look = (max(-1.0, min(1.0, dx / n)), max(-1.0, min(1.0, dy / n)))

    # ---- 音频 ----
    def on_audio(self, items):
        st = self.st
        if self.ptt_active:
            for samples, _rms in items:
                self.ptt_buf.append(samples)
            if st.ptt_seconds >= MAX_PTT:
                print("[按住说话] 到 20 秒上限，自动发送")
                self._finish_ptt()
            return
        if st.mode in ("thinking", "speaking", "happy"):
            return
        if not st.auto_listen or self.sv.vad is None:
            return
        seg = None
        for samples, _rms in items:
            s = self.sv.feed_vad(samples)
            if s is not None:
                seg = s
        if seg is not None and st.mode == "idle":
            text = self.sv.recognize(seg)
            print(f"  [免提识别] {text!r}")
            if text:
                self._reply(text)

    # ---- 业务 ----
    def on_cmd(self, cmd):
        st = self.st
        kind = cmd[0]
        if kind == "ptt_down":
            self.sv.stop_speak()
            self.sv.clear_audio()
            self.sv.reset_vad()
            self.ptt_buf = []
            self.ptt_t0 = time.monotonic()
            self.ptt_active = True
            st.mode = "listening"
            st.user_text = ""
            st.partial = ""
            st.reply_text = ""
            print("[按住说话] 开始录音")
        elif kind == "ptt_up":
            if self.ptt_active:
                self._finish_ptt()
        elif kind == "toggle_mute":
            self.sv.muted = not self.sv.muted
            st.muted = self.sv.muted
            if self.sv.muted:
                self.sv.stop_speak()
                st.level = 0.0
            print(f"[静音] {'开' if self.sv.muted else '关'}")
        elif kind == "toggle_auto":
            st.auto_listen = not st.auto_listen
            self.sv.reset_vad()
            print(f"[免提] {'开' if st.auto_listen else '关'}")
        elif kind == "repeat":
            if st.reply_text:
                st.mode = "speaking"
                self.sv.speak(st.reply_text)
                self.sv.flush_input()
                st.mode = "idle"
        elif kind == "pet":
            line = random.choice(PET_LINES)
            st.mode = "happy"
            st.reply_text = line
            self.sv.speak(line)
            self.sv.flush_input()
            time.sleep(0.5)
            st.mode = "idle"
        elif kind == "reset":
            st.history = []
            st.user_text = ""
            st.reply_text = "好啦，之前聊的我都忘掉了。"
            st.mode = "happy"
            self.sv.speak(st.reply_text)
            time.sleep(0.4)
            st.mode = "idle"
            print("[上下文] 已清空")

    def _finish_ptt(self):
        st = self.st
        self.ptt_active = False
        st.ptt_seconds = time.monotonic() - self.ptt_t0
        seg = np.concatenate(self.ptt_buf) if self.ptt_buf else np.zeros(0, dtype=np.float32)
        self.ptt_buf = []
        if self.sv.rec is None:                            # --demo 模式没有麦克风
            st.mode = "idle"
            st.reply_text = "演示模式：没有麦克风，只听不见。"
            return
        if len(seg) < MIN_PTT * RATE or speech_seconds(seg) < 0.25:
            st.mode = "idle"
            st.reply_text = "太短啦，按住按钮说完再松手。"
            print(f"  [太短 {len(seg) / RATE:.2f}s，丢弃]")
            return
        st.mode = "thinking"
        t0 = time.time()
        text = self.sv.recognize(seg)
        print(f"  [识别 {time.time() - t0:.2f}s] {text!r}")
        if not text:
            st.mode = "idle"
            st.reply_text = "没听清，再说一遍？"
            self.sv.speak("没听清，再说一遍？")
            return
        self._reply(text)

    def _reply(self, text):
        st = self.st
        if not hasattr(st, "history"):
            st.history = []
        if self.sv.llm_cfg is None:
            st.mode = "idle"
            st.reply_text = "我的大模型还没配好，先去 config 里填 key。"
            self.sv.speak(st.reply_text)
            return
        st.user_text = text
        st.history.append({"role": "user", "content": text})
        if len(st.history) > HISTORY_MAX:
            del st.history[:-HISTORY_MAX]
        st.mode = "thinking"
        t0 = time.time()
        answer = handle_memo_cmd(text)
        if answer is None:
            remember_from(text)         # 先记，这一句就能用上新名字
            w = None
            try:                        # 天气页拉到的数据直接用，不让模型自己去猜
                ws = self.hub.weather
                with ws.lock:
                    if ws.data:
                        w = dict(ws.data)
                        w["desc"] = ws.desc
                        w["city"] = ws.city
            except Exception:           # noqa: BLE001
                w = None
            answer = ask_llm(st.history, self.sv.llm_cfg, live_context(w or {}),
                             deep=is_deep(text))
        st.history.append({"role": "assistant", "content": answer})
        st.reply_text = answer
        print(f"你: {text}\n小派: {answer}   ({(time.time() - t0) * 1000:.0f}ms)", flush=True)
        st.mode = "speaking"
        self.sv.speak(answer)
        self.sv.flush_input()
        st.mode = "idle"


# ================================================================ 天气页
class WeatherApp(App):
    key = "weather"
    title = "天气"

    def enter(self):
        if self.hub.weather.data is None and not self.hub.weather.err:
            threading.Thread(target=self.hub.weather.refresh, daemon=True).start()

    def hit(self, x, y):
        return "tap" if y > hubui.HEAD_H else None

    def on_up(self, region, x, y, dur):
        if region == "tap":
            threading.Thread(target=self.hub.weather.refresh, daemon=True).start()

    def _view(self):
        w = self.hub.weather
        with w.lock:
            d = dict(w.data) if w.data else None
            err = w.err
            kind, desc = w.kind, w.desc
            city = w.city or "定位中"
        if not d:
            return types.SimpleNamespace(
                city=city_label(city), temp="--",
                desc="获取中…" if not err else "不可用",
                kind="cloud", feels="--", hilo="--", pop="--", hum="--", wind="--",
                note=["还没拿到天气数据", "检查网络，或轻触屏幕重试"] +
                     ([f"错误：{err}"] if err else []), ts=0)
        upd = time.strftime("%H:%M", time.localtime(d["ts"]))
        note = [f"更新 {upd} · open-meteo", "数据来源 open-meteo · 免密钥"]
        if err:
            note[0] = f"更新 {upd}（本次刷新失败）"
        return types.SimpleNamespace(
            city=city_label(city), temp=f"{d['temp']}°", desc=desc, kind=kind,
            feels=f"{d['feels']}°", hilo=f"{d['hi']}° / {d['lo']}°",
            pop=f"{d['pop']}%", hum=f"{d['hum']}%", wind=f"{d['wind']:.1f} m/s",
            note=note, ts=d["ts"])

    def draw(self, now):
        return hubui.render_weather(self._view())

    def sig(self, now):
        v = self._view()
        return (v.ts, v.temp, v.desc, v.city, v.note[0] if v.note else "")


# ================================================================ 温湿度页
class EnvApp(App):
    key = "env"
    title = "温湿度"

    def _view(self):
        e = self.hub.env
        with e.lock:
            temp, hum = e.temp, e.hum
            temps = list(e.hist)
            src, err = e.src, e.err
        cpu = e.cpu
        notes = []
        if temp is None:
            w = self.hub.weather
            with w.lock:
                d = dict(w.data) if w.data else None
            if d:
                temp, hum = d["temp"], d["hum"]
                src = "室外"
                notes.append(("没接 DHT22，上面显示的是室外天气值", hubui.WARM))
            else:
                src = "无数据"
                notes.append(("没接 DHT22，也拿不到天气", hubui.RED))
            notes.append(("接法：VCC→3.3V  DATA→GPIO4  GND→GND", petui.MUTED))
        else:
            notes.append((f"采样间隔 2 秒 · 共 {len(temps)} 点", petui.MUTED))
            notes.append(("DHT22 精度 ±0.5°C / ±2%RH", petui.MUTED))
        if cpu is not None:
            notes.append((f"树莓派 CPU {cpu:.1f}°C", petui.ACCENT if cpu < 70 else hubui.RED))
        if err and temp is None:
            notes.append((f"传感器：{err}", (96, 112, 132)))
        return types.SimpleNamespace(
            src=src,
            tlabel="室外温度" if src == "室外" else "室内温度",
            hlabel="室外湿度" if src == "室外" else "相对湿度",
            temp=f"{temp:.1f}" if isinstance(temp, (int, float)) else "--",
            hum=f"{hum:.0f}" if isinstance(hum, (int, float)) else "--",
            trend=["实时", "实时"] if temps else ["", ""],
            temps=temps,
            curve_title=(f"最近 {len(temps) * 2 // 60} 分 {len(temps) * 2 % 60} 秒"
                         if temps else "未接入传感器，暂无曲线"),
            curve_empty="" if temps else "接上 DHT22 后显示曲线",
            lo=f"{min(temps):.1f}°" if temps else "",
            hi=f"{max(temps):.1f}°" if temps else "",
            notes=notes)

    def draw(self, now):
        return hubui.render_env(self._view())

    def sig(self, now):
        v = self._view()
        return (v.temp, v.hum, len(v.temps), v.src)


# ================================================================ 遥控页
class DriveApp(App):
    key = "drive"
    title = "遥控"

    def __init__(self, hub):
        super().__init__(hub)
        self.press = None
        self.speed = 1.4
        self.hint = ""
        self.t_press = 0.0

    def enter(self):
        self.hub.cars.ensure()
        self.speed = float(self.hub.cars.cfg.get("speed", 1.4))
        self.hint = ""

    def leave(self):
        self.hub.cars.send("[stop]")
        self.press = None
        self.hint = ""

    def tick(self, now):
        """按住行驶的超时兜底：万一抬起事件丢了（触摸屏偶发），车不能一直跑"""
        if self.press in ("up", "down") and now - self.t_press > 6.0:
            self.hub.cars.send("[stop]")
            self.press = None
            self.hint = "连续走了 6 秒，已自动停车"

    def hit(self, x, y):
        return hubui.hit_drive(x, y)

    def on_down(self, region):
        self.press = region
        self.t_press = time.monotonic()
        cars = self.hub.cars
        if cars.link is None:
            self.hint = "车没连上，先给车上电"
            return
        if region == "up":
            cars.send(f"[go,{self.speed:.2f},0]")
            self.hint = "行驶中 · 松手停车"
        elif region == "down":
            cars.send(f"[go,{-self.speed:.2f},0]")
            self.hint = "倒车中 · 松手停车"
        elif region == "left":
            cars.send("[turn,-90]")
            self.hint = "左转 90° · 车自己转到位"
        elif region == "right":
            cars.send("[turn,90]")
            self.hint = "右转 90° · 车自己转到位"
        elif region == "stop":
            cars.send("[stop]")
            self.hint = "已停车"
        elif region in ("faster", "slower"):
            step = float(cars.cfg.get("speed_step", 0.4))
            lo = float(cars.cfg.get("speed_min", 0.5))
            hi = float(cars.cfg.get("speed_max", 3.2))
            self.speed = max(lo, min(hi, self.speed + (step if region == "faster" else -step)))
            self.hint = f"速度 {self.speed:.1f}"

    def on_up(self, region, x, y, dur):
        self.press = None
        if region in ("up", "down"):
            self.hub.cars.send("[stop]")
            self.hint = "已停"

    def _tel(self):
        return self.hub.cars.snap()

    def draw(self, now):
        s = self._tel()
        cars = self.hub.cars
        return hubui.render_drive(types.SimpleNamespace(
            car_ok=cars.status == "ok", connecting=cars.status == "connecting",
            car_lost=cars.status == "lost",
            angle=f"{s['angle']:+.1f}°", speed=f"{s['speed']:+.2f}",
            yaw=f"{s['yaw']:+.0f}°", flags=s.get("flags", 0),
            spd=f"{self.speed:.1f}", press=self.press, hint=self.hint))

    def sig(self, now):
        s = self._tel()
        return (int(s["angle"] * 10), int(s["speed"] * 100), int(s["yaw"]),
                s.get("flags", 0), self.hub.cars.status, f"{self.speed:.1f}",
                self.press, self.hint)


# ================================================================ 语音控车页
class VoiceCarApp(App):
    key = "voice"
    title = "语音控车"

    def __init__(self, hub):
        super().__init__(hub)
        self.said = ""
        self.action = ""
        self.listening = False
        self.secs = 0.0
        self.press = None
        self.ptt_buf = []
        self.ptt_t0 = 0.0
        self.speed = 1.4
        self.routes = None          # 示教路线库，第一次用到才读文件
        self.teach = ""             # 当前示教状态，显示在屏幕上
        self.epoch = 0              # 每次点"停车"或离开页面 +1，后台动作看到变化就放弃

    def enter(self):
        self.hub.cars.ensure()
        self.speed = float(self.hub.cars.cfg.get("speed", 1.4))

    def _routes(self):
        if self.routes is None:
            import car
            self.routes = car.Routes(car.ROUTE_DB)
        return self.routes

    def leave(self):
        self.listening = False
        self.press = None
        self.ptt_buf = []
        self.epoch += 1                 # 让还在转的后台动作收手
        self.hub.cars.send("[stop]")

    def hit(self, x, y):
        return hubui.hit_voice(x, y)

    def on_down(self, region):
        self.press = region
        if region == "talk":
            self.hub.cmdq.put(("vcar_down",))
        elif region == "stop":
            self.epoch += 1             # 刹车优先：正在转圈/回放的线程下一段就不发了
            self.hub.cars.send("[stop]")
            self.sv.stop_speak()
            self.action = "停车"

    def on_up(self, region, x, y, dur):
        self.press = None
        if region == "talk":
            self.hub.cmdq.put(("vcar_up",))

    def tick(self, now):
        if self.listening:
            self.secs = time.monotonic() - self.ptt_t0
            if self.secs >= MAX_PTT:
                self.hub.cmdq.put(("vcar_up",))

    def on_audio(self, items):
        if self.listening:
            for samples, _rms in items:
                self.ptt_buf.append(samples)

    def on_cmd(self, cmd):
        kind = cmd[0]
        if kind == "vcar_down":
            self.sv.stop_speak()
            self.sv.clear_audio()
            self.ptt_buf = []
            self.ptt_t0 = time.monotonic()
            self.listening = True
            self.said = ""
            self.action = ""
            print("[语音控车] 开始录音")
        elif kind == "vcar_up":
            if self.listening:
                self._finish()

    def _finish(self):
        self.listening = False
        seg = np.concatenate(self.ptt_buf) if self.ptt_buf else np.zeros(0, dtype=np.float32)
        self.ptt_buf = []
        if self.sv.rec is None:                            # --demo 模式
            self.action = "演示模式没有麦克风"
            return
        if len(seg) < MIN_PTT * RATE:
            self.action = "太短了"
            return
        self.sv.clear_audio()
        text = self.sv.recognize(seg)
        self.said = text
        print(f"[语音控车] 识别：{text!r}")
        if not text:
            self.action = "没听清，再说一次"
            return
        got = self._resolve(text)
        if got is None and asr_fix is not None:
            # 按原话一个动作都解析不出来，才动用拼音纠正。这样做的好处是
            # 正常说法永远走原路，纠正层没有机会把对的改错。
            fixed = asr_fix.fix(text)
            if fixed and fixed != text:
                self.said = fixed
                print(f"[语音控车] 纠正：{text!r} → {fixed!r}  {asr_fix.last}")
                got = self._resolve(fixed)
        if got is None:
            self.action = "没听懂这句"
            self.sv.speak("没听懂")
            return
        kind, obj = got
        if kind == "teach":
            threading.Thread(target=self._teach, args=(obj,), daemon=True).start()
        elif kind == "vision":
            threading.Thread(target=self._vision, args=(obj,), daemon=True).start()
        else:
            threading.Thread(target=self._run, args=(obj,), daemon=True).start()

    def _resolve(self, text):
        """一句话 → (类别, 动作)。三级顺序不能换：

        示教（记住这条路）和视觉（看我是谁 / 打开手势）的说法如果先交给
        parse_intent，会被当成"没听懂这句"，所以在它前面单独判掉。
        """
        try:
            import car
        except Exception as exc:                           # noqa: BLE001
            print(f"  [car 不可用] {exc}")
            return None
        try:
            ti = car.teach_intent(text)
        except Exception as exc:                           # noqa: BLE001
            print(f"  [示教解析失败] {exc}")
            ti = None
        if ti:
            return ("teach", ti)
        if vision_intent is not None:
            try:
                vi = vision_intent.parse(text)
            except Exception as exc:                       # noqa: BLE001
                print(f"  [视觉解析失败] {exc}")
                vi = None
            if vi:
                return ("vision", vi)
        try:
            intent = car.parse_intent(text, self.hub.cars.cfg or car.DEFAULT_CFG)
        except Exception as exc:                           # noqa: BLE001
            print(f"  [意图解析失败] {exc}")
            intent = None
        return ("car", intent) if intent else None

    # ---------------- 视觉 ----------------
    def _vision(self, vi):
        """视觉要开子进程读摄像头，几百毫秒到几秒都正常，必须另开线程，
        否则整块屏幕在等识别结果时是不动的。"""
        act = (vi or {}).get("act")
        self.action = "让我看看…" if act in ("who", "what") else "好"
        try:
            reply = vision_intent.run(vi)
        except Exception:                                  # noqa: BLE001
            print(f"  [视觉出错]\n{traceback.format_exc()}")
            reply = "视觉那边出问题了"
        self.action = reply
        self.sv.speak(reply)

    # ---------------- 示教再现 ----------------
    def _teach(self, ti):
        rt = self._routes()
        a = ti.get("act")
        if a == "teach_start":
            rt.start()
            self.teach = "● 录制中"
            self.action = "开始记路 · 你指挥"
            self.sv.speak("好，你指挥，我跟着记")
            print("[示教] 开始记录")
        elif a == "teach_stop":
            name = rt.finish()
            self.teach = ""
            if name:
                n = len(rt.data[name])
                self.action = f"已存「{name}」{n}步"
                self.sv.speak(f"记好了，{n}步")
                print(f"[示教] 存为「{name}」，{n} 步")
            else:
                self.action = "还没记东西"
                self.sv.speak("还没记东西")
        elif a == "teach_list":
            names = "、".join(rt.data) or "还没有路线"
            self.action = names
            self.teach = f"共{len(rt.data)}条"
            self.sv.speak(f"我记了{len(rt.data)}条路" if rt.data else "还没记过路")
        elif a == "teach_replay":
            key = ti.get("name") or rt.last or ""
            name, seq = rt.find(key)
            if not seq:
                self.action = "没有这条路"
                self.sv.speak("我没有记过这条路")
                print(f"[示教] 找不到「{key}」，现有：{'、'.join(rt.data) or '空'}")
                return
            self.action = f"重走「{name}」{len(seq)}步"
            self.sv.speak("这就走")
            print(f"[示教] 回放「{name}」，{len(seq)} 步")
            self._replay(seq)

    def _replay(self, seq):
        cars = self.hub.cars
        cfg = cars.cfg or {}
        v = self.speed
        e0 = self.epoch
        stopped = False
        for i, st in enumerate(seq):
            if self.hub.app is not self or self.epoch != e0:
                print("[示教] 已离开语音页或被叫停，回放中止")
                stopped = True
                break
            a = st.get("act")
            self.action = f"重走 {i + 1}/{len(seq)}"
            if a in ("forward", "back"):
                sec = float(st.get("sec") or cfg.get("drive_seconds", 2.0))
                cars.send(f"[go,{v if a == 'forward' else -v:.2f},0]")
                time.sleep(sec + 0.35)
                cars.send("[stop]")
                time.sleep(0.2)
            elif a == "turn":
                if not self._turn(float(st.get("deg", 90))):
                    stopped = True
                    break
            elif a == "stop":
                cars.send("[stop]")
        cars.send("[stop]")
        if stopped:
            self.action = "已叫停"
            return
        self.action = f"重走完了 {len(seq)} 步"
        self.sv.speak("走完了")
        print("[示教] 回放结束")

    def _rec(self, step):
        """录制中就把这一步存进去，回放时照着重走"""
        if self.routes is not None and self.routes.rec is not None:
            self.routes.add(step)
            self.teach = f"● 录制中 {len(self.routes.rec)} 步"

    def _turn(self, deg):
        """转向闭环在车上，Pi 只负责等。56°/s 是实测转速，+0.8 秒留给起步和收尾。

        车端收到 [stop] 会立刻把 YawMode 清零，所以这里不用再拆段了 ——
        360° 一段发下去，中途按停车随时能打断。每 0.1 秒瞟一眼 epoch，
        按下停车后最多 0.1 秒就收手，屏幕上的"转向中"立刻停住。"""
        import car
        why = self._guard("turn")
        if why:
            print(f"[转向] 放弃：{why}")
            self.action = why
            self.hub.cars.send("[stop]")
            return False
        e0 = self.epoch
        for seg in car.turn_plan(deg):
            if self.epoch != e0:
                print("[转向] 开转前就被叫停了")
                self.hub.cars.send("[stop]")
                return False
            self.hub.cars.send(f"[turn,{seg:.0f}]")
            deadline = time.time() + abs(seg) / 56.0 + 0.8
            while time.time() < deadline:
                if self.epoch != e0:
                    print("[转向] 中途被叫停")
                    self.hub.cars.send("[stop]")
                    return False
                time.sleep(0.1)
        return True

    def _guard(self, action=None):
        """动手之前先看车端状态位，别对着一个不会动的车瞎发指令。

        返回 None 表示可以执行，否则返回给用户看的提示语。

        位定义（固件 `[s,...,flags]`）：运行 1 + 转向中 2 + 跌倒锁定 4 + 陀螺仪异常 8。
        这两个都是"发了也没用、但不发又不明所以"的情况，必须说清楚：
          * 跌倒锁：车摔过，`Fallen=1` 会锁死，语音指令一律不敢复活，只能按 Key1；
          * 陀螺仪异常：上电时车在动，零偏标定错了，航向角以几百 °/s 自己漂。
            这时候发转向，闭环会把漂移当成真转向，几百毫秒就"以为"转到位 ——
            表现出来就是"车抽两下就停"，参数怎么调都没用。"""
        if self.hub.cars.status == "lost":
            return "车掉线了，正在自动重连"
        s = self.hub.cars.snap()
        flags = int(s.get("flags") or 0)
        if flags & 8:
            return "陀螺仪没标好：放平静止后重新上电"
        if flags & 4 and action != "stop":
            return "车摔过，按车上 Key1 复位"
        return None

    def _run(self, it):
        cars = self.hub.cars
        cfg = cars.cfg or {}
        act = it.get("act")
        if cars.link is None:
            self.action = "车没连上"
            return
        why = self._guard(act)
        if why:
            print(f"[语音控车] 拒绝执行：{why}")
            self.action = why
            self.sv.speak(why)
            return
        if act in ("forward", "back"):
            sec = it.get("sec")
            hold = it.get("hold")
            if hold:
                sec = float(cfg.get("hold_max", 8.0))
            elif not sec:
                sec = float(cfg.get("drive_seconds", 2.0))
            v = self.speed if act == "forward" else -self.speed
            cars.send(f"[go,{v:.2f},0]")
            self.action = f"{'前进' if act == 'forward' else '后退'} {sec:.1f}s"
            self._rec({"act": act, "sec": sec})
            time.sleep(sec)
            cars.send("[stop]")
            self.action += " · 已停"
        elif act == "turn":
            deg = float(it.get("deg", 90))
            self.action = "原地转一圈" if abs(deg) >= 355 else \
                f"{'右' if deg > 0 else '左'}转 {abs(deg):.0f}°"
            self._rec({"act": "turn", "deg": deg})
            if self._turn(deg):
                self.action += " · 完成"
            else:
                self.action = "已叫停"
        elif act == "stop":
            cars.send("[stop]")
            self.action = "停车"
            self._rec({"act": "stop"})
        elif act in ("faster", "slower"):
            step = float(cfg.get("speed_step", 0.4))
            lo = float(cfg.get("speed_min", 0.5))
            hi = float(cfg.get("speed_max", 3.2))
            self.speed = max(lo, min(hi, self.speed + (step if act == "faster" else -step)))
            self.action = f"速度 {self.speed:.1f}"
        elif act == "status":
            s = cars.snap()
            txt = f"我歪了{s['angle']:+.0f}度，速度{s['speed']:+.2f}，航向{s['yaw']:+.0f}度"
            self.action = "状态已汇报"
            self.sv.speak(f"我现在歪{s['angle']:+.0f}度，速度{s['speed']:+.2f}")
            print(f"[语音控车] {txt}")

    def draw(self, now):
        s = self.hub.cars.snap()
        cars = self.hub.cars
        tel = (f"俯仰 {s['angle']:+.1f}°   速度 {s['speed']:+.2f}   "
               f"航向 {s['yaw']:+.0f}°")
        return hubui.render_voice(types.SimpleNamespace(
            car_ok=cars.status == "ok", tel=tel, flags=s.get("flags", 0),
            car_lost=cars.status == "lost",
            said=self.said, action=self.action, listening=self.listening,
            secs=self.secs, press=self.press, spd=f"{self.speed:.1f}"))

    def sig(self, now):
        s = self.hub.cars.snap()
        return (int(s["angle"] * 10), int(s["yaw"]), s.get("flags", 0),
                self.hub.cars.status, self.said, self.action,
                self.listening, int(self.secs), self.press, f"{self.speed:.1f}")


# ================================================================ 系统页
class SysApp(App):
    key = "sys"
    title = "系统"

    def _rows(self):
        rows = []
        # 第一行是风扇开关，点一下在 手动关 → 手动开 → 自动 之间循环
        mode, duty, _ft = fan_state()
        if not fan_alive():
            ftext, fcol = "未运行", hubui.RED
        elif mode == "off":
            ftext, fcol = "手动·关", petui.MUTED
        elif duty > 0:
            ftext = f"{FAN_NAMES[mode]} {duty}%"
            # 手动开用暖色，和自动跑的绿色分开 —— 一眼能看出现在是人在管
            fcol = petui.WARM if mode == "on" else hubui.GREEN
        else:
            ftext, fcol = f"{FAN_NAMES[mode]} 待机", petui.MUTED
        rows.append(("风扇", ftext, fcol))

        t = cpu_temp()
        rows.append(("CPU 温度", f"{t:.1f} °C" if t else "—",
                     hubui.RED if (t or 0) > 70 else hubui.WARM))
        try:
            l1, l5, l15 = os.getloadavg()
            rows.append(("负载 (1/5/15)", f"{l1:.2f} {l5:.2f} {l15:.2f}", petui.TEXT))
        except OSError:
            rows.append(("负载", "—", petui.MUTED))
        tot, used = mem_info()
        rows.append(("内存", f"{used:.1f}/{tot:.1f} GB ({used / tot * 100:.0f}%)"
                     if tot else "—", petui.TEXT))
        tot, used = disk_info()
        rows.append(("磁盘", f"{used:.1f}/{tot:.1f} GB ({used / tot * 100:.0f}%)"
                     if tot else "—", petui.TEXT))
        ip = local_ip()
        rows.append(("本机地址", ip, petui.ACCENT if ip.count(".") == 3 else petui.MUTED))
        rows.append(("运行时长", uptime_text(), petui.TEXT))
        st = self.hub.cars.status
        rows.append(("小车蓝牙", {"ok": "已连接", "connecting": "连接中",
                                  "lost": "掉线（自动重连中）",
                                  "fail": "连接失败"}.get(st, "未连接"),
                     hubui.GREEN if st == "ok" else
                     (petui.WARM if st == "lost" else petui.MUTED)))
        return rows

    def hit(self, x, y):
        return "fan" if hubui.inside(hubui.SYS_FAN_BOX, x, y) else None

    def on_up(self, region, x, y, dur):
        if region == "fan" and dur < 1.5 and hubui.inside(hubui.SYS_FAN_BOX, x, y):
            mode, _d, _t = fan_state()
            fan_set(fan_next(mode))

    def draw(self, now):
        return hubui.render_sys(types.SimpleNamespace(clock=fmt_clock(), rows=self._rows()))

    def sig(self, now):
        mode, duty, _t = fan_state()
        return tuple((a, b) for a, b, _c in self._rows()) + (fmt_clock(), mode, duty)


# ================================================================ 主程序
class Hub:
    def __init__(self, args):
        self.args = args
        self.demo = args.demo
        self.running = True
        self.cmdq = queue.Queue()
        self.sv = Services(args)
        self.weather = WeatherSvc()
        self.env = EnvSvc()
        self.cars = CarCtl()

        self.app = None
        self.home = None
        self.lock = None
        self.apps = {}
        self.locked = False          # True = 还没过人脸验证
        self.lock_on = not getattr(args, "no_lock", False)
        self._allow = None           # 一次性通行证：验证通过后放行这一页
        self.face_ok_at = -1e9       # 上次验证通过的时刻，配合 FACE_TTL
        self.frames = 0
        self.t_start = time.monotonic()

    # ---------------- 硬件 ----------------
    def open_hw(self):
        screen.load_cal()
        self.lcd = screen.open_display()
        self.lcd.show(hubui.render_splash("正在启动……"))
        self.touch, self.irq = screen.open_touch()
        self.events = screen.touch_queue()
        self.poller = screen.TouchPoller(self.touch, self.irq, self.events)
        self.poller.start()

    # ---------------- 应用切换 ----------------
    def build_apps(self):
        self.home = HomeApp(self)
        self.lock = LockApp(self)
        for cls in (ChatApp, WeatherApp, EnvApp, DriveApp, VoiceCarApp, SysApp):
            self.apps[cls.key] = cls(self)
        self.app = self.home
        # 后台先把蓝牙连上，进遥控页就能立刻看到遥测，不用等 1~2 秒
        self.cars.ensure()

    def open(self, key):
        app = self.apps.get(key)
        if app is None:
            return
        if self.locked:          # 还没过开机验证，任何页面都不放行
            return
        if key in FACE_APPS and self._allow != key:
            # 敏感页每次进都要验。_allow 是 unlocked() 发的一次性通行证，
            # 没有它这里会自己锁自己、无限递归 —— 这是这类门禁最典型的坑。
            if FACE_TTL <= 0 or time.monotonic() - self.face_ok_at > FACE_TTL:
                print(f"[锁屏] 进「{app.title}」需要人脸验证", flush=True)
                self.lock_now(target=key)
                return
        self._allow = None
        try:
            self.app.leave()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()
        self.app = app
        try:
            app.enter()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()

    def unlocked(self, target="home", skipped=False):
        """人脸验证通过（或长按跳过）之后调用。target 是要进的页面。"""
        print(f"[锁屏] {'跳过验证' if skipped else '验证通过'} → {target}",
              flush=True)
        self.locked = False
        self.face_ok_at = time.monotonic()
        if target != "home" and target in self.apps:
            self._allow = target          # 先发票，再开门，否则 open 又锁回来
            self.open(target)
        else:
            self.app = self.home

    def lock_now(self, target="home"):
        """锁回去。开机自动调一次，进敏感页前也会调。"""
        if self.lock is None:
            return
        self.locked = True
        self._allow = None
        self.lock.target = target or "home"
        try:
            self.app.leave()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()
        self.app = self.lock
        self.lock.enter()

    def go_home(self):
        if self.locked or self.app is self.home:
            return
        self._allow = None
        try:
            self.app.leave()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()
        self.app = self.home

    # ---------------- 大脑线程 ----------------
    def brain(self):
        while self.running:
            try:
                cmd = self.cmdq.get(timeout=0.02)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                app = self.app
                try:
                    app.on_cmd(cmd)
                except Exception:                          # noqa: BLE001
                    traceback.print_exc()
            items = self.sv.take_audio()
            if not items:
                continue
            if not self.sv.speaking:
                self.sv.level = min(1.0, max(r for _s, r in items) * 8)
            try:
                self.app.on_audio(items)
            except Exception:                              # noqa: BLE001
                traceback.print_exc()

    # ---------------- 界面线程 ----------------
    def ui(self):
        press = None
        last_sig = object()          # 保证第一帧一定推
        self.frames = 0
        self.t_start = time.monotonic()
        usb_last = usb_sig()             # USB 设备表快照，插拔后会和这里不一样
        recover_at = 0.0                 # 上次复位屏幕的时刻，用来做冷却
        cap_next = 0.0                   # 下次检查录音进程的时刻

        while self.running:
            t0 = time.monotonic()

            # 触摸事件
            while True:
                try:
                    ev = self.events.get_nowait()
                except queue.Empty:
                    break
                app = self.app
                if ev.kind == "down":
                    if app is not self.home and app is not self.lock \
                            and hubui.inside(hubui.BACK_BOX, ev.x, ev.y):
                        press = "@back"
                    else:
                        press = app.hit(ev.x, ev.y)
                    app.on_down(press)
                elif ev.kind == "move":
                    app.on_move(ev.x, ev.y)
                else:                                      # up
                    region, press = press, None
                    if region == "@back":
                        if hubui.inside(hubui.BACK_BOX, ev.x, ev.y):
                            self.go_home()
                    else:
                        app.on_up(region, ev.x, ev.y, ev.dur)

            self.cars.tick()          # 链路死活每帧刷一次（内部节流 0.5s）
            app = self.app
            now = time.monotonic()
            app.tick(now)
            img = app.draw(now)
            sig = app.sig(now)

            if app.SPLIT:
                self.lcd.show(img, 0, app.SPLIT)
                if sig != last_sig:
                    self.lcd.show(img, app.SPLIT, screen.H)
                    last_sig = sig
            elif sig != last_sig:
                self.lcd.show(img)
                last_sig = sig
            self.frames += 1

            # 屏幕自愈：见文件头「屏幕自愈」那一节
            t_now = time.monotonic()
            sig_usb = usb_sig()
            slow = self.lcd.last_frame_ms > 150
            if (sig_usb != usb_last or slow) and t_now >= recover_at:
                usb_last = sig_usb
                recover_at = t_now + 3.0        # 一次插拔只复位一次，别连着重来
                try:
                    self.lcd.init()
                    last_sig = object()         # 强制下一帧整帧重推
                    why = f"单帧 {self.lcd.last_frame_ms:.0f}ms，疑似被打断" if slow \
                        else "USB 设备变动"
                    print(f"[屏幕] {why}，已重新初始化", flush=True)
                except Exception:              # noqa: BLE001
                    traceback.print_exc()

            # 采集看门狗：插拔 USB 后 arecord 会报 No such device 自己退出，
            # 而界面照常刷新，看起来一切正常，其实已经听不见了
            if t_now >= cap_next:
                self.sv.ensure_capture()
                cap_next = time.monotonic() + 2.0

            spent = time.monotonic() - t0
            time.sleep(max(0.0, 1.0 / FPS - spent))

    # ---------------- 启动 / 退出 ----------------
    def run(self):
        self.open_hw()
        self.build_apps()

        if not self.demo:
            self.sv.load()
            self.sv.start_capture()
        else:
            self.apps["chat"].st.reply_text = "演示模式：界面能点，但没有麦克风。"

        self.weather.start()
        self.env.start()
        threading.Thread(target=self.brain, daemon=True).start()

        # 开机门禁：先验脸，认出主人才进主页。
        # 放在 ui() 之前、硬件就绪之后 —— 屏幕已经能画了，才有地方显示"正在认人"。
        if self.lock_on:
            self.lock_now()
        else:
            print("[锁屏] 已关闭（--no-lock），直接进主页", flush=True)

        try:
            self.ui()
        except KeyboardInterrupt:
            print("\n再见")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        if self.frames:
            dt = time.monotonic() - self.t_start
            print(f"[界面] {self.frames} 帧 / {dt:.0f}s = {self.frames / dt:.1f} fps")
        self.sv.stop_speak()
        self.cars.close()
        try:
            if self.sv.proc is not None:
                self.sv.proc.terminate()
                self.sv.proc.wait(timeout=2)
        except Exception:                                  # noqa: BLE001
            pass
        try:
            self.poller.stop()
            self.irq.close()
            self.touch.cs.close()
            self.lcd.bl.off()
            self.lcd.spi.close()
        except Exception:                                  # noqa: BLE001
            pass


# ================================================================ 校准 / 自检
def calibrate():
    import pet
    pet.calibrate()


def check():
    import pet
    pet.check()


def main():
    ap = argparse.ArgumentParser(description="小派控制中心")
    ap.add_argument("-d", "--device", default="auto", help="麦克风设备，auto=按名字现查")
    ap.add_argument("-o", "--out", default="auto", help="喇叭设备，auto=按名字现查")
    ap.add_argument("-v", "--voice", default="huihui")
    ap.add_argument("--rate", type=int, default=180, help="SAPI 语速")
    ap.add_argument("--speed", type=int, default=168, help="espeak 语速")
    ap.add_argument("--espeak-voice", default="cmn+f3")
    ap.add_argument("--tts", choices=["sapi", "edge", "espeak"], default="sapi")
    ap.add_argument("-m", "--model")
    ap.add_argument("--mute", action="store_true", help="启动时静音")
    ap.add_argument("--no-hotwords", action="store_true")
    ap.add_argument("--streaming", action="store_true",
                    help="用 14M 流式模型（快，但同音字容易听错）")
    ap.add_argument("--demo", action="store_true", help="不接麦克风，只跑界面")
    ap.add_argument("--no-lock", action="store_true",
                    help="跳过开机人脸验证，直接进主页（调试用）")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--spi-hz", type=int, default=screen.SPI_HZ_DISPLAY)
    args = ap.parse_args()

    if args.calibrate:
        calibrate()
        return
    if args.check:
        check()
        return

    screen.SPI_HZ_DISPLAY = args.spi_hz
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Hub(args).run()


if __name__ == "__main__":
    main()
