#!/usr/bin/env python3
"""视觉三件事的统一入口：手势停车 / 人脸认主人 / 拍照问模型。

本脚本不新造摄像头和识别逻辑，只把已有的 face_detect.py（YuNet 检测 +
CSI/USB 双路摄像头）和 face_db.py（SFace 特征 + 本地人脸库）组织成三条命令，
方便插上摄像头后一次验证，也方便被主程序调用。

用法（树莓派上，在 scripts/ 的同级目录执行）：

    # 0. 先体检：能不能出帧、模型在不在
    python3 scripts/vision_service.py check

    # 1. 人脸认主人（先录入：python3 scripts/face_db.py enroll --name 张三）
    python3 scripts/vision_service.py who

    # 2. 拍照问模型（需要先把 vision 段写进 llm.json，见下）
    python3 scripts/vision_service.py snap --ask "这是什么东西"

    # 3. 手势（张开手掌 → 停车；握拳 → 前进 4 秒），--mac 直接发蓝牙
    python3 scripts/vision_service.py gesture --mac AA:BB:CC:DD:EE:FF

    # 3b. 同上，但同时在电脑浏览器里看画面（判定参数叠在画面上，调阈值用）
    #     打开 http://<树莓派IP>:8099/   —— 绿框是判定用的那只手，
    #     右下小图是肤色掩膜，左上三行是 ratio / compact / 指缝条数
    python3 scripts/vision_service.py gesture --cmd /tmp/vision.cmd --stream 8099

llm.json 里加一段（base_url 换成你用的厂商，DeepSeek 目前没有视觉接口）：

    "vision": {
      "api_key": "sk-xxx",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "model": "qwen-vl-max",
      "max_tokens": 120
    }

设计取舍：
  * 一律只输出"结论"，不输出控制量。手势只发 [stop] 或 [go]/[stop]，
    人脸只报名字，识别结果交给上层决定做什么 —— 和"闭环留在车端"是同一个原则。
  * 抓图取连拍里最清晰的一帧。平衡车为了直立一直在微调，车体高频抖动，
    单帧抓图很容易糊掉，连拍选锐度最高的一帧几乎不花额外代价。
  * 手势判定用 YCrCb 肤色面积 + 凸包实心度，而不是"比数字"。
    纯 OpenCV 做静态手势分类很不稳，而"手举到镜头前"这个动作信号量极大
    （画面被大面积肤色占满），再拿实心度把"张开"和"握拳"分开就够了 ——
    张开的手有指缝填不满凸包，握拳是实心的，两者差 0.2 以上，很好分。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 OpenCV 或 numpy。\n"
        "树莓派推荐：sudo apt install -y python3-opencv python3-numpy\n"
        "或：pip install -r requirements-face.txt"
    ) from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from face_detect import FaceDetector, ensure_yunet, open_camera  # noqa: E402
from face_db import FaceDB, SFace, ensure_sface  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
DEFAULT_LLM = Path(os.environ.get("PETBOT_LLM", "/home/pi/llm.json"))

# 肤色经验区间（YCrCb），室内灯光下标定过；换环境只调这几个数
SKIN_CR = (133, 173)
SKIN_CB = (77, 127)


def log(msg: str) -> None:
    print(msg, flush=True)


# ==================== 电脑预览（MJPEG）====================
# 手势调阈值最痛苦的地方是"看不见摄像头眼里的自己"：明明举了手它不认，
# 只能靠日志里的 sol/ratio 猜。把画面（连同判定用到的三个数和掩膜）推到
# 浏览器上，一眼就能看出是手太小、光线偏色，还是真被判成握拳了。
#
# 为什么用 MJPEG 而不是 WebRTC/HLS：浏览器原生支持 multipart/x-mixed-replace，
# 一行 <img> 就完事，不用装任何东西，也不用在电脑上跑客户端。

STREAM_W = 480          # 推流长边。640x480 全尺寸编码又慢又糊（网络也吃），
                        # 480 宽在浏览器里放大到 960 看完全够，编码量只有 56%
JPEG_Q = 65             # 预览画质。70→65 几乎看不出差别，体积再小一成
_ENC_MIN_GAP = 0.0      # 编码线程最小间隔（秒）。0 = 跟着检测帧率走


class _Snap:
    """一帧的判定快照。给编码线程用，避免它去读正在被改写的 detector。"""

    __slots__ = ("mask", "contour", "ratio", "min_ratio", "compact", "defects",
                 "fist_compact", "fist_defects", "act", "fired", "fps")

    def __init__(self, det, act, fired, fps=0.0):
        # mask / contour 每次 update 都是新对象，直接拿引用安全（不会被原地改）
        self.mask = det.mask
        self.contour = det.contour
        self.ratio = det.ratio
        self.min_ratio = det.min_ratio
        self.compact = det.compact
        self.defects = det.defects
        self.fist_compact = det.fist_compact
        self.fist_defects = det.fist_defects
        self.act = act
        self.fired = fired
        self.fps = fps


class _Live:
    """最新一帧 + 观众数。没人看就不编码，编码放独立线程。

    为什么非要把编码挪出去：原来检测、标注、JPEG 编码全在一个线程里，
    Pi 4 上单核跑满（实测 CPU 99%），一帧 80ms，推流只有 12fps —— 手上
    的动作看着一顿一顿。而 Pi 4 有 4 个核，cv2 的编码会释放 GIL，所以
    分到第二个线程是真的并行，不是假的。挪完主循环只剩检测，帧率回到
    检测本身的极限。
    """

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.pending = None          # 待编码的最新帧（只保留最后一帧，旧的直接丢）
        self.jpeg = b""
        self.seq = 0
        self.clients = 0
        self.dropped = 0             # 编码来不及而丢掉的帧数，用来判断是不是编码瓶颈
        self._enc_th = None

    def start_encoder(self) -> None:
        if self._enc_th is not None:
            return
        self._enc_th = threading.Thread(target=self._encode_loop,
                                        name="mjpeg-enc", daemon=True)
        self._enc_th.start()

    def offer(self, frame, snap: "_Snap") -> None:
        """主循环每帧调一次，立刻返回。没人看时什么也不做。"""
        self.start_encoder()
        with self.cond:
            if self.clients <= 0:
                self.pending = None
                return
            if self.pending is not None:
                self.dropped += 1        # 上一帧还没编完，这帧作废（永远只发最新）
            self.pending = (frame, snap)
            self.cond.notify()

    def _encode_loop(self) -> None:
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait(0.5)
                frame, snap = self.pending
                self.pending = None
            try:
                jpg = _encode_snapshot(frame, snap)
            except Exception as exc:                      # noqa: BLE001
                log(f"预览编码失败：{type(exc).__name__}: {exc}")
                continue
            if jpg:
                with self.cond:
                    self.jpeg = jpg
                    self.seq += 1
                    self.cond.notify_all()

    def wait(self, last: int, timeout: float = 2.0):
        """等下一帧。用条件变量而不是 sleep 轮询 —— 轮询最多会多出 30ms
        的抖动，帧间隔一不均匀，观感上就是"卡"。"""
        with self.cond:
            self.cond.wait_for(
                lambda: self.seq != last and bool(self.jpeg), timeout)
            if self.seq != last and self.jpeg:
                return self.jpeg, self.seq
            return None, last


LIVE = _Live()


def _encode_snapshot(frame, snap: "_Snap"):
    """缩放 → 标注 → JPEG。全部在编码线程里跑。"""
    h, w = frame.shape[:2]
    if w > STREAM_W:
        nw = STREAM_W
        nh = max(1, int(round(h * STREAM_W / float(w))))
        frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    img = _annotate(frame, snap)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
    return buf.tobytes() if ok else None


class _FaceWorker:
    """人脸框检测的独立线程。

    为什么非拆不可：实测 YuNet 跑一帧 640x480 要 **245ms**，而它只需要
    0.7 秒检一次。放在主循环里同步做，就是每 0.7 秒把整个循环卡住 245ms
    —— 观感上是"隔一会儿顿一下"，比帧率低本身更难受。

    拆出去之后主循环永远不等它，拿到的是最近一次的脸框（最多晚一秒，
    而人站着不动，一秒前的框完全够用）。顺便把检脸分辨率降到 320 宽，
    耗时降到约 1/4，另一个核基本无感。
    """

    SCALE_W = 320          # 检脸用 320 宽足够，坐标最后再乘回去

    def __init__(self, model, interval: float = 0.7) -> None:
        self.model = model
        self.interval = interval
        self.faces: list = []
        self.err = None
        self.calls = 0
        self._frame = None
        self._t_take = 0.0
        self._det = None
        self._dims = None
        self._cond = threading.Condition()
        threading.Thread(target=self._loop, name="face-det", daemon=True).start()

    def offer(self, frame) -> None:
        """主循环每帧调一次，绝大多数时候立即返回。"""
        if time.time() - self._t_take < self.interval:
            return
        with self._cond:
            if self._frame is not None:        # 上一帧还没取走，不排队
                return
            self._frame = frame
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._frame is None:
                    self._cond.wait(0.5)
                frame = self._frame
                self._frame = None
            self._t_take = time.time()
            try:
                self._detect(frame)
            except Exception as exc:                     # noqa: BLE001
                # 模型缺失不该让手势整个停摆，退化成"不排除脸"
                self.err = exc
            time.sleep(self.interval)

    def _detect(self, frame) -> None:
        h, w = frame.shape[:2]
        if w > self.SCALE_W:
            small = cv2.resize(
                frame, (self.SCALE_W, max(1, int(round(h * self.SCALE_W / w)))),
                interpolation=cv2.INTER_AREA)
        else:
            small = frame
        sh, sw = small.shape[:2]
        if self._det is None or self._dims != (sw, sh):
            self._det = FaceDetector(self.model, sw, sh, 0.5)
            self._dims = (sw, sh)
        k = w / float(sw)
        out = []
        for f in self._det.detect(small):
            v = f.tolist()
            for i in range(4):               # x, y, w, h 换算回原图坐标
                v[i] = v[i] * k
            out.append(v)
        self.faces = out                     # 原子替换：主循环要么读旧的、要么读新的
        self.calls += 1
        self.err = None

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>手势预览</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px/1.6 system-ui,sans-serif}
 img{width:100%;max-width:960px;display:block;margin:0 auto}
 p{text-align:center;margin:8px 0 16px}
 b{color:#4ade80}
</style></head>
<body>
<img src="/stream" alt="等待画面">
<p><b>握拳</b> = 前进 4 秒 &nbsp;·&nbsp; <b>张开手掌</b> = 停车 &nbsp;·&nbsp;
   绿框 = 判定用的手，右下小图 = 肤色掩膜</p>
</body></html>
""".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):          # 每帧一个请求，别刷日志
        pass

    def do_GET(self):                      # noqa: N802
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            # ★关掉 Nagle：21KB 的 JPEG 被 TCP 拆成多段时，Nagle 会攒着不发，
            # 等上一个包 ACK 才走 —— 局域网里也能攒出几十毫秒，一帧一卡。
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP,
                                           socket.TCP_NODELAY, 1)
            except Exception:                             # noqa: BLE001
                pass
            with LIVE.cond:
                LIVE.clients += 1
            seq = -1
            try:
                while True:
                    jpg, seq = LIVE.wait(seq)
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                           # 浏览器关页面，正常
            finally:
                with LIVE.cond:
                    LIVE.clients -= 1
                    LIVE.cond.notify_all()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)


def lan_ip() -> str:
    """本机在局域网里的地址。不真的发包，只是让内核选一条出口。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:                      # noqa: BLE001
        return "127.0.0.1"


def start_stream(port: int) -> bool:
    if port <= 0:
        return False
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        log(f"预览端口 {port} 打不开（{exc}），画面在命令行里看不到，但不影响手势")
        return False
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"电脑预览：http://{lan_ip()}:{port}/   浏览器直接打开就能看画面")
    return True


def _annotate(frame, s: "_Snap"):
    """把判定结果画在画面上。只用 ASCII —— cv2.putText 画不了中文。

    s 是 _Snap 快照，不是活的 detector：编码在另一个线程跑，读活的
    detector 会读到正在被改写的中间状态（数字和画面对不上帧）。
    """
    img = frame.copy()
    h, w = img.shape[:2]
    k = w / 640.0                    # 画到 480 宽时字号等比缩小，不然字挤成一团
    fs = lambda v: max(0.32, v * k)  # noqa: E731

    if s.mask is not None:
        mh, mw = s.mask.shape[:2]
        sc = w / float(max(mw, 1))
        if s.contour is not None:
            c = (s.contour.astype(np.float32) * sc).astype(np.int32)
            cv2.drawContours(img, [c], -1, (0, 210, 0), 2)
        tw, th = w // 4, h // 4
        thumb = cv2.resize(s.mask, (tw, th))
        img[h - th - 8:h - 8, w - tw - 8:w - 8] = cv2.cvtColor(
            thumb, cv2.COLOR_GRAY2BGR)

    if s.act == "forward":
        head, col = "FIST -> FORWARD", (0, 210, 0)
    elif s.act == "stop":
        head, col = "PALM -> STOP", (0, 140, 255)
    else:
        head, col = "no gesture", (190, 190, 190)

    cv2.putText(img, head, (int(10 * k), int(30 * k)),
                cv2.FONT_HERSHEY_SIMPLEX, fs(0.8), col, 2, cv2.LINE_AA)
    cv2.putText(img, "ratio %.2f (>=%.2f)  compact %.2f  gaps %d" %
                (s.ratio, s.min_ratio, s.compact, s.defects),
                (int(10 * k), int(56 * k)), cv2.FONT_HERSHEY_SIMPLEX,
                fs(0.5), col, 1, cv2.LINE_AA)
    cv2.putText(img, "fist: compact>=%.2f  gaps<=%d" %
                (s.fist_compact, s.fist_defects),
                (int(10 * k), int(76 * k)), cv2.FONT_HERSHEY_SIMPLEX,
                fs(0.45), (160, 160, 160), 1, cv2.LINE_AA)
    # 帧率放在这里：卡的时候一眼能分清是识别慢（fps 低）还是网络慢（fps 正常）
    cv2.putText(img, "fired %d   %.0f fps" % (s.fired, s.fps),
                (int(10 * k), int(96 * k)), cv2.FONT_HERSHEY_SIMPLEX,
                fs(0.45), (160, 160, 160), 1, cv2.LINE_AA)
    return img



# ----------------------------------------------------------------- 抓图


def grab_best(camera, tries: int = 5, gap: float = 0.06):
    """连拍几帧，返回最清晰的一帧（拉普拉斯方差最大）。

    平衡车车体一直在抖，单帧极容易糊；多抓 5 帧的代价只有几十毫秒，
    换来的是识别率明显提升。
    """
    best, best_score = None, -1.0
    for i in range(tries):
        frame = camera.read()
        if frame is None:
            time.sleep(gap)
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if score > best_score:
            best, best_score = frame, score
        if i + 1 < tries:
            time.sleep(gap)
    return best, best_score


def frame_report(frame) -> str:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    mean = float(gray.mean())
    return f"{w}x{h} 亮度 {mean:.0f}" + ("（偏暗，换亮一点的地方）" if mean < 40 else "")


# ----------------------------------------------------------------- 人脸


def cmd_who(args) -> int:
    db = FaceDB(ROOT / "faces")
    db.load()
    if not db.entries:
        log("还没有人脸记录。先录入：")
        log("  python3 scripts/face_db.py enroll --name 你的名字")
        log("FACE none -1")
        return 2

    yunet = ensure_yunet(args.model, allow_download=not args.no_download)
    sface = SFace(ensure_sface(args.sface_model, allow_download=not args.no_download))
    detector = FaceDetector(yunet, args.width, args.height, args.score)

    camera = open_camera(args.source, args.camera, args.width, args.height)
    try:
        found = None
        deadline = time.time() + max(1.0, args.timeout)
        while time.time() < deadline:
            frame, _sharp = grab_best(camera, tries=3)
            if frame is None:
                continue
            faces = detector.detect(frame)
            if len(faces):
                # 多张脸时取最大的那个（按面积），通常是离镜头最近的人
                areas = [float(f[2]) * float(f[3]) for f in faces]
                found = (frame, faces[int(np.argmax(areas))])
                break
            time.sleep(0.2)
    finally:
        camera.close()

    if found is None:
        log("画面里没有人脸。")
        log("FACE none -1")
        return 0

    frame, face = found
    name, score = db.match(sface.feature(frame, face), args.threshold)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        annotated = frame.copy()
        from face_detect import draw_faces

        draw_faces(annotated, face.reshape(1, -1), [f"{name} {score:.2f}"])
        cv2.imwrite(str(args.save), annotated)
        log(f"截图：{args.save}")

    if name == "unknown":
        log(f"这张脸不认识（最接近 {score:.2f}，阈值 {args.threshold}）。")
    else:
        log(f"这是 {name}（相似度 {score:.2f}）。")
    log(f"FACE {name} {score:.3f}")
    return 0


# ----------------------------------------------------------------- 手势


class HandDetector:
    """两个手势：张开手掌 → 停车；握拳 → 前进一小段。

    **判据是紧致度和指缝，不是实心度。**

    一开始用凸包实心度（solidity）区分，方向就错了。实测 65 秒三组动作
    （每 0.5 秒聚合）的数字是这样：

        动作      实心度   面积   紧致度 4πA/P²   指缝条数
        握拳      0.84    16%    0.53            1~2
        张开手掌  0.55    20%    0.15            5~6

    实心度只差 0.3，而且方向反直觉 —— **张开的手反而更"不实"**，
    因为手指之间的缝让轮廓填不满凸包，可一旦闭运算把缝糊上，
    两者都变成大实心团，直接混在一起。这也是"张手想停车却被判成前进"的来源。

    紧致度（4πA/P²）差 3.5 倍：手指张开时周长暴涨、面积没怎么变，值掉到
    0.15；握拳是个圆疙瘩，周长最短，值升到 0.53。指缝条数同理，5~6 vs 1~2。
    这两个数一起看，两组之间留出一条很宽的隔离带（紧致度 0.32~0.45 不判）。

    共同前提还是"手离镜头够近"：最大连通域占画面比例够大。远处的路人手、
    木色桌面过不了这一关。

    两个动作各算各的冷却：停车 0.8 秒（必须随时能停），前进 3 秒。
    分开还有个更要紧的原因 —— 刚握拳让车走、马上张手要停，
    共用一个冷却的话这一掌会被吞掉。
    """

    COOLDOWN = {"stop": 0.8, "forward": 3.0}

    def __init__(self, min_ratio=0.12, fist_ratio=0.09,
                 fist_compact=0.45, fist_defects=3,
                 open_compact=0.32, open_defects=4,
                 frames=6):
        self.min_ratio = min_ratio        # 停车：手占画面至少这么多
        self.fist_ratio = fist_ratio      # 拳头小一圈，门槛低一点
        self.fist_compact = fist_compact  # 握拳：够圆
        self.fist_defects = fist_defects  # 握拳：指缝少
        self.open_compact = open_compact  # 张手：明显不圆
        self.open_defects = open_defects  # 张手：指缝多
        self.frames = frames              # 连续 N 帧才认，0.4 秒左右
        self.kind = None
        self.hits = 0
        self.last_fire = {"stop": 0.0, "forward": 0.0}
        self.ratio = 0.0
        self.solidity = 0.0
        self.compact = 0.0
        self.mask = None        # 最近一帧的肤色掩膜（小尺寸），只给电脑预览用
        self.contour = None     # 最近一帧判定用的那只手的轮廓，同上
        self.defects = 0

    def update(self, frame, faces=None):
        """返回 "stop" / "forward" / None。

        faces 传 YuNet 检到的人脸框（原图坐标）。为什么要传：
        人站得近一点，脸就占画面十几个百分点、实心度还高（一张正脸轮廓
        接近圆形），和"举起的手掌"在数值上几乎一模一样 —— 实测站着不动
        就是 ratio 0.17 / sol 0.75，正好压在判定线上，会被反复判成"停车"。
        把脸框（外扩 25% 把下巴和头发边缘也盖住）从肤色图里涂掉，
        剩下的才是真正需要判断的手。
        """
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (320, int(320 * h / max(w, 1))))
        ycrcb = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, (0, SKIN_CR[0], SKIN_CB[0]),
                           (255, SKIN_CR[1], SKIN_CB[1]))
        mask = cv2.medianBlur(mask, 5)
        # 闭运算核从 7x7 收到 5x5：7x7 会把手指之间的缝一起填平，
        # 张开的手掌因此变得和拳头一样"实"（实心度冲到 0.9 以上），
        # 这就是"张手想停车却被判成正前走"的直接原因。
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                np.ones((5, 5), np.uint8))

        if faces:
            mh, mw = mask.shape[:2]
            sc = mw / max(w, 1)
            for f in faces:
                try:
                    x, y, fw, fh = (float(v) for v in f[:4])
                except (TypeError, ValueError):
                    continue
                if fw <= 0 or fh <= 0:
                    continue
                px, py = fw * 0.25, fh * 0.25
                x0 = max(0, int((x - px) * sc))
                y0 = max(0, int((y - py) * sc))
                x1 = min(mw, int((x + fw + px) * sc))
                y1 = min(mh, int((y + fh + py) * sc))
                if x1 > x0 and y1 > y0:
                    mask[y0:y1, x0:x1] = 0

        area = float(mask.shape[0] * mask.shape[1])
        self.ratio = float(cv2.countNonZero(mask)) / area
        self.solidity = 0.0
        self.compact = 0.0      # 4πA/P²：圆≈1，手指张开明显小于 1
        self.defects = 0        # 深指缝的条数：握拳 0~1，张手 2~4
        self.mask = mask
        self.contour = None

        kind = None
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            big = max(contours, key=cv2.contourArea)
            self.contour = big
            carea_abs = float(cv2.contourArea(big))
            carea = carea_abs / area
            hull = cv2.convexHull(big)
            harea = float(cv2.contourArea(hull))
            if harea > 0:
                self.solidity = carea_abs / harea

            # 光靠实心度在"手贴得比较近"时会失效：指缝被填平后，
            # 张开的掌和握紧的拳都是一个大实心团。这两个数看得更细 ——
            # 周长算出来的紧致度、以及凸包缺陷（指缝）的条数和深度。
            peri = cv2.arcLength(big, True)
            if peri > 0:
                self.compact = 4.0 * math.pi * carea_abs / (peri * peri)
            try:
                idx = cv2.convexHull(big, returnPoints=False)
                d = cv2.convexityDefects(big, idx)
                if d is not None:
                    # 第四列是"最深点离凸包的距离"，单位是 1/256 像素
                    self.defects = int(np.sum(d[:, 0, 3] / 256.0 > 6.0))
            except cv2.error:
                self.defects = 0

            if (carea >= self.fist_ratio
                    and self.compact >= self.fist_compact
                    and self.defects <= self.fist_defects):
                kind = "forward"
            elif (carea >= self.min_ratio
                    and self.compact <= self.open_compact
                    and self.defects >= self.open_defects):
                kind = "stop"

        if kind != self.kind:
            self.kind, self.hits = kind, 0
        if kind is None:
            return None

        self.hits += 1
        if self.hits < self.frames:
            return None
        now = time.time()
        if now - self.last_fire[kind] < self.COOLDOWN[kind]:
            return None
        self.last_fire[kind] = now
        self.hits = 0
        self.kind = None
        return kind

    # 老名字留着：万一别处还 import 着 HandStopDetector


HandStopDetector = HandDetector


def bluetooth_send(mac: str, payload: str, channel: int = 1) -> bool:
    """直连车上的 HC-04 发一帧。失败不抛异常，由调用方决定重试。"""
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                             socket.BTPROTO_RFCOMM)
        sock.settimeout(4)
        sock.connect((mac, channel))
        sock.sendall(payload.encode("ascii"))
        time.sleep(0.05)
        sock.close()
        return True
    except Exception as exc:
        log(f"蓝牙发送失败：{type(exc).__name__}: {exc}")
        return False


def bluetooth_send_stop(mac: str, channel: int = 1) -> bool:
    return bluetooth_send(mac, "[stop]", channel)


def write_cmd(path: Path, text: str) -> None:
    """写给主程序看的小文件。用文件而不是再开一个蓝牙连接 ——
    HC-04 的 SPP 只允许一路连接，主程序占着的时候第二个连不上去。"""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.chmod(tmp, 0o666)
        tmp.replace(path)
    except Exception as exc:
        log(f"写 {path} 失败：{exc}")


def _tune_frame_rate(camera, fps: float) -> str:
    """把摄像头帧率顶到 fps。返回一句给人看的说明。

    ★「画面有点卡」的真正原因就在这里：室内灯光下自动曝光把曝光时间拉满到
      66ms，帧率就被钉死在 15fps（ExposureTime=66640us、FrameDuration=66767us
      都实测出来了）。跟编码、跟协议、跟网络一点关系都没有 —— 摄像头每秒
      就只给 15 张，后面怎么优化都是 15 张。

    限制帧时长之后 AGC 只会把曝光压到限值，增益不太肯往上走（实测只到 8，
    画面亮度从 121 掉到 72）。所以再按亮度自己补一次增益：白天不补（不会过曝），
    夜里补到 110 左右，亮度和 15fps 时基本一致（实测 115）。
    """
    p = getattr(camera, "picam", None)          # 只有 CSI 摄像头有这套控制
    if p is None or fps <= 0:
        return ""
    us = int(1e6 / fps)
    try:
        p.set_controls({"FrameDurationLimits": (us, us)})
    except Exception as exc:                    # noqa: BLE001
        return f"限帧率失败（{exc}），按原来的走"
    time.sleep(1.0)
    try:
        for _ in range(3):
            camera.read()                       # 把限制生效前的旧帧读掉
        frame = camera.read()
        mean = float(frame.mean())
        gain = float(p.capture_metadata().get("AnalogueGain", 1.0))
        if 0 < mean < 100:                      # 偏暗才补，亮了会过曝
            want = min(32.0, gain * (110.0 / mean))
            p.set_controls({"AnalogueGain": want})
            time.sleep(0.4)
            for _ in range(3):
                camera.read()
            return (f"帧率顶到 {fps:g}fps（曝光 {us}us，亮度 {mean:.0f}→"
                    f"{float(camera.read().mean()):.0f}，增益 {want:.1f}）")
        return f"帧率顶到 {fps:g}fps（亮度 {mean:.0f}，不用补增益）"
    except Exception as exc:                    # noqa: BLE001
        return f"帧率顶到 {fps:g}fps（亮度补偿失败：{exc}）"


def cmd_gesture(args) -> int:
    # 分辨率不要动：试过降到 480x360（想省 read 的 42ms），实测帧率反而从
    # 16.3 掉到 14.9 —— ov5647 在 640x480 是传感器原生 binning 直出，
    # 480x360 要走 ISP 缩放，pipeline 多一级反而更慢。保持用户给的分辨率。
    camera = open_camera(args.source, args.camera, args.width, args.height)
    fps_note = _tune_frame_rate(camera, float(getattr(args, "fps", 0) or 0))
    if fps_note:
        log(f"预览流畅度：{fps_note}")
    detector = HandDetector(fist_compact=args.fist_compact,
                            open_compact=args.open_compact)
    started = time.time()
    fired = 0
    fps = 0.0                      # 检测帧率，画在预览角上
    n_proc = 0
    fps_t0 = started
    stream = int(getattr(args, "stream", 0) or 0)
    if stream:
        start_stream(stream)

    # 人脸框：每 0.7 秒找一次，用来把"脸"从肤色图里排除掉。
    # 不排除脸的话，人往前站一点（脸占 17%、实心度 0.75）就正好踩在判定线上，
    # 站着不动也会被当成"举起手掌"反复发电。
    #
    # ★但这件事必须放独立线程：实测 YuNet 在 Pi 4 上跑一帧 640x480 要 245ms，
    #   原来同步做，主循环每 0.7 秒被掐住 245ms —— 推流画面就是"隔一会儿顿
    #   一下"，比整体帧率低更难受。cv2 检测会释放 GIL，分到第二个核是真并行。
    face_worker = None
    if not args.no_face_guard:
        face_worker = _FaceWorker(args.model)
    face_warned = False
    faces = []

    how = ("发蓝牙" if args.mac else
           (f"写 {args.cmd}" if args.cmd else "只打印（加 --mac 或 --cmd 才会生效）"))
    log(f"手势：张开手掌 → 停车；握拳 → 前进 {args.sec:g} 秒。"
        f"（握拳 紧致度≥{args.fist_compact:.2f}；张手 紧致度≤{args.open_compact:.2f}）"
        f"Ctrl-C 退出。触发时{how}")
    try:
        while True:
            frame = camera.read()
            if frame is None:
                time.sleep(0.05)
                continue

            now = time.time()
            if face_worker is not None:
                faces = face_worker.faces          # 永远拿最近一次结果，不等
                face_worker.offer(frame)           # 内部按 0.7s 节流
                if face_worker.err is not None and not face_warned:
                    face_warned = True
                    log(f"人脸框检测不可用（{type(face_worker.err).__name__}: "
                        f"{face_worker.err}），继续跑，只是不排除脸")

            act = detector.update(frame, faces)
            if act:
                fired += 1
                payload = "stop" if act == "stop" else f"forward {args.sec:g}"
                tag = "手掌→停车" if act == "stop" else "握拳→前进"
                log(f"GESTURE {act.upper()} #{fired} {tag} "
                    f"sol={detector.solidity:.2f} ratio={detector.ratio:.2f}")
                if args.mac:
                    _fire_bluetooth(args.mac, args.channel, act, args.sec)
                elif args.cmd:
                    write_cmd(Path(args.cmd), payload)
                if act == "forward" and args.once:
                    log("--once：发完一次前进就退出")
                    break
            elif args.verbose:
                log(f"  sol={detector.solidity:.2f} ratio={detector.ratio:.2f}")

            # 只把帧交给编码线程，标注和 JPEG 编码都在那边做。
            # 没人看时 offer 直接返回，一帧都不编。
            if stream:
                n_proc += 1
                if now - fps_t0 >= 1.0:
                    fps = n_proc / (now - fps_t0)
                    n_proc = 0
                    fps_t0 = now
                LIVE.offer(frame, _Snap(detector, act, fired, fps))
            # 不睡：camera.read() 本身就阻塞到下一帧，再睡就是白扔 5ms
            #（原来是 20ms，占了每帧 1/4 的时间）。
            if args.duration and (time.time() - started) >= args.duration:
                break
            time.sleep(0.001)
    except KeyboardInterrupt:
        log("已退出。")
    finally:
        camera.close()
    return 0


def _fire_bluetooth(mac: str, channel: int, act: str, sec: float) -> None:
    """--mac 模式（主程序没占链路时自己发）。前进要自己掐表收尾，
    因为这条路上没有别人替它发 [stop]。"""
    if act == "stop":
        ok = bluetooth_send(mac, "[stop]", channel)
        log("  已发车" if ok else "  发送失败")
        return
    ok = bluetooth_send(mac, "[go,1.40,0]", channel)
    log("  已发 go" if ok else "  发 go 失败")
    time.sleep(sec)
    ok = bluetooth_send(mac, "[stop]", channel)
    log("  已发 stop" if ok else "  发 stop 失败")


# ----------------------------------------------------------------- 问模型


def load_vision_cfg(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    vision = cfg.get("vision") or {}
    if not vision.get("api_key"):
        vision["api_key"] = os.environ.get("VISION_API_KEY", "")
    return vision


def ask_vision(jpeg: bytes, question: str, cfg: dict) -> str:
    """把一张 JPEG 发给多模态模型。走 OpenAI 兼容的 chat/completions。"""
    import urllib.request

    if not cfg.get("api_key"):
        return ("没配视觉模型。在 llm.json 里加一段 vision：\n"
                '  {"api_key":"sk-xxx","base_url":"https://dashscope.aliyuncs.com/'
                'compatible-mode/v1","model":"qwen-vl-max"}')

    b64 = base64.b64encode(jpeg).decode("ascii")
    body = {
        "model": cfg.get("model", "qwen-vl-max"),
        "max_tokens": int(cfg.get("max_tokens", 120)),
        "temperature": float(cfg.get("temperature", 0.6)),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    req = urllib.request.Request(
        cfg.get("base_url", "").rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return f"视觉模型调用失败：{type(exc).__name__}: {exc}"


def cmd_snap(args) -> int:
    camera = open_camera(args.source, args.camera, args.width, args.height)
    try:
        frame, sharp = grab_best(camera, tries=args.burst)
    finally:
        camera.close()

    if frame is None:
        log("没抓到画面。")
        return 2

    out = args.save or (ARTIFACTS / time.strftime("snap-%Y%m%d-%H%M%S.jpg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    log(f"已抓图：{out}（{frame_report(frame)}，锐度 {sharp:.0f}）")

    if not args.ask:
        log("（加 --ask \"这是什么东西\" 可以顺便问模型）")
        return 0

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        log("JPEG 编码失败。")
        return 2

    cfg = load_vision_cfg(args.llm)
    t0 = time.time()
    answer = ask_vision(buf.tobytes(), args.ask, cfg)
    log(f"模型（{int((time.time() - t0) * 1000)} ms）：{answer}")
    return 0


# ----------------------------------------------------------------- 体检


def cmd_check(args) -> int:
    log("=== 1. 模型 ===")
    yunet = None
    try:
        yunet = ensure_yunet(args.model, allow_download=not args.no_download)
        log(f"  YuNet  {yunet.name}  {yunet.stat().st_size / 1024:.0f} KB  OK")
    except Exception as exc:
        log(f"  YuNet  缺失：{exc}")
    try:
        sface = ensure_sface(args.sface_model, allow_download=not args.no_download)
        log(f"  SFace  {sface.name}  {sface.stat().st_size / 1024 / 1024:.1f} MB  OK")
    except Exception as exc:
        log(f"  SFace  缺失：{exc}")

    log("=== 2. 摄像头 ===")
    try:
        camera = open_camera(args.source, args.camera, args.width, args.height)
    except Exception as exc:
        log(f"  打不开：{exc}")
        log("  → CSI 摄像头：rpicam-hello --list-cameras 看枚举，"
            "sudo dmesg | tail -20 看 MIPI 有没有报错")
        return 2

    try:
        frames, t0 = 0, time.time()
        last = None
        for _ in range(20):
            frame = camera.read()
            if frame is not None:
                frames += 1
                last = frame
        dt = max(time.time() - t0, 1e-6)
        log(f"  20 次读取成功 {frames} 帧，{frames / dt:.1f} fps")
        if last is None:
            log("  一帧都没拿到：数据通道没通。先跑 python cam_check.py 定位硬件。")
            return 2
        log(f"  画质：{frame_report(last)}")

        frame, sharp = grab_best(camera, tries=3)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        out = ARTIFACTS / "check.jpg"
        cv2.imwrite(str(out), frame)
        log(f"  样张：{out}（锐度 {sharp:.0f}）")

        # 顺手验一下人脸检测器能不能跑（不要求画面里有人）
        if yunet is None:
            log("=== 3. 人脸检测 === 跳过（模型没就位）")
        else:
            try:
                det = FaceDetector(yunet, args.width, args.height, args.score)
                faces = det.detect(frame)
                log(f"=== 3. 人脸检测 === 当前画面 {len(faces)} 张脸")
            except Exception as exc:
                log(f"=== 3. 人脸检测 === 初始化失败：{exc}")
                return 2
    finally:
        camera.close()

    cfg = load_vision_cfg(args.llm)
    log("=== 4. 视觉模型 ===")
    log(f"  {cfg.get('model') or '未配置'}  "
        f"{'key 已填' if cfg.get('api_key') else '缺 api_key'}")
    log("全部就绪。" if cfg.get("api_key") else "摄像头可用；要问模型还需在 llm.json 补 vision 段。")
    return 0


# ----------------------------------------------------------------- 入口


def main() -> int:
    # 公共参数挂在每个子命令上，写命令时可以放在子命令之后，
    # 例如 `vision_service.py who --source usb`（不用挤在子命令前面）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", choices=["auto", "picamera", "usb"], default="auto")
    common.add_argument("--camera", type=int, default=0, help="USB 摄像头编号")
    common.add_argument("--width", type=int, default=640)
    common.add_argument("--height", type=int, default=480)
    common.add_argument("--score", type=float, default=0.6, help="YuNet 检测阈值")
    common.add_argument("--model", type=Path,
                        default=ROOT / "models" / "face_detection_yunet_2023mar.onnx")
    common.add_argument("--sface-model", type=Path,
                        default=ROOT / "models" / "face_recognition_sface_2021dec.onnx")
    common.add_argument("--no-download", action="store_true")
    common.add_argument("--llm", type=Path, default=DEFAULT_LLM, help="llm.json 路径")

    ap = argparse.ArgumentParser(description="视觉三件事：手势停车 / 人脸认主人 / 拍照问模型")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", parents=[common], help="体检：模型 / 出帧 / 检测 / 模型配置")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("who", parents=[common], help="人脸认主人")
    p.add_argument("--threshold", type=float, default=0.363, help="SFace 余弦阈值")
    p.add_argument("--timeout", type=float, default=3.0, help="最多等几秒等到脸")
    p.add_argument("--save", type=Path, default=None)
    p.set_defaults(func=cmd_who)

    p = sub.add_parser("snap", parents=[common], help="拍照，可顺便问模型")
    p.add_argument("--ask", default="", help="要问的问题，例如 \"这是什么东西\"")
    p.add_argument("--burst", type=int, default=5, help="连拍帧数，取最清晰的一张")
    p.add_argument("--save", type=Path, default=None)
    p.set_defaults(func=cmd_snap)

    p = sub.add_parser("gesture", parents=[common], help="手势：张手停车 / 握拳前进")
    p.add_argument("--mac", default="", help="车的蓝牙 MAC；不给就只打印不发车")
    p.add_argument("--cmd", default="", help="触发时写这个文件（主程序读它再发车）")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--sec", type=float, default=4.0, help="握拳前进几秒")
    p.add_argument("--fist-compact", type=float, default=0.45,
                   help="握拳的紧致度下限（实测握拳 0.53、张手 0.15）")
    p.add_argument("--open-compact", type=float, default=0.32,
                   help="张手的紧致度上限")
    p.add_argument("--verbose", action="store_true", help="每帧打印实心度和面积占比")
    p.add_argument("--no-face-guard", action="store_true",
                   help="不排除人脸区域（默认排除，防止站着不动被当成举手）")
    p.add_argument("--once", action="store_true", help="触发一次前进就退出（调阈值用）")
    p.add_argument("--duration", type=float, default=0, help="跑多少秒后退出，0 为一直跑")
    p.add_argument("--stream", type=int, default=8099,
                   help="在电脑浏览器里看画面（MJPEG 端口，0 = 不开）")
    p.add_argument("--fps", type=float, default=30.0,
                   help="顶住摄像头帧率。室内光下自动曝光会把帧率压到 15fps，"
                        "画面就一顿一顿的；默认顶到 30，0 = 不干预")
    p.set_defaults(func=cmd_gesture)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
