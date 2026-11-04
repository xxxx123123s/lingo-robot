#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测 edge-tts 流式播放：合成的同时就开始出声"""
import asyncio
import subprocess
import sys
import time

import edge_tts

TEXT = sys.argv[1] if len(sys.argv) > 1 else "你好，我是小派，住在树莓派里的小桌宠，很高兴认识你。"
OUT = "plughw:2,0"


async def run():
    t0 = time.time()
    proc = subprocess.Popen(["mpg123", "-q", "-a", OUT, "-"],
                            stdin=subprocess.PIPE)
    first = None
    n = 0
    async for chunk in edge_tts.Communicate(TEXT, "zh-CN-XiaoxiaoNeural").stream():
        if chunk["type"] != "audio":
            continue
        if first is None:
            first = time.time() - t0
        proc.stdin.write(chunk["data"])
        proc.stdin.flush()
        n += len(chunk["data"])
    proc.stdin.close()
    proc.wait()
    print(f"首包 {first:.2f}s  共 {n/1024:.1f}KB  总耗时 {time.time()-t0:.2f}s  rc={proc.returncode}")


asyncio.run(run())
