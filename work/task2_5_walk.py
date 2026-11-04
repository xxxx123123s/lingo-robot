import numpy as np

# ============ 拓展任务2-5 【§-05】 随机漫步 ============
np.random.seed(66)                  # 固定随机种子，保证结果可复现
steps = 3000                        # 总步数
STEP_LENGTH = 0.5                   # 每步 0.5 米（与示例代码一致）

draws = np.random.randint(0, 2, size=steps)      # 随机抽 0 / 1
direction_steps = np.where(draws > 0, 1, -1)     # 1 -> 向前 +1，0 -> 向后 -1
distance = direction_steps.cumsum()              # 累计距离（单位：步）

# ① 打印前 20 个累计距离用于调试
print("① distance 前 20 个元素：")
print(distance[:20])

# ② 第一次 |距离| 超过 20 米（20 米 = 40 步）
first_20m = (np.abs(distance) > 20 / STEP_LENGTH).argmax()
print(f"\n② 第一次距离原点超过 20 米：第 {first_20m} 步")

# ③ 全程 |距离| 超过 10 米（10 米 = 20 步）的总步数
over_10m = np.sum(np.abs(distance) > 10 / STEP_LENGTH)
print(f"③ 全程距离原点超过 10 米的总步数：{over_10m} 步")

# ④ 输出业务报告（开头标记与讲义一致）
print()
print("【§-05】随机漫步统计报告")
print("姓名：许鑫")
print(f"第一次距离原点超过20米的步数：{first_20m}")
print(f"全程距离原点超过10米的总步数：{over_10m}")
