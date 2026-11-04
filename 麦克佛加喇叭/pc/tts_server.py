#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 端语音合成服务（给树莓派用）

树莓派 GET /tts?text=你好&rate=180&voice=huihui  ->  返回 WAV
走 Windows 自带的 SAPI 中文音色（Huihui / Kangkang / Yaoyao），
离线、无需密钥、合成一句约 0.2 秒。

启动: python tts_server.py      默认监听 192.168.137.1:8765
"""
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyttsx3

HOST = os.environ.get("TTS_HOST", "192.168.137.1")
PORT = int(os.environ.get("TTS_PORT", "8765"))
LOCK = threading.Lock()          # SAPI 不是线程安全的


def find_voice(engine, keyword):
    for v in engine.getProperty("voices"):
        if keyword.lower() in v.id.lower() or keyword.lower() in v.name.lower():
            return v.id
    return None


MINI = {
    # 音色别名 -> SAPI 里的匹配关键字
    "huihui": "HUIHUI",
    "huihui_onecore": "Huihui",
    "kangkang": "Kangkang",
    "yaoyao": "Yaoyao",
    "zira": "ZIRA",
}


def synth(text, rate=180, voice="huihui"):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with LOCK:
            eng = pyttsx3.init()
            vid = find_voice(eng, MINI.get(voice, voice))
            if vid:
                eng.setProperty("voice", vid)
            eng.setProperty("rate", rate)
            eng.save_to_file(text, path)
            eng.runAndWait()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/ping":
            self._send(200, b"ok", "text/plain")
            return

        if u.path != "/tts":
            self._send(404, b"not found", "text/plain")
            return

        text = (q.get("text", [""])[0] or "").strip()
        if not text:
            self._send(400, b"missing text", "text/plain")
            return
        rate = int(q.get("rate", ["180"])[0])
        voice = q.get("voice", ["huihui"])[0]

        try:
            wav = synth(text, rate, voice)
        except Exception as e:
            print("合成失败:", e, flush=True)
            self._send(500, str(e).encode(), "text/plain")
            return
        self._send(200, wav, "audio/wav")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"语音合成服务已启动: http://{HOST}:{PORT}/tts?text=你好")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
