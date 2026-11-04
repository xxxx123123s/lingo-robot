import numpy as np

# ============ 拓展任务2-3 【§-03】 员工绩效筛选 ============
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

print("\n② 满足条件的员工：")
print(final_data)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-03】员工筛选报告")
print("姓名：许鑫")
print(f"满足条件员工总人数：{np.sum(condition)}")
print(f"员工名单：{'、'.join(employee_name)}")
