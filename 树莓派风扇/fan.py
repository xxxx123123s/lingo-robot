#!/usr/bin/env python3
"""CPU 温控风扇 —— DRV8833 + 5V 小电机

===== 重要：TB6612 和 DRV8833 不是一回事 =====
TB6612 是「PWMA 单独调速 + AIN1/AIN2 管方向 + STBY 使能」四根线。
DRV8833 只有 IN1/IN2 两个输入，**没有 PWMA，也没有 STBY**（只有 nSLEEP 休眠脚）。
照 TB6612 的接法把 PWM 打在一个脚、方向脚一高一低，IN1 会一直有电平、
IN2 一直为高 —— 芯片直接导通，马达全速跑，PWM 根本不起作用。
所以设 12% 和设 25% 听到的一样大。这就是"上电就跑、不管温度"的原因。

DRV8833 正确接法（单路，树莓派 4B 40 脚编号）：
    DRV8833 VM    → Pin 4  (5V)
    DRV8833 VCC   → Pin 1  (3V3)     逻辑供电
    DRV8833 GND   → Pin 6  (GND)
    DRV8833 SLP   → Pin 1  (3V3)     直接短接 3V3，不要接 GPIO
    DRV8833 AIN1  → Pin 32 (GPIO12)  ← 打 PWM 的就是这一根
    DRV8833 AIN2  → Pin 6  (GND)     ← 固定低，滑行端，不要接 GPIO
    DRV8833 AO1 / AO2 → 电机两根线

    要点：PWM 必须打在 IN 上，IN2 必须固定为低。
    IN1=PWM / IN2=0 → 单极性驱动，占空比就是电压比例，线性和静音都最好。
    IN1=PWM / IN2=1 → 每个周期都在"刹车"，同样转速但电流大、芯片发烫。

    GPIO23 / GPIO27 这两根线在 DRV8833 上不接；程序仍会把它们
    输出低/高，只是防止悬空，不影响驱动。

用法：
    python3 fan.py            按温度自动调速，前台运行
    python3 fan.py --test     软启动转 6 秒，用来验证接线
    python3 fan.py --on       全速，Ctrl-C 退出
    python3 fan.py --off      立刻停
    python3 fan.py --status   报温度、电源状况和判断结果，不碰 GPIO

阈值可以用环境变量改，不用改代码：
    FAN_ON=45 FAN_MAX=1.0 python3 fan.py
"""
import argparse
import os
import signal
import subprocess
import sys
import time

TEMP_FILE = "/sys/class/thermal/thermal_zone0/temp"

PIN_PWM, PIN_AIN1, PIN_AIN2 = 12, 23, 27

ON_TEMP = float(os.environ.get("FAN_ON", 40))      # 高于它起转
OFF_TEMP = float(os.environ.get("FAN_OFF", 35))    # 低于它停转（迟滞 5℃）
# 迟滞从 3℃ 拉到 5℃：现在满速时一分钟能降 3~4℃，3℃ 迟滞会在 40 附近
# 反复启停，每次启停都要踢一脚（80%），既吵又费电。
# 一度改成 52/45，理由是 4B 空闲就在 40~45℃，40 起转等于常开。现在接对了、
# 占空比真的能调了，风小电也省，改回 40 起转：热一点就动，演示时看得见。
# 唯一的副作用是温度在 40 上下晃时会启停抖动 —— 真遇到就 FAN_OFF=35 重启服务，
# 把迟滞拉到 5℃。
MIN_DUTY = float(os.environ.get("FAN_MIN", 0.45))  # 下限 = 一开就明显在转的转速
# 12% ≈ 0.6V 是转不动的：马达堵在那里、电流反而大，芯片会更烫，
# 还伴有"嗡嗡"声。20%/25%/32% 都偏弱 —— 能转，但肉眼几乎看不出在动。
# 45% ≈ 2.25V 才是一开就"看得出在转"的位置。代价是声音大，用户接受。
MAX_DUTY = float(os.environ.get("FAN_MAX", 1.00))  # 上限，直接放到顶（5V 全速）
# 一度只敢给 0.45，是怕重演"芯片烫手"。但那次烫的根本不是电压，是接错成
# 刹车态：IN1/IN2 同时有效 = 电机两端被短路，电流全变成热，转速还不高。
# 现在 IN2 固定为低走单极性滑行，5V 全速时电流是正常值（空载 0.1~0.2A），
# DRV8833 的 MOSFET Rds_on ≈ 0.3Ω，功耗不到 0.05W，凉得很。
# 代价只剩噪音和寿命 —— 用户接受，那就给到顶。
# 之前是 0.65/0.85（按 5V 风扇设的）。85% = 4.3V 对小马达确实是超压，
# 但现在 100% 只有 5V，而且用户就是要快。

# 起转的「踢一脚」。直流小马达的静摩擦远大于动摩擦：从 0 慢慢爬到 30%
# 时它前半段根本不转，电全变成线圈发热和嗡嗡声，等爬过起转点才动 ——
# 这就是"启动困难"。所以起转瞬间给一个大值（KICK）压过静摩擦，
# 转稳了再滑回目标占空比。
KICK_DUTY = float(os.environ.get("FAN_KICK", 0.95))   # 起转瞬间的占空比（≈4.75V）
KICK_SECS = float(os.environ.get("FAN_KICK_T", 1.0))  # 保持多久
RAMP_SECS = float(os.environ.get("FAN_RAMP", 2.0))    # 从 KICK 滑回目标用几秒
FULL_TEMP = float(os.environ.get("FAN_FULL", 50))  # 到这里用到 MAX_DUTY
# 原来 70℃，太晚了 —— 4B 正常负载就在 45~55℃，等到 70 才给满速等于没给。
# 50℃ 以上全速，46℃ 已经在 70% 档。

# 开机默认状态：**一律先关着，等人工点开**。
# 为什么不用"按温度自动"打底：断电重开时 SoC 还是热的（刚跑过 50℃ 以上），
# 一上电温度就过阈值，温控逻辑立刻拉满 —— 而那正是开机最脆弱的时候
# （SD 卡在写、USB 在枚举、电源还没稳），多一路 5V 大电流纯属给自己找事。
# 之前 2A 电源就是在这个时刻欠压重启、把 SD 卡写坏的。
# 所以服务每次启动（开机、重启、systemctl restart）都先把模式写成 off，
# 屏幕显示「手动·关」、舵机收回到收起位，要吹就点屏幕。
# 想恢复"开机就按温度自动跑"：FAN_BOOT_MODE=auto。
BOOT_MODE = os.environ.get("FAN_BOOT_MODE", "off").strip().lower()
if BOOT_MODE not in ("auto", "on", "off"):
    BOOT_MODE = "off"

# ---------------------------------------------------------------- 舵机
# SG90 负责把风扇摆到 CPU 上方 / 收起来。它和风扇用的是两套独立的硬件 PWM
# 通道（舵机 PWM1/GPIO13，风扇 PWM0/GPIO12），互不干扰。
SERVO_ENABLE = os.environ.get("FAN_SERVO", "1").strip().lower() not in (
    "0", "false", "no", "off")
ANGLE_ON = float(os.environ.get("FAN_ANGLE_ON", 120))    # 吹风位
ANGLE_OFF = float(os.environ.get("FAN_ANGLE_OFF", 0))    # 收起位
# 为什么不让舵机跟着每次风扇启停动：40℃ 起转、35℃ 停，空闲时可能一分钟
# 启停一次，舵机就跟着来回摆一次，一天上千次 —— SG90 的塑料齿轮撑不了几天。
# 所以只在「工作 / 收起」这两态之间动，一天两次。
PWM_HZ = int(os.environ.get("FAN_HZ", 8000))       # DRV8833 可到 100kHz，lgpio 上限 10kHz
# 原来 1000Hz —— 这正好是人耳最敏感的频段，直流电机会把方波变成刺耳的
# "吱——"，占空比降到 6% 也一样响。提到 8kHz 基本就听不见了，且不改变
# 等效电压，属于纯收益。lgpio 的软件 PWM 超过 10kHz 会报错。
#
# 驱动芯片型号。TB6612 是「PWMA 调速 + AIN1/AIN2 定向 + STBY 使能」，
# DRV8833 只有 IN1/IN2，没有独立的 PWM 脚和 STBY。两者的电平含义完全不同，
# 接错的表现就是"马达一直全速，调什么都没反应"。
DRIVER = os.environ.get("FAN_DRV", "drv8833").strip().lower()
# 转向。TB6612 模式靠 AIN1/AIN2 换相；DRV8833 模式 IN2 固定为低，
# 没有软件换向的余地 —— 要反风直接把 AO1/AO2 两根线对调。
REVERSE = os.environ.get("FAN_REV", "1").strip().lower() in ("1", "true", "yes", "on")
STEP = 0.03                                        # 占空比变化小于 3% 不动
TICK = 0.4                                         # 主循环节拍。屏幕点一下要 0.4 秒内
#   有反应，所以循环跑得比温控需要的快；vcgencmd 是 fork 子进程，单独节流到 2 秒一次

_DRIVER = {}

for _p in ("/home/pi", os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.append(_p)


def cpu_temp():
    """读 SoC 温度，单位 ℃。读不到返回 None"""
    try:
        with open(TEMP_FILE) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def throttled():
    """PMIC 状态位。低 4 位是“此刻”，高 4 位是“曾经发生过”"""
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"],
                                      text=True, timeout=2)
        return int(out.strip().split("=")[1], 16)
    except Exception:                                  # noqa: BLE001
        return 0


def under_voltage_now():
    """bit0 = 当前正在欠压。bit16 是历史上欠过压，那个会一直亮，不能用来判断"""
    return bool(throttled() & 0x1)


def describe_throttled(bits):
    if not bits:
        return "正常"
    now = []
    if bits & 0x1:
        now.append("正在欠压")
    if bits & 0x2:
        now.append("正在降频")
    if bits & 0x4:
        now.append("正在限流")
    if bits & 0x8:
        now.append("温度限制")
    hist = []
    if bits & 0x10000:
        hist.append("曾欠压")
    if bits & 0x20000:
        hist.append("曾降频")
    if bits & 0x40000:
        hist.append("曾限流")
    parts = now + hist
    return "、".join(parts) if parts else f"0x{bits:x}"


def duty_for(t, ceiling):
    """温度 → 占空比。ON_TEMP 处给 MIN_DUTY，FULL_TEMP 处给 ceiling，中间线性"""
    if t >= FULL_TEMP:
        return ceiling
    span = max(1.0, FULL_TEMP - ON_TEMP)
    d = MIN_DUTY + (t - ON_TEMP) / span * (ceiling - MIN_DUTY)
    return max(MIN_DUTY, min(ceiling, d))


def drive_dir(reverse=None):
    """把两根非 PWM 的脚摆到「能正常调速、且停得下来」的状态。

    DRV8833：只能 IN1 打 PWM、IN2 固定为低。两者的电平含义和 TB6612 完全
        不同，这里必须**两个脚都拉低**：
          · IN2 拉高 → 每个周期都在刹车，转速一样但电流大、芯片烫
          · 要是 IN1/IN2 直接连在这两个脚上，一高一低就是全速导通，
            PWM 打多少都没用 —— 这正是"上电就跑、降到 40℃ 也不停"的原因
        软启动/停转期间 PWM=0，IN1、IN2 都为低，马达才是真正断电滑行。
        SLP(nSLEEP) 必须硬接 3V3，**不能接 GPIO27**，否则拉低就休眠。
        软件无法换向，要反风就对调 AO1/AO2。

    TB6612：AIN1/AIN2 一高一低定方向，PWMA 单独调速。"""
    if DRIVER == "drv8833":
        _DRIVER["ain1"].off()
        _DRIVER["ain2"].off()
        return
    if reverse is None:
        reverse = REVERSE
    if reverse:
        _DRIVER["ain1"].off()
        _DRIVER["ain2"].on()
    else:
        _DRIVER["ain1"].on()
        _DRIVER["ain2"].off()


class Fan:
    """GPIO 只在需要时初始化，--status 不碰硬件"""

    def __init__(self):
        try:
            from gpiozero import DigitalOutputDevice, PWMOutputDevice
        except ImportError:
            raise SystemExit("没装 gpiozero：sudo apt install -y python3-gpiozero")

        _DRIVER["ain1"] = DigitalOutputDevice(PIN_AIN1)
        _DRIVER["ain2"] = DigitalOutputDevice(PIN_AIN2)
        _DRIVER["pwm"] = PWMOutputDevice(PIN_PWM, frequency=PWM_HZ, initial_value=0)
        drive_dir()                           # 先把非 PWM 脚摆好，再动 PWM
        self.on = False
        self.duty = 0.0
        self.ceiling = MAX_DUTY               # 欠压时会被一档档压下来

    def ramp_start(self, duty, secs=RAMP_SECS):
        """起转要「踢一脚」，不是慢慢爬。

        小马达的静摩擦远大于动摩擦：从 0 平滑升到 20% 时，它在前半段
        纹丝不动，电全变成线圈发热和"嗡嗡"声，等爬过起转点才转 —— 表现
        就是"启动困难、要等好几秒"。而且原来的 6 秒软启动把这段时间拉得更长。

        所以改成三段：
          ① 0.3 秒快速冲到 KICK（不直跳是为了避免电流阶跃）
          ② 保持 KICK 0.6 秒 —— 这道冲击专门用来克服静摩擦
          ③ 再用 secs 平滑滑回目标占空比（不直接掉，否则可能又停转）
        只有从「停」到「转」走这套，运行中调速度走 update()，不会再踢。

        每段查一次欠压，一旦发现就退一档上限重来，最多试 3 次。"""
        target = 0.0
        for _ in range(3):
            target = round(min(self.ceiling, max(MIN_DUTY, duty)), 3)
            kick = min(1.0, max(KICK_DUTY, target))
            ok = True

            for i in range(1, 11):                        # ① 0.3s → kick
                _DRIVER["pwm"].value = round(kick * i / 10, 4)
                time.sleep(0.03)
            end = time.time() + KICK_SECS                 # ② 保持，冲过静摩擦
            while time.time() < end:
                if under_voltage_now():
                    ok = False
                    break
                time.sleep(0.05)
            if ok and target < kick and secs > 0:         # ③ 滑回目标
                steps = 20
                for i in range(1, steps + 1):
                    _DRIVER["pwm"].value = round(
                        kick + (target - kick) * i / steps, 4)
                    time.sleep(secs / steps)
                    if i % 5 == 0 and under_voltage_now():
                        ok = False
                        break

            if ok:
                self.duty = target
                self.on = True
                return True

            _DRIVER["pwm"].value = 0
            old = self.ceiling
            self.ceiling = max(MIN_DUTY, round(self.ceiling - 0.10, 2))
            print(f"[风扇] 升到 {target:.0%} 时欠压，上限 {old:.0%} → {self.ceiling:.0%}，"
                  f"退回去重试", flush=True)
            if self.ceiling <= MIN_DUTY and old <= MIN_DUTY:
                break
            time.sleep(2)

        self.stop()
        return False

    def update(self, duty):
        duty = round(min(self.ceiling, max(MIN_DUTY, duty)), 3)
        if abs(duty - self.duty) >= STEP:
            self.duty = duty
            _DRIVER["pwm"].value = duty

    def stop(self):
        _DRIVER["pwm"].value = 0
        self.duty = 0.0
        self.on = False

    def check_uv(self):
        """欠压就把上限降一档。返回是否真的降了"""
        if not under_voltage_now():
            return False
        old = self.ceiling
        self.ceiling = max(MIN_DUTY, round(self.ceiling - 0.10, 2))
        if self.on and self.duty > self.ceiling:
            self.duty = self.ceiling
            _DRIVER["pwm"].value = self.ceiling
        print(f"[风扇] 欠压！上限 {old:.0%} → {self.ceiling:.0%}"
              f"{'（已到底，电源实在不够）' if self.ceiling <= MIN_DUTY else ''}",
              flush=True)
        return True

    def close(self):
        self.stop()
        for dev in _DRIVER.values():
            try:
                dev.close()
            except Exception:                 # noqa: BLE001
                pass
        _DRIVER.clear()


def cmd_test():
    t = cpu_temp()
    print(f"当前 CPU 温度：{t:.1f}℃" if t else "读不到温度")
    print(f"电源状况：{describe_throttled(throttled())}")
    print(f"驱动：{DRIVER}")
    print(f"起转踢 {KICK_DUTY:.0%}×{KICK_SECS:.1f}s → 稳定在 {MAX_DUTY:.0%} → 停\n", flush=True)

    fan = Fan()
    try:
        # 第一步永远是"确认能停"。之前接错成一路直通时，PWM=0 马达照样全速，
        # 后面所有百分比都失去意义 —— 所以先空转 3 秒让你听。
        print("  ① 先不通电，3 秒 —— 这时候马达必须完全静止", flush=True)
        time.sleep(3)
        print("     还在转？那就是 IN 被拉死了：SLP 必须接 3V3、"
              "AIN2 必须接 GND、PWM 只打 AIN1\n", flush=True)

        print("  ② 升速中…", flush=True)
        if not fan.ramp_start(MAX_DUTY):
            print("\n起不来：电源带不动，需要加电容或换电源（见 README）", flush=True)
            return
        for _ in range(16):
            fan.check_uv()
            time.sleep(0.3)
        print(f"  ③ 已到 {fan.duty:.0%}，保持 6 秒", flush=True)
        time.sleep(6)
        fan.stop()
        time.sleep(1)
        print(f"\n结束。上限现在 {fan.ceiling:.0%}")
        print("转了 → 接线正确。中途重启 → 电源带不动，见 README 的“供电不够”。")
    finally:
        fan.close()


def cmd_jog(duty, secs, reverse=False):
    """点动：低占空比 + 短脉冲。专给硬件排查用 —— 电机要是堵转或短路，
    --test 那种在 70% 上连灌 6 秒的做法足以把 6612 烤掉。

    用法：先 sudo systemctl stop fan.service（服务占着引脚，不能抢），
    再 python3 fan.py --jog 2 --duty 0.4"""
    duty = max(0.0, min(1.0, duty))
    fan = Fan()
    try:
        drive_dir(REVERSE != reverse)         # --rev 是相对当前方向的“再反一次”
        _DRIVER["pwm"].value = duty
        tag = "反转" if (reverse and DRIVER != "drv8833") else "正转"
        if reverse and DRIVER == "drv8833":
            tag = "DRV8833 无软件换向，--rev 无效（对调 AO1/AO2）"
        print(f"点动 {secs:.1f}s @ {duty:.0%}（{tag}）"
              f" —— 趁现在量 AO1-AO2", flush=True)
        time.sleep(secs)
        _DRIVER["pwm"].value = 0
        print(f"已停。预期 AO1-AO2 ≈ {5.0 * duty:.1f}V。"
              f"量到 0V，或 AO1、AO2 对 GND 都是 5V → 输出级已坏", flush=True)
    finally:
        fan.close()


def cmd_on(duty):
    fan = Fan()
    try:
        if not fan.ramp_start(duty):
            print("起不来：电源带不动，需要加电容或换电源", flush=True)
            return
        print(f"运行中（{fan.duty:.0%}），Ctrl-C 停止", flush=True)
        while True:
            fan.check_uv()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        fan.close()
        print("已停")


def cmd_status():
    t = cpu_temp()
    bits = throttled()
    if t is None:
        print("读不到温度")
        return
    if t >= ON_TEMP:
        verdict = f"该转（≥{ON_TEMP:.0f}℃），占空比 {int(duty_for(t, MAX_DUTY) * 100)}%"
    elif t <= OFF_TEMP:
        verdict = f"该停（≤{OFF_TEMP:.0f}℃）"
    else:
        verdict = f"迟滞区，保持当前状态（{OFF_TEMP:.0f}~{ON_TEMP:.0f}℃）"
    print(f"CPU   {t:.1f}℃ → {verdict}")
    print(f"电源  {describe_throttled(bits)}  (0x{bits:x})")
    print(f"驱动  {DRIVER}    PWM 打在 GPIO12，IN2 固定低")
    print(f"阈值  起 {ON_TEMP:.0f}℃ / 停 {OFF_TEMP:.0f}℃ / 上限 {MAX_DUTY:.0%}")
    print(f"模式  {MODE_NAMES[read_mode()[0]]}（--mode auto/on/off 切换，或点屏幕上的风扇行）")


# ================================================================ 手动开关
# 触屏（hub.py）写 CMD_FILE 表达意图，本程序写 STATE_FILE 回报实际状态。
# 用一个文件而不是 socket，是因为两边一个是 root 服务、一个是 pi 用户，
# 文件权限最好控制，断线也不需要重连。
CMD_FILE = "/tmp/fan.cmd"
STATE_FILE = "/tmp/fan.state"

MODES = ("auto", "on", "off")
MODE_NAMES = {"auto": "自动", "on": "手动开", "off": "手动关"}


def read_mode():
    """读触屏写的模式，可以带一个临时占空比：`on` 或 `on 0.2`。
    文件不在或内容不对，一律退回自动 —— 宁可自己管，也不要因为一个坏文件
    把风扇永久钉死在某个状态。返回 (模式, 手动占空比或 None)。

    带数值这条是给现场调参用的：调风扇大小不用改代码重启，
    直接 echo "on 0.2" > /tmp/fan.cmd 就生效。"""
    try:
        with open(CMD_FILE) as f:
            parts = f.read().strip().lower().split()
    except OSError:
        return "auto", None
    if not parts or parts[0] not in MODES:
        return "auto", None
    if len(parts) > 1:
        try:
            return parts[0], max(0.0, min(1.0, float(parts[1])))
        except ValueError:
            pass
    return parts[0], None


def write_state(mode, duty, temp):
    """给界面读的一行：<模式> <占空比百分比> <温度>"""
    try:
        with open(STATE_FILE, "w") as f:
            f.write(f"{mode} {int(round(duty * 100))} {temp:.1f}\n")
    except OSError:
        pass


def ensure_files():
    """服务以 root 跑，建出来的是 root:root 644，pi 就写不进去了。
    cmd 要让 pi 可写，state 只要可读。"""
    for path, mode in ((CMD_FILE, 0o666), (STATE_FILE, 0o644)):
        try:
            open(path, "a").close()
            os.chmod(path, mode)
        except OSError:
            pass


def cmd_mode(m):
    """命令行直接切模式，方便不用屏幕也能测"""
    ensure_files()
    with open(CMD_FILE, "w") as f:
        f.write(m + "\n")
    print(f"已设为「{MODE_NAMES[m]}」")


def servo_to(servo, angle):
    """把舵机转到指定角度。已经在那个位置就不动。

    反复发同一条 tx_pwm 命令看着无害，实际上每发一次 PWM 队列就多一条，
    舵机会在原地极轻微地哆嗦一下。只在真正要换位置时发。"""
    if servo is None or servo.now == angle:
        return
    try:
        servo.angle(angle)
    except Exception as e:                    # noqa: BLE001
        print(f"[舵机] 转到 {angle:.0f}° 失败：{e}", flush=True)


def cmd_run():
    fan = Fan()
    stopping = {"v": False}

    def bye(*_):
        stopping["v"] = True

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    ensure_files()
    print(f"[风扇] 驱动 {DRIVER} / 起转 {ON_TEMP:.0f}℃ / 停 {OFF_TEMP:.0f}℃ "
          f"/ 全速 {FULL_TEMP:.0f}℃ / 上限 {MAX_DUTY:.0%} "
          f"/ 起转踢 {KICK_DUTY:.0%}×{KICK_SECS:.1f}s / PWM {PWM_HZ}Hz", flush=True)

    # 开机/重启一律先关着。写在模式文件里而不是只存在内存里，是为了让屏幕
    # 也看到真实状态（显示「手动·关」），而不是屏幕上还挂着上次的「自动 68%」。
    try:
        with open(CMD_FILE, "w") as f:
            f.write(BOOT_MODE + "\n")
    except OSError:
        pass

    # 舵机是可选的：没接、没装库、或者用户明确关掉，都不能影响风扇本身。
    # 所以这里的任何异常都只打印一行，然后 servo 保持 None 继续往下跑。
    #
    # 先定模式再初始化舵机：不然舵机会先摆在吹风位、第一拍才发现模式是
    # "手动关"，白摆一次。
    mode, man = BOOT_MODE, None
    servo = None
    if SERVO_ENABLE:
        try:
            from servo import Servo
            servo = Servo()
            servo.angle(ANGLE_OFF if mode == "off" else ANGLE_ON)
            print(f"[舵机] 已到{'收起位' if mode == 'off' else '吹风位'} "
                  f"{ANGLE_OFF if mode == 'off' else ANGLE_ON:.0f}°"
                  f"（吹风 {ANGLE_ON:.0f}° / 收起 {ANGLE_OFF:.0f}°）",
                  flush=True)
        except Exception as e:                # noqa: BLE001
            print(f"[舵机] 不可用：{e} —— 风扇照常工作", flush=True)
            servo = None

    print(f"[风扇] 开机初始模式：{MODE_NAMES[mode]}"
          + ("（不会自己起转，点屏幕上的风扇行手动开）" if mode == "off" else ""),
          flush=True)

    last_log = 0.0
    last_state = None
    next_try = 0.0
    try:
        tick = 0
        while not stopping["v"]:
            tick += 1
            probe_uv = tick % 5 == 0        # 每 5 拍（2 秒）查一次欠压
            t = cpu_temp()
            if t is None:                     # 读不到就维持现状，不瞎猜
                time.sleep(TICK)
                continue

            m, md = read_mode()
            target = md if md is not None else MAX_DUTY
            if m != mode or (m == "on" and md is not None and md != man):
                if m != mode:
                    print(f"[风扇] 模式 {MODE_NAMES[mode]} → {MODE_NAMES[m]}", flush=True)
                else:
                    print(f"[风扇] 手动占空比 → {target:.0%}", flush=True)
                mode, man = m, md
                # 先把新状态报给界面，再去爬升。软启动要 6 秒，
                # 不然用户点完得盯着不动 6 秒，像是没点上
                if mode == "on":
                    servo_to(servo, ANGLE_ON)     # 先摆到位，再吹
                    write_state(mode, target, t)
                    # 只改数值时别重走软启动，否则会先掉到 0 再爬，看起来像停了一下
                    if not fan.on:
                        if not fan.ramp_start(target):
                            print("[风扇] 手动开：起不来（电源带不动），30 秒后重试", flush=True)
                            next_try = time.time() + 30
                    else:
                        fan.update(target)
                elif mode == "off":
                    fan.stop()                    # 先停扇，再把机身收回去
                    servo_to(servo, ANGLE_OFF)
                    write_state(mode, 0.0, t)
                    last_state = False
                else:
                    servo_to(servo, ANGLE_ON)     # 自动模式 = 在工作，摆到位等温控
                    write_state(mode, fan.duty if fan.on else 0.0, t)

            if mode == "on":
                # 手动开：不看温度，保持目标转速
                if not fan.on and time.time() >= next_try:
                    if not fan.ramp_start(target):
                        next_try = time.time() + 30
                elif fan.on:
                    fan.update(target)
                if probe_uv:
                    fan.check_uv()
            elif mode == "off":
                pass                          # 手动关：什么都不做，等用户切回来
            elif not fan.on and t >= ON_TEMP and time.time() >= next_try:
                want = duty_for(t, fan.ceiling)
                # 起转（踢一脚 + 滑回）要阻塞近 3 秒，先把这个目标报给界面，
                # 不然屏幕会停在旧值上，看着像没反应
                write_state(mode, want, t)
                if fan.ramp_start(want):
                    end = time.time() + 5      # 起来后 5 秒是浪涌期，密集看欠压
                    while time.time() < end and not stopping["v"]:
                        fan.check_uv()
                        time.sleep(0.3)
                else:
                    print("[风扇] 起不来（电源带不动），30 秒后再试", flush=True)
                    next_try = time.time() + 30
            elif fan.on and t <= OFF_TEMP:
                fan.stop()
            elif fan.on:
                fan.update(duty_for(t, fan.ceiling))
                if probe_uv:
                    fan.check_uv()

            write_state(mode, fan.duty if fan.on else 0.0, t)

            now = time.time()
            if fan.on != last_state or now - last_log > 600:
                tag = f"转 {int(fan.duty * 100)}%" if fan.on else "停"
                print(f"[风扇] {t:.1f}℃ → {tag}", flush=True)
                last_log = now
                last_state = fan.on

            time.sleep(TICK)
    finally:
        fan.close()
        # 退出前把机身收回去。等 0.5 秒是必须的：tx_pwm 只是"开始发脉冲"，
        # 舵机还要几百毫秒才走到位，这时候立刻 detach 会把它停在半路上。
        if servo is not None:
            try:
                servo_to(servo, ANGLE_OFF)
                if servo.now is not None:
                    time.sleep(0.5)
                servo.detach()
                servo.close()
            except Exception:                 # noqa: BLE001
                pass
        write_state("auto", 0.0, cpu_temp() or 0.0)
        print("[风扇] 已停止", flush=True)


def main():
    ap = argparse.ArgumentParser(description="树莓派 CPU 温控风扇")
    ap.add_argument("--test", action="store_true", help="软启动后全速 6 秒，验证接线")
    ap.add_argument("--on", action="store_true", help="持续运行")
    ap.add_argument("--off", action="store_true", help="立刻停止（临时，服务会再拉起来）")
    ap.add_argument("--mode", choices=MODES, help="设手动模式后退出：auto 自动 / on 常开 / off 常关")
    ap.add_argument("--status", action="store_true", help="只报状态，不碰 GPIO")
    ap.add_argument("--duty", type=float, default=MAX_DUTY, help="配合 --on，0~1")
    ap.add_argument("--jog", type=float, metavar="秒", default=None,
                    help="点动：低占空比短脉冲，硬件排查用（先停 fan.service）")
    ap.add_argument("--rev", action="store_true", help="配合 --jog 反转")
    args = ap.parse_args()

    if args.status:
        cmd_status()
    elif args.mode:
        cmd_mode(args.mode)
    elif args.jog is not None:
        cmd_jog(args.duty, max(0.1, args.jog), args.rev)
    elif args.test:
        cmd_test()
    elif args.on:
        cmd_on(max(0.0, min(1.0, args.duty)))
    elif args.off:
        fan = Fan()
        fan.close()
        print("已停")
    else:
        cmd_run()


if __name__ == "__main__":
    main()
