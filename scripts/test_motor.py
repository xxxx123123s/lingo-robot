#!/usr/bin/env python3
"""M1: TB6612FNG 双电机安全测试。

在树莓派上运行：
    python3 scripts/test_motor.py --dry-run   # 只打印引脚，不动电机
    python3 scripts/test_motor.py --yes       # 架空后实测

默认接线（BCM 编号）：
    TB6612 STBY -> GPIO23
    AIN1/AIN2   -> GPIO5 / GPIO6
    PWMA        -> GPIO12
    BIN1/BIN2   -> GPIO16 / GPIO26
    PWMB        -> GPIO13

安全：
- 第一次必须把机器人架空，轮子离地。
- 脚本用 try/finally 保证退出、Ctrl-C、异常时停电机。
- 真机测试前请确认电机电池有保险丝和可切断的开关。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Pins:
    stby: int = 23
    ain1: int = 5
    ain2: int = 6
    pwma: int = 12
    bin1: int = 16
    bin2: int = 26
    pwmb: int = 13


class TB6612:
    """TB6612 双路电机，带方向、PWM 调速和统一停止。"""

    def __init__(self, pins: Pins, left_invert: bool = False, right_invert: bool = False) -> None:
        # 延迟导入：--dry-run 在没装 gpiozero 的电脑上也能运行。
        from gpiozero import DigitalOutputDevice, PWMOutputDevice

        self.pins = pins
        self.left_invert = left_invert
        self.right_invert = right_invert

        self.stby = DigitalOutputDevice(pins.stby, initial_value=False)
        self.ain1 = DigitalOutputDevice(pins.ain1, initial_value=False)
        self.ain2 = DigitalOutputDevice(pins.ain2, initial_value=False)
        self.pwma = PWMOutputDevice(pins.pwma, initial_value=0.0)
        self.bin1 = DigitalOutputDevice(pins.bin1, initial_value=False)
        self.bin2 = DigitalOutputDevice(pins.bin2, initial_value=False)
        self.pwmb = PWMOutputDevice(pins.pwmb, initial_value=0.0)

        # 初始化完成后再使能，避免上电瞬间乱转。
        self.stby.on()

    @staticmethod
    def _set_motor(in1, in2, pwm, speed: float) -> None:
        """speed: -1.0 ~ +1.0；0 为滑行停止。"""
        # 先关 PWM，再换方向，避免换向瞬间的电流冲击。
        pwm.value = 0.0
        if speed > 0:
            in1.on()
            in2.off()
        elif speed < 0:
            in1.off()
            in2.on()
        else:
            in1.off()
            in2.off()
        pwm.value = min(abs(speed), 1.0)

    def drive(self, left: float, right: float) -> None:
        """left/right: -1.0 ~ +1.0。"""
        left = -left if self.left_invert else left
        right = -right if self.right_invert else right
        self._set_motor(self.ain1, self.ain2, self.pwma, left)
        self._set_motor(self.bin1, self.bin2, self.pwmb, right)

    def stop(self) -> None:
        self.pwma.value = 0.0
        self.pwmb.value = 0.0
        self.ain1.off()
        self.ain2.off()
        self.bin1.off()
        self.bin2.off()

    def close(self) -> None:
        self.stop()
        self.stby.off()
        for device in (
            self.pwma,
            self.pwmb,
            self.ain1,
            self.ain2,
            self.bin1,
            self.bin2,
            self.stby,
        ):
            device.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TB6612 双电机安全测试")
    parser.add_argument("--stby", type=int, default=23, help="STBY 引脚，默认 GPIO23")
    parser.add_argument("--ain1", type=int, default=5, help="左电机 AIN1，默认 GPIO5")
    parser.add_argument("--ain2", type=int, default=6, help="左电机 AIN2，默认 GPIO6")
    parser.add_argument("--pwma", type=int, default=12, help="左电机 PWMA，默认 GPIO12")
    parser.add_argument("--bin1", type=int, default=16, help="右电机 BIN1，默认 GPIO16")
    parser.add_argument("--bin2", type=int, default=26, help="右电机 BIN2，默认 GPIO26")
    parser.add_argument("--pwmb", type=int, default=13, help="右电机 PWMB，默认 GPIO13")
    parser.add_argument("--speed", type=float, default=0.35, help="测试速度 0.1–0.7，默认 0.35")
    parser.add_argument("--seconds", type=float, default=1.0, help="每段持续时间，默认 1 秒")
    parser.add_argument("--left-invert", action="store_true", help="左电机方向取反")
    parser.add_argument("--right-invert", action="store_true", help="右电机方向取反")
    parser.add_argument("--dry-run", action="store_true", help="只打印引脚，不导入 gpiozero")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（用于自动化/远程）")
    return parser.parse_args()


def print_wiring(pins: Pins) -> None:
    print("TB6612 接线（BCM 编号）：")
    print(f"  STBY -> GPIO{pins.stby}")
    print(f"  AIN1 -> GPIO{pins.ain1}   AIN2 -> GPIO{pins.ain2}   PWMA -> GPIO{pins.pwma}")
    print(f"  BIN1 -> GPIO{pins.bin1}   BIN2 -> GPIO{pins.bin2}   PWMB -> GPIO{pins.pwmb}")
    print("  VCC  -> 3.3V   GND -> GND（与电池负极共地）   VM -> 电池+（经开关/保险丝）")


def confirm_run() -> bool:
    print()
    print("请确认：")
    print("  1. 机器人已经架空，轮子离地；")
    print("  2. 电机电池有开关/保险丝；")
    print("  3. 手边能立刻断电。")
    try:
        answer = input("输入 YES 继续，其他键退出: ").strip()
    except EOFError:
        return False
    return answer == "YES"


def run_sequence(motor: TB6612, speed: float, seconds: float) -> None:
    steps = [
        ("左轮正转", speed, 0.0),
        ("右轮正转", 0.0, speed),
        ("双轮正转", speed, speed),
        ("双轮反转", -speed, -speed),
        ("原地左转", -speed, speed),
        ("原地右转", speed, -speed),
    ]

    for label, left, right in steps:
        print(f"  {label:6s}  {seconds:.1f}s")
        motor.drive(left, right)
        time.sleep(seconds)
        motor.drive(0.0, 0.0)
        time.sleep(0.4)

    print("  停止")
    motor.stop()


def main() -> int:
    args = parse_args()
    pins = Pins(
        stby=args.stby,
        ain1=args.ain1,
        ain2=args.ain2,
        pwma=args.pwma,
        bin1=args.bin1,
        bin2=args.bin2,
        pwmb=args.pwmb,
    )

    print_wiring(pins)

    pin_values = [
        pins.stby,
        pins.ain1,
        pins.ain2,
        pins.pwma,
        pins.bin1,
        pins.bin2,
        pins.pwmb,
    ]
    if any(pin < 0 or pin > 27 for pin in pin_values):
        print("错误：BCM 引脚必须在 0–27 之间。", file=sys.stderr)
        return 2
    if len(set(pin_values)) != len(pin_values):
        print("错误：有重复引脚，请检查参数。", file=sys.stderr)
        return 2

    if args.dry_run:
        print("\n--dry-run：未导入 gpiozero，未操作 GPIO。")
        return 0

    speed = max(0.1, min(float(args.speed), 0.7))
    seconds = max(0.1, min(float(args.seconds), 3.0))
    if speed != args.speed:
        print(f"注意：速度已从 {args.speed} 限制到 {speed}（测试上限 0.7）。")
    if seconds != args.seconds:
        print(f"注意：时长已从 {args.seconds} 限制到 {seconds} 秒。")

    if not args.yes and not confirm_run():
        print("已取消。")
        return 0

    try:
        motor = TB6612(pins, args.left_invert, args.right_invert)
    except ImportError as exc:
        print(f"\n无法导入 gpiozero：{exc}", file=sys.stderr)
        print("安装：sudo apt install python3-gpiozero python3-lgpio", file=sys.stderr)
        print("Pi 5 如仍失败，可试：GPIOZERO_PIN_FACTORY=lgpio python3 scripts/test_motor.py --yes", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nGPIO 初始化失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("检查接线、引脚是否被占用，以及是否装了 lgpio。", file=sys.stderr)
        return 2

    print("\n开始测试，随时 Ctrl-C 停止。")
    try:
        run_sequence(motor, speed, seconds)
        print("测试完成。")
        return 0
    except KeyboardInterrupt:
        print("\n已中断，正在停止电机……")
        return 130
    finally:
        motor.close()
        print("电机已停止，STBY 已拉低。")


if __name__ == "__main__":
    sys.exit(main())
