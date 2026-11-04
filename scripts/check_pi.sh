#!/usr/bin/env bash
# M0: 树莓派桌宠硬件盘点
# 用法：bash scripts/check_pi.sh
# 说明：只读取信息，不修改系统；某些命令不存在时会跳过。

set -u

section() {
  printf '\n========== %s ==========\n' "$1"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

section "1. 树莓派型号"
if [ -r /proc/device-tree/model ]; then
  tr -d '\0' < /proc/device-tree/model
  printf '\n'
else
  echo "读不到 /proc/device-tree/model（可能不是树莓派）"
fi

section "2. 系统与内核"
uname -a
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "发行版: ${PRETTY_NAME:-unknown}"
fi
echo "架构: $(uname -m)"
echo "Python: $(python3 --version 2>&1 || echo '未安装')"

section "3. 内存与磁盘"
free -h 2>/dev/null || echo "free 不可用"
df -h / 2>/dev/null || echo "df 不可用"

section "4. 温度与供电状态"
if have vcgencmd; then
  echo -n "温度: "
  vcgencmd measure_temp 2>/dev/null || echo "读取失败"
  echo -n "欠压标志: "
  vcgencmd get_throttled 2>/dev/null || echo "读取失败"
  echo "  （0x0 正常；出现 0x50000/0x50005 说明曾经欠压/降频）"
else
  echo "vcgencmd 不存在，跳过"
fi

section "5. 摄像头（CSI）"
CAMERA_TOOL=""
if have rpicam-hello; then
  CAMERA_TOOL="rpicam-hello"
elif have libcamera-hello; then
  CAMERA_TOOL="libcamera-hello"
fi

if [ -n "$CAMERA_TOOL" ]; then
  echo "使用 $CAMERA_TOOL --list-cameras："
  "$CAMERA_TOOL" --list-cameras 2>&1 | head -n 40
else
  echo "找不到 rpicam-hello 或 libcamera-hello。"
  echo "Raspberry Pi OS Bookworm 新版命令叫 rpicam-hello；旧版叫 libcamera-hello。"
fi

echo
echo "--- 摄像头自动检测配置 ---"
if [ -r /boot/firmware/config.txt ]; then
  grep -E 'camera_auto_detect|dtoverlay=.*(imx|ov5647)|camera' /boot/firmware/config.txt 2>/dev/null || true
elif [ -r /boot/config.txt ]; then
  grep -E 'camera_auto_detect|dtoverlay=.*(imx|ov5647)|camera' /boot/config.txt 2>/dev/null || true
else
  echo "读不到 /boot/firmware/config.txt 或 /boot/config.txt"
fi

echo
echo "--- /dev/video* ---"
if compgen -G "/dev/video*" >/dev/null; then
  ls -l /dev/video* 2>/dev/null
else
  echo "没有 /dev/video* 设备（CSI 摄像头不一定会出现在这里，这是正常的）"
fi

echo
echo "--- 最近的内核摄像头日志 ---"
if have dmesg; then
  dmesg 2>/dev/null | grep -iE 'imx219|imx708|ov5647|csi|unicam' | tail -n 15 || true
else
  echo "dmesg 不可用，跳过"
fi

section "6. 音频设备"
echo "--- 录音设备 ---"
if have arecord; then
  arecord -l 2>&1
else
  echo "arecord 不存在，安装：sudo apt install alsa-utils"
fi

echo
echo "--- 播放设备 ---"
if have aplay; then
  aplay -l 2>&1
else
  echo "aplay 不存在，安装：sudo apt install alsa-utils"
fi

section "7. GPIO / I2C / SPI"
echo "--- /dev/i2c-* ---"
ls -l /dev/i2c-* 2>/dev/null || echo "没有 I2C 设备（可在 raspi-config 里启用）"
echo "--- /dev/spidev* ---"
ls -l /dev/spidev* 2>/dev/null || echo "没有 SPI 设备（可在 raspi-config 里启用）"

section "8. Python 库"
if have python3; then
  python3 - <<'PY' 2>&1
mods = [
    ("gpiozero", "GPIO"),
    ("lgpio", "GPIO (Pi 5 推荐)"),
    ("picamera2", "CSI 摄像头"),
    ("cv2", "OpenCV"),
    ("numpy", "NumPy"),
    ("vosk", "离线语音识别"),
    ("edge_tts", "Edge TTS"),
]
for name, purpose in mods:
    try:
        mod = __import__(name)
        version = getattr(mod, "__version__", "unknown")
        print(f"OK   {name:12s} {version:12s} {purpose}")
    except Exception as exc:
        print(f"MISS {name:12s} {'':12s} {purpose}  ({type(exc).__name__}: {exc})")
PY
else
  echo "python3 不存在，跳过"
fi

section "9. 网络"
if have ip; then
  ip -brief addr 2>/dev/null || ip addr 2>/dev/null
else
  echo "ip 命令不存在"
fi

section "10. 建议的下一步"
cat <<'EOF'
1. 如果摄像头是 CSI 排线，先拍照确认：
   # 新版系统：
   rpicam-still -o ~/camera_test.jpg --width 1280 --height 720 -t 2000 -n
   # 旧版系统：
   libcamera-still -o ~/camera_test.jpg --width 1280 --height 720 -t 2000 -n
2. 录 3 秒并回放：
   arecord -f S16_LE -r 16000 -c 1 -d 3 ~/mic_test.wav && aplay ~/mic_test.wav
3. 如果 vcgencmd get_throttled 不是 0x0，先换 5V/3A 以上电源。
4. 把本脚本输出、摄像头照片、声卡列表发给我，再进入人脸检测。
EOF
