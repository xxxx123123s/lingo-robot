# -*- coding: utf-8 -*-
"""构建任务2-8的 Notebook 单元格，并真实执行一次，记录运行输出。"""
import io
import json
import os
import sys

WORK = r"D:\智能桌宠\work"
MARK = "\u3010\u00a7-08\u3011"
TITLE = "拓展任务2-8　兴趣班调查（for 循环 + np.unique）"

CELL_TEMPLATE = '''import numpy as np

# ============ 拓展任务2-8 @MARK@ 兴趣班调查 ============
course_data = np.array([
    ["张三", "舞蹈", "钢琴", "武术"],
    ["李四", "绘画", "舞蹈", "游泳"],
    ["王五", "游泳", "舞蹈", "轮滑"],
    ["赵六", "轮滑", "绘画", "武术"],
    ["周七", "外语", "声乐", "绘画"],
    ["韩八", "游泳", "外语", "钢琴"],
    ["秦九", "篮球", "钢琴", "声乐"],
    ["郑十", "轮滑", "篮球", "绘画"]
])

# ① for 循环遍历每一行，筛选同时报名"绘画"和"舞蹈"的学生
selected_names = []
for row in course_data:
    name = str(row[0])            # 第 0 列是姓名（转成 Python 字符串）
    courses = row[1:]             # 第 1 列之后是报名的兴趣班
    if ("绘画" in courses) and ("舞蹈" in courses):
        selected_names.append(name)

print("① 同时报名绘画和舞蹈的学生名单：")
print(selected_names)

# ② 统计满足条件的学生总人数
total_people = len(selected_names)

# ③ 用 np.unique 统计全部兴趣班的报名人数
all_courses = course_data[:, 1:].flatten()
unique_courses, course_counts = np.unique(all_courses, return_counts=True)

print("\\n③ 各兴趣班报名人数：")
for course, count in zip(unique_courses, course_counts):
    print(f"{course}：{count} 人")

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("@MARK@兴趣班统计报告")
print("姓名：许鑫")
print(f"筛选出的学生名单：{'、'.join(selected_names)}")
print(f"满足条件的学生总人数：{total_people}")
'''

CELL = CELL_TEMPLATE.replace("@MARK@", MARK)


def main():
    nb = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 拓展任务2-8　兴趣班调查\n",
                    "\n",
                    "for 循环逐行筛选 → 列表收集姓名 → np.unique 统计报名人数 → 【§-08】业务报告。",
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
    nb_path = os.path.join(WORK, "task2_8_course.ipynb")
    with io.open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(CELL, "<task2-8>", "exec"), {"__name__": "__main__"})
    finally:
        sys.stdout = old
    out = buf.getvalue()

    with io.open(os.path.join(WORK, "task2_8_output.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with io.open(os.path.join(WORK, "task2_8_course.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CELL)

    print("notebook:", nb_path)
    print("output chars:", len(out))


if __name__ == "__main__":
    main()
