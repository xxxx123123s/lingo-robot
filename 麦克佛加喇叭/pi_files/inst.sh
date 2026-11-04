#!/bin/bash
# 后台安装 sherpa-onnx，结果写 /tmp/pip.log
echo "START $(date +%H:%M:%S)" > /tmp/pip.log
pip3 install --user --break-system-packages sherpa-onnx >> /tmp/pip.log 2>&1
echo "DONE rc=$? $(date +%H:%M:%S)" >> /tmp/pip.log
