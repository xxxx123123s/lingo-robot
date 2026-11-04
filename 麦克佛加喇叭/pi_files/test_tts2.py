#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比 edge-tts 两种取音频方式的稳定性"""
import asyncio
import os
import time

import edge_tts

T = "对我做了介绍那么我想说的是大家如果对我的研究感兴趣呢"
V = "zh-CN-XiaoxiaoNeural"


def save_once(path):
    async def run():
        await edge_tts.Communicate(T, V).save(path)
    try:
        asyncio.run(run())
        return os.path.getsize(path) > 1000
    except Exception as e:
        return f"{type(e).__name__}"


def stream_close(path):
    async def run():
        n = 0
        gen = edge_tts.Communicate(T, V).stream()
        try:
            async for ch in gen:
                if ch["type"] == "audio":
                    n += len(ch["data"])
        finally:
            await gen.aclose()
        return n
    try:
        return asyncio.run(run()) > 1000
    except Exception as e:
        return f"{type(e).__name__}"


print("--- save() ---")
for i in range(4):
    print(f"  {i+1}: {save_once('/tmp/s.mp3')}")
    time.sleep(1)

print("--- stream + aclose() ---")
for i in range(4):
    print(f"  {i+1}: {stream_close('/tmp/s2.mp3')}")
    time.sleep(1)
