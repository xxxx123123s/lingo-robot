#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录音并统计电平，判断麦克风是否正常。
用法: python3 miclevel.py [秒数]  默认 3 秒
"""
import sys, os, subprocess, array, time

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
WAV = "/tmp/_lvl.wav"
subprocess.run(["arecord", "-D", "plughw:3,0", "-f", "S16_LE", "-r", "16000",
                "-c", "1", "-d", str(int(round(DUR))), "-q", WAV], check=True)

import wave
w = wave.open(WAV, "rb")
n = w.getnframes()
data = w.readframes(n)
w.close()
s = array.array("h", data)
if not s:
    print("no data"); sys.exit(1)
peak = max(max(s), -min(s))
rms = (sum(x * x for x in s) / len(s)) ** 0.5
# 每秒峰值
per_sec = []
step = 16000
for i in range(0, len(s), step):
    seg = s[i:i + step]
    per_sec.append(int(max(max(seg), -min(seg))))
dbfs = lambda v: 20 * (v / 32768.0 and __import__("math").log10(v / 32768.0)) if v > 0 else -99
print(f"时长 {n/16000:.1f}s  采样点 {len(s)}")
print(f"峰值 {peak} ({dbfs(peak):.1f} dBFS)  RMS {rms:.1f}")
print("每秒峰值:", per_sec)
if peak < 100:
    print(">>> 几乎静音：麦克风可能被静音或选错设备")
elif peak > 32000:
    print(">>> 接近削波：建议调小麦克风增益")
else:
    print(">>> 麦克风工作正常")
