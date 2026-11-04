#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任意 wav 转 16k 单声道 16bit，给 Vosk 用。用法: python3 wav16k.py 输入.wav 输出.wav"""
import sys, wave, array

src, dst = sys.argv[1], sys.argv[2]
w = wave.open(src, "rb")
ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
data = w.readframes(n)
w.close()
if sw != 2:
    sys.exit("只支持 16bit wav")

s = array.array("h", data)
if ch == 2:
    s = array.array("h", [(s[i] + s[i + 1]) // 2 for i in range(0, len(s) - 1, 2)])

if sr != 16000:
    ratio = sr / 16000.0
    out = array.array("h")
    i = 0.0
    while i < len(s) - 1:
        i0 = int(i)
        f = i - i0
        out.append(int(s[i0] * (1 - f) + s[i0 + 1] * f))
        i += ratio
    s = out

o = wave.open(dst, "wb")
o.setnchannels(1)
o.setsampwidth(2)
o.setframerate(16000)
o.writeframes(s.tobytes())
o.close()
print(f"ok -> {dst}  {len(s)} 采样点 ({len(s)/16000:.1f}s)")
