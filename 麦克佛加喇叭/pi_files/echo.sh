#!/bin/bash
# ============================================
# 麦克风 -> 耳机 实时回放（对着麦说话，耳机立刻听到自己）
# 用法:  ./echo.sh            默认参数
#        RATE=48000 ./echo.sh 换采样率
#        PERIOD=256 ./echo.sh 更低延迟（若有爆音就调大）
# 停止:  Ctrl+C
# ============================================
set -u
CARD_IN=${CARD_IN:-plughw:3,0}    # USB 麦克风
CARD_OUT=${CARD_OUT:-plughw:2,0}  # 板载 3.5mm 耳机口
RATE=${RATE:-16000}
PERIOD=${PERIOD:-512}             # 每块帧数，越小延迟越低
BUF=${BUF:-2048}                  # 总缓冲帧数

echo "输入: $CARD_IN   输出: $CARD_OUT   ${RATE}Hz  period=$PERIOD  buffer=$BUF"
echo "延迟约 $((BUF * 1000 / RATE)) ms  —— 按 Ctrl+C 停止"

trap 'kill 0 2>/dev/null; exit 0' INT TERM

arecord -D "$CARD_IN"  -f S16_LE -r "$RATE" -c 1 -t raw -q \
        --period-size="$PERIOD" --buffer-size="$BUF" \
| aplay  -D "$CARD_OUT" -f S16_LE -r "$RATE" -c 1 -t raw -q \
        --period-size="$PERIOD" --buffer-size="$BUF"
