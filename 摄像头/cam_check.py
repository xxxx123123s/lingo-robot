# -*- coding: utf-8 -*-
"""树莓派摄像头一键体检 + 抓图

用法：
    python cam_check.py            # 完整体检 + 抓一张图
    python cam_check.py --no-shot  # 只体检，不抓图

输出三段判定：
    [1] 网络/SSH
    [2] 控制通道（I2C 是否能找到传感器）
    [3] 数据通道（MIPI 是否能真正出帧）
"""
import base64
import os
import subprocess
import sys

import paramiko

HOST = "192.168.137.160"
USER = "pi"
PASS = "123"
REMOTE_JPG = "/tmp/snapshot.jpg"
LOCAL_JPG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.jpg")
SUDO = "echo 123 | sudo -S"


def run(c, cmd, timeout=60):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return out, err, stdout.channel.recv_exit_status()


def main():
    take_shot = "--no-shot" not in sys.argv

    print("[1] 网络连通性 ... ", end="", flush=True)
    r = subprocess.run(["ping", "-n", "1", "-w", "2000", HOST],
                       capture_output=True)
    if r.returncode != 0:
        print("失败  -> 检查网线/Wi-Fi，确认 IP 是 " + HOST)
        return 1
    print("OK")

    print("[2] SSH 登录 ... ", end="", flush=True)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, username=USER, password=PASS, timeout=10)
    except Exception as e:
        print("失败  -> " + str(e))
        return 1
    print("OK")

    try:
        out, _, _ = run(c, SUDO + " /usr/sbin/i2cdetect -y 10 2>/dev/null")
        line = [l for l in out.splitlines() if l.startswith("10:")]
        i2c_ok = "UU" in (line[0] if line else "") or "10 " in (line[0] if line else "")
        print("    控制通道 I2C: " + ("传感器应答 0x10 (UU) -> 正常"
                                    if i2c_ok else "无应答 -> 排线/供电/模块问题"))

        out, _, _ = run(c, "rpicam-hello --list-cameras 2>&1")
        listed = "imx219" in out
        print("    驱动枚举: " + ("imx219 已注册" if listed else "未找到摄像头"))
        if not listed:
            print("\n>> 结论：传感器没上线，先查排线（两端重插压实）与供电。")
            return 1

        if not take_shot:
            print("\n>> 结论：传感器在线，未做抓图测试。")
            return 0

        print("[3] 数据通道（抓图 1920x1080）... ", end="", flush=True)
        cmd = ("pkill -9 -f '[r]picam-still' 2>/dev/null; sleep 1; rm -f " + REMOTE_JPG +
               "; timeout 40 rpicam-still -n -o " + REMOTE_JPG +
               " --width 1920 --height 1080 --timeout 1500 >/tmp/cam.log 2>&1; "
               "ls -l " + REMOTE_JPG + " 2>/dev/null | awk '{print $5}'")
        size, _, _ = run(c, cmd, timeout=60)
        size = size.strip()

        if not size.isdigit():
            print("失败")
            out, _, _ = run(c, "tail -4 /tmp/cam.log")
            for l in out.splitlines():
                if l.strip():
                    print("    " + l.strip())
            print("\n>> 结论：控制通道正常但出不了帧 = MIPI 数据通道断了。")
            print("   换 15-pin / 1.0mm 树莓派摄像头专用排线（几块钱，类型要与原线一致）；")
            print("   仍不行换摄像头模块（Waveshare / Arducam 正规厂货）。")
            return 2

        print("OK (%s 字节)" % size)
        b64, _, _ = run(c, "base64 -w0 " + REMOTE_JPG)
        with open(LOCAL_JPG, "wb") as f:
            f.write(base64.b64decode(b64))
        print("    已保存: " + LOCAL_JPG)
        print("\n>> 结论：摄像头完全正常，可以接入桌宠主程序。")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
