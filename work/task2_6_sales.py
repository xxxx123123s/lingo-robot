import numpy as np

# ============ 拓展任务2-6 【§-06】 转换销售数据 ============
sales_data = np.array([230, 150, 200, 180, 220, 250, 300, 280, 320, 350, 400, 380])

# ① 把一维数组重塑成 (3, 4)
reshaped_sales_data = sales_data.reshape(3, 4)

print("① 重塑后的销售数据 (3,4)：")
print(reshaped_sales_data)

# ② 计算每一行的总和
row_sums = np.sum(reshaped_sales_data, axis=1)

print("\n② 每一行的总和：")
print(row_sums)

# ③ 用 vstack 把行总和追加到表格下方
#    注意：vstack 要求列数一致，(3,4) 是 4 列而行总和只有 3 个数，直接拼会报错，
#    所以先把表格转置成 (4,3)，行总和正好 3 个数，拼成 (5,3) 的完整表格
sales_with_sum = np.vstack([reshaped_sales_data.T, row_sums])

print("\n③ 已把行总和追加为表格最后一行，shape：", sales_with_sum.shape)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-06】销售汇总报告")
print("姓名：许鑫")
print("拼接完成后的完整表格：")
print(sales_with_sum)
