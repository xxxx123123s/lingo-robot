#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测 sherpa-onnx 中文流式模型在这台机器上的速度/效果。
用法: python3 test_sherpa.py xxx.wav   (16k 单声道)
"""
import sys, time, wave
import numpy as np
import sherpa_onnx

M = "/home/pi/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
wav_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/t1_16k.wav"

t0 = time.time()
rec = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=f"{M}/tokens.txt",
    encoder=f"{M}/encoder-epoch-99-avg-1.int8.onnx",
    decoder=f"{M}/decoder-epoch-99-avg-1.int8.onnx",
    joiner=f"{M}/joiner-epoch-99-avg-1.int8.onnx",
    num_threads=2,
    sample_rate=16000,
    feature_dim=80,
    decoding_method="greedy_search",
)
print(f"模型加载 {time.time()-t0:.2f}s")

w = wave.open(wav_path, "rb")
total = w.getnframes() / w.getframerate()
s = rec.create_stream()

t1 = time.time()
first = None
while True:
    data = w.readframes(1600)          # 0.1 秒
    if not data:
        break
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    s.accept_waveform(16000, samples)
    while rec.is_ready(s):
        rec.decode_stream(s)
    if first is None and rec.get_result(s).strip():
        first = time.time() - t1
s.input_finished()
while rec.is_ready(s):
    rec.decode_stream(s)
el = time.time() - t1
w.close()

print(f"音频 {total:.1f}s  解码 {el:.2f}s  RTF={el/total:.2f}  首字延迟 {first if first else -1:.1f}s")
print("识别:", rec.get_result(s))
