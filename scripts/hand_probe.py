#!/usr/bin/env python3
"""手势特征采集器：只记录数字，不做判定。

为什么需要它：手掌 vs 拳头靠实心度分不开（指缝被闭运算填平后，两者都是
一个实心团，都在 0.86 以上）。要换判据就得先看清楚两个动作各自长什么样 ——
凭空调阈值只会来回改，改到最后全是特例。

跑法：
    python3 scripts/hand_probe.py --secs 90
输出每行：时间 sol(实心度) ratio(面积) compact(紧致度) defects(指缝条数)

用户配合：握拳保持几秒 → 张开手掌保持几秒 → 反复三轮。
之后用直方图看两组数能不能分开，再定阈值。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2                                              # noqa: E402

from face_detect import FaceDetector, open_camera       # noqa: E402
from vision_service import HandDetector                 # noqa: E402

MODEL = Path("/home/pi/models/face_detection_yunet_2023mar.onnx")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=90)
    ap.add_argument("--out", type=Path, default=Path("/tmp/probe.log"))
    args = ap.parse_args()

    cam = open_camera("picamera", 0, 640, 480)
    det = HandDetector()
    face_det = FaceDetector(MODEL, 640, 480, 0.5)
    faces = []
    last_face = 0.0
    t0 = time.time()
    n = 0

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("# t sol ratio compact defects\n")
        while time.time() - t0 < args.secs:
            frame = cam.read()
            if frame is None:
                time.sleep(0.02)
                continue
            now = time.time()
            if now - last_face >= 0.7:
                last_face = now
                try:
                    faces = [f.tolist() for f in face_det.detect(frame)]
                except Exception:                       # noqa: BLE001
                    faces = []
            det.update(frame, faces)
            out.write(f"{now - t0:.2f} {det.solidity:.3f} {det.ratio:.3f} "
                      f"{det.compact:.3f} {det.defects}\n")
            n += 1
            if n % 20 == 0:
                out.flush()
                print(f"  {now - t0:.0f}s  {n} 帧  "
                      f"sol={det.solidity:.2f} ratio={det.ratio:.2f} "
                      f"c={det.compact:.2f} d={det.defects}", flush=True)
            time.sleep(0.15)
    cam.close()
    print(f"采完 {n} 帧 → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
