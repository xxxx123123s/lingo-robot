#!/usr/bin/env python3
"""视觉调试台：浏览器打开 http://192.168.137.160:8080 就能看画面 + 调手势。

为什么要单独做一个：调机位、撕膜、验收识别效果时，把 JPEG 照片 scp 回电脑
太慢（一张 60KB、来回两秒），看不到连续画面就判断不了"到底是没对上焦、
还是脸出框了"。MJPEG 走 HTTP 只要几行代码，浏览器原生支持，零依赖。

这一版加了两件事，都是为了在电脑上调手势：

  1. **实时显示手势的判定依据**。画面上直接打 `SOL 0.66 R 0.18`，页面下面
     用中文说"张开手掌 / 握拳 / —"。之前调阈值只能改一个数、跑一次、
     看日志猜，现在举一下手就知道该往哪边调。
  2. **发车开关**。默认只在画面里显示判定、不碰车；勾上"发车"才往
     /tmp/vision.cmd 写指令，主程序照旧读那个文件再发蓝牙。
     调参阶段车乱跑很危险，所以默认是关的。

还有个"释放摄像头"按钮。picamera2 同一时刻只允许一个进程开摄像头，
而锁屏的人脸验证也要用 —— 调完手势点一下，进程退出，人脸立刻能验。

用法（树莓派上）：
    cd /home/pi && python3 cam_view.py                # 默认 8080，只显示不发车
    python3 cam_view.py --send                         # 启动就允许手势发车
    python3 cam_view.py --port 8081 --no-hand          # 换端口 / 不算手势

停止：pkill -f '^python3 cam_view'
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2

MODEL = Path("/home/pi/models/face_detection_yunet_2023mar.onnx")
CMD_FILE = Path(os.environ.get("PETBOT_VISION_CMD", "/tmp/vision.cmd"))

# 共享状态。相机的帧率远高于检测，所以抓帧和检测分两个线程：
# 检测慢（约 6 fps）就让它慢，抓帧这一步不能等它，否则画面会一顿一顿。
_latest = {"faces": [], "t": 0.0}
_lock = threading.Lock()
_jpeg = {"buf": b""}
_run = {"on": True}

# 手势这一路的结果。sol/ratio 每帧都刷新（便宜），act 是"刚刚触发的那一下"。
_hand = {"sol": 0.0, "ratio": 0.0, "act": "", "fire": "", "n": 0,
         "compact": 0.0, "defects": 0}
_send = {"on": False}          # 页面上的"发车"开关
_fwd = {"on": False}           # "允许握拳前进"开关。默认关：停车永远优先

# 特征流水。用户做动作时不用掐点，做完随时把这段时间的原始数字导出来，
# 拿回去分成"握拳"和"张手"两堆比 —— 比隔空喊"你现在握拳"靠谱得多。
# 4000 帧 ≈ 4 分钟（15fps），够做十几轮动作。
_hist = collections.deque(maxlen=4000)
_t0 = time.time()

# 检测必须串行。之前每 150ms 起一个新线程去调同一个 ONNX 检测器，
# 上一轮还没跑完下一轮就进来，setInputSize/detect 的内部状态被并发写坏，
# 结果是满屏几百个 0.5~0.9 的假框（画面里根本没有脸）。
# 改成「一个检测线程 + 最新帧槽位」：不管抓帧多快，检测永远一次一个。
_det_slot = {"frame": None}
_det_evt = threading.Event()


def _fire(payload: str) -> None:
    """写一行指令给主程序。和手势进程写的是同一个文件，不会互相打架 ——
    同一时刻只可能有一个进程在开摄像头，也就只有一处在写。"""
    try:
        tmp = CMD_FILE.with_suffix(CMD_FILE.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.chmod(tmp, 0o666)
        tmp.replace(CMD_FILE)
    except Exception as exc:                              # noqa: BLE001
        print(f"[调试台] 写 {CMD_FILE} 失败：{exc}", flush=True)


def camera_loop(width: int, height: int, use_face: bool,
                use_hand: bool, sec: float) -> None:
    from scripts.face_detect import open_camera           # 复用已有的封装
    from scripts.vision_service import HandDetector

    cam = open_camera("picamera", 0, width, height)
    det_h = HandDetector() if use_hand else None
    last_detect = 0.0
    try:
        while _run["on"]:
            frame = cam.read()
            if frame is None:
                time.sleep(0.02)
                continue

            now = time.time()
            with _lock:
                _latest["t"] = now
                faces = list(_latest["faces"])

            # 标注框直接复用上一轮检测结果：延迟 150ms 肉眼看不出来，
            # 但省掉每帧一次推理，帧率能稳在 15 以上。
            if use_face:
                for f in faces[:3]:                        # 最多画 3 个，防脏画面
                    score = float(f[-1])
                    if score < 0.6:                        # 低于 0.6 的都是噪声
                        continue
                    x, y, w, h = [int(round(v)) for v in f[:4]]
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                    cv2.putText(frame, f"{score:.2f}", (x, max(y - 8, 14)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1,
                                cv2.LINE_AA)

            if det_h is not None:
                # 把当前的人脸框传进去：不排除脸的话，人往前站一点就正好
                # 踩在"举起手掌"的判定线上（实测站定 ratio 0.17 / sol 0.75）。
                # 只信 0.6 以上的框 —— 低分的那些是噪声，跟着涂黑会误伤手。
                guard = [f for f in faces
                         if len(f) >= 5 and float(f[-1]) >= 0.6] if use_face else []
                act = det_h.update(frame, guard)           # 每帧都要喂，状态机
                with _lock:
                    _hand["sol"] = det_h.solidity
                    _hand["ratio"] = det_h.ratio
                    _hand["compact"] = det_h.compact
                    _hand["defects"] = det_h.defects
                    _hist.append((round(now - _t0, 2), round(det_h.solidity, 3),
                                  round(det_h.ratio, 3), round(det_h.compact, 3),
                                  det_h.defects, act or ""))
                if act:
                    payload = "stop" if act == "stop" else f"forward {sec:g}"
                    with _lock:
                        _hand["act"] = act
                        _hand["fire"] = time.strftime("%H:%M:%S")
                        _hand["n"] += 1
                    # 停车无条件放行，前进要两道开关都开着。
                    # 顺序反过来的话，调参时一次误判就能让车冲出去。
                    if _send["on"] and (act == "stop" or _fwd["on"]):
                        _fire(payload)
                        print(f"[调试台] {act} → {payload}", flush=True)
                    elif act == "forward":
                        print("[调试台] 握拳已识别，但「前进」开关没开，未发车",
                              flush=True)

                # 画面上打数值。putText 只认 ASCII，中文放到网页里说。
                color = (0, 255, 0) if det_h.ratio >= 0.12 else (200, 200, 200)
                cv2.putText(frame, f"SOL {det_h.solidity:.2f}", (8, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2, cv2.LINE_AA)
                cv2.putText(frame, f"R {det_h.ratio:.2f}", (8, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2, cv2.LINE_AA)
                cv2.putText(frame, f"C {det_h.compact:.2f}  D {det_h.defects}",
                            (8, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2,
                            cv2.LINE_AA)
                # 底部两条判定线：握拳看"够不够圆"，张手看"够不够散"
                cv2.putText(frame,
                            f"FIST c>={det_h.fist_compact:.2f} d<={det_h.fist_defects}",
                            (8, frame.shape[0] - 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1,
                            cv2.LINE_AA)
                cv2.putText(frame,
                            f"OPEN c<={det_h.open_compact:.2f} d>={det_h.open_defects}",
                            (8, frame.shape[0] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1,
                            cv2.LINE_AA)

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            if ok:
                _jpeg["buf"] = buf.tobytes()

            # 只把最新帧塞进槽位并敲一下事件，不在这里开线程
            if use_face and now - last_detect >= 0.15:
                last_detect = now
                _det_slot["frame"] = frame.copy()
                _det_evt.set()
    finally:
        cam.close()


def detect_loop() -> None:
    """唯一的检测线程：有帧就检，没有就等。保证任何时候只有一次推理在跑。"""
    from scripts.face_detect import FaceDetector

    det = None
    dims = None
    while _run["on"]:
        if not _det_evt.wait(0.5):
            continue
        _det_evt.clear()
        frame = _det_slot["frame"]
        if frame is None:
            continue
        try:
            if det is None or dims != frame.shape[:2]:
                det = FaceDetector(MODEL, frame.shape[1], frame.shape[0], 0.5)
                dims = frame.shape[:2]
            faces = [f.tolist() for f in det.detect(frame)]
            with _lock:
                _latest["faces"] = faces
        except Exception as exc:                          # noqa: BLE001
            print(f"[预览] 检测出错：{exc}", flush=True)


PAGE = """<!doctype html><meta charset=utf-8>
<title>小派视觉调试台</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px/1.6 system-ui,-apple-system,sans-serif}
 .wrap{max-width:960px;margin:0 auto;padding:12px}
 img{width:100%;display:block;border-radius:6px;background:#000}
 .bar{margin-top:10px;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
 .bar label{cursor:pointer;user-select:none}
 .k{color:#888}
 b{color:#4ade80;font-variant-numeric:tabular-nums}
 button{background:#262626;color:#ddd;border:1px solid #444;border-radius:6px;
        padding:6px 12px;cursor:pointer;font-size:13px}
 button:hover{background:#333}
 .warn{color:#f59e0b}
</style>
<div class="wrap">
  <img src="/stream" alt="摄像头画面">
  <div class="bar">
    <span><span class="k">实心度</span> <b id="sol">-</b></span>
    <span><span class="k">面积</span> <b id="rat">-</b></span>
    <span><span class="k">紧致度</span> <b id="comp">-</b></span>
    <span><span class="k">指缝</span> <b id="defs">-</b></span>
    <span><span class="k">判定</span> <b id="act">—</b></span>
    <span><span class="k">触发</span> <b id="n">0</b><span id="fire"></span></span>
  </div>
  <div class="bar">
    <label><input type="checkbox" id="send"> 发车（勾上才真的动轮子）</label>
    <label><input type="checkbox" id="fwd"> 允许握拳前进</label>
    <button onclick="quit()">释放摄像头（还给锁屏人脸）</button>
  </div>
  <p class="k" style="margin-top:14px">
    张开手掌 → 停车 · 握拳 → 前进 N 秒。中间那段紧致度故意不判，宁可漏也不误触发。<br>
    四个数怎么读：<b>紧致度</b>越接近 1 越像拳头（握拳约 0.53，张手约 0.15）·
    <b>指缝</b>是凸出来的凹口条数（握拳 1~2，张手 5~6）·
    <b>实心度</b>和<b>面积</b>只作参考，不再参与判定。
  </p>
</div>
<script>
const ACTNAME={stop:"张开手掌 → 停车",forward:"握拳 → 前进"};
async function poll(){
  try{
    const r=await fetch('/state',{cache:'no-store'});
    const s=await r.json();
    sol.textContent=s.sol.toFixed(2);
    rat.textContent=(s.ratio*100).toFixed(0)+'%';
    comp.textContent=s.compact.toFixed(2);
    defs.textContent=s.defects;
    act.textContent=s.act?ACTNAME[s.act]:"—";
    act.style.color=s.act?(s.act==='stop'?'#38bdf8':'#4ade80'):'#4ade80';
    n.textContent=' '+s.n+' 次 '+s.fire;
    document.getElementById('send').checked=s.send;
    document.getElementById('fwd').checked=s.fwd;
  }catch(e){}
}
async function setSend(v){ await fetch('/set?send='+(v?1:0)); poll(); }
async function setFwd(v){ await fetch('/set?fwd='+(v?1:0)); poll(); }
async function quit(){
  await fetch('/quit'); document.body.innerHTML=
    '<div class="wrap"><p class="warn">预览已退出，摄像头已释放。'+
    '锁屏的人脸验证现在可以用了。</p></div>';
}
document.getElementById('send').addEventListener('change',e=>setSend(e.target.checked));
document.getElementById('fwd').addEventListener('change',e=>setFwd(e.target.checked));
setInterval(poll,800); poll();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):                            # 关掉每帧一行日志
        pass

    def _text(self, body: str, code: int = 200,
              ctype: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                     # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path == "/":
            self._text(PAGE)
            return

        if path == "/state":
            with _lock:
                data = dict(sol=_hand["sol"], ratio=_hand["ratio"],
                            act=_hand["act"], fire=_hand["fire"],
                            n=_hand["n"], send=_send["on"],
                            compact=_hand["compact"], defects=_hand["defects"],
                            fwd=_fwd["on"])
            self._text(json.dumps(data), 200)
            return

        if path == "/set":
            if "send" in query:
                _send["on"] = query["send"][0] == "1"
                print(f"[调试台] 发车开关：{'开' if _send['on'] else '关'}",
                      flush=True)
            if "fwd" in query:
                _fwd["on"] = query["fwd"][0] == "1"
                print(f"[调试台] 前进开关：{'开' if _fwd['on'] else '关'}",
                      flush=True)
            self._text("ok")
            return

        if path == "/dump":
            # 纯文本，一行一帧：t sol ratio compact defects 触发
            with _lock:
                rows = list(_hist)
            body = "# t sol ratio compact defects act\n" + "\n".join(
                f"{r[0]:.2f} {r[1]:.3f} {r[2]:.3f} {r[3]:.3f} {r[4]} {r[5]}"
                for r in rows) + "\n"
            self._text(body, 200, ctype="text/plain; charset=utf-8")
            return

        if path == "/clear":
            with _lock:
                _hist.clear()
            self._text("ok")
            return

        if path == "/quit":
            self._text("<h3>已退出</h3>")
            # 先回页面再退，否则浏览器只会看到连接被重置。
            # 用 os._exit 是因为这个进程里还有守护线程卡在摄像头读上，
            # 正常退出流程会被它拖住。
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                             daemon=True).start()
            return

        if path != "/stream":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while _run["on"]:
                buf = _jpeg["buf"]
                if not buf or buf is last:
                    time.sleep(0.01)
                    continue
                last = buf
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(buf)).encode()
                                 + b"\r\n\r\n" + buf + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass                                          # 用户关了页面


def main() -> int:
    ap = argparse.ArgumentParser(description="摄像头 MJPEG 预览 + 手势调试台")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--no-face", action="store_true", help="不画人脸框")
    ap.add_argument("--no-hand", action="store_true", help="不算手势")
    ap.add_argument("--send", action="store_true", help="启动就允许手势发车")
    ap.add_argument("--fwd", action="store_true", help="启动就允许握拳前进")
    ap.add_argument("--sec", type=float, default=4.0, help="握拳前进几秒")
    args = ap.parse_args()

    _send["on"] = bool(args.send)
    _fwd["on"] = bool(args.fwd)

    threading.Thread(target=camera_loop,
                     args=(args.width, args.height, not args.no_face,
                           not args.no_hand, args.sec),
                     daemon=True).start()
    if not args.no_face:
        threading.Thread(target=detect_loop, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[调试台] http://192.168.137.160:{args.port}  "
          f"画框={'否' if args.no_face else '是'} "
          f"手势={'否' if args.no_hand else '是'} "
          f"发车={'开' if _send['on'] else '关'} "
          f"前进={'开' if _fwd['on'] else '关'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _run["on"] = False
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
