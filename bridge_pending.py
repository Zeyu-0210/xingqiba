"""调度员挂起的任务：到点的（“四十分钟后”“明天早上七点”）和等条件的（“洗完澡后”）。

    定时任务：把 when 解析成具体时间存进 groupchat\\pending.json（重启不丢），后台每 15 秒看一次，到点就办。
    条件任务：先记着；主人之后说的话像是条件满足了（“我洗完了”），就把它派出去。
    “提醒”由星期八自己做：到点用语音说出来（老人最常用：吃药、起床、打电话）。
    派给还没接入的成员（家电、美团……）：到点时照样说出来，请主人自己处理，并在群里留一条。
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

import friendly

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_FILE = os.path.join(PROJECT_DIR, "groupchat", "pending.json")
CHECK_SECONDS = 15
SPEAK_WAIT_SECONDS = 120   # 到点时星期八正在和人说话：最多等这么久再说，免得插嘴
IDLE_STATES = ("待命中", "已暂停")
LOCAL = ("提醒", "星期八")  # 这两个由星期八自己办，不算“未接入”

_lock = threading.Lock()
_thread = None

# ---------------------------------------------------------------------------
# 时间解析（固定规则，不用模型）
# ---------------------------------------------------------------------------

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"[0-9]+|[零〇一二两三四五六七八九十]+"


def cn_number(text):
    """“四十”“二十五”“两”“12” → 整数；认不出返回 None。"""
    if text.isdigit():
        return int(text)
    if not text or any(c not in _DIGITS and c != "十" for c in text):
        return None
    if "十" not in text:
        value = 0
        for c in text:
            value = value * 10 + _DIGITS[c]
        return value
    tens, _, ones = text.partition("十")
    return (_DIGITS.get(tens, 1) if tens else 1) * 10 + (_DIGITS.get(ones, 0) if ones else 0)


_RELATIVE = re.compile(r"(?P<n>" + _NUM + r"|半)?\s*个?\s*(?P<half>半)?\s*(?P<unit>分钟|分|小时|钟头|秒钟|秒)\s*(?:后|以后|之后|过后)")
_DAY = {"今天": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2}
_ABSOLUTE = re.compile(
    r"(?P<day>今天|今晚|明天|明早|明晚|后天)?\s*(?P<part>早上|早晨|上午|中午|下午|傍晚|晚上|夜里|凌晨)?\s*"
    r"(?P<h>" + _NUM + r")\s*(?:点|:|：|时)\s*(?P<m>半|一刻|三刻|" + _NUM + r")?\s*分?")


def parse_when(when, now=None):
    """把 when 变成具体时间。返回 datetime（定时）或 None（是个条件，或者认不出）。"""
    now = now or datetime.now()
    text = (when or "").strip()
    m = _RELATIVE.search(text)
    if m:
        n = m.group("n")
        amount = 0.5 if n == "半" else (cn_number(n) if n else 1)
        if amount is None:
            return None
        if m.group("half"):
            amount += 0.5
        unit = m.group("unit")
        seconds = amount * (3600 if unit in ("小时", "钟头") else 60 if unit in ("分钟", "分") else 1)
        return now + timedelta(seconds=seconds)
    m = _ABSOLUTE.search(text)
    if m:
        hour = cn_number(m.group("h"))
        minute_text = m.group("m")
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(minute_text) if minute_text else 0
        if minute is None:
            minute = cn_number(minute_text)
        if hour is None or minute is None or hour > 24 or minute > 59:
            return None
        part = m.group("part") or ("晚上" if m.group("day") in ("今晚", "明晚") else
                                   "早上" if m.group("day") == "明早" else None)
        if part in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
            hour += 12
        elif part == "中午" and hour < 11:
            hour += 12
        hour %= 24
        day = m.group("day")
        due = (now + timedelta(days=_DAY.get(day, 0))).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day is None and due <= now:
            due += timedelta(days=1)  # 没说哪天、时间已经过了：就是明天这个点
        return due
    return None


# ---------------------------------------------------------------------------
# 条件匹配（固定规则）：“洗完澡后” ↔ “我洗完了”
# ---------------------------------------------------------------------------

_CONDITION_NOISE = re.compile(r"如果|要是|的话|等到?|以后|之后|后|的时候|时候|我|你|完|好|了|再|就|一下|把|先")
_DONE = re.compile(r"完了|完啦|好了|好啦|回来了|到家了|到了|结束了|洗完|吃完|弄完|忙完|搞定")


def condition_met(condition, text):
    """主人这句话是不是在说条件满足了：要有“完了/好了/回来了”这类词，并且和条件说的是同一件事。"""
    if not _DONE.search(text):
        return False
    key = [c for c in _CONDITION_NOISE.sub("", condition) if "一" <= c <= "鿿"]
    if not key:
        return False
    hits = sum(1 for c in set(key) if c in text)
    return hits / len(set(key)) >= 0.5


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------


def _load():
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(items):
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PENDING_FILE)


def add(task, now=None):
    """记下一件挂起的任务。返回记录（带 due 或 condition）。"""
    now = now or datetime.now()
    due = parse_when(task.get("when"), now)
    item = {"id": uuid.uuid4().hex[:6], "to": task["to"], "do": task["do"], "when": task.get("when") or "",
            "ask": task.get("ask") or "", "created": now.isoformat(timespec="seconds"),
            "due": due.isoformat(timespec="seconds") if due else None,
            "condition": None if due else (task.get("when") or "")}
    with _lock:
        items = _load()
        items.append(item)
        _save(items)
    return item


def describe(item):
    """给人看的一句话：“15:40 提醒：吃药”“等“洗完澡后”：开电视（家电）”。"""
    if item.get("due"):
        due = datetime.fromisoformat(item["due"])
        day = "" if due.date() == datetime.now().date() else ("明天 " if due.date() == (datetime.now() + timedelta(days=1)).date() else f"{due:%m-%d} ")
        return f"{day}{due:%H:%M}（{item['to']}）：{item['do']}"
    return f"等“{item['condition']}”（{item['to']}）：{item['do']}"


def listing():
    items = _load()
    if not items:
        return "现在没有挂起的任务。"
    lines = [f"{i}. {describe(item)}" for i, item in enumerate(items, 1)]
    return "挂起的任务：\n" + "\n".join(lines) + "\n\n发“取消待办 序号”可以取消。"


def cancel(index):
    with _lock:
        items = _load()
        if not 1 <= index <= len(items):
            return f"没有第 {index} 条待办。"
        item = items.pop(index - 1)
        _save(items)
    return f"已取消：{describe(item)}"


def _take(predicate):
    """取出满足条件的任务（从文件里删掉），返回它们。"""
    with _lock:
        items = _load()
        hit = [item for item in items if predicate(item)]
        if hit:
            _save([item for item in items if item not in hit])
    return hit


def due_now(now=None):
    now = now or datetime.now()
    return _take(lambda item: item.get("due") and datetime.fromisoformat(item["due"]) <= now)


def conditions_met(text):
    return _take(lambda item: item.get("condition") and condition_met(item["condition"], text))


# ---------------------------------------------------------------------------
# 到点 / 条件满足：去办
# ---------------------------------------------------------------------------


def spoken(item, reason="到点了"):
    """要念给主人听的一句话。"""
    if item["to"] in LOCAL:
        # “提醒主人吃药” → “提醒你：吃药。”（去掉重复的“提醒主人”）
        do = re.sub(r"^(?:提醒|叫|喊)(?:一下)?(?:主人|我|你|他|她)?(?:一下)?", "", item["do"]).strip(" ，,") or item["do"]
        return f"提醒你：{do}。"
    return f"{reason}，{item['do']}。这个{item['to']}还没接好，需要你自己弄一下。"


def fire(items, reason="到点了", *, speak_out=True):
    """把一批到点/条件满足的任务办掉：群里留一条，派给能办的成员，需要的念出来。返回要说的话（可能为空）。"""
    if not items:
        return ""
    import groupchat

    members = {m["name"]: m for m in groupchat.members()}
    lines, targets, words = [], {}, []
    for item in items:
        member = members.get(item["to"])
        line = f"⏰ {reason}\n@{item['to']}\n任务：{item['do']}\n追问：{item['ask'] or '无'}"
        if member and member.get("enabled") and item["to"] not in LOCAL:
            targets[item["to"]] = member
        else:
            words.append(spoken(item, reason))
            if item["to"] not in LOCAL:
                line += "\n未接入，已提醒主人自己处理"
        lines.append(line)
    hint = "以下任务的时间到了或条件满足了，请处理 @ 给你的那件。有追问就先问主人。\n\n" + "\n\n".join(lines)
    try:
        groupchat.post_dispatch(lines, list(targets.values()), hint, wait=False,
                                generation=groupchat.dispatch_generation())
    except Exception:
        friendly.LOG.exception("挂起任务写进群聊失败")
    text = "".join(words)
    if text and speak_out:
        threading.Thread(target=_say_when_idle, args=(text,), daemon=True).start()
    return text


def _say_when_idle(text, stop=None):
    """等星期八不在和人说话时再念，免得插嘴；等太久就直接念。托盘也弹一条。"""
    import status

    if _notify is not None:
        try:
            _notify(text)
        except Exception:
            pass
    status.publish({"type": "reminder", "text": text})
    deadline = time.monotonic() + SPEAK_WAIT_SECONDS
    while time.monotonic() < deadline and status.current()["state"] not in IDLE_STATES:
        if stop is not None and stop.is_set():
            return
        time.sleep(1)
    try:
        import speak

        if speak.ENABLED:
            speak.Speaker().say(text)
    except Exception:
        friendly.LOG.exception("念提醒失败")


_notify = None


def start(stop, notify=None):
    """程序启动时调用：后台每 15 秒看一次有没有到点的任务（重启前挂起的也会接着算）。"""
    global _thread, _notify
    _notify = notify
    if _thread is not None:
        return

    def loop():
        while not stop.wait(CHECK_SECONDS):
            try:
                fire(due_now())
            except Exception:
                friendly.LOG.exception("检查挂起任务失败")

    _thread = threading.Thread(target=loop, name="bridge-pending", daemon=True)
    _thread.start()
