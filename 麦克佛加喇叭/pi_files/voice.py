#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
树莓派离线语音闭环：麦克风 -> Vosk 识别 -> espeak-ng 朗读 -> 耳机

这台 Pi 是 32 位用户态，Vosk 解码 RTF≈2（比实时慢一倍）。
所以采用「整句识别」：靠音量判断你说完了，再一次性解码整段，
避免流式识别追不上实时输入而丢帧。

用法:
  python3 voice.py                 # 说一句 -> 识别 -> 用语音念回来
  python3 voice.py -q              # 只识别打印，不出声
  python3 voice.py -f xx.wav       # 用 16k wav 文件测试，不占用麦克风
  python3 voice.py --thresh 800    # 环境吵就调高
退出: Ctrl+C
"""
import argparse
import array
import json
import os
import select
import subprocess
import time
import wave

from vosk import Model, KaldiRecognizer, SetLogLevel

SetLogLevel(-1)

RATE = 16000
CHUNK = 3200      # 0.1 秒（bytes）
PRE_ROLL = 3      # 触发前多留 0.3 秒，防止吞字
MAX_SEC = 10      # 单句最长 10 秒


def rms16(buf):
    s = array.array("h", buf)
    if not s:
        return 0.0
    return (sum(x * x for x in s) / len(s)) ** 0.5


def speak(text, voice, speed, out_dev):
    wav = subprocess.run(
        ["espeak-ng", "-v", voice, "-s", str(speed), "--stdout", text],
        capture_output=True,
    ).stdout
    if wav:
        subprocess.run(["aplay", "-D", out_dev, "-q"], input=wav)


def drain(fd):
    while True:
        r, _, _ = select.select([fd], [], [], 0)
        if not r or not os.read(fd, 65536):
            return


def open_mic(device):
    cmd = ["arecord", "-D", device, "-f", "S16_LE", "-r", str(RATE),
           "-c", "1", "-t", "raw", "-q",
           "--period-size=3200", "--buffer-size=32000"]   # 2 秒缓冲，抗卡顿
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)


def mic_chunks(proc):
    fd = proc.stdout.fileno()
    while True:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            yield b""
            continue
        d = os.read(fd, CHUNK)
        if not d:
            return
        yield d


def wav_chunks(path):
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE:
            raise SystemExit(f"需要 16k wav，当前 {w.getframerate()}")
        while True:
            d = w.readframes(CHUNK // 2)
            if not d:
                return
            yield d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", default="/home/pi/vosk-model-small-cn-0.22")
    ap.add_argument("-d", "--device", default="plughw:3,0")
    ap.add_argument("-o", "--out", default="plughw:2,0")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-v", "--voice", default="cmn+f3")
    ap.add_argument("-s", "--speed", type=int, default=165)
    ap.add_argument("-f", "--file", help="从 16k wav 读取（测试用）")
    ap.add_argument("--thresh", type=float, default=600, help="语音判定音量阈值")
    args = ap.parse_args()

    print("加载模型...", end="", flush=True)
    t0 = time.time()
    model = Model(args.model)
    print(f" 完成 ({time.time() - t0:.1f}s)")

    def handle(text):
        text = text.strip()
        if not text:
            print("  (没听清)", flush=True)
            return
        print(f"[{time.strftime('%H:%M:%S')}] 识别: {text}", flush=True)
        if not args.quiet:
            speak(text, args.voice, args.speed, args.out)

    def decode(audio):
        t1 = time.time()
        rec = KaldiRecognizer(model, RATE)
        rec.SetWords(False)
        for i in range(0, len(audio), CHUNK):
            rec.AcceptWaveform(audio[i:i + CHUNK])
        text = json.loads(rec.FinalResult()).get("text", "")
        dur = len(audio) / (RATE * 2)
        print(f"  (音频 {dur:.1f}s / 解码 {time.time() - t1:.1f}s)", flush=True)
        return text

    proc = None
    try:
        if args.file:
            src = wav_chunks(args.file)
        else:
            proc = open_mic(args.device)
            src = mic_chunks(proc)
        print("就绪。说一句话，停顿后自动识别。Ctrl+C 退出", flush=True)

        noise = 300.0
        pre, seg = [], []
        speaking = False
        silence = 0.0

        for d in src:
            if not d:
                if proc is not None:
                    continue
                break
            v = rms16(d)
            th = max(args.thresh, noise * 3)

            if not speaking:
                noise = 0.95 * noise + 0.05 * v
                pre.append(d)
                if len(pre) > PRE_ROLL:
                    pre.pop(0)
                if v > th:
                    speaking = True
                    seg = list(pre)
                    pre = []
                    silence = 0.0
                    print("… 开始", flush=True)
            else:
                seg.append(d)
                silence = silence + 0.1 if v < th else 0.0
                if silence >= 0.8 or len(seg) * 0.1 >= MAX_SEC:
                    speaking = False
                    audio = b"".join(seg)
                    seg = []
                    handle(decode(audio))
                    if proc is not None:
                        drain(proc.stdout.fileno())

        if speaking:
            handle(decode(b"".join(seg)))

    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if proc:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
