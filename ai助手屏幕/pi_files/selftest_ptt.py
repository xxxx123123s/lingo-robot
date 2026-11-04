#!/usr/bin/env python3
"""按住说话链路自测：不碰屏幕，用 TTS 合成的 16k 音频灌进音频队列，
验证 按下 -> 攒音频 -> 松手 -> 识别 -> 大模型 -> 朗读 这一整条。"""
import sys, time, urllib.parse, urllib.request, wave, io
import numpy as np

sys.path.insert(0, "/home/pi")
from argparse import Namespace
import pet


def fetch_16k(text):
    q = urllib.parse.urlencode({"text": text, "voice": "huihui", "rate": 180})
    raw = urllib.request.urlopen(f"http://192.168.137.1:8765/tts?{q}", timeout=15).read()
    w = wave.open(io.BytesIO(raw), "rb")
    sr, ch = w.getframerate(), w.getnchannels()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        n = int(len(pcm) * 16000 / sr)
        pcm = np.interp(np.linspace(0, len(pcm), n, endpoint=False),
                        np.arange(len(pcm)), pcm).astype(np.int16)
    print(f"  合成 {len(pcm)/16000:.2f}s @16k")
    return (pcm.astype(np.float32) / 32768.0)


args = Namespace(device="plughw:3,0", out="plughw:2,0", voice="huihui", rate=180,
                 speed=168, espeak_voice="cmn+f3", tts="sapi", model=None,
                 auto=False, mute=False, no_hotwords=False, demo=True, say=None)

app = pet.PetApp(args)
app.load_models()
import threading
threading.Thread(target=app.brain, daemon=True).start()


def feed(seg, chunk=1600):
    for i in range(0, len(seg), chunk):
        part = seg[i:i + chunk]
        rms = float(np.sqrt(np.mean(part ** 2)))
        app.audio.append((part, rms))
        time.sleep(0.1)          # 模拟实时录音节奏


app.cmdq.put(("ptt_down",))
time.sleep(0.4)
print("[测试] 开始灌音频")
feed(fetch_16k("你好小派，讲个笑话"))
time.sleep(0.3)
print("[测试] 松手")
app.cmdq.put(("ptt_up",))

for _ in range(300):
    time.sleep(0.1)
    if app.st.mode == "idle" and app.st.reply_text:
        break
print("=" * 40)
print("识别:", repr(app.st.user_text))
print("回答:", app.st.reply_text)
print("模式:", app.st.mode, " 静音:", app.st.muted)
print("OK" if app.st.user_text else "FAIL: 没识别出东西")
app.running = False
