import numpy as np

# ============ 拓展任务2-7 【§-07】 门店销售排序筛选 ============
sales_data = np.array([
    [550, 700, 620, 800],
    [430, 500, 480, 550],
    [900, 850, 950, 1000],
    [720, 680, 710, 690],
    [620, 580, 600, 650],
    [800, 750, 780, 820]
])

# ① 计算每个门店的销售总销量
store_total = np.sum(sales_data, axis=1)

print("① 各门店总销量：")
print(store_total)

# ② argsort 拿到升序索引，再用花式索引把原始表格按总销量从小到大重排
sorted_index = np.argsort(store_total)
sorted_sales = sales_data[sorted_index]          # 花式索引：按索引数组取行
sorted_total = store_total[sorted_index]

print("\n② 总销量升序的索引：", sorted_index)
print("重排后的销售表（每一行是一个门店）：")
print(sorted_sales)

# ③ 筛选总销量大于 2500 的门店全部销售数据
mask = sorted_total > 2500
big_stores = sorted_sales[mask]

print("\n③ 总销量大于 2500 的门店数据：")
print(big_stores)

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-07】门店销售分析报告")
print("姓名：许鑫")
print(f"总销量大于2500的门店数量：{big_stores.shape[0]}")
print("筛选得到的门店数据：")
print(big_stores)
print(f"对应门店总销量：{sorted_total[mask]}")
