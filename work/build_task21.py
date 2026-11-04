# -*- coding: utf-8 -*-
"""构建任务2-1的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"

# 讲义里【§‑01】的短横线是不间断连字符（U+2011），在 GBK 控制台会报 UnicodeEncodeError，
# 这里统一用普通半角 "-"：显示效果完全一致，任何环境都能跑。
MARK = "\u3010\u00a7-01\u3011"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-1 @MARK@ 19×19 围棋棋盘 ============
go_board = np.zeros((19, 19), dtype=int)          # 零棋盘：0 = 空位

# ① 切片批量赋值：第3行、第15行、第7列 全部赋值为 2（辅助线）
go_board[3:4, :] = 2        # 第3行（下标从0开始）
go_board[15:16, :] = 2      # 第15行
go_board[:, 7] = 2          # 第7列

print("① 切片赋值后的棋盘（0=空位，2=辅助线）：")
print(go_board)

# ② 四个角落坐标赋黑子数值 1
go_board[0, 0] = 1
go_board[0, 18] = 1
go_board[18, 0] = 1
go_board[18, 18] = 1

print("\\n② 四角落黑子后的棋盘：")
print(go_board)

# ③ 统计黑子、辅助线格子数量
black_stones = np.sum(go_board == 1)
line_cells = np.sum(go_board == 2)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@棋盘统计报告")
print("姓名：许鑫")
print(f"黑子（值=1）数量：{black_stones}")
print(f"辅助线格子（值=2）数量：{line_cells}")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-1　创建 19×19 围棋棋盘\n",
                    "\n",
                    "切片批量赋值 → 四角落子 → 条件统计 → 【§-01】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_1_board.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-1>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_1_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_1_board.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
