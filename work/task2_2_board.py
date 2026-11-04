import numpy as np

# ============ 拓展任务2-2 【§-02】 填色小游戏（11×11） ============
# ① 初始化：整盘先全部填"白"
board = np.zeros((11, 11), dtype=int).astype(np.object_)
board[:, :] = "白"

# ② 步长切片：行列下标奇偶不同的格子涂"黑"，形成黑白交错
board[1::2, ::2] = "黑"      # 奇数行、偶数列
board[::2, 1::2] = "黑"      # 偶数行、奇数列

print("① 黑白交错的棋盘：")
print(board)

# ③ 中间第5行改"黄"，四个角改"蓝"
board[5, :] = "黄"
board[0, 0] = board[0, 10] = board[10, 0] = board[10, 10] = "蓝"

print("\n② 中间行变黄、四角变蓝后的棋盘：")
print(board)

# ④ 统计黑色、黄色格子数量
black_cells = np.sum(board == "黑")
yellow_cells = np.sum(board == "黄")

# ⑤ 输出业务报告（开头标记与讲义一致）
print()
print("【§-02】棋盘颜色统计")
print("姓名：许鑫")
print(f"黑色格子数量：{black_cells}")
print(f"黄色格子数量：{yellow_cells}")
