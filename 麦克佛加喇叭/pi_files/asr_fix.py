#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""识别文本的命令词纠正（拼音模糊匹配）

为什么需要这一层：
  ASR 的错误绝大多数是「同音/近音写错字」，不是"完全没听出来"。
  实测日志：
      你说「用手势停车」 → 识别成「有用手术室停车」
      你说「打开手势」   → 识别成「爸开」「大开」
      「手势」shou-shi 和「手术室」shou-shou-shi 只差一个音，14M 参数下分不清。
      换成离线大模型能好很多，但不可能 100%，所以还要一层文本兜底。

用法：
    import asr_fix
    fixed = asr_fix.fix("有用手术室停车")    # → "有用手势停车"
    print(asr_fix.last)                      # [("手术室", "手势")]

**只在按原意解析不出意图时才调用**（见 hub.py 的 _resolve 失败分支）。
原文能用就绝不碰它 —— 这是最重要的约束，纠错层没有机会把对的话改错。

## 两条边界（第一版就栽在这里）

1. **已经正确的命令词是保护区。** 文本里凡是本身就和某个命令词完全一致的位置，
   一律标成保护区间，替换不许把它吃掉。否则「手势停车」里的「势停车」会被
   当成「停车」的错误写法，整句缩成「停车」。
   替换覆盖了保护区时，还要检查被覆盖的词在替换后是否仍然存在，不存在就否决。
2. **多字匹配只在字符偏移时用。** 拼音长度差 1 个音，说明可能多打/少打一个字；
   差两个以上就不是"写错"，是"说了别的话"，不碰。
"""
import sys

try:
    from pypinyin import lazy_pinyin
except Exception:                                    # noqa: BLE001
    lazy_pinyin = None

# 标准命令词。**不要放单字**（"车""停"），一字之差能命中任何句子。
COMMANDS = [
    # 停车
    "停车", "停下", "停止", "站住", "别动", "刹车", "别停",
    # 前进后退
    "前进", "往前", "向前", "直走", "往前走", "向前走", "过来",
    "后退", "往后", "向后", "倒车", "往后退", "回来", "回去",
    # 转向
    "左转", "往左", "向左", "右转", "往右", "向右",
    "转一圈", "原地转圈", "掉头", "九十度", "四十五度", "一百八十度",
    # 速度
    "快一点", "快点", "加速", "慢一点", "慢点", "减速",
    # 示教路线
    "记住这条路", "记好了", "走一遍", "原路返回",
    # 视觉
    "手势", "打开手势", "关掉手势", "关闭手势", "手势停车",
    "看看我是谁", "这是什么东西",
    # 「爸开/大开」是「打开」的常见听错，两字词容一个音正好能救回来。
    # 它只在解析失败时才被调用，所以「打开音乐」这种正常句子不会被碰到。
    "打开", "关掉", "关闭",
]
COMMANDS.sort(key=len, reverse=True)     # 长词优先，短词不许抢长词的字

MAX_LEN = 14          # 超过这个长度就认为是聊天，不纠正
MAX_ROUNDS = 4        # 一句话最多改这么多处
# 命令词不会以这些字开头。用来挡住「又转」→「右转」这类同音误纠，
# 「又/右」「在/再」光听音是分不开的，只能靠"没人会用虚词下命令"来判。
STOP_CHARS = set("又再还也就都才刚已经正在的都了着过被把给和跟同与或")
_CACHE = {}
last = []             # 最近一次 fix 的替换明细，便于打印排查


def _py(s):
    """取拼音串（不标调、不空格）。'手势' → 'shoushi'"""
    got = _CACHE.get(s)
    if got is None:
        got = "".join(lazy_pinyin(s, errors="ignore")) if lazy_pinyin else ""
        _CACHE[s] = got
    return got


def _ed(a, b):
    """编辑距离。命令词都很短（2~6 字），直接 DP 就够。"""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _match(sub, cmd):
    """sub 算不算 cmd 的错误写法。"""
    ps, pc = _py(sub), _py(cmd)
    if not ps or not pc:
        return False
    if ps == pc:                                   # 同音异字：钱进→前进、停扯→停车
        return True
    if len(sub) == len(cmd):
        # **只有两字词才容忍一个音的偏差。** 这条限制很关键：
        # 「术室停车」和「手势停车」拼音只差一个字母，但那是四个字的句子片段，
        # 按音放过会把「手术室停车」拆成「手」+「手势停车」。
        # 两字词本身没有上下文，错一个音就是真的听错了，反而该纠。
        return len(cmd) == 2 and _ed(ps, pc) <= 1
    if len(sub) == len(cmd) + 1:                   # 多一个字
        return ps.startswith(pc) or ps.endswith(pc)
    return False


def _protected(text):
    """找出文本里已经正确的命令词，返回 [(start, end, word)]。"""
    out = []
    for cmd in COMMANDS:
        start = 0
        while True:
            i = text.find(cmd, start)
            if i < 0:
                break
            out.append((i, i + len(cmd), cmd))
            start = i + 1
    return out


def _one(text):
    """找一个最优替换点，返回 (i, w, cmd) 或 None。"""
    prot = _protected(text)
    for cmd in COMMANDS:
        n = len(cmd)
        for i in range(len(text)):
            for w in (n + 1, n):
                if w <= 1 or i + w > len(text):
                    continue
                sub = text[i:i + w]
                if sub == cmd or not _match(sub, cmd):
                    continue
                # 「又转」和「右转」同音，任何拼音纠错都分不开这两种。靠位置判：
                # 命令词不会以虚词开头，所以片段首字是虚词、而命令词首字不是，
                # 就不是在说命令（「又转九十度」不该被改成「右转九十度」）。
                if sub[0] in STOP_CHARS and sub[0] != cmd[0]:
                    continue
                # 多一个字时先看这个字落在哪：落在前面说明是"旁边还有个字"，
                # 不归我们管（「我打开手势」里的「我」），放弃这一档。
                if w == n + 1:
                    if sub[1:] == cmd:
                        continue
                    if sub[:n] == cmd:
                        w = n
                # 前一个字和命令词首字重复（「有用手术室停车」→「有用」+「手」+
                # 「术室停车」），把那个字一起替换掉，否则会得到「手手势停车」。
                if i > 0 and text[i - 1] == cmd[0]:
                    i -= 1
                    w += 1
                # 同音的多余字一起吞掉，最多一个字。
                # 「手术室」→「手术」+「室」，纠成「手势」后还剩个 shi，
                # 按拼音看得出是同一个音重复，属于同一种错。
                if i + w < len(text) and text[i + w] != cmd[-1] \
                        and _py(text[i + w]) == _py(cmd[-1]):
                    w += 1
                elif i > 0 and text[i - 1] != cmd[0] \
                        and _py(text[i - 1]) == _py(cmd[0]):
                    i -= 1
                    w += 1
                new = text[:i] + cmd + text[i + w:]
                if new == text:                    # 改了等于没改，别占轮次
                    continue
                # 被覆盖的保护词必须在新句子里还活着，否则是把它吃掉了
                covered = [word for s, e, word in prot if s < i + w and i < e]
                if all(word in new for word in covered):
                    return (i, w, cmd)
    return None


def fix(text, limit=MAX_LEN):
    """把命令词近音写法改成标准写法。返回新文本（可能没变）。"""
    global last
    last = []
    if not text or lazy_pinyin is None:
        return text
    text = text.strip()
    if len(text) > limit:                 # 长句多半是聊天，不动
        return text

    for _ in range(MAX_ROUNDS):
        got = _one(text)
        if got is None:
            break
        i, w, cmd = got
        last.append((text[i:i + w], cmd))
        text = text[:i] + cmd + text[i + w:]
    return text


def explain(text):
    """调试用：返回 (纠正后文本, 明细)"""
    new = fix(text)
    return new, list(last)


if __name__ == "__main__":
    cases = sys.argv[1:] or [
        "有用手术室停车", "用手术室停车", "手术室停车", "手势停车",
        "爸开手势", "大开手势", "打开手势", "帮我打开手势",
        "钱进", "又转九十度", "停扯", "前井", "又转九十",
        "今天天气怎么样", "我今天很累", "给我讲个笑话",
    ]
    for c in cases:
        new, pairs = explain(c)
        flag = f"→ {pairs}" if new != c else "（不变）"
        print(f"  {c:<12} {new:<14} {flag}")
