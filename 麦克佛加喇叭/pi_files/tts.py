#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一语音合成模块，三级回退：

  sapi  : 由 PC(192.168.137.1) 上的 Windows SAPI 中文音色合成，走局域网取 WAV。
          离线、无需密钥、合成一句约 0.2 秒 —— 默认方案。
  edge  : 微软神经网络语音，音色最好，但国内网络实测成功率仅 2~5/10。
  espeak: 本机离线兜底，机械音、一字一顿。

用法:
  from tts import speak
  speak("你好", engine="sapi", voice="huihui")
"""
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

SAPI_URL = os.environ.get("TTS_SERVER", "http://192.168.137.1:8765")

VOICES = {
    "huihui":   "huihui",     # SAPI 女声（默认）
    "kangkang": "kangkang",   # SAPI 男声
    "yaoyao":   "yaoyao",     # SAPI 女声（更新）
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # edge 女声
    "xiaoyi":   "zh-CN-XiaoyiNeural",
    "yunxi":    "zh-CN-YunxiNeural",
    "yunjian":  "zh-CN-YunjianNeural",
    "yunyang":  "zh-CN-YunyangNeural",
}


def clean(text):
    text = re.sub(r"[*#`_>\[\]{}()~|]", " ", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------- PC 端 SAPI ----------------
def _sapi_wav(text, voice, rate, timeout=15):
    q = urllib.parse.urlencode({"text": text, "voice": voice, "rate": rate})
    with urllib.request.urlopen(f"{SAPI_URL}/tts?{q}", timeout=timeout) as r:
        return r.read()


def _sapi_speak(text, out, voice="huihui", rate=180):
    try:
        wav = _sapi_wav(text, voice, rate)
    except Exception as e:
        print(f"  [SAPI 服务不可用: {e}]")
        return False
    if len(wav) < 1000:
        return False
    path = os.path.join(tempfile.gettempdir(), "tts_sapi.wav")
    with open(path, "wb") as f:
        f.write(wav)
    return subprocess.run(["aplay", "-D", out, "-q", path]).returncode == 0


# ---------------- edge 在线 ----------------
def _edge_stream(text, voice, out):
    import asyncio

    import edge_tts

    async def run():
        proc = subprocess.Popen(["mpg123", "-q", "-a", out, "-"],
                                stdin=subprocess.PIPE)
        got = False
        try:
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk["type"] != "audio":
                    continue
                got = True
                proc.stdin.write(chunk["data"])
                proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
        return got and proc.returncode == 0

    return asyncio.run(run())


# ---------------- espeak 兜底 ----------------
def _espeak(text, espeak_voice, speed, out):
    wav = subprocess.run(
        ["espeak-ng", "-v", espeak_voice, "-s", str(speed), "--stdout", text],
        capture_output=True).stdout
    if wav:
        subprocess.run(["aplay", "-D", out, "-q"], input=wav)


def speak(text, engine="sapi", voice="huihui", rate=180, out="plughw:2,0",
          espeak_voice="cmn+f3", espeak_speed=168):
    """把 text 念出来，按 sapi -> edge -> espeak 顺序回退。"""
    text = clean(text)
    if not text:
        return
    voice = VOICES.get(voice, voice)

    if engine == "sapi":
        if _sapi_speak(text, out, voice, rate):
            return

    if engine in ("sapi", "edge"):
        v = voice if voice.startswith("zh-") else "zh-CN-XiaoxiaoNeural"
        for i in range(2):
            try:
                if _edge_stream(text, v, out):
                    return
            except Exception as e:
                last = e
            else:
                last = "没拿到音频"
            if i == 0:
                time.sleep(0.3)
        print(f"  [edge-tts 失败: {last}，改用 espeak]")

    _espeak(text, espeak_voice, espeak_speed, out)


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "你好，我是小派，很高兴认识你。"
    eng = sys.argv[2] if len(sys.argv) > 2 else "sapi"
    t0 = time.time()
    speak(t, engine=eng)
    print(f"完成 ({time.time() - t0:.2f}s)")
