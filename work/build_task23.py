# -*- coding: utf-8 -*-
"""构建任务2-3的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"
MARK = "\u3010\u00a7-03\u3011"

TITLE = "拓展任务2-3　评选优秀员工（布尔索引）"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-3 @MARK@ 员工绩效筛选 ============
work_performance = np.array([
    ['张三', 'A', 105, 4.5],
    ['李四', 'B', 100, 4.6],
    ['王五', 'B', 100, 4.5],
    ['赵六', 'A', 116, 4.9],
    ['孙七', 'B', 100, 4.5],
    ['周八', 'B', 99, 4.3],
    ['吴九', 'A', 120, 4.6],
    ['郑十', 'A', 115, 4.8]
])

# ① 提取等级、完成率、满意度三列，后两列转成浮点
performance_level = work_performance[:, 1]
target_rate = work_performance[:, 2].astype(float)
satisfaction = work_performance[:, 3].astype(float)

# ② 构造布尔掩码：(A级 并且 完成率>=105) 或者 满意度>=4.8
condition = ((performance_level == 'A') & (target_rate >= 105)) | (satisfaction >= 4.8)

print("① 布尔掩码（True 表示满足条件）：")
print(condition)

# ③ 用掩码筛选员工，并统计人数
final_data = work_performance[condition]
employee_name = final_data[:, 0]

print("\\n② 满足条件的员工：")
print(final_data)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@员工筛选报告")
print("姓名：许鑫")
print(f"满足条件员工总人数：{np.sum(condition)}")
print(f"员工名单：{'、'.join(employee_name)}")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-3　评选优秀员工\n",
                    "\n",
                    "提取三列 → 多条件布尔掩码 → 布尔索引筛选 → 【§-03】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_3_staff.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-3>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_3_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_3_staff.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))
    print("title:", TITLE)


if __name__ == "__main__":
    main()
