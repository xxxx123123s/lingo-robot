# -*- coding: utf-8 -*-
"""构建任务2-5的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"
MARK = "\u3010\u00a7-05\u3011"
TITLE = "拓展任务2-5　随机漫步（seed=66，3000 步）"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-5 @MARK@ 随机漫步 ============
np.random.seed(66)                  # 固定随机种子，保证结果可复现
steps = 3000                        # 总步数
STEP_LENGTH = 0.5                   # 每步 0.5 米（与示例代码一致）

draws = np.random.randint(0, 2, size=steps)      # 随机抽 0 / 1
direction_steps = np.where(draws > 0, 1, -1)     # 1 -> 向前 +1，0 -> 向后 -1
distance = direction_steps.cumsum()              # 累计距离（单位：步）

# ① 打印前 20 个累计距离用于调试
print("① distance 前 20 个元素：")
print(distance[:20])

# ② 第一次 |距离| 超过 20 米（20 米 = 40 步）
first_20m = (np.abs(distance) > 20 / STEP_LENGTH).argmax()
print(f"\\n② 第一次距离原点超过 20 米：第 {first_20m} 步")

# ③ 全程 |距离| 超过 10 米（10 米 = 20 步）的总步数
over_10m = np.sum(np.abs(distance) > 10 / STEP_LENGTH)
print(f"③ 全程距离原点超过 10 米的总步数：{over_10m} 步")

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@随机漫步统计报告")
print("姓名：许鑫")
print(f"第一次距离原点超过20米的步数：{first_20m}")
print(f"全程距离原点超过10米的总步数：{over_10m}")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-5　随机漫步\n",
                    "\n",
                    "seed(66) + 3000 步 → where 转方向 → cumsum 累计 → 【§-05】业务报告。",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": CELL.splitlines(keepends=True),
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    nb_path = os.path.join(WORK, "task2_5_walk.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-5>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_5_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_5_walk.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
