#!/bin/bash
# 后台安装 edge-tts，结果写 /tmp/pip2.log
echo "START $(date +%H:%M:%S)" > /tmp/pip2.log
pip3 install --user --break-system-packages edge-tts >> /tmp/pip2.log 2>&1
echo "DONE rc=$? $(date +%H:%M:%S)" >> /tmp/pip2.log
