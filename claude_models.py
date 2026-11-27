"""
claude_models.py —— Claude 用哪个模型：智能选择，或者你自己选；顺便记下每天用了多少额度

    三个模型（按 API 价格比较，Pro 会员不额外扣钱，但占的额度大致成这个比例）：
        Haiku 5.5   最省：价格约是 Opus 的 1/40，查信息、开软件、简单问答都够用
        Sonnet 5.5  均衡：价格是 Opus 的一半，多步操作、写小脚本、整理文件
        Opus 5.5    最强：写程序、排查问题、复杂分析
    “智能选择”（默认）：本地 gemma 先判断这件事难不难（免费，约 0.3 秒），简单的用 Haiku、一般的用 Sonnet、
        复杂的用 Opus；便宜的没做成，自动换高一档的再试一次。
    在托盘右键 → 大脑 → “Claude 用哪个模型”里可以改，也能看到今天用了多少。
"""

import json
import os
import re
import threading
from datetime import date

import settings

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
USAGE_FILE = os.path.join(PROJECT_DIR, "logs", "claude_usage.json")
AUTO = "auto"

# effort（思考深度）：念出来的回答要快，所以普遍偏低；接管电脑（要动手、多步）比聊天高一点
MODELS = {
    "haiku": {"id": "claude-haiku-5-5", "name": "Haiku 5.5", "about": "最省，约 Opus 的 1/40",
              "chat": "low", "takeover": "low"},
    "sonnet": {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5", "about": "均衡，Opus 的一半",
               "chat": "low", "takeover": "medium"},
    "opus": {"id": "claude-opus-5-5", "name": "Opus 5.5", "about": "最强，最费额度",
             "chat": "low", "takeover": "medium"},
}
ORDER = ["haiku", "sonnet", "opus"]
LEVELS = {"简单": "haiku", "一般": "sonnet", "复杂": "opus"}

# 判断难度的函数（assistant.py 启动时填进来：用本地 gemma，免费）。没有的话按“一般”算
classifier = None

_lock = threading.Lock()


def current():
    """你选的是哪个：auto（智能）/ haiku / sonnet / opus。"""
    choice = settings.get("claude_model", AUTO)
    return choice if choice == AUTO or choice in MODELS else AUTO


def set_choice(choice):
    if choice != AUTO and choice not in MODELS:
        raise ValueError(f"没有这个模型：{choice}")
    settings.set("claude_model", choice)


def label(choice):
    return "智能选择（推荐）" if choice == AUTO else f"{MODELS[choice]['name']}（{MODELS[choice]['about']}）"


def choose(text, at_least=None):
    """
    这件事用哪个模型。返回 (模型代号, 原因)。
    at_least：最低用哪一档（比如本地没做成才交给 Claude 的，至少用 Sonnet）。
    """
    choice = current()
    if choice != AUTO:
        return choice, "你选的"
    level = "一般"
    if classifier:
        try:
            level = classifier(text) or "一般"
        except Exception:
            level = "一般"
    key = LEVELS.get(level, "sonnet")
    if at_least and ORDER.index(key) < ORDER.index(at_least):
        key = at_least
    return key, f"判断为“{level}”"


def stronger(key):
    """高一档的模型（已经是最强的就返回 None）。"""
    i = ORDER.index(key)
    return ORDER[i + 1] if i + 1 < len(ORDER) else None


FAILED = re.compile(r"没做成|没能做|没有成功|失败了|出错了|做不到|没办法完成")
REFUSED = re.compile(r"禁止|不允许|确定吗")


def looks_failed(result):
    """Claude 的这次回答看起来是不是“没做成”（是的话智能模式会换高一档再试）。"""
    if result.get("error"):
        return True
    if result.get("needs_confirm"):
        return False
    reply = result.get("reply", "")
    return bool(FAILED.search(reply)) and not REFUSED.search(reply)


# ---------------------------------------------------------------------------
# 用量：每次记下按 API 价格折算的花费（Pro 会员不额外扣钱，只是代表占了多少额度）
# ---------------------------------------------------------------------------

def _load():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record(key, cost_usd):
    """记一笔：今天哪个模型用了多少。"""
    if cost_usd is None:
        return
    with _lock:
        data = _load()
        day = data.setdefault(date.today().isoformat(), {"usd": 0.0, "runs": 0, "models": {}})
        day["usd"] = round(day["usd"] + float(cost_usd), 4)
        day["runs"] += 1
        model = day["models"].setdefault(key, {"usd": 0.0, "runs": 0})
        model["usd"] = round(model["usd"] + float(cost_usd), 4)
        model["runs"] += 1
        for old in sorted(data)[:-60]:  # 只留最近 60 天
            del data[old]
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USAGE_FILE)


def today():
    """今天的用量：{"usd": 折算美元, "runs": 次数, "models": {...}}。"""
    return _load().get(date.today().isoformat(), {"usd": 0.0, "runs": 0, "models": {}})


def today_text():
    day = today()
    if not day["runs"]:
        return "今天还没用 Claude"
    parts = "，".join(f"{MODELS[k]['name']} {v['runs']} 次" for k, v in day["models"].items() if k in MODELS)
    return f"今天 Claude 用了约 {day['usd']:.2f} 美元额度（{day['runs']} 次：{parts}）"
