#!/usr/bin/env python3
"""F1：摄像头 + YuNet 人脸检测（不需要电机）。

在树莓派上（CSI 摄像头）：
    python3 scripts/face_detect.py --source picamera --save artifacts/face.jpg

依赖：
    python3-opencv（推荐 apt）
    python3-picamera2（CSI 摄像头）
    numpy

第一次运行会自动下载 YuNet 模型到 models/；也可以用 --no-download 手动放置。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "缺少 OpenCV 或 numpy。\n"
        "树莓派推荐：sudo apt install -y python3-opencv python3-numpy\n"
        "或：pip install -r requirements-face.txt"
    ) from exc

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DEFAULT_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/"
    "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


class CameraError(RuntimeError):
    pass


def ensure_yunet(path: Path, allow_download: bool = True) -> Path:
    if path.exists() and path.stat().st_size > 100_000:
        return path

    if not allow_download:
        raise FileNotFoundError(
            f"找不到模型：{path}\n"
            "请手动下载：\n"
            f"  curl -L -o {path} {YUNET_URL}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    print(f"下载 YuNet 模型：{YUNET_URL}")
    try:
        request = urllib.request.Request(
            YUNET_URL, headers={"User-Agent": "petbot-face-detect/1.0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as fh:
            fh.write(response.read())
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"模型下载失败：{exc}\n"
            "可以手动下载后放到：\n"
            f"  {path}\n"
            f"  curl -L -o {path} {YUNET_URL}"
        ) from exc

    if tmp.stat().st_size < 100_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("模型文件太小，可能下载失败了。请手动重新下载。")

    tmp.replace(path)
    print(f"模型已保存：{path}（{path.stat().st_size / 1024:.0f} KB）")
    return path


class Picamera2Source:
    """CSI 摄像头：必须用 picamera2，不能用 cv2.VideoCapture(0)。"""

    def __init__(self, width: int, height: int) -> None:
        from picamera2 import Picamera2

        self.picam = Picamera2()
        config = self.picam.create_preview_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        self.picam.configure(config)
        self.picam.start()

        # Camera Module 3 支持自动对焦；v1/v2 是定焦，设置失败就跳过。
        try:
            from libcamera import controls

            self.picam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            self.autofocus = True
        except Exception:
            self.autofocus = False

        # 给自动曝光/白平衡/对焦一点时间，否则第一帧可能很暗或糊。
        time.sleep(1.0)

    def read(self) -> np.ndarray | None:
        arr = self.picam.capture_array("main")
        if arr is None:
            return None
        if arr.ndim == 2:
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        # 坑：picamera2 的 "RGB888" 在内存里其实是 BGR 排列（libcamera 的历史包袱），
        # capture_array 是原样透传的。这里要是不加判断再做一次 RGB2BGR，
        # 红蓝就对调了 —— 画面变成"阿凡达"蓝紫色，YCrCb 里的 Cr/Cb 全乱掉。
        # 实测同一帧：直接当 BGR 肤色占比 46.8%，多转一次只剩 17.1%，
        # 手势根本触发不了。所以取前三个通道直接用，不转。
        return np.ascontiguousarray(arr[:, :, :3])

    def close(self) -> None:
        try:
            self.picam.stop()
        finally:
            self.picam.close()


class UsbCameraSource:
    """USB 摄像头：OpenCV 直接打开。"""

    def __init__(self, index: int, width: int, height: int) -> None:
        # Linux 上优先 V4L2；Windows/macOS 用默认后端。
        if sys.platform.startswith("linux"):
            self.cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        else:
            self.cap = cv2.VideoCapture(index)

        if not self.cap.isOpened():
            self.cap.release()
            self.cap = cv2.VideoCapture(index)

        if not self.cap.isOpened():
            raise CameraError(
                f"打不开 USB 摄像头 /dev/video{index}。\n"
                "检查：ls -l /dev/video*；或试 --camera 1。"
            )

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def read(self) -> np.ndarray | None:
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self) -> None:
        self.cap.release()


def open_camera(source: str, index: int, width: int, height: int):
    """source: auto | picamera | usb"""
    if source not in {"auto", "picamera", "usb"}:
        raise ValueError("--source 只能是 auto / picamera / usb")

    picamera_error = None
    if source in {"auto", "picamera"}:
        try:
            cam = Picamera2Source(width, height)
            focus = "自动对焦" if getattr(cam, "autofocus", False) else "定焦或自动对焦未启用"
            print(f"摄像头：CSI（picamera2，{focus}）")
            return cam
        except Exception as exc:
            picamera_error = exc
            if source == "picamera":
                raise CameraError(
                    f"CSI 摄像头打开失败：{type(exc).__name__}: {exc}\n"
                    "检查排线、rpicam-hello --list-cameras（旧系统 libcamera-hello），以及 python3-picamera2。"
                ) from exc
            print(f"CSI 摄像头不可用，尝试 USB：{type(exc).__name__}: {exc}")

    if source in {"auto", "usb"}:
        try:
            cam = UsbCameraSource(index, width, height)
            print(f"摄像头：USB /dev/video{index}")
            return cam
        except Exception as exc:
            if source == "usb":
                raise
            detail = f"\nCSI 错误：{picamera_error}" if picamera_error else ""
            raise CameraError(
                f"没有可用摄像头。请检查 rpicam-hello --list-cameras（旧系统 libcamera-hello）和 ls /dev/video*。{detail}"
            ) from exc

    raise CameraError("没有可用摄像头。")


class FaceDetector:
    def __init__(self, model: Path, width: int, height: int, score_threshold: float) -> None:
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError(
                "当前 OpenCV 没有 FaceDetectorYN，版本太旧。\n"
                f"当前版本：{cv2.__version__}\n"
                "请升级：pip install --upgrade opencv-python-headless"
            )
        self.detector = cv2.FaceDetectorYN.create(
            str(model),
            "",
            (width, height),
            score_threshold,
            0.3,
            5000,
        )

    def detect(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _retval, faces = self.detector.detect(frame)
        if faces is None:
            return np.empty((0, 15), dtype=np.float32)
        if faces.ndim == 1:
            faces = faces.reshape(1, -1)
        return faces


def draw_faces(frame: np.ndarray, faces: np.ndarray, labels: list[str] | None = None) -> None:
    for i, face in enumerate(faces):
        x, y, w, h = [int(round(v)) for v in face[:4]]
        score = float(face[-1])
        x2, y2 = x + w, y + h
        cv2.rectangle(frame, (x, y), (x2, y2), (0, 255, 0), 2)
        label = labels[i] if labels and i < len(labels) else f"face {score:.2f}"
        label_y = y - 8 if y > 24 else y2 + 20
        cv2.putText(
            frame,
            label,
            (x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        # 5 个关键点：右眼、左眼、鼻尖、右嘴角、左嘴角
        landmarks = face[4:14].reshape(5, 2).astype(int)
        for px, py in landmarks:
            cv2.circle(frame, (int(px), int(py)), 2, (0, 200, 255), -1)

        cv2.putText(
            frame,
            str(i),
            (x, y + h - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="树莓派摄像头 + YuNet 人脸检测")
    parser.add_argument("--source", choices=["auto", "picamera", "usb"], default="auto")
    parser.add_argument("--camera", type=int, default=0, help="USB 摄像头编号，默认 0")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--score", type=float, default=0.6, help="检测阈值，默认 0.6")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--no-download", action="store_true", help="模型缺失时不自动下载")
    parser.add_argument("--save", type=Path, default=None, help="保存带框截图，例如 artifacts/face.jpg")
    parser.add_argument("--show", action="store_true", help="打开窗口（需要桌面环境）")
    parser.add_argument("--max-frames", type=int, default=0, help="最多处理多少帧，0 为一直跑")
    parser.add_argument("--duration", type=float, default=0, help="运行多少秒后退出，0 为一直跑")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        model = ensure_yunet(args.model, allow_download=not args.no_download)
    except Exception as exc:
        print(f"模型准备失败：{exc}", file=sys.stderr)
        return 2

    try:
        camera = open_camera(args.source, args.camera, args.width, args.height)
    except Exception as exc:
        print(f"摄像头打开失败：{exc}", file=sys.stderr)
        return 2

    try:
        detector = FaceDetector(model, args.width, args.height, args.score)
    except Exception as exc:
        camera.close()
        print(f"人脸检测器初始化失败：{exc}", file=sys.stderr)
        return 2

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)

    show = args.show
    if show:
        try:
            cv2.namedWindow("petbot face", cv2.WINDOW_NORMAL)
        except cv2.error as exc:
            print(f"无法打开窗口（可能没有桌面环境）：{exc}")
            print("继续无窗口运行，用 --save 保存截图。")
            show = False

    print("按 Ctrl-C 退出。")
    frames = 0
    failures = 0
    last = time.perf_counter()
    fps = 0.0
    last_faces = -1
    last_frame = None
    started = time.perf_counter()

    try:
        while True:
            frame = camera.read()
            if frame is None:
                failures += 1
                if failures >= 30:
                    print("连续 30 帧读取失败，退出。检查摄像头是否被占用。", file=sys.stderr)
                    return 2
                time.sleep(0.05)
                continue
            failures = 0
            frames += 1
            last_frame = frame

            faces = detector.detect(frame)
            annotated = frame.copy()
            draw_faces(annotated, faces)

            now = time.perf_counter()
            dt = max(now - last, 1e-6)
            inst_fps = 1.0 / dt
            fps = inst_fps if fps == 0 else (0.9 * fps + 0.1 * inst_fps)
            last = now

            if len(faces) != last_faces:
                print(f"\rFPS {fps:5.1f}  faces {len(faces)}", end="", flush=True)
                last_faces = len(faces)
            elif frames % 30 == 0:
                print(f"\rFPS {fps:5.1f}  faces {len(faces)}", end="", flush=True)

            if show:
                cv2.putText(
                    annotated,
                    f"FPS {fps:.1f}  faces {len(faces)}",
                    (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
                cv2.imshow("petbot face", annotated)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break

            if args.max_frames and frames >= args.max_frames:
                break
            if args.duration and (time.perf_counter() - started) >= args.duration:
                break
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        print()
        camera.close()
        if show:
            cv2.destroyAllWindows()
        if args.save and last_frame is not None:
            try:
                faces = detector.detect(last_frame)
                annotated = last_frame.copy()
                draw_faces(annotated, faces)
                cv2.imwrite(str(args.save), annotated)
                print(f"截图已保存：{args.save}（{len(faces)} 张人脸）")
            except Exception as exc:
                print(f"截图保存失败：{exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
