import numpy as np

# ============ 拓展任务2-4 【§-04】 计算同比增速 ============
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

print("\n② 增速大于 30% 的数据：")
print(high_growth)

# ③ 计算高增速数据的平均值
average_high_growth = np.mean(high_growth)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-04】增速统计报告")
print("姓名：许鑫")
print(f"高增速数据个数：{high_growth.size}")
print(f"高增速的平均值：{average_high_growth:.2f}%")
