#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
树莓派桌宠对话：麦克风 -> VAD 切句 -> sherpa 识别 -> DeepSeek 对话 -> espeak 朗读 -> 耳机

配置：/home/pi/llm.json
  {
    "api_key":  "sk-xxxx",
    "base_url": "https://api.deepseek.com",
    "model":    "deepseek-chat",
    "system":   "人设提示词",
    "max_tokens": 150,
    "temperature": 0.9
  }
也可以用环境变量 DEEPSEEK_API_KEY 提供密钥。

用法:
  python3 chat.py                     # 开始对话
  python3 chat.py --say "你好"        # 只测对话+TTS，不开麦
  python3 chat.py --model deepseek-chat
  python3 chat.py -v cmn+m3 -s 180    # 换音色/语速
退出: Ctrl+C
"""
import argparse
import datetime
import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, "/home/pi")
from talk import make_rec, make_vad, speech_seconds, MODEL, VAD_MODEL, RATE  # noqa: E402
from tts import speak as tts_speak  # noqa: E402

CHUNK = 1600
CONFIG = "/home/pi/llm.json"
HISTORY_MAX = 12          # 保留最近 12 条消息（6 轮）

DEFAULT_SYSTEM = (
    "你是一只住在树莓派里的小桌宠，名字叫小派。"
    "说话口语化、简短、带点俏皮，每次回复不超过 40 个字。"
    "绝对不要使用 Markdown、表情符号、列表或括号说明，因为你的话会被念出来。"
    "用户的话来自语音识别，可能有同音错字，请按最合理的意思理解，不要纠正对方。"
    "你不知道的事就直说不知道，绝对不要编。"
)


# ---------------- 实时信息（代替“联网”） ----------------
# 模型的参数里没有“今天几号、现在几点、外面几度”，这些每秒都在变，
# 训练数据再新也答不对。所以每次提问前先自己查一遍，把结果塞进 system。
# 这就是最朴素的工具调用：先取数据，再让模型用数据说话。

_WMO = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
        51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻雨", 57: "冻雨",
        61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
        95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹"}

_WHERE = {"t": 0.0, "v": None}          # 定位缓存，1 小时
_WX = {"t": 0.0, "v": None}             # 天气缓存，5 分钟


def wmo_desc(code):
    try:
        return _WMO.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def _where():
    """IP 定位，失败就退回成都。定位走一次管一小时，别每句话都问。"""
    if time.time() - _WHERE["t"] < 3600 and _WHERE["v"]:
        return _WHERE["v"]
    try:
        url = "http://ip-api.com/json/?fields=status,city,regionName,lat,lon"
        with urllib.request.urlopen(url, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("status") == "success":
            v = (float(j["lat"]), float(j["lon"]), j.get("city") or "")
            _WHERE.update(t=time.time(), v=v)
            return v
    except Exception as exc:                                  # noqa: BLE001
        print(f"  [定位失败] {exc}")
    return (30.66, 104.06, "")


def quick_weather():
    """只为“说话”服务的天气：一句话能说清的量，5 分钟内复用。
    带未来两天预报 —— 只答“今天”的话，问“明天带不带伞”就哑了。"""
    if time.time() - _WX["t"] < 300 and _WX["v"]:
        return _WX["v"]
    try:
        lat, lon, city = _where()
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat:.4f}&longitude={lon:.4f}"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
               "weather_code,wind_speed_10m"
               "&daily=temperature_2m_max,temperature_2m_min,"
               "weather_code,precipitation_probability_max"
               "&timezone=auto&forecast_days=3")
        with urllib.request.urlopen(url, timeout=8) as r:
            j = json.loads(r.read().decode("utf-8"))
        cur, day = j["current"], j["daily"]

        def at(key, i, default=0):
            seq = day.get(key) or []
            return seq[i] if len(seq) > i else default

        v = {
            "city": city,
            "desc": wmo_desc(cur.get("weather_code")),
            "temp": round(float(cur["temperature_2m"])),
            "feels": round(float(cur["apparent_temperature"])),
            "hum": int(cur.get("relative_humidity_2m") or 0),
            "wind": float(cur.get("wind_speed_10m") or 0),
            "hi": round(float(day["temperature_2m_max"][0])),
            "lo": round(float(day["temperature_2m_min"][0])),
            "pop": int(at("precipitation_probability_max", 0, 0) or 0),
            "days": [
                {"name": name,
                 "desc": wmo_desc(at("weather_code", i, 0)),
                 "hi": round(float(at("temperature_2m_max", i, 0))),
                 "lo": round(float(at("temperature_2m_min", i, 0))),
                 "pop": int(at("precipitation_probability_max", i, 0) or 0)}
                for i, name in ((1, "明天"), (2, "后天"))
            ],
        }
        _WX.update(t=time.time(), v=v)
        return v
    except Exception as exc:                                  # noqa: BLE001
        print(f"  [天气失败] {exc}")
        return _WX["v"]                       # 旧的也比没有强


def live_context(weather=None):
    """拼一句“现场实况”，塞进 system。weather 可传外部现成的（屏幕那边有缓存）"""
    now = datetime.datetime.now()
    parts = ["现在是 %d年%d月%d日 周%s %02d:%02d"% (
        now.year, now.month, now.day, "一二三四五六日"[now.weekday()],
        now.hour, now.minute)]
    w = weather if weather is not None else quick_weather()
    if w:
        txt = f"外面{w['desc']}，{w['temp']}度"
        if w.get("feels") is not None and abs(w["feels"] - w["temp"]) >= 2:
            txt += f"（体感{w['feels']}度）"
        txt += f"，湿度{w.get('hum', 0)}%，风{w.get('wind', 0):.0f}米每秒"
        txt += f"，今天{w.get('lo')}到{w.get('hi')}度，降雨概率{w.get('pop', 0)}%"
        # ip-api 常给英文地名（Ziyang），念出来别扭，非中文就不说地点
        if w.get("city") and not re.search(r"[A-Za-z]", str(w["city"])):
            txt = f"{w['city']}，" + txt
        parts.append(txt)
        for d in (w.get("days") or []):            # 明天、后天
            if not d.get("desc"):
                continue
            parts.append("%s%s，%d到%d度，降雨概率%d%%"
                         % (d["name"], d["desc"], d["lo"], d["hi"], d["pop"]))
    return "；".join(parts)


# ---------------- 配置 ----------------
def load_cfg(args):
    cfg = {"api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
           "base_url": "https://api.deepseek.com",
           "model": "deepseek-chat",
           "system": DEFAULT_SYSTEM,
           "max_tokens": 150,
           "temperature": 0.9}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if args.model:
        cfg["model"] = args.model
    if not cfg.get("api_key"):
        sys.exit(f"缺少 API 密钥：请写入 {CONFIG} 的 api_key 字段，或设置环境变量 DEEPSEEK_API_KEY")
    return cfg


# ---------------- 长期记忆 ----------------
# 对话历史只留 6 轮，重启就清空。但"我叫什么、我在做什么"这种话十年也不会变，
# 每次都要重新介绍一遍太蠢。所以单独存一份，每次提问前拼进 system。
# 用 JSON 而不是向量库：要记的是几个固定字段，不是一整段散文。
MEMO_FILE = "/home/pi/memo.json"
MEMO_LABEL = {"name": "名字", "project": "正在做的事",
              "school": "学校", "note": "其他"}

# 只认这几句明确的自我介绍。宁可漏，不可错 —— 认错了比不认更尴尬。
_MEMO_PAT = (
    ("name", r"我(?:的名字)?(?:叫做|叫|是)([\u4e00-\u9fa5]{2,4})(?=[，。！？,.!?\s]|$)"),
    ("project", r"我(?:在|正在|最近在|现在在|这学期在)(?:做|搞|弄|写|开发|研究)"
                r"(?:一个|一份|个|的是)?([^，。！？,.!?]{2,26})"),
    ("project", r"我的(?:项目|课题|毕设|毕业设计|作业)(?:是|叫|做的是)?([^，。！？,.!?]{2,26})"),
    ("school", r"我(?:在|就读于|来自)([\u4e00-\u9fa5]{2,8}(?:大学|学院))"),
)

# "我叫什么""我在做什么"长得和自我介绍一模一样，只有这一处区别：
# 宾语是个疑问词。踩过这个坑 —— 问一句"我叫什么"，名字就被改成了"什么"。
_QUESTION = ("什么", "啥", "谁", "哪", "多少", "几个", "怎样", "怎么",
             "干吗", "干嘛", "吗", "呢")

_MEMO_TAIL = ("的项目", "项目", "的课题", "课题", "的作业", "作业",
              "的东西", "的玩意儿", "的毕业设计", "毕业设计")


def load_memo():
    try:
        with open(MEMO_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                         # noqa: BLE001
        return {}


def save_memo(d):
    """先写临时文件再改名，免得断电时留下半个 JSON"""
    try:
        tmp = MEMO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MEMO_FILE)
    except Exception as exc:                                  # noqa: BLE001
        print(f"  [记忆保存失败] {exc}")


def remember_from(text):
    """从这句话里抽能长期记住的事，抽到就落盘。返回这次新记下的"""
    old = load_memo()
    got = {}
    for key, pat in _MEMO_PAT:
        m = re.search(pat, text or "")
        if not m:
            continue
        val = m.group(1).strip(" ，。！？,.!?")
        if any(q in val for q in _QUESTION):
            continue                       # "我叫什么"是在问，不是在告诉
        if key == "name" and any(c in val for c in "你我他她它车这那"):
            continue                       # "我叫你小派"不是在自我介绍
        if key == "project":
            for tail in _MEMO_TAIL:        # "语音控车的项目" → "语音控车"
                if val.endswith(tail) and len(val) > len(tail):
                    val = val[: -len(tail)]
                    break
        if len(val) >= 2 and old.get(key) != val:
            old[key] = val
            got[key] = val
    if got:
        save_memo(old)
        print(f"  [记忆] 记下了 {got}")
    return got


def forget(key=None):
    """key 为空 = 全忘"""
    if key:
        d = load_memo()
        d.pop(key, None)
    else:
        d = {}
    save_memo(d)
    return d


def memo_hint():
    d = load_memo()
    parts = [f"{MEMO_LABEL.get(k, k)}：{v}" for k, v in d.items() if v]
    return "；".join(parts)


def handle_memo_cmd(text):
    """“忘了我叫什么”这种话得拦在模型前面，不然它会边道歉边照旧记得"""
    t = (text or "").strip()
    if any(k in t for k in ("忘掉我的名字", "忘记我的名字", "忘了我叫什么", "忘了我的名字")):
        forget("name")
        return "行，你叫什么我忘了，待会儿再告诉我一遍。"
    if any(k in t for k in ("忘掉我的项目", "忘了我的项目", "忘掉项目")):
        forget("project")
        return "好，你在做什么我不记得了。"
    if any(k in t for k in ("清空记忆", "全忘掉", "忘掉所有", "把记忆清掉", "你都忘了吧")):
        forget()
        return "好，我把记得的事全清掉了。"
    return None


# ---------------- 对话 ----------------
# 一句话能不能讲透，取决于问的是什么。喊"开灯"要快，问"为什么天是蓝的"要展开。
# 所以按问题分两档：快答（40 字，TTS 一秒念完）和讲解（最多 180 字）。
DEEP_WORDS = ("讲讲", "讲一下", "讲个", "解释", "为什么", "为啥", "原理", "区别",
              "是什么", "什么是", "介绍", "详细", "展开", "举个例子", "举例",
              "分析", "评价", "看法", "教我", "怎么做", "什么意思", "科普",
              "历史", "原因", "有什么关系", "有什么用")

DEEP_TAIL = (
    "\n【这次要讲清楚】用户在问需要解释的问题，别用一句话打发。"
    "用 100 到 180 个字讲明白，最好带一个具体的例子。"
    "仍然要口语化、一口气说完，不要分点、不要 Markdown、不要说"
    "\u201c首先其次最后\u201d这种书面结构，因为这段话会被念出来。"
)


def is_deep(text):
    """这句是想听道理，还是只要一个答复"""
    t = (text or "").strip()
    if len(t) >= 16:            # 语音识别出一长串，多半不是在下指令
        return True
    return any(k in t for k in DEEP_WORDS)


def build_system(cfg, live=None, deep=False):
    """人设 + 长期记忆 + 现场实况。越靠后模型越当回事，所以实况压最后"""
    system = cfg["system"]
    if "直说不知道" not in system:        # llm.json 里的人设可能是老的，兜一道
        system += "你不知道的事就直说不知道，绝对不要编。"
    memo = memo_hint()
    if memo:
        system += ("\n【你记得关于主人的事】" + memo
                   + "。自然地用，不需要每条都复述，也别再问已经知道的事。")
    if deep:
        system += DEEP_TAIL
    if not live:
        return system
    return (system
            + "\n【现场实况，回答时间日期天气只能用这里的数字】" + live
            + "。这里没有的信息，比如别人的事、没发生过的事，就直说不知道，不要编。")


def ask_llm(history, cfg, live=None, deep=False):
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": build_system(cfg, live, deep)}] + history,
        "temperature": cfg.get("temperature", 0.9) if not deep
                       else max(0.3, cfg.get("temperature", 0.9) - 0.3),
        "max_tokens": 700 if deep else cfg.get("max_tokens", 150),
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        print(f"  [接口错误 {e.code}] {detail}")
        if e.code == 401:
            return "我的密钥好像不对，你检查一下。"
        if e.code == 402:
            return "账户余额不足了。"
        return "我脑袋卡住了，等一下再问我。"
    except Exception as e:
        print(f"  [网络错误] {e}")
        return "我连不上网了。"


# ---------------- 朗读 ----------------
def speak(text, args):
    tts_speak(text, engine=args.tts, voice=args.voice, rate=args.rate,
              out=args.out, espeak_speed=args.speed)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--device", default="plughw:3,0")
    ap.add_argument("-o", "--out", default="plughw:2,0")
    ap.add_argument("-v", "--voice", default="huihui",
                    help="音色: huihui/kangkang/yaoyao (SAPI) 或 xiaoxiao/yunxi (edge)")
    ap.add_argument("-s", "--speed", type=int, default=168, help="espeak 语速（兜底用）")
    ap.add_argument("--tts", choices=["sapi", "edge", "espeak"], default="sapi")
    ap.add_argument("--rate", type=int, default=180, help="SAPI 语速（正常约 180）")
    ap.add_argument("-t", "--threads", type=int, default=2)
    ap.add_argument("-m", "--model", help="覆盖模型名")
    ap.add_argument("--say", help="只测对话+TTS，不占用麦克风")
    ap.add_argument("--quiet", action="store_true", help="不出声，只打印")
    ap.add_argument("--memo", action="store_true", help="看看它记住了什么")
    ap.add_argument("--forget", nargs="?", const="all",
                    help="清记忆：不带参数全清，--forget name 只清名字")
    args = ap.parse_args()

    if args.memo:                     # 这两条不碰麦克风也不碰模型，先处理
        d = load_memo()
        print(json.dumps(d, ensure_ascii=False, indent=2) if d else "（还什么都没记住）")
        return
    if args.forget:
        forget(None if args.forget == "all" else args.forget)
        print("记忆已清空" if args.forget == "all" else f"已忘掉 {args.forget}")
        return

    cfg = load_cfg(args)

    print(f"模型: {cfg['model']}   后端: {cfg['base_url']}")
    print("加载语音模型...", end="", flush=True)
    t0 = time.time()
    rec = make_rec(args.threads, "/home/pi/hotwords.txt")
    vad, window = make_vad()
    print(f" 完成 ({time.time() - t0:.1f}s)")

    history = []

    def reply_to(text):
        history.append({"role": "user", "content": text})
        if len(history) > HISTORY_MAX:
            del history[:-HISTORY_MAX]
        t = time.time()
        answer = handle_memo_cmd(text)
        if answer is None:
            remember_from(text)           # 先记，这一句就能用上新名字
            answer = ask_llm(history, cfg, live_context(), deep=is_deep(text))
        history.append({"role": "assistant", "content": answer})
        print(f"小派: {answer}    ({(time.time() - t) * 1000:.0f}ms)", flush=True)
        if not args.quiet:
            speak(answer, args)

    if args.say:
        print(f"你: {args.say}")
        reply_to(args.say)
        return

    def recognize(samples):
        s = rec.create_stream()
        s.accept_waveform(RATE, samples)
        s.input_finished()
        while rec.is_ready(s):
            rec.decode_stream(s)
        return rec.get_result(s).strip()

    cmd = ["arecord", "-D", args.device, "-f", "S16_LE", "-r", str(RATE),
           "-c", "1", "-t", "raw", "-q",
           "--period-size=1600", "--buffer-size=16000"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    fd = proc.stdout.fileno()
    print("开始对话吧，说完一句我会接话。Ctrl+C 退出", flush=True)

    carry = np.zeros(0, dtype=np.float32)
    hist = np.zeros(0, dtype=np.float32)
    preroll = int(0.35 * RATE)
    buf = None

    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            data = os.read(fd, CHUNK * 2)
            if not data:
                break
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

            carry = np.concatenate([carry, samples]) if len(carry) else samples
            while len(carry) >= window:
                vad.accept_waveform(carry[:window])
                carry = carry[window:]

            hist = np.concatenate([hist, samples])
            if len(hist) > preroll:
                hist = hist[-preroll:]

            started = False
            if buf is None and vad.is_speech_detected():
                buf = hist.copy()
                started = True
            if buf is not None and not started:
                buf = np.concatenate([buf, samples])

            if not vad.empty():
                seg = buf if buf is not None else vad.front.samples
                while not vad.empty():
                    vad.pop()
                buf = None
                hist = np.zeros(0, dtype=np.float32)
                if speech_seconds(seg) >= 0.3:
                    text = recognize(seg)
                    if text:
                        print(f"\n你: {text}", flush=True)
                        reply_to(text)
                        # 朗读期间麦里积压的数据丢掉了，免得下一句被污染
                        while select.select([fd], [], [], 0)[0]:
                            if not os.read(fd, 65536):
                                break
    except KeyboardInterrupt:
        print("\n再见")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
