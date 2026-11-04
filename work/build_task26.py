# -*- coding: utf-8 -*-
"""构建任务2-6的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"
MARK = "\u3010\u00a7-06\u3011"
TITLE = "拓展任务2-6　转换销售数据（reshape + vstack）"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-6 @MARK@ 转换销售数据 ============
sales_data = np.array([230, 150, 200, 180, 220, 250, 300, 280, 320, 350, 400, 380])

# ① 把一维数组重塑成 (3, 4)
reshaped_sales_data = sales_data.reshape(3, 4)

print("① 重塑后的销售数据 (3,4)：")
print(reshaped_sales_data)

# ② 计算每一行的总和
row_sums = np.sum(reshaped_sales_data, axis=1)

print("\\n② 每一行的总和：")
print(row_sums)

# ③ 用 vstack 把行总和追加到表格下方
#    注意：vstack 要求列数一致，(3,4) 是 4 列而行总和只有 3 个数，直接拼会报错，
#    所以先把表格转置成 (4,3)，行总和正好 3 个数，拼成 (5,3) 的完整表格
sales_with_sum = np.vstack([reshaped_sales_data.T, row_sums])

print("\\n③ 已把行总和追加为表格最后一行，shape：", sales_with_sum.shape)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@销售汇总报告")
print("姓名：许鑫")
print("拼接完成后的完整表格：")
print(sales_with_sum)
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-6　转换销售数据\n",
                    "\n",
                    "reshape(3,4) → 按行求和 → vstack 追加汇总行 → 【§-06】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_6_sales.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-6>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_6_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_6_sales.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
