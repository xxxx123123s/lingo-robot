#!/usr/bin/env python3
"""视觉三件事的语音入口：识别意图 → 调 vision_service.py → 给出一句话。

为什么要走子进程而不是 import：cv2 一进主进程就是几十兆常驻内存，
而且它的解码线程会和识别、界面抢 GIL —— 语音识别延迟会当场变差。
子进程用完就退，主程序零负担。

为什么手势不停留在子进程里直接发蓝牙：HC-04 的 SPP 同一时刻只允许
一路连接，主程序已经占着链路了，第二个 socket.connect 必然失败。
所以手势进程只往 /tmp/vision.cmd 写一个 "stop"，主程序在自己的
0.5 秒心跳里读到再发车 —— 和风扇开关用 /tmp/fan.cmd 是同一个套路。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(os.environ.get("PETBOT_VISION", "/home/pi/scripts/vision_service.py"))
CMD_FILE = Path(os.environ.get("PETBOT_VISION_CMD", "/tmp/vision.cmd"))
PID_FILE = Path("/tmp/vision.pid")
LOG_FILE = Path("/tmp/vision.log")
PY = sys.executable or "python3"

# 顺序有意义：先判"关掉手势"，否则"关掉手势停车"会被"手势停车"抢走
WORDS = (
    ("gesture_off", ("关掉手势", "关闭手势", "别用手势", "手势关掉")),
    ("gesture_on", ("打开手势", "开启手势", "用手势停车", "手势停车打开")),
    ("who", ("看看我是谁", "我是谁", "认得我吗", "还认得我", "看我是谁",
             "看看是谁", "这是谁啊", "认得我是谁")),
    ("what", ("看看这是什么", "这是什么", "前面是什么", "帮我看看",
              "看看这个", "你看一眼", "这是什么东西")),
)


def parse(text: str):
    """把一句话翻译成视觉动作，认不出返回 None（交回原有流程）。"""
    t = (text or "").strip()
    if not t:
        return None
    for act, keys in WORDS:
        if any(k in t for k in keys):
            return {"act": act, "q": t}
    return None


# ------------------------------------------------------------------ 调子脚本


def _run(args, timeout=25):
    if not SCRIPT.exists():
        return -1, f"视觉脚本不在：{SCRIPT}"
    try:
        p = subprocess.run([PY, str(SCRIPT)] + args,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "超时"
    except Exception as exc:                       # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _tidy(s: str, limit: int = 80) -> str:
    """模型回复常带换行和 Markdown，念出来很怪，压成一句话。"""
    s = " ".join(s.replace("*", "").split())
    return s if len(s) <= limit else s[:limit] + "……"


def who_check(wait: float = 3.0, score: float = 0.5) -> dict:
    """识别一次，返回结构化结果给锁屏用。

    和 run_who 的差别：锁屏要按「认到了谁」决定放不放行，所以不能只要一句话。
    none（没人）和 unknown（有脸但不是主人）在语音里都只是"没认出来"，
    在门禁上却是两种完全不同的提示 —— 前者让人往前站，后者说明站错人了。

    检测阈值给 0.5：这颗 OV5647 在室内逆光下画面偏暗，0.6 会时不时漏检
    （实测同一张脸 0.62 检出、下一帧 0.6 就没了）。SFace 那边相似度 0.86，
    余量很大，把检测放宽一点反而更稳。
    """
    rc, out = _run(["who", "--timeout", str(wait), "--score", str(score)])
    print(f"[视觉] who rc={rc}\n{out}".rstrip())
    res = {"ok": False, "name": "", "score": -1.0, "why": "没出结果"}
    if rc == -1:
        res["why"] = "视觉服务没起来"
        return res
    for line in (out or "").splitlines():
        if not line.startswith("FACE "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        res["name"] = name
        res["score"] = float(parts[2]) if len(parts) > 2 else -1.0
        if name == "none":
            res["why"] = "画面里没人"
        elif name == "unknown":
            res["why"] = "这张脸我不认识"
        else:
            res["ok"] = True
            res["why"] = f"欢迎回来，{name}"
        return res

    # 没有 FACE 行 —— 说明脚本在出结果之前就失败了。最常见的原因是摄像头
    # 被别的进程占着（cam_view 预览页、或手势检测），picamera2 不允许两个进程
    # 同时开。这时候必须说清楚，否则用户只会看到"没认出你"，然后一直重试。
    low = (out or "").lower()
    if "busy" in low or "in use" in low or "占用" in low:
        res["why"] = "摄像头被别人占着"
    elif "no camera" in low or "找不到" in low or "failed to open" in low:
        res["why"] = "找不到摄像头"
    return res


def run_who() -> str:
    r = who_check()
    if not r["name"]:
        return "视觉没准备好" if r["why"] == "视觉服务没起来" else (
            "摄像头被人占着" if "占着" in r["why"] else "人脸那边没出结果")
    if r["name"] == "none":
        return "没看到有人啊"
    if r["name"] == "unknown":
        return "这张脸我不认识"
    return _tidy(f"这是{r['name']}")


def run_what(question: str) -> str:
    rc, out = _run(["snap", "--ask", question or "这是什么东西"], timeout=40)
    print(f"[视觉] what rc={rc}\n{out}".rstrip())
    if rc == -1:
        return "视觉没准备好"
    for line in (out or "").splitlines():
        if "模型（" in line and "）：" in line:
            answer = line.split("）：", 1)[1].strip()
            if "没配视觉模型" in answer:
                return "还没配视觉模型，先去 llm.json 加一段"
            return _tidy(answer)
    for line in (out or "").splitlines():
        if "没抓到画面" in line:
            return "摄像头没画面"
    return "这个问题我没看成"


# ------------------------------------------------------------------ 手势进程


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:                            # noqa: BLE001
        return False


def _gesture_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:                            # noqa: BLE001
        return 0


def gesture_on() -> str:
    pid = _gesture_pid()
    if pid and _alive(pid):
        return "手势已经是开着的"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "ab") as log:
        p = subprocess.Popen(
            [PY, str(SCRIPT), "gesture", "--cmd", str(CMD_FILE)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    PID_FILE.write_text(str(p.pid))
    time.sleep(0.5)
    if p.poll() is not None:
        return "手势没起来，检查摄像头"
    return "手势开了，张手就停，握拳就走四秒"


def gesture_off() -> str:
    pid = _gesture_pid()
    if not pid or not _alive(pid):
        return "手势本来就是关的"
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:                     # noqa: BLE001
        return f"关不掉：{exc}"
    try:
        PID_FILE.unlink()
    except Exception:                            # noqa: BLE001
        pass
    return "手势关了"


def pending_cmd() -> str:
    """手势进程写了什么指令。只读不清，等主程序真发出去了再清。

    从"只有 stop"升级成一整行指令，因为现在有两种动作：
        stop            张开手掌
        forward 2       握拳，走 2 秒
    多带一个参数是为了以后能改秒数而不用动主程序 —— 手势进程说了算，
    主程序照着做，两边不用同步改。
    """
    try:
        if not CMD_FILE.exists():
            return ""
        return CMD_FILE.read_text(encoding="utf-8").strip()
    except Exception:                            # noqa: BLE001
        return ""


def clear_cmd() -> None:
    try:
        CMD_FILE.unlink()
    except Exception:                            # noqa: BLE001
        pass


# 老名字，别的地方可能还写着
def pending_stop() -> bool:
    return pending_cmd() == "stop"


def clear_stop() -> None:
    clear_cmd()


# ------------------------------------------------------------------ 入口


def run(vi: dict) -> str:
    act = (vi or {}).get("act")
    if act == "who":
        return run_who()
    if act == "what":
        return run_what((vi or {}).get("q", ""))
    if act == "gesture_on":
        return gesture_on()
    if act == "gesture_off":
        return gesture_off()
    return "不知道要我看什么"


if __name__ == "__main__":                       # 手动试：python3 vision_intent.py 我是谁
    text = " ".join(sys.argv[1:]) or "我是谁"
    vi = parse(text)
    print(f"意图：{vi}")
    print(f"回答：{run(vi) if vi else '没认出来'}")
