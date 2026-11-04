# -*- coding: utf-8 -*-
"""构建任务2-2的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"

# 讲义里【§‑02】的短横线是不间断连字符（U+2011），GBK 控制台会报编码错，
# 这里统一用普通半角 "-"，显示效果完全一致。
MARK = "\u3010\u00a7-02\u3011"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-2 @MARK@ 填色小游戏（11×11） ============
# ① 初始化：整盘先全部填"白"
board = np.zeros((11, 11), dtype=int).astype(np.object_)
board[:, :] = "白"

# ② 步长切片：行列下标奇偶不同的格子涂"黑"，形成黑白交错
board[1::2, ::2] = "黑"      # 奇数行、偶数列
board[::2, 1::2] = "黑"      # 偶数行、奇数列

print("① 黑白交错的棋盘：")
print(board)

# ③ 中间第5行改"黄"，四个角改"蓝"
board[5, :] = "黄"
board[0, 0] = board[0, 10] = board[10, 0] = board[10, 10] = "蓝"

print("\\n② 中间行变黄、四角变蓝后的棋盘：")
print(board)

# ④ 统计黑色、黄色格子数量
black_cells = np.sum(board == "黑")
yellow_cells = np.sum(board == "黄")

# ⑤ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@棋盘颜色统计")
print("姓名：许鑫")
print(f"黑色格子数量：{black_cells}")
print(f"黄色格子数量：{yellow_cells}")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-2　填色小游戏（11×11）\n",
                    "\n",
                    "整体填白 → 步长切片黑白交错 → 中间行变黄、四角变蓝 → 【§-02】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_2_board.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-2>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_2_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_2_board.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
