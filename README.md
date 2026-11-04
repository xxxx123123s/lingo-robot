# 智能桌宠（Raspberry Pi）

一台先“能看、能听、能说”，以后再加履带移动的桌宠。

> **当前优先级：人脸 + 语音；电机/履带后置。**
> 先让机器人坐在桌上和你对话，调试安全，也不需要先买底盘。
>
> **你的配置：Pi 4B + 32GB SD + CSI 摄像头。** 用 64 位 Raspberry Pi OS Bookworm；32GB 卡够用。

## 先看什么

1. **[人脸 + 语音优先路线](docs/方案-人脸语音优先.md)**：现在按这个做。
2. 在树莓派上运行硬件盘点：

   ```bash
   bash scripts/check_pi.sh
   ```

3. 摄像头人脸检测：

   ```bash
   python3 scripts/face_detect.py --source picamera --save artifacts/face.jpg
   ```

4. 录入并识别熟人：

   ```bash
   python3 scripts/face_db.py enroll --name xiaoming
   python3 scripts/face_db.py recognize --show
   ```

5. 文字/语音对话：

   ```bash
   cp .env.example .env      # 填入大模型 API Key
   python3 scripts/voice_chat.py --text
   python3 scripts/voice_chat.py --voice   # 需要 USB 耳麦 + Vosk
   ```

6. 电机部分暂时不用看；以后要装履带时再看 [完整硬件/电机方案](docs/方案.md)。

## 现在需要补什么

| 物品 | 参考价 | 用途 |
|---|---:|---|
| USB 耳麦（麦克风+喇叭一体） | 30–40 元 | 语音输入输出；Pi 4B 有 3.5mm 孔，但 USB 更稳 |
| 大模型 API Key | 按量计费 | DeepSeek / 通义千问，二选一 |
| Pi 4B 散热片/风扇（如果没有） | 15–30 元 | 视频 + 语音同时跑 |

履带、TB6612、电机电池都等语音和人脸稳定后再买。

> 如果你已经有 **INMP441 + MAX98357**，可以替代 USB 耳麦；但 Pi 4B 只有一组 I2S，需要配置设备树/ALSA。接线和退路见 [音频-INMP441-MAX98357.md](docs/音频-INMP441-MAX98357.md)。

## 当前路线

```text
F0 硬件盘点
 → F1 摄像头 + 人脸检测
 → F2 人脸录入 + 识别
 → F3 音频输入/输出
 → F4 文字对话 → TTS → 语音识别
 → F5 人脸 + 语音联动（叫名字、打招呼）
 → F6 网页/外壳
 → 最后：电机 + 履带
```

| 模块 | 第一版选型 | 说明 |
|---|---|---|
| 主控 | 你的树莓派 4B | 64 位系统；2GB 建议无桌面 + 云端语音，4GB 更从容 |
| 视觉 | 已有的摄像头 | CSI 用 `picamera2`，USB 用 OpenCV |
| 人脸检测 | OpenCV YuNet | 小而快，不用 dlib |
| 人脸识别 | OpenCV SFace | F1 稳定后再加 |
| ASR | Vosk 中文小模型 | 离线、便宜、够用；以后可换 SenseVoice |
| 对话 | DeepSeek / 通义千问 | OpenAI 兼容接口，按量计费 |
| TTS | Edge TTS | 自然；失败时可用 espeak-ng 兜底 |
| 唤醒 | 先回车/网页按钮 | 中文唤醒词最后再加 |
| 运动 | 后置：履带 + TB6612 | 等 F1–F5 通过后再做 |

## 安全底线（以后装电机时）

- 电机电池与树莓派电源分开，**只共地**；不要用树莓派 5V 给电机供电。
- 电机回路串保险丝，装一个能真正切断电机电源的急停/船型开关。
- 运动代码必须有 0.5 秒看门狗：命令超时、程序崩溃、Ctrl-C 都要停电机。
- 第一次测试架空；确认方向、限速、急停后再落地。

## 目录

```text
.
├─ README.md
├─ .env.example                 # 大模型 API 配置模板
├─ docs/
│  ├─ 方案-人脸语音优先.md       # 当前路线：F0–F6
│  ├─ 音频-INMP441-MAX98357.md   # I2S 麦克风 + 功放接线与 ALSA 配置
│  └─ 方案.md                   # 完整硬件/电机方案（后置）
├─ scripts/
│  ├─ check_pi.sh               # F0：系统/摄像头/音频/GPIO 盘点
│  ├─ face_detect.py            # F1：摄像头 + YuNet 人脸检测
│  ├─ face_db.py                # F2：SFace 人脸录入/识别
│  ├─ voice_chat.py             # F4：文字/语音对话
│  └─ test_motor.py             # 以后：TB6612 电机测试
├─ requirements-face.txt
└─ requirements-voice.txt
```

## 下一步

把下面几样发我，我帮你判断 F1 参数，再继续调 F2 阈值和 F4 麦克风：

1. `check_pi.sh` 输出；
2. `rpicam-hello --list-cameras` 的输出（确认是 `imx708` / `imx219` / `ov5647`）；
3. `face_detect.py` 的 FPS / faces 输出，或 `artifacts/face.jpg`；
4. 有没有 USB 耳麦；
5. 打算用 DeepSeek 还是通义千问。
