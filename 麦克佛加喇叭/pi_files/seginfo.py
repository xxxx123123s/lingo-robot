#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析录下来的句子：时长、能量包络、整段识别 vs 去掉开头若干毫秒后的识别"""
import sys, wave
import numpy as np
import sherpa_onnx

M = "/home/pi/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"

rec = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=f"{M}/tokens.txt",
    encoder=f"{M}/encoder-epoch-99-avg-1.int8.onnx",
    decoder=f"{M}/decoder-epoch-99-avg-1.int8.onnx",
    joiner=f"{M}/joiner-epoch-99-avg-1.int8.onnx",
    num_threads=2, sample_rate=16000, feature_dim=80,
    decoding_method="greedy_search",
)


def asr(samples):
    s = rec.create_stream()
    s.accept_waveform(16000, samples)
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    return rec.get_result(s)


for path in sys.argv[1:]:
    w = wave.open(path, "rb")
    d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    w.close()
    a = d.astype(np.float32) / 32768.0
    dur = len(a) / 16000
    print(f"\n=== {path}  {dur:.2f}s  {len(a)} 采样 ===")

    # 每 100ms 的 RMS，看看有几段人声
    fr = 1600
    rms = [float(np.sqrt(np.mean(a[i:i+fr]**2))) * 32768 for i in range(0, len(a) - fr, fr)]
    bars = "".join("#" if r > 300 else ("+" if r > 100 else ".") for r in rms)
    print("包络:", bars)
    print(f"峰值 {np.abs(d).max()}  前0.4s峰值 {np.abs(d[:6400]).max()}")

    print("整段     :", asr(a))
    print("去掉0.2s :", asr(a[3200:]))
    print("去掉0.35s:", asr(a[5600:]))
