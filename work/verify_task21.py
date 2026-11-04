# -*- coding: utf-8 -*-
"""校验任务2-1：代码可执行、输出与记录一致、标记字符与讲义一致、统计结果正确。"""
import io
import json
import os
import sys

import numpy as np

WORK = r"D:\智能桌宠\work"

with io.open(os.path.join(WORK, "task2_1_board.ipynb"), encoding="utf-8") as f:
    nb = json.load(f)
code = "".join(nb["cells"][1]["source"])

buf = io.StringIO()
old = sys.stdout
sys.stdout = buf
try:
    exec(compile(code, "<task2-1>", "exec"), {})
finally:
    sys.stdout = old
out = buf.getvalue()

with io.open(os.path.join(WORK, "task2_1_output.txt"), encoding="utf-8") as f:
    recorded = f.read()

lines = []
lines.append("output identical to recorded: %s" % (out == recorded))
lines.append("marker U+2011 present: %s" % ("\u201101\u3011" in out))
lines.append("marker U+002D present: %s" % ("-01\u3011" in out))
for l in out.splitlines():
    if "§" in l:
        lines.append("marker line: %r" % l)
        lines.append("codepoints: " + " ".join("U+%04X" % ord(c) for c in l[:6]))

g = np.zeros((19, 19), dtype=int)
g[3:4, :] = 2
g[15:16, :] = 2
g[:, 7] = 2
g[0, 0] = g[0, 18] = g[18, 0] = g[18, 18] = 1
lines.append("check black=%d line=%d" % (np.sum(g == 1), np.sum(g == 2)))
lines.append("has U+2460 in code: %s" % ("\u2460" in code))
lines.append("code has literal backslash-u: %s" % ("\\u" in code))

with io.open(os.path.join(WORK, "verify.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("ok")
