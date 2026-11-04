#!/usr/bin/env python3
"""F4：文字/语音对话（先文字，再语音）。

文字模式（不需要麦克风）：
    python3 scripts/voice_chat.py --text

语音模式（需要 USB 麦克风/耳麦 + Vosk 中文模型）：
    python3 scripts/voice_chat.py --voice

配置：
    cp .env.example .env
    编辑 .env，填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

依赖：
    文字模式：Python 标准库即可
    语音模式：arecord、vosk、Vosk 中文模型
    TTS：edge-tts + mpg123（或 ffplay）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / ".env"
DEFAULT_VOSK = ROOT / "models" / "vosk-model-small-cn-0.22"
ARTIFACTS = ROOT / "artifacts"

DEFAULT_SYSTEM = (
    "你叫“小派”，是一只住在树莓派里的桌宠。"
    "你说话短、自然、有点好奇，回答控制在两句话以内。"
    "不要使用 Markdown、列表、代码块或表情符号。"
    "用户用中文时用中文回答。"
    "你现在只能看和说，还没有装履带，不能移动；"
    "如果用户让你走，就坦白说履带还没装好。"
    "不知道就说不知道，不要编造。"
)


def load_env(path: Path) -> None:
    """读取简单的 KEY=VALUE .env；不覆盖已有的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="树莓派桌宠：文字/语音对话")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--text", action="store_true", help="文字模式（默认）")
    mode.add_argument("--voice", action="store_true", help="语音模式：回车录音 → ASR → LLM → TTS")
    parser.add_argument("--no-tts", action="store_true", help="只输出文字，不播放语音")
    parser.add_argument("--voice-name", default="zh-CN-XiaoxiaoNeural", help="Edge TTS 音色")
    parser.add_argument("--seconds", type=float, default=4.0, help="每次录音秒数，默认 4 秒")
    parser.add_argument("--audio-device", default=None, help="arecord 录音设备，例如 plughw:1,0")
    parser.add_argument("--playback-device", default=None, help="mpg123 播放设备，例如 plughw:0,0")
    parser.add_argument("--asr-model", type=Path, default=DEFAULT_VOSK, help="Vosk 模型目录")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容 API 地址")
    parser.add_argument("--api-key", default=None, help="API Key；建议用 .env")
    parser.add_argument("--model", default=None, help="模型名，例如 deepseek-chat / qwen-turbo")
    parser.add_argument("--system-file", type=Path, default=None, help="自定义人格提示词文件")
    parser.add_argument("--max-turns", type=int, default=10, help="最多保留多少轮对话")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--check", action="store_true", help="只检查配置和依赖，不发起对话")
    return parser.parse_args()


def resolve_config(args: argparse.Namespace) -> tuple[str, str, str, str]:
    base_url = args.base_url or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1"
    api_key = args.api_key or os.environ.get("LLM_API_KEY") or ""
    model = args.model or os.environ.get("LLM_MODEL") or "deepseek-chat"
    system = DEFAULT_SYSTEM
    if args.system_file:
        system = args.system_file.read_text(encoding="utf-8").strip()
    return base_url, api_key, model, system


def chat_once(
    messages: list[dict],
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    max_tokens: int,
) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "petbot-voice-chat/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"API HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"API 超时（{timeout} 秒）") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"API 返回格式不对：{str(data)[:500]}") from exc
    return str(content).strip()


def find_player(playback_device: str | None = None) -> list[str] | None:
    if shutil.which("mpg123"):
        cmd = ["mpg123", "-q"]
        if playback_device:
            cmd.extend(["-a", playback_device])
        return cmd
    if shutil.which("ffplay"):
        if playback_device:
            print("[提示] ffplay 不能直接指定 ALSA 设备；建议安装 mpg123，或把默认声卡设为耳机。")
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    return None


def speak(text: str, voice: str, no_tts: bool, playback_device: str | None = None) -> None:
    """Edge TTS 生成 mp3，再用 mpg123/ffplay 播放；失败时尝试 espeak-ng。"""
    if no_tts or not text.strip():
        return

    edge_tts = shutil.which("edge-tts")
    player = find_player(playback_device)
    if not edge_tts or not player:
        print("[TTS 跳过] 需要 edge-tts 和 mpg123/ffplay。")
        print("  安装：pip install edge-tts; sudo apt install mpg123")
        return

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / "tts.mp3"
    try:
        result = subprocess.run(
            [edge_tts, "--voice", voice, "--text", text, "--write-media", str(out)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError((result.stderr or result.stdout or "edge-tts 失败").strip()[-300:])
        subprocess.run(
            [*player, str(out)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        print(f"[TTS 失败] {exc}")
        fallback = shutil.which("espeak-ng") or shutil.which("espeak")
        if fallback:
            try:
                subprocess.run(
                    [fallback, "-v", "cmn", "-s", "150", text],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except Exception as fallback_exc:
                print(f"[TTS 备用也失败] {fallback_exc}")


def record_wav(seconds: float, device: str | None) -> Path:
    arecord = shutil.which("arecord")
    if not arecord:
        raise RuntimeError("找不到 arecord。安装：sudo apt install alsa-utils")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / "asr.wav"
    cmd = [
        arecord,
        "-q",
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
        "-d",
        str(max(1, int(seconds))),
    ]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(out))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 10, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        raise RuntimeError(f"录音失败：{detail}")
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError("录音文件太小，检查麦克风设备。")
    return out


class VoskASR:
    def __init__(self, model_path: Path) -> None:
        try:
            import vosk
        except ImportError as exc:
            raise RuntimeError(
                "没有安装 vosk。\n"
                "  source .venv/bin/activate\n"
                "  pip install vosk"
            ) from exc

        if not model_path.exists():
            raise RuntimeError(
                f"找不到 Vosk 模型：{model_path}\n"
                "下载：\n"
                "  mkdir -p models && cd models\n"
                "  wget https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip\n"
                "  unzip vosk-model-small-cn-0.22.zip"
            )

        self.vosk = vosk
        self.model = vosk.Model(str(model_path))
        self.rate = 16000

    def transcribe(self, wav_path: Path) -> str:
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise RuntimeError("录音格式不对：需要 16-bit 单声道 WAV。")
            if wf.getframerate() != self.rate:
                raise RuntimeError(f"采样率不对：需要 {self.rate}，实际 {wf.getframerate()}。")

            recognizer = self.vosk.KaldiRecognizer(self.model, self.rate)
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                recognizer.AcceptWaveform(data)

        result = json.loads(recognizer.FinalResult())
        return str(result.get("text", "")).replace(" ", "").strip()


def trim_history(messages: list[dict], max_turns: int) -> list[dict]:
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-max_turns * 2 :]


def print_check(args: argparse.Namespace) -> int:
    base_url, api_key, model, _system = resolve_config(args)
    print("=== 语音对话依赖检查 ===")
    print(f"Python: {sys.version.split()[0]}")
    print(f"API 地址: {base_url}")
    print(f"模型: {model}")
    print(f"API Key: {'已设置' if api_key else '未设置'}")
    print(f"arecord: {shutil.which('arecord') or '未找到'}")
    print(f"edge-tts: {shutil.which('edge-tts') or '未找到'}")
    player = find_player(args.playback_device)
    print(f"播放器: {' '.join(player) if player else '未找到 mpg123/ffplay'}")
    print(f"录音设备: {args.audio_device or '系统默认'}")
    print(f"播放设备: {args.playback_device or '系统默认'}")
    print(f"Vosk 模型: {args.asr_model} ({'存在' if args.asr_model.exists() else '不存在'})")
    try:
        import vosk  # noqa: F401

        print("vosk: 已安装")
    except ImportError:
        print("vosk: 未安装")

    if not api_key:
        print("\n先配置 .env 里的 LLM_API_KEY。")
        return 2
    return 0


def run_chat(args: argparse.Namespace, voice: bool) -> int:
    base_url, api_key, model, system = resolve_config(args)
    if not api_key:
        print("缺少 API Key。", file=sys.stderr)
        print("  cp .env.example .env", file=sys.stderr)
        print("  # 编辑 .env，填入 LLM_API_KEY", file=sys.stderr)
        return 2

    asr = None
    if voice:
        try:
            asr = VoskASR(args.asr_model)
        except Exception as exc:
            print(f"语音识别初始化失败：{exc}", file=sys.stderr)
            return 2

    messages: list[dict] = [{"role": "system", "content": system}]
    print("小派已启动。输入 q 退出。")
    if voice:
        print("按回车录音；也可以直接输入文字发送。")
    else:
        print("直接输入文字；如果配置了 TTS，会同时朗读。")

    while True:
        try:
            if voice:
                raw = input("\n回车录音 / 输入文字 / q 退出: ").strip()
            else:
                raw = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if raw.lower() in {"q", "quit", "exit"}:
            print("再见。")
            break

        if raw:
            user_text = raw
        else:
            if not voice:
                continue
            print(f"录音 {args.seconds:.1f} 秒……")
            try:
                wav = record_wav(args.seconds, args.audio_device)
                print("识别中……")
                user_text = asr.transcribe(wav) if asr else ""
            except Exception as exc:
                print(f"录音/识别失败：{exc}")
                continue
            if not user_text:
                print("没听清，再说一次。")
                continue
            print(f"你说：{user_text}")

        messages.append({"role": "user", "content": user_text})
        messages = trim_history(messages, max(1, args.max_turns))

        print("思考中……")
        try:
            reply = chat_once(
                messages,
                base_url,
                api_key,
                model,
                args.timeout,
                args.max_tokens,
            )
        except Exception as exc:
            messages.pop()  # 失败时不把这条用户消息留在历史里
            print(f"对话失败：{exc}")
            continue

        messages.append({"role": "assistant", "content": reply})
        print(f"小派：{reply}")
        speak(reply, args.voice_name, args.no_tts, args.playback_device)

    return 0


def main() -> int:
    load_env(DEFAULT_ENV)
    args = parse_args()
    if args.check:
        return print_check(args)
    return run_chat(args, voice=args.voice and not args.text)


if __name__ == "__main__":
    sys.exit(main())
