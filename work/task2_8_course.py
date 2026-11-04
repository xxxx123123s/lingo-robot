import numpy as np

# ============ 拓展任务2-8 【§-08】 兴趣班调查 ============
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

print("\n③ 各兴趣班报名人数：")
for course, count in zip(unique_courses, course_counts):
    print(f"{course}：{count} 人")

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-08】兴趣班统计报告")
print("姓名：许鑫")
print(f"筛选出的学生名单：{'、'.join(selected_names)}")
print(f"满足条件的学生总人数：{total_people}")
