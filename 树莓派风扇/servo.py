#!/usr/bin/env python3
"""SG90 舵机 —— 把风扇转向 CPU / 收回一边

接线（树莓派 4B 40 脚编号）：
    SG90 棕线 GND  → Pin 6  (GND)
    SG90 红线 VCC  → Pin 2  (5V)    ← 不要用 Pin 4，那是电机 VM
    SG90 橙线 信号 → Pin 33 (GPIO13)

为什么信号必须走 GPIO13：
    树莓派只有 PWM0 / PWM1 两个硬件通道，且只出现在 GPIO12/13/18/19 上。
    12 被风扇调速占了（8kHz），18 是屏幕背光，19 和 13 同属 PWM1、不能各走各的。
    剩下的就是 13 —— 走 PWM1，和风扇的 PWM0 是两套独立定时器，互不干扰。

为什么不直接用 gpiozero 的 Servo：
    它在这块脚上会判断"没有硬件 PWM"，自动退回 SoftwarePWM，并警告
    "To reduce servo jitter, use the pigpio pin factory"。软件 PWM 的脉宽
    受 CPU 调度影响，舵机会原地哆嗦、嗡嗡响，负载一变还会突然抽一下。
    实测 lgpio.tx_pwm 在 GPIO13 上是走硬件 PWM 的，那就直接调底层。

    （不要为了这个去装 pigpio：pigpiod 会接管全部 GPIO，和风扇、屏幕那两路
      lgpio 打架，收益不值这个风险。）

SG90 的脉宽约定：0.5ms = 0°，1.5ms = 90°，2.5ms = 180°。
便宜的 SG90 实际行程普遍差 5~10°，两端还会顶到机械限位发出"咯咯"声，
所以**不要一上来就试 0 和 180**，从 90 开始，每次 15° 往外试。

用法：
    python3 servo.py --angle 90            转到 90° 并保持（Ctrl-C 退出）
    python3 servo.py --angle 45 --hold 3   转过去停 3 秒后松开
    python3 servo.py --sweep               30→150→30 慢扫，看清实际行程
    python3 servo.py --detach              松开，不再发力（不抖、不耗电、不发热）

注意：舵机堵转电流能到 700mA 以上，树莓派 5V 那路有余量但不多。
      如果一转舵机树莓派就重启，`vcgencmd get_throttled` 会显示欠压，
      解决办法是在舵机电源附近并一个 100~470µF 电解电容。
"""
import argparse
import os
import time

import lgpio

PIN_SERVO = int(os.environ.get("SERVO_PIN", "13"))
FREQ = 50.0                                        # SG90 标准 20ms 周期
# SG90 的脉宽范围。写全是为了能单独校准：有些舵机 0.4/2.6 才走满，
# 有些 0.6/2.4 就顶住了。改这两个数比改角度更接近"校准"。
MIN_US = float(os.environ.get("SERVO_MIN_US", 500))
MAX_US = float(os.environ.get("SERVO_MAX_US", 2500))
PERIOD_US = 1e6 / FREQ                             # 20000µs


class Servo:
    """硬件 PWM 驱动的 SG90。angle() 不发命令就一直保持力矩"""

    def __init__(self, pin=PIN_SERVO):
        self.pin = pin
        self.h = lgpio.gpiochip_open(0)
        try:
            lgpio.gpio_claim_output(self.h, pin, 0)
        except Exception:                          # noqa: BLE001
            pass                                   # 已经被占用时 tx_pwm 仍可试
        self.now = None

    def pulse_us(self, angle):
        return MIN_US + (MAX_US - MIN_US) * max(0.0, min(180.0, angle)) / 180.0

    def angle(self, a):
        a = max(0.0, min(180.0, float(a)))
        us = self.pulse_us(a)
        # 实参顺序是 (频率Hz, 占空比%)，别按 sg90 的习惯想成先占空比。
        # 传反了会发出"7.5Hz、50% 占空比"——每半个周期 66ms 的高电平，
        # 远超 SG90 能接受的 2.5ms，舵机会直接顶到一边死扛。
        lgpio.tx_pwm(self.h, self.pin, FREQ, us / PERIOD_US * 100.0)
        self.now = a
        print(f"→ {a:.0f}°（脉宽 {us / 1000:.2f}ms，占空比 {us / PERIOD_US * 100:.2f}%）",
              flush=True)
        return a

    def detach(self):
        """松开：占空比给 0，频率照旧 → 输出恒为低，等于没有脉冲。

        文档说频率传 0 就是 off，但那样 lgpio 会拿 0 去算周期微秒，
        抛 'bad PWM micros'。所以只能靠 0% 占空比来表达"别发脉冲"。"""
        lgpio.tx_pwm(self.h, self.pin, FREQ, 0)
        self.now = None

    def close(self):
        try:
            self.detach()
        finally:
            lgpio.gpiochip_close(self.h)


def cmd_sweep(servo):
    print("慢扫 30° → 150° → 30°，每步 8°，停 0.4 秒。\n"
          "注意听：顶到限位会有'咯咯'的堵转声，那两头的角度就是不能用。\n", flush=True)
    try:
        for a in list(range(30, 151, 8)) + list(range(150, 29, -8)):
            servo.angle(a)
            print(f"  {a:3d}°", end="\r", flush=True)
            time.sleep(0.4)
        print("\n扫完。", flush=True)
    except KeyboardInterrupt:
        print("\n中断。", flush=True)


def main():
    ap = argparse.ArgumentParser(description="SG90 舵机测试（硬件 PWM）")
    ap.add_argument("--angle", type=float, default=None, help="目标角度 0~180")
    ap.add_argument("--hold", type=float, default=0,
                    help="保持几秒后松开；0 = 一直保持")
    ap.add_argument("--sweep", action="store_true", help="慢扫一遍看机械行程")
    ap.add_argument("--detach", action="store_true", help="松开不再发力")
    ap.add_argument("--pin", type=int, default=PIN_SERVO, help="信号脚，默认 GPIO13")
    args = ap.parse_args()

    servo = Servo(args.pin)
    try:
        if args.detach:
            servo.detach()
            print("已松开。舵机不再发力，可以用手轻轻转（别硬拧）")
        elif args.sweep:
            cmd_sweep(servo)
            servo.detach()
            print("已松开")
        else:
            servo.angle(90.0 if args.angle is None else args.angle)
            try:
                if args.hold > 0:
                    time.sleep(args.hold)
                    servo.detach()
                    print("已松开")
                else:
                    print("保持中，Ctrl-C 退出", flush=True)
                    while True:
                        time.sleep(1)
            except KeyboardInterrupt:
                print()
    finally:
        servo.close()


if __name__ == "__main__":
    main()
