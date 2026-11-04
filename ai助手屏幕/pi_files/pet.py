#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小派 · 触屏对话（ST7789 240x320 + XPT2046）

在原来的语音闭环（talk.py / chat.py / tts.py）上加了一层触摸交互：
  * 按住底部大按钮说话，松手立刻识别 -> DeepSeek -> 朗读（比 VAD 更好控）
  * 点宠物脸 = 摸头，它会撒娇；长按 1 秒 = 清空上下文（新话题）
  * 点左键静音/取消静音，点右键重念上一句
  * 点右上角"免提"= 打开自动听（原来 chat.py 的 VAD 模式）
  * 朗读时按大按钮可以直接打断它
  * 眼睛会跟着你的手指看

用法:
  python3 pet.py                  # 启动
  python3 pet.py --auto           # 启动就打开免提（VAD 自动听）
  python3 pet.py --calibrate      # 重做触摸校准（点四个十字）
  python3 pet.py --check          # 触摸自检，打印原始坐标
  python3 pet.py --demo           # 不接麦克风，只跑界面（看效果）
  python3 pet.py --say "你好"     # 只测 LLM + 朗读 + 界面
  python3 pet.py --mute           # 启动时静音
退出: Ctrl+C

依赖：python3-spidev python3-gpiozero python3-pil python3-numpy fonts-wqy-zenhei
"""

import argparse
import collections
import json
import math
import os
import queue
import random
import select
import signal
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, "/home/pi")

import petui
import screen
import tts
from chat import HISTORY_MAX, ask_llm, load_cfg          # noqa: E402
from talk import make_rec, make_vad, speech_seconds, RATE  # noqa: E402

CHUNK = 1600                     # 0.1 秒
BAND_SPLIT = 176                 # 上半屏/下半屏的分界线（和 petui.BUBBLE 顶对齐）
MIN_PTT_SECONDS = 0.45           # 短于这个就不送了
MAX_PTT_SECONDS = 20.0
PET_LINES = [
    "嘿嘿，痒。",
    "再摸一下嘛。",
    "我在呢，随时待命。",
    "别摸啦，我要认真工作了。",
    "唔……好舒服。",
    "你今天心情怎么样？",
]


# ------------------------------------------------------------------ 共享状态
class State:
    """界面线程读、大脑线程写，字段都是简单值，读写不会撕裂"""

    def __init__(self):
        self.mode = "idle"                 # idle / listening / thinking / speaking / happy / error
        self.history = []
        self.user_text = ""
        self.reply_text = "你好呀，我是小派。按住下面的按钮跟我说话，或者戳我的脸。"
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
        self.interrupt = False
        self.speaking = False


# ------------------------------------------------------------------ 应用
class PetApp:
    def __init__(self, args):
        self.args = args
        self.st = State()
        self.st.muted = args.mute
        self.st.auto_listen = args.auto

        self.cmdq = queue.Queue()
        self.audio = collections.deque(maxlen=240)     # 最多 24 秒，超出丢最老的
        self.audio_lock = threading.Lock()
        self.running = True

        self.ptt_buf = []
        self.ptt_active = False
        self.ptt_t0 = 0.0

        self.rec = None
        self.vad = None
        self.window = 0
        self.carry = np.zeros(0, dtype=np.float32)
        self.hist = np.zeros(0, dtype=np.float32)
        self.buf = None
        self.preroll = int(0.35 * RATE)

        self.capture = None
        self.spk = None
        self.cfg = None
        self.frames = 0
        self.t_start = time.monotonic()

    # ---------------- 硬件 ----------------
    def open_hw(self):
        screen.load_cal()
        self.lcd = screen.open_display()
        self.touch, self.irq = screen.open_touch()
        self.events = screen.touch_queue()
        self.poller = screen.TouchPoller(self.touch, self.irq, self.events)
        self.poller.start()

    # ---------------- 语音 ----------------
    def load_models(self):
        print("加载语音模型...", end="", flush=True)
        t0 = time.time()
        hw = "" if self.args.no_hotwords else "/home/pi/hotwords.txt"
        try:
            self.rec = make_rec(2, hw)
        except Exception as exc:                      # noqa: BLE001
            print(f"\n[警告] 热词模式失败({exc})，回退 greedy", end="")
            self.rec = make_rec(2, "")
        self.vad, self.window = make_vad()
        self.cfg = load_cfg(self.args)
        print(f" 完成 ({time.time() - t0:.1f}s)")

    def recognize(self, samples):
        s = self.rec.create_stream()
        s.accept_waveform(RATE, samples)
        s.input_finished()
        while self.rec.is_ready(s):
            self.rec.decode_stream(s)
        return self.rec.get_result(s).strip()

    def start_capture(self):
        cmd = ["arecord", "-D", self.args.device, "-f", "S16_LE", "-r", str(RATE),
               "-c", "1", "-t", "raw", "-q",
               "--period-size=1600", "--buffer-size=16000"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
        self.capture = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture.start()

    def _capture_loop(self):
        fd = self.proc.stdout.fileno()
        while self.running:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            data = os.read(fd, CHUNK * 2)
            if not data:
                break
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

    def flush_input(self):
        """把喇叭放出来的回声和 VAD 里残留的句子都倒掉"""
        self.clear_audio()
        try:
            self.reset_vad()
        except Exception:                             # noqa: BLE001
            pass

    # ---------------- 朗读 ----------------
    def speak(self, text):
        """可打断的朗读：拿 WAV 用 aplay 播，说话时按按钮能立刻掐掉"""
        if self.st.muted:
            return
        text = tts.clean(text)
        if not text:
            return
        voice = tts.VOICES.get(self.args.voice, self.args.voice)
        if self.spk and self.spk.poll() is None:       # 上一句还没播完，先掐掉
            self.spk.terminate()
        wav = None
        if self.args.tts == "sapi":
            try:
                wav = tts._sapi_wav(text, voice, self.args.rate, timeout=8)
            except Exception as exc:                  # noqa: BLE001
                print(f"  [SAPI 不可用: {exc}]")
        self.st.speaking = True
        self.st.interrupt = False
        try:
            if wav and len(wav) > 1000:
                path = os.path.join("/tmp", "pet_tts.wav")
                with open(path, "wb") as f:
                    f.write(wav)
                self.spk = subprocess.Popen(["aplay", "-D", self.args.out, "-q", path])
                t0 = time.monotonic()
                while self.spk.poll() is None:
                    self.st.level = 0.45 + 0.35 * abs(math.sin((time.monotonic() - t0) * 9))
                    if self.st.interrupt:
                        self.spk.terminate()
                        try:
                            self.spk.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            self.spk.kill()
                        print("  [被打断]")
                        break
                    time.sleep(0.03)
                self.spk = None
            else:
                print("  [改用 espeak]")
                tts._espeak(text, self.args.espeak_voice, self.args.speed, self.args.out)
        finally:
            self.st.level = 0.0
            self.st.speaking = False
            self.st.interrupt = False

    # ---------------- VAD ----------------
    def reset_vad(self):
        try:
            while not self.vad.empty():
                self.vad.pop()
        except Exception:                             # noqa: BLE001
            pass
        reset = getattr(self.vad, "reset", None)
        if callable(reset):
            reset()
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

    # ---------------- 对话 ----------------
    def reply_to(self, text):
        st = self.st
        st.user_text = text
        st.history.append({"role": "user", "content": text})
        if len(st.history) > HISTORY_MAX:
            del st.history[:-HISTORY_MAX]
        st.mode = "thinking"
        t0 = time.time()
        answer = ask_llm(st.history, self.cfg)
        st.history.append({"role": "assistant", "content": answer})
        st.reply_text = answer
        print(f"你: {text}\n小派: {answer}   ({(time.time() - t0) * 1000:.0f}ms)", flush=True)
        st.mode = "speaking"
        if st.interrupt:
            print("  [你已经按住按钮了，这句就不念了]")
        else:
            self.speak(answer)
        self.flush_input()
        st.mode = "idle"

    def finish_ptt(self):
        st = self.st
        self.ptt_active = False
        st.ptt_seconds = time.monotonic() - self.ptt_t0
        seg = np.concatenate(self.ptt_buf) if self.ptt_buf else np.zeros(0, dtype=np.float32)
        self.ptt_buf = []

        if len(seg) < MIN_PTT_SECONDS * RATE or speech_seconds(seg) < 0.25:
            st.mode = "idle"
            st.reply_text = "太短啦，按住按钮说完再松手。"
            print(f"  [太短 {len(seg) / RATE:.2f}s，丢弃]")
            return

        st.mode = "thinking"
        t0 = time.time()
        text = self.recognize(seg)
        print(f"  [识别 {time.time() - t0:.2f}s] {text!r}")
        if not text:
            st.mode = "idle"
            st.reply_text = "没听清，再说一遍？"
            self.speak("没听清，再说一遍？")
            return
        self.reply_to(text)

    # ---------------- 命令 ----------------
    def handle(self, cmd):
        st = self.st
        kind = cmd[0]
        if kind == "ptt_down":
            st.interrupt = True                   # 正在朗读就打断
            time.sleep(0.05)                      # 给播放线程一个反应窗口
            st.interrupt = False                  # 用完了就清掉，别影响这一轮的回答
            self.clear_audio()
            self.reset_vad()
            self.ptt_buf = []
            self.ptt_t0 = time.monotonic()
            self.ptt_active = True
            st.mode = "listening"
            st.user_text = ""
            st.partial = ""
            st.reply_text = ""
            print("[按住说话] 开始录音", flush=True)
        elif kind == "ptt_up":
            if self.ptt_active:
                print("[按住说话] 松手", flush=True)
                self.finish_ptt()
        elif kind == "toggle_mute":
            st.muted = not st.muted
            if st.muted:
                st.interrupt = True
                st.level = 0.0
            print(f"[静音] {'开' if st.muted else '关'}")
        elif kind == "toggle_auto":
            st.auto_listen = not st.auto_listen
            self.reset_vad()
            print(f"[免提] {'开' if st.auto_listen else '关'}")
        elif kind == "repeat":
            if st.reply_text:
                st.mode = "speaking"
                self.speak(st.reply_text)
                self.flush_input()
                st.mode = "idle"
        elif kind == "pet":
            line = random.choice(PET_LINES)
            st.mode = "happy"
            st.reply_text = line
            self.speak(line)
            self.flush_input()
            time.sleep(0.5)
            st.mode = "idle"
        elif kind == "reset":
            st.history = []
            st.user_text = ""
            st.reply_text = "好啦，之前聊的我都忘掉了。"
            st.mode = "happy"
            self.speak(st.reply_text)
            time.sleep(0.4)
            st.mode = "idle"
            print("[上下文] 已清空")

    # ---------------- 大脑 ----------------
    def brain(self):
        while self.running:
            try:
                self._brain_once()
            except Exception as exc:                  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.st.mode = "error"
                self.st.reply_text = f"出错了：{exc}"
                time.sleep(1.0)
                self.st.mode = "idle"

    def _brain_once(self):
        try:
            cmd = self.cmdq.get(timeout=0.02)
        except queue.Empty:
            cmd = None
        if cmd:
            self.handle(cmd)
            return

        items = self.take_audio()
        if not items:
            return

        if self.ptt_active:
            for samples, rms in items:
                self.ptt_buf.append(samples)
            lvl = max(rms for _, rms in items) * 6
            self.st.level = min(1.0, 0.5 * self.st.level + 0.5 * lvl)
            self.st.ptt_seconds = time.monotonic() - self.ptt_t0
            if self.st.ptt_seconds >= MAX_PTT_SECONDS:
                print("[按住说话] 到 20 秒上限，自动发送", flush=True)
                self.finish_ptt()
            return

        if self.st.mode in ("thinking", "speaking", "happy"):
            return                        # 这时候的声音是它自己在说话，不喂 VAD

        rms = max(r for _, r in items)
        self.st.level = min(1.0, 0.45 * self.st.level + 0.55 * min(1.0, rms * 8))

        if not self.st.auto_listen:
            return
        seg = None
        for samples, _ in items:
            s = self.feed_vad(samples)
            if s is not None:
                seg = s
        if seg is not None and self.st.mode == "idle":
            text = self.recognize(seg)
            print(f"  [免提识别] {text!r}")
            if text:
                self.reply_to(text)

    # ---------------- 界面 ----------------
    def ui(self):
        st = self.st
        lcd = self.lcd
        next_blink = time.monotonic() + 2.5
        blink_until = 0.0
        last_gaze = time.monotonic()
        press_t0 = 0.0
        last_sig = None
        first = True

        while self.running:
            frame_t0 = time.monotonic()
            now = frame_t0
            st.t = now

            # 触摸事件
            while True:
                try:
                    ev = self.events.get_nowait()
                except queue.Empty:
                    break
                if ev.kind == "down":
                    region = petui.hit(ev.x, ev.y)
                    st.press_region = region
                    press_t0 = now
                    st.press_seconds = 0.0
                    self._look_at(ev.x, ev.y)
                    last_gaze = now
                    if region == "btn_ptt":
                        self.cmdq.put(("ptt_down",))
                elif ev.kind == "move":
                    self._look_at(ev.x, ev.y)
                    last_gaze = now
                elif ev.kind == "up":
                    region = st.press_region
                    st.press_region = None
                    if region == "btn_ptt":
                        self.cmdq.put(("ptt_up",))
                    elif region == "btn_mute":
                        self.cmdq.put(("toggle_mute",))
                    elif region == "btn_repeat":
                        self.cmdq.put(("repeat",))
                    elif region == "auto":
                        self.cmdq.put(("toggle_auto",))
                    elif region == "bubble":
                        self.cmdq.put(("repeat",))
                    elif region == "face":
                        if ev.dur >= 0.9:
                            self.cmdq.put(("reset",))
                        else:
                            self.cmdq.put(("pet",))
                    st.press_seconds = 0.0

            if st.press_region:
                st.press_seconds = now - press_t0

            # 眨眼
            if now >= next_blink:
                blink_until = now + 0.12
                next_blink = now + random.uniform(2.8, 5.5)
            st.blink = now < blink_until

            # 空闲时眼睛自己乱看，别像死机
            if now - last_gaze > 4.0 and st.mode == "idle":
                st.look = (random.uniform(-0.6, 0.6), random.uniform(-0.4, 0.4))
                last_gaze = now + random.uniform(-2.0, 2.0)

            img = petui.render(st)
            # 上半屏（脸 + 声波）每帧都推；下半屏（气泡 + 按钮）只在内容变了才推，
            # 这样 40MHz 的 SPI 也能跑到 20fps 左右
            lcd.show(img, 0, BAND_SPLIT)
            sig = (st.mode, st.user_text, st.reply_text, st.muted,
                   st.press_region, st.auto_listen, int(st.ptt_seconds))
            if first or sig != last_sig:
                lcd.show(img, BAND_SPLIT, screen.H)
                last_sig = sig
                first = False
            self.frames += 1

            # 按实际耗时补睡，目标 20fps；追不上就不睡，别拖慢语音线程
            spent = time.monotonic() - frame_t0
            time.sleep(max(0.0, 1 / 20 - spent))

    def _look_at(self, x, y):
        dx = (x - petui.FACE_CX) / 70.0
        dy = (y - petui.FACE_CY) / 90.0
        n = max(1.0, math.hypot(dx, dy))
        self.st.look = (max(-1.0, min(1.0, dx / n)), max(-1.0, min(1.0, dy / n)))

    # ---------------- 启动 ----------------
    def run(self):
        self.open_hw()
        self.lcd.fill((11, 17, 26))
        boot = Image.new("RGB", (screen.W, screen.H), (11, 17, 26))
        d = ImageDraw.Draw(boot)
        petui.center_text(d, screen.W / 2, 150, "小派", petui.font(28), petui.TEXT)
        petui.center_text(d, screen.W / 2, 182, "正在加载语音模型……", petui.font(13), petui.MUTED)
        self.lcd.show(boot)

        if not self.args.demo:
            self.load_models()
            self.start_capture()
        else:
            self.st.reply_text = "演示模式：界面能点，但没有麦克风和耳朵。"

        if self.args.say:
            self.st.user_text = self.args.say
            self.st.reply_text = "……"
            threading.Thread(target=self.reply_to, args=(self.args.say,), daemon=True).start()

        threading.Thread(target=self.brain, daemon=True).start()
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
        try:
            if self.spk:
                self.spk.terminate()
        except Exception:                             # noqa: BLE001
            pass
        try:
            if self.capture:
                self.proc.terminate()
                self.proc.wait(timeout=2)
        except Exception:                             # noqa: BLE001
            pass
        try:
            self.poller.stop()
            self.irq.close()
            self.touch.cs.close()
            self.lcd.bl.off()
            self.lcd.spi.close()
        except Exception:                             # noqa: BLE001
            pass


# ------------------------------------------------------------------ 校准 / 自检
TARGETS = ((20, 20), (screen.W - 20, 20), (20, screen.H - 20), (screen.W - 20, screen.H - 20))


def _target_img(n):
    img = Image.new("RGB", (screen.W, screen.H), (11, 17, 26))
    d = ImageDraw.Draw(img)
    cx, cy = TARGETS[n - 1]
    d.line((cx - 16, cy, cx + 16, cy), fill=(255, 90, 90), width=2)
    d.line((cx, cy - 16, cx, cy + 16), fill=(255, 90, 90), width=2)
    d.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), outline=(255, 90, 90), width=2)
    petui.center_text(d, screen.W / 2, screen.H / 2, f"{n}/4", petui.font(24), petui.MUTED)
    petui.center_text(d, screen.W / 2, screen.H / 2 + 40, "点十字中心", petui.font(15), petui.TEXT)
    return img


def _info_img(title, lines):
    img = Image.new("RGB", (screen.W, screen.H), (11, 17, 26))
    d = ImageDraw.Draw(img)
    petui.center_text(d, screen.W / 2, 70, title, petui.font(24), petui.ACCENT)
    y = 118
    for line in lines:
        petui.center_text(d, screen.W / 2, y, line, petui.font(14), petui.TEXT)
        y += 28
    return img


def calibrate():
    lcd = screen.open_display()
    touch, irq = screen.open_touch()
    lcd.show(_info_img("触摸校准", ["依次点屏幕上的十字中心", "点完自动保存"]))
    time.sleep(1.2)
    pts = []
    for i in range(1, 5):
        lcd.show(_target_img(i))
        t0 = time.monotonic()
        while not irq.is_active:
            if time.monotonic() - t0 > 20:
                raise SystemExit("没检测到触摸：检查 T_IRQ(GPIO22) / T_CS(GPIO6) 接线")
            time.sleep(0.005)
        time.sleep(0.05)
        pts.append(touch.read_stable(0.25))
        while irq.is_active and time.monotonic() - t0 < 8:
            time.sleep(0.01)

    tl, tr, bl, br = pts
    if abs(tl[0] - tr[0]) >= abs(tl[1] - tr[1]):
        swap = False
        left, right = (tl[0] + bl[0]) / 2, (tr[0] + br[0]) / 2
        top, bottom = (tl[1] + tr[1]) / 2, (bl[1] + br[1]) / 2
    else:
        swap = True
        left, right = (tl[1] + bl[1]) / 2, (tr[1] + br[1]) / 2
        top, bottom = (tl[0] + tr[0]) / 2, (bl[0] + br[0]) / 2
    if abs(right - left) < 100 or abs(bottom - top) < 100:
        raise SystemExit("四个点太接近，校准失败，请点准十字重试")

    xfac = (screen.W - 40) / (right - left)
    yfac = (screen.H - 40) / (bottom - top)
    if abs(xfac) > 2 or abs(yfac) > 2:
        raise SystemExit("校准结果异常：轴可能点反了，请重试")
    screen.CAL.update({"swap": swap, "xfac": xfac, "xoff": 20 - xfac * left,
                       "yfac": yfac, "yoff": 20 - yfac * top})

    def project(rx, ry):
        if swap:
            rx, ry = ry, rx
        return xfac * rx + screen.CAL["xoff"], yfac * ry + screen.CAL["yoff"]

    worst = max(max(abs(project(rx, ry)[0] - tx), abs(project(rx, ry)[1] - ty))
                for (tx, ty), (rx, ry) in zip(TARGETS, pts))
    if worst > 25:
        raise SystemExit(f"偏差 {worst:.0f}px，请重新点准四个十字")
    screen.save_cal()
    print("已保存 touch_cal.json:", json.dumps(screen.CAL, ensure_ascii=False))
    lcd.show(_info_img("校准完成", ["参数已保存到", "touch_cal.json"]))
    time.sleep(1.5)


def check():
    lcd = screen.open_display()
    touch, irq = screen.open_touch()
    screen.load_cal()
    print("触摸自检：按住划动屏幕，Ctrl+C 退出")
    while True:
        active = irq.is_active
        x, y = touch.read_xy()
        sx, sy = screen.to_screen(x, y)
        img = Image.new("RGB", (screen.W, screen.H), (11, 17, 26))
        d = ImageDraw.Draw(img)
        petui.center_text(d, 120, 40, "触摸自检", petui.font(22), petui.ACCENT)
        petui.center_text(d, 120, 90, "按下" if active else "没按", petui.font(20),
                          (255, 110, 110) if active else petui.ACCENT)
        d.text((24, 130), f"raw = ({x}, {y})", font=petui.font(15), fill=petui.TEXT)
        d.text((24, 160), f"屏幕 = ({sx}, {sy})", font=petui.font(15), fill=petui.TEXT)
        d.line((sx - 12, sy, sx + 12, sy), fill=petui.WARM, width=2)
        d.line((sx, sy - 12, sx, sy + 12), fill=petui.WARM, width=2)
        lcd.show(img)
        print(f"irq={0 if active else 1} raw=({x},{y}) -> ({sx},{sy})")
        time.sleep(0.1)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="小派触屏对话")
    ap.add_argument("-d", "--device", default="plughw:3,0", help="麦克风设备")
    ap.add_argument("-o", "--out", default="plughw:2,0", help="喇叭设备")
    ap.add_argument("-v", "--voice", default="huihui", help="音色 huihui/kangkang/yaoyao")
    ap.add_argument("--rate", type=int, default=180, help="SAPI 语速")
    ap.add_argument("--speed", type=int, default=168, help="espeak 语速（兜底）")
    ap.add_argument("--espeak-voice", default="cmn+f3")
    ap.add_argument("--tts", choices=["sapi", "edge", "espeak"], default="sapi")
    ap.add_argument("-m", "--model", help="覆盖模型名")
    ap.add_argument("--auto", action="store_true", help="启动即开免提（VAD 自动听）")
    ap.add_argument("--mute", action="store_true", help="启动时静音")
    ap.add_argument("--no-hotwords", action="store_true")
    ap.add_argument("--demo", action="store_true", help="不接麦克风，只跑界面")
    ap.add_argument("--say", help="只测一轮对话（不占麦克风）")
    ap.add_argument("--calibrate", action="store_true", help="触摸校准")
    ap.add_argument("--check", action="store_true", help="触摸自检")
    ap.add_argument("--spi-hz", type=int, default=screen.SPI_HZ_DISPLAY,
                    help="屏幕 SPI 频率，默认 40MHz；设 62500000 更流畅（线材短的话）")
    args = ap.parse_args()

    if args.calibrate:
        calibrate()
        return
    if args.check:
        check()
        return

    screen.SPI_HZ_DISPLAY = args.spi_hz
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    PetApp(args).run()


if __name__ == "__main__":
    main()
