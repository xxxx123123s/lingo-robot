#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
树莓派语音闭环：麦克风 -> silero VAD 切句 -> sherpa-onnx 识别 -> espeak 朗读 -> 耳机

特点：
  * silero VAD 只在检测到人声时才送去识别，环境噪声不会瞎猜字
  * 流式 zipformer int8 模型，RTF≈0.18，识别一句话不到 0.5 秒
  * 停顿 0.5 秒自动断句

用法:
  python3 talk.py                    # 说话 -> 识别 -> 语音复读
  python3 talk.py -q                 # 只识别打印，不出声
  python3 talk.py -f x.wav           # 用 16k wav 测试
  python3 talk.py -v cmn+m3 -s 190   # 换男声/语速
  python3 talk.py --no-vad           # 关掉 VAD，用端点检测（旧行为）
退出: Ctrl+C
"""
import argparse
import os
import select
import subprocess
import sys
import time
import wave

import numpy as np
import sherpa_onnx

sys.path.insert(0, "/home/pi")
import tts  # noqa: E402

TTS_ENGINE = "sapi"        # sapi / edge / espeak
EDGE_VOICE = "huihui"     # 见 tts.VOICES

RATE = 16000
CHUNK = 1600                      # 0.1 秒
DIR = "/home/pi"
MODEL = f"{DIR}/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
OFFLINE_MODEL = f"{DIR}/sherpa-onnx-paraformer-zh-2024-03-09"
VAD_MODEL = f"{DIR}/silero_vad.onnx"

# 识别后端。paraformer = 离线非流式（默认），streaming = 原来的 14M 流式模型。
#
# 为什么默认换成离线：这两条链路的音频都是「VAD 切好一整句再送去识别」，
# 根本不存在"边说边出字"的需求，用流式模型纯亏 —— 流式为了低延迟不看后文，
# 14M 参数又极小，同音字只能靠蒙。实测「手势」被听成「手术室」、「打开」
# 听成「爸开」。离线 paraformer 是一整句一起看，中文词错率低一个档次。
# 代价：一句 1~2 秒（流式 0.5~0.9 秒）。按住说话本来就等一下，值得。
# 想换回去：ASR_BACKEND=streaming python3 talk.py
ASR_BACKEND = os.environ.get("ASR_BACKEND", "paraformer")


def speech_seconds(seg, thresh=250.0):
    """估算一段音频里真正有声音的时长（秒）"""
    fr = 1600
    n = 0
    for i in range(0, max(0, len(seg) - fr), fr):
        if float(np.sqrt(np.mean(seg[i:i + fr] ** 2))) * 32768 > thresh:
            n += 1
    return n * 0.1


def make_rec(threads=2, hotwords_file=""):
    kw = {}
    method = "greedy_search"
    if hotwords_file and os.path.exists(hotwords_file):
        method = "modified_beam_search"      # 热词只在这个解码方式下生效
        kw = dict(hotwords_file=hotwords_file, hotwords_score=2.0)
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f"{MODEL}/tokens.txt",
        encoder=f"{MODEL}/encoder-epoch-99-avg-1.int8.onnx",
        decoder=f"{MODEL}/decoder-epoch-99-avg-1.int8.onnx",
        joiner=f"{MODEL}/joiner-epoch-99-avg-1.int8.onnx",
        num_threads=threads,
        sample_rate=RATE,
        feature_dim=80,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=1.0,
        rule3_min_utterance_length=20,
        decoding_method=method,
        **kw,
    )


class ASR:
    """统一识别接口，屏蔽在线/离线两种模型的 API 差异。

    在线（zipformer）：create_stream → accept_waveform → input_finished
                      → is_ready/decode_stream → get_result(s)
    离线（paraformer）：create_stream → accept_waveform → decode_stream
                      → s.result.text

    调用方只需要 recognize(samples) -> str。hub.py 和本文件的 main 都走这里，
    以后换模型只改这个类，不用满项目找 create_stream。
    """

    def __init__(self, backend="paraformer", threads=2, hotwords_file=""):
        self.backend = backend
        self.online = backend == "streaming"
        if self.online:
            self.rec = make_rec(threads, hotwords_file)
        else:
            self.rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=f"{OFFLINE_MODEL}/model.int8.onnx",
                tokens=f"{OFFLINE_MODEL}/tokens.txt",
                num_threads=threads,
                sample_rate=RATE,
                feature_dim=80,
                decoding_method="greedy_search",
            )

    def recognize(self, samples):
        s = self.rec.create_stream()
        s.accept_waveform(RATE, samples)
        if self.online:
            s.input_finished()
            while self.rec.is_ready(s):
                self.rec.decode_stream(s)
            return self.rec.get_result(s).strip()
        self.rec.decode_stream(s)
        return (s.result.text or "").strip()


def make_asr(threads=2, hotwords_file="", backend=None):
    """建识别器。热词只对在线模型有意义（离线 paraformer 用的是拼音纠正层，
    见 asr_fix.py），所以 backend 是 paraformer 时 hotwords 直接忽略。"""
    b = (backend or ASR_BACKEND or "paraformer").strip().lower()
    if b not in ("paraformer", "streaming"):
        b = "paraformer"
    return ASR(b, threads, hotwords_file if b == "streaming" else "")


def make_vad():
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = VAD_MODEL
    # threshold 0.5 → 0.55：日志里成片 "[太短 0.00s]" 说明环境噪声一直在触发 VAD，
    # 切出来一堆 0~0.1 秒的碎片，全被丢弃还白占 CPU。稍抬高一点，只认更像人声的。
    cfg.silero_vad.threshold = 0.55
    # min_silence 0.5 → 0.6：说话稍一停顿就被切断，一句话变两半，前半句单独送去
    # 识别必然缺字（"打开手势"变成"大开"就是这么来的）。0.6 秒给足换气时间。
    cfg.silero_vad.min_silence_duration = 0.6
    cfg.silero_vad.min_speech_duration = 0.3     # 0.25 → 0.3，滤掉短促噪声
    cfg.silero_vad.max_speech_duration = 20
    cfg.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
    return vad, cfg.silero_vad.window_size


def speak(text, voice, speed, out_dev):
    """voice 为 espeak 音色；默认走 edge 神经网络音色，失败回退 espeak"""
    tts.speak(text, engine=TTS_ENGINE, voice=EDGE_VOICE, out=out_dev,
              espeak_voice=voice, espeak_speed=speed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--device", default="plughw:3,0")
    ap.add_argument("-o", "--out", default="plughw:2,0")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-v", "--voice", default="cmn+f3")
    ap.add_argument("-s", "--speed", type=int, default=165)
    ap.add_argument("-f", "--file", help="从 16k wav 读取（测试用）")
    ap.add_argument("-t", "--threads", type=int, default=2)
    ap.add_argument("--no-vad", action="store_true", help="不用 VAD，改用端点检测")
    ap.add_argument("--hotwords", default=f"{DIR}/hotwords.txt", help="热词文件")
    ap.add_argument("--no-hotwords", action="store_true", help="不用热词")
    ap.add_argument("--streaming", action="store_true",
                    help="用原来的 14M 流式模型（快但同音易错）")
    ap.add_argument("--save-dir", default="", help="把每句录音存成 wav，便于排查")
    ap.add_argument("--tts", choices=["sapi", "edge", "espeak"], default="sapi")
    ap.add_argument("--edge-voice", dest="edge_voice", default="huihui",
                    help="音色: huihui/kangkang/yaoyao (SAPI) 或 xiaoxiao/yunxi (edge)")
    args = ap.parse_args()

    global TTS_ENGINE, EDGE_VOICE
    TTS_ENGINE = args.tts
    EDGE_VOICE = args.edge_voice

    print("加载模型...", end="", flush=True)
    t0 = time.time()
    hw = "" if args.no_hotwords else args.hotwords
    backend = "streaming" if args.streaming else None
    try:
        rec = make_asr(args.threads, hw, backend)
    except Exception as e:
        print(f"\n[警告] {backend or 'paraformer'} 加载失败({e})，回退流式小模型", end="")
        rec = make_asr(args.threads, "", "streaming")

    use_vad = not args.no_vad and os.path.exists(VAD_MODEL)
    if not rec.online and not use_vad:
        # 离线模型没有流式端点检测（is_endpoint 只存在于 OnlineRecognizer），
        # 硬走 --no-vad 那条路会 AttributeError，这里强制用 VAD 切句。
        use_vad = True
        print("\n[提示] 离线模型必须靠 VAD 切句，已忽略 --no-vad", end="")
    vad = window = None
    if use_vad:
        vad, window = make_vad()
    elif not args.no_vad:
        print(f"\n[警告] 找不到 {VAD_MODEL}，退回端点检测模式", end="")
    print(f" 完成 ({time.time() - t0:.1f}s)   模式: {'VAD门控' if use_vad else '端点检测'}"
          f"   识别: {'离线 paraformer' if not rec.online else '流式 zipformer-14M'}")

    def recognize(samples):
        return rec.recognize(samples)

    def handle(text, seg=None):
        if seg is not None and args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
            name = os.path.join(args.save_dir, time.strftime("%H%M%S") + ".wav")
            pcm = (np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes()
            with wave.open(name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(RATE)
                wf.writeframes(pcm)
        if not text:
            print(f"[{time.strftime('%H:%M:%S')}] (有声音但没识别出内容)", flush=True)
            return
        print(f"[{time.strftime('%H:%M:%S')}] 识别: {text}", flush=True)
        if not args.quiet:
            speak(text, args.voice, args.speed, args.out)

    # 数据源
    proc = None
    try:
        if args.file:
            w = wave.open(args.file, "rb")
            if w.getframerate() != RATE:
                raise SystemExit(f"需要 16k wav，当前 {w.getframerate()}")
            print(f"识别文件: {args.file}", flush=True)

            def chunks():
                while True:
                    d = w.readframes(CHUNK)
                    if not d:
                        return
                    yield np.frombuffer(d, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            cmd = ["arecord", "-D", args.device, "-f", "S16_LE", "-r", str(RATE),
                   "-c", "1", "-t", "raw", "-q",
                   "--period-size=1600", "--buffer-size=16000"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
            fd = proc.stdout.fileno()
            print("就绪，请说话（停顿约 0.5 秒断句）。Ctrl+C 退出", flush=True)

            def chunks():
                while True:
                    r, _, _ = select.select([fd], [], [], 0.5)
                    if not r:
                        yield None
                        continue
                    d = os.read(fd, CHUNK * 2)
                    if not d:
                        return
                    yield np.frombuffer(d, dtype=np.int16).astype(np.float32) / 32768.0

        if use_vad:
            carry = np.zeros(0, dtype=np.float32)   # 跨块余数，避免丢采样
            hist = np.zeros(0, dtype=np.float32)    # 预滚历史
            preroll = int(0.35 * RATE)
            buf = None                              # 当前这句话（含预滚）
            for samples in chunks():
                if samples is None:
                    continue
                carry = np.concatenate([carry, samples]) if len(carry) else samples
                while len(carry) >= window:
                    vad.accept_waveform(carry[:window])
                    carry = carry[window:]

                hist = np.concatenate([hist, samples])
                if len(hist) > preroll:
                    hist = hist[-preroll:]

                just_started = False
                if buf is None and vad.is_speech_detected():
                    buf = hist.copy()      # 把说话前 0.35 秒也带上，避免吞掉第一个字
                    just_started = True
                if buf is not None and not just_started:
                    buf = np.concatenate([buf, samples])

                if not vad.empty():
                    seg = buf if buf is not None else vad.front.samples
                    if speech_seconds(seg) >= 0.3:      # 太短的多半是杂音，丢掉
                        handle(recognize(seg), seg)
                    while not vad.empty():
                        vad.pop()
                    buf = None
                    hist = np.zeros(0, dtype=np.float32)

            if len(carry):
                vad.accept_waveform(np.pad(carry, (0, window - len(carry))))
            vad.flush()
            if buf is not None:
                handle(recognize(buf), buf)
            else:
                while not vad.empty():
                    s = vad.front.samples
                    handle(recognize(s), s)
                    vad.pop()
        else:
            stream = rec.create_stream()
            last = ""
            for samples in chunks():
                if samples is None:
                    continue
                stream.accept_waveform(RATE, samples)
                while rec.is_ready(stream):
                    rec.decode_stream(stream)
                p = rec.get_result(stream).strip()
                if p and p != last:
                    print(f"\r  {p}    ", end="", flush=True)
                    last = p
                if rec.is_endpoint(stream):
                    t = rec.get_result(stream).strip()
                    print(f"\r\033[K", end="", flush=True)
                    handle(t)
                    rec.reset(stream)
                    last = ""
                    if proc is not None:
                        while select.select([proc.stdout.fileno()], [], [], 0)[0]:
                            if not os.read(proc.stdout.fileno(), 65536):
                                break

    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if proc:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
