#!/bin/sh
# 用法: ./fanprobe.sh [占空比]   默认 0.70
DUTY=${1:-0.70}
cd /home/pi || exit 1
rm -f /tmp/uv.log /tmp/fantest.log

setsid nohup sh -c 'i=0; while [ $i -lt 220 ]; do printf "%s " "$(vcgencmd get_throttled | cut -d= -f2)"; i=$((i+1)); sleep 0.15; done > /tmp/uv.log 2>&1' >/dev/null 2>&1 &
sleep 0.3
FAN_MAX="$DUTY" setsid nohup python3 fan.py --test > /tmp/fantest.log 2>&1 &
echo "started duty=$DUTY, 33s 后取结果"
