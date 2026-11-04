#!/usr/bin/env python3
"""F2：人脸录入 + 识别（SFace）。

先跑通 scripts/face_detect.py，再使用本脚本。

录入：
    python3 scripts/face_db.py enroll --name xiaoming

识别：
    python3 scripts/face_db.py recognize --show

数据保存在 faces/<名字>.npz，只存本地特征，不上传云端。
"""

from __future__ import annotations

import argparse
import re
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

from face_detect import (
    DEFAULT_MODEL,
    MODELS_DIR,
    FaceDetector,
    draw_faces,
    ensure_yunet,
    open_camera,
)

ROOT = Path(__file__).resolve().parent.parent
FACES_DIR = ROOT / "faces"
DEFAULT_SFACE = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
SFACE_URL = (
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/"
    "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
)


def ensure_sface(path: Path, allow_download: bool = True) -> Path:
    if path.exists() and path.stat().st_size > 1_000_000:
        return path

    if not allow_download:
        raise FileNotFoundError(
            f"找不到模型：{path}\n"
            "请手动下载：\n"
            f"  curl -L -o {path} {SFACE_URL}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    print(f"下载 SFace 模型（约 37MB）：{SFACE_URL}")
    try:
        request = urllib.request.Request(
            SFACE_URL, headers={"User-Agent": "petbot-face-db/1.0"}
        )
        with urllib.request.urlopen(request, timeout=180) as response, open(tmp, "wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"模型下载失败：{exc}\n"
            f"可以手动下载后放到：{path}\n"
            f"  curl -L -o {path} {SFACE_URL}"
        ) from exc

    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("SFace 模型文件太小，可能下载失败。")

    tmp.replace(path)
    print(f"模型已保存：{path}（{path.stat().st_size / 1024 / 1024:.1f} MB）")
    return path


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        raise ValueError("名字里没有可用的字符，换一个。")
    return cleaned[:40]


def normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


class SFace:
    def __init__(self, model: Path) -> None:
        if not hasattr(cv2, "FaceRecognizerSF"):
            raise RuntimeError(
                "当前 OpenCV 没有 FaceRecognizerSF，版本太旧。\n"
                f"当前版本：{cv2.__version__}\n"
                "请升级 OpenCV。"
            )
        self.recognizer = cv2.FaceRecognizerSF.create(str(model), "")

    def feature(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        aligned = self.recognizer.alignCrop(frame, face)
        feature = self.recognizer.feature(aligned)
        return normalize(feature)


class FaceDB:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.entries: list[tuple[str, np.ndarray]] = []

    def load(self) -> None:
        self.entries = []
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.npz")):
            try:
                with np.load(path, allow_pickle=False) as data:
                    name = str(data["name"])
                    embeddings = np.asarray(data["embeddings"], dtype=np.float32)
                if embeddings.ndim != 2 or embeddings.shape[1] != 128:
                    print(f"跳过格式不对的文件：{path}")
                    continue
                self.entries.append((name, normalize(embeddings.mean(axis=0))))
            except Exception as exc:
                print(f"跳过读取失败的文件 {path}：{exc}")
        if self.entries:
            print(f"已加载 {len(self.entries)} 个人脸：{', '.join(n for n, _ in self.entries)}")

    def save(self, name: str, embeddings: list[np.ndarray], force: bool = False) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{safe_name(name)}.npz"
        if path.exists() and not force:
            raise FileExistsError(f"{path} 已存在；加 --force 覆盖。")
        stack = np.stack([normalize(e) for e in embeddings]).astype(np.float32)
        np.savez_compressed(
            path,
            name=np.array(name),
            embeddings=stack,
            created_at=np.array(time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return path

    def match(self, feature: np.ndarray, threshold: float) -> tuple[str, float]:
        if not self.entries:
            return "unknown", 0.0
        best_name = "unknown"
        best_score = -1.0
        for name, mean in self.entries:
            score = float(np.dot(feature, mean))
            if score > best_score:
                best_name, best_score = name, score
        if best_score < threshold:
            return "unknown", best_score
        return best_name, best_score


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", choices=["auto", "picamera", "usb"], default="auto")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--score", type=float, default=0.6)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="YuNet 模型")
    parser.add_argument("--sface-model", type=Path, default=DEFAULT_SFACE)
    parser.add_argument("--no-download", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="人脸录入/识别（SFace）")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll = sub.add_parser("enroll", help="录入一个人")
    add_common_args(enroll)
    enroll.add_argument("--name", required=True, help="显示的名字，例如 xiaoming")
    enroll.add_argument("--samples", type=int, default=10, help="采集多少张，默认 10")
    enroll.add_argument("--interval", type=float, default=0.35, help="采样间隔秒数")
    enroll.add_argument("--timeout", type=float, default=40.0, help="最长等待秒数")
    enroll.add_argument("--force", action="store_true", help="覆盖同名记录")

    recognize = sub.add_parser("recognize", help="实时识别")
    add_common_args(recognize)
    recognize.add_argument("--threshold", type=float, default=0.363, help="余弦阈值，默认 0.363")
    recognize.add_argument("--show", action="store_true")
    recognize.add_argument("--save", type=Path, default=None)
    recognize.add_argument("--max-frames", type=int, default=0)
    recognize.add_argument("--duration", type=float, default=0)

    return parser.parse_args()


def run_enroll(args: argparse.Namespace) -> int:
    try:
        yunet = ensure_yunet(args.model, allow_download=not args.no_download)
        sface_model = ensure_sface(args.sface_model, allow_download=not args.no_download)
        detector = FaceDetector(yunet, args.width, args.height, args.score)
        sface = SFace(sface_model)
        camera = open_camera(args.source, args.camera, args.width, args.height)
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2

    samples = max(3, min(int(args.samples), 30))
    interval = max(0.2, min(float(args.interval), 2.0))
    timeout = max(5.0, float(args.timeout))
    embeddings: list[np.ndarray] = []

    print(f"录入：{args.name}")
    print("请正对摄像头，保持只有一张脸。3 秒后开始……")
    time.sleep(3.0)

    started = time.perf_counter()
    last_note = 0.0
    try:
        while len(embeddings) < samples and (time.perf_counter() - started) < timeout:
            frame = camera.read()
            if frame is None:
                time.sleep(0.05)
                continue

            faces = detector.detect(frame)
            if len(faces) != 1:
                now = time.perf_counter()
                if now - last_note > 1.0:
                    print(f"\r请保持画面里只有一张脸（检测到 {len(faces)} 张）……", end="", flush=True)
                    last_note = now
                time.sleep(0.1)
                continue

            face = faces[0]
            _x, _y, w, h = face[:4]
            if w < 60 or h < 60:
                now = time.perf_counter()
                if now - last_note > 1.0:
                    print("\r离摄像头近一点……", end="", flush=True)
                    last_note = now
                time.sleep(0.1)
                continue

            try:
                embeddings.append(sface.feature(frame, face))
            except Exception as exc:
                print(f"\n特征提取失败：{exc}")
                time.sleep(0.2)
                continue

            print(f"\r采集 {len(embeddings):2d}/{samples}", end="", flush=True)
            time.sleep(interval)
    finally:
        camera.close()

    print()
    if len(embeddings) < samples:
        print(f"只采集到 {len(embeddings)} 张，未保存。请改善光照/距离后重试。", file=sys.stderr)
        return 2

    db = FaceDB(FACES_DIR)
    try:
        path = db.save(args.name, embeddings, force=args.force)
    except Exception as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 2

    print(f"已保存 {len(embeddings)} 张特征：{path}")
    print("下一步：python3 scripts/face_db.py recognize --show")
    return 0


def run_recognize(args: argparse.Namespace) -> int:
    db = FaceDB(FACES_DIR)
    db.load()
    if not db.entries:
        print("还没有任何人脸记录。先运行：", file=sys.stderr)
        print("  python3 scripts/face_db.py enroll --name 你的名字", file=sys.stderr)
        return 2

    try:
        yunet = ensure_yunet(args.model, allow_download=not args.no_download)
        sface_model = ensure_sface(args.sface_model, allow_download=not args.no_download)
        detector = FaceDetector(yunet, args.width, args.height, args.score)
        sface = SFace(sface_model)
        camera = open_camera(args.source, args.camera, args.width, args.height)
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)

    show = args.show
    if show:
        try:
            cv2.namedWindow("petbot face db", cv2.WINDOW_NORMAL)
        except cv2.error as exc:
            print(f"无法打开窗口（可能没有桌面环境）：{exc}")
            show = False

    print("识别中，按 Ctrl-C 退出。")
    last_summary = ""
    frames = 0
    last_frame = None
    started = time.perf_counter()

    try:
        while True:
            frame = camera.read()
            if frame is None:
                time.sleep(0.05)
                continue

            frames += 1
            last_frame = frame
            faces = detector.detect(frame)
            labels: list[str] = []
            summary: list[str] = []

            for face in faces:
                try:
                    feature = sface.feature(frame, face)
                    name, score = db.match(feature, args.threshold)
                except Exception:
                    name, score = "unknown", 0.0
                labels.append(f"{name} {score:.2f}" if name != "unknown" else "unknown")
                summary.append(f"{name}:{score:.2f}")

            annotated = frame.copy()
            draw_faces(annotated, faces, labels)

            text = f"faces={len(faces)} " + (" ".join(summary) if summary else "")
            if text != last_summary:
                print(f"\r{text}", end="", flush=True)
                last_summary = text

            if show:
                cv2.imshow("petbot face db", annotated)
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
                labels = []
                for face in faces:
                    feature = sface.feature(last_frame, face)
                    name, score = db.match(feature, args.threshold)
                    labels.append(f"{name} {score:.2f}" if name != "unknown" else "unknown")
                annotated = last_frame.copy()
                draw_faces(annotated, faces, labels)
                cv2.imwrite(str(args.save), annotated)
                print(f"截图已保存：{args.save}")
            except Exception as exc:
                print(f"截图保存失败：{exc}", file=sys.stderr)

    return 0


def main() -> int:
    args = parse_args()
    if args.command == "enroll":
        return run_enroll(args)
    if args.command == "recognize":
        return run_recognize(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
