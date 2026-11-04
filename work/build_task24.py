# -*- coding: utf-8 -*-
"""构建任务2-4的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"
MARK = "\u3010\u00a7-04\u3011"
TITLE = "拓展任务2-4　计算同比增速（布尔索引+统计）"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-4 @MARK@ 计算同比增速 ============
last_year_data = np.array([
    [100, 105, 111],
    [115, 113, 120],
    [130, 125, 131],
    [150, 151, 160]
])
this_year_data = np.array([
    [110, 125, 135],
    [145, 155, 165],
    [175, 185, 195],
    [205, 215, 225]
])

# ① 复用示例代码，得到同比增速矩阵（单位：%）
year_on_year_growth_rate = (this_year_data - last_year_data) / last_year_data * 100

print("① 完整的同比增速矩阵（%）：")
print(year_on_year_growth_rate)

# ② 布尔索引：取出所有增速大于 30 的数据
high_growth = year_on_year_growth_rate[year_on_year_growth_rate > 30]

print("\\n② 增速大于 30% 的数据：")
print(high_growth)

# ③ 计算高增速数据的平均值
average_high_growth = np.mean(high_growth)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@增速统计报告")
print("姓名：许鑫")
print(f"高增速数据个数：{high_growth.size}")
print(f"高增速的平均值：{average_high_growth:.2f}%")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-4　计算同比增速\n",
                    "\n",
                    "复用示例代码 → 布尔索引筛出 >30% → 求平均 → 【§-04】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_4_growth.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-4>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_4_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_4_growth.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
