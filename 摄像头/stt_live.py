#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""树莓派实时语音转文字（Vosk 中文）
用法: arecord -D plughw:3,0 -f S16_LE -r 16000 -c 1 -t raw | python3 stt_live.py
输出: 每识别完一句，打印 [秒数] 文字 到 stdout
"""
import sys
import time
import array
from vosk import Model, KaldiRecognizer

MODEL = "/home/pi/vosk-model-small-cn-0.22"
RATE = 16000
GAIN = 12          # 数字增益，补偿低灵敏度麦克风
CHUNK = 4000       # 0.125 秒 @16k 单声道 16bit

model = Model(MODEL)
rec = KaldiRecognizer(model, RATE)
rec.SetWords(False)

start = time.time()
stream = sys.stdin.buffer

while True:
    data = stream.read(CHUNK)
    if not data:
        break
    # 数字增益（防削波）
    samples = array.array("h", data)
    for i, s in enumerate(samples):
        g = s * GAIN
        samples[i] = 35 if g > 32767 else (-35 if g < -32767 else g)
    if rec.AcceptWaveform(samples.tobytes()):
        text = rec.Result()
        # 提取 "text" 字段内容，避免引入 json
        if '"text" : ""' not in text:
            t = time.time() - start
            line = text.split('"text" : "')[1].split('"')[0] if '"text" : "' in text else ""
            if line.strip():
                print(f"[{t:05.1f}s] {line}", flush=True)

final = rec.FinalResult()
if '"text" : "' in final:
    line = final.split('"text" : "')[1].split('"')[0]
    if line.strip():
        print(f"[final] {line}", flush=True)
