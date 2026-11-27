"""
memory.py —— 助手的记忆

    ① 短期记忆 ShortTermMemory：最近 6 轮对话，放在内存里，每次都和你的新问题一起发给模型
                                  → 听得懂“调回去”“再大一点”“刚才那个”
    ② 历史记录 History：memory\\history\\日期.jsonl，一句话一行（只记文字，不存录音）
                         → 程序重启后能接上之前的对话；模型需要时可以回头查
    ③ 长期记忆 Profile：memory\\profile.md，关于你的习惯和喜好，每次对话都会带上
                         → 越用越懂你
    ④ 暂存区 pending：对话窗口里“不是对助手说的话”（闲聊、队友语音、游戏声音），
                      只放在内存里，不写硬盘；窗口关闭后由“复盘”决定留下哪些，其余直接丢掉

memory 文件夹里的文件都能用记事本打开，你可以随时查看、修改或删除。
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(PROJECT_DIR, "memory")

SHORT_TERM_TURNS = 6     # 短期记忆：记住最近几轮对话
RESUME_MINUTES = 30      # 程序重启时，把最近 30 分钟内的对话接回短期记忆
SEARCH_LIMIT = 10        # 查历史时最多返回几条

PROFILE_HEADER = (
    "# 关于你的长期记忆\n\n"
    "（助手会把你让它记住的事、复盘时发现的习惯写在下面，每次对话都会参考。"
    "你也可以直接用记事本修改或删除。）\n\n"
)


def _time(record):
    return datetime.fromisoformat(record["time"])


# ---------------------------------------------------------------------------
# ① 短期记忆
# ---------------------------------------------------------------------------


class ShortTermMemory:
    """内存里的最近几轮对话。每一轮是一组发给模型的消息（你说的话、调用的工具、结果、回复）。"""

    def __init__(self, max_turns=SHORT_TERM_TURNS):
        self.turns = deque(maxlen=max_turns)  # 满了以后，最旧的一轮会自动被挤出去

    def add(self, turn_messages):
        self.turns.append(list(turn_messages))

    def messages(self):
        return [m for turn in self.turns for m in turn]

    def __len__(self):
        return len(self.turns)


def record_to_messages(record):
    """把历史记录里的一行，还原成发给模型的消息格式（重启后接回短期记忆时用）。"""
    messages = [{"role": "user", "content": record["you"]}]
    actions = record.get("actions") or []
    if actions:
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": a["tool"], "arguments": a["args"]}} for a in actions],
        })
        for a in actions:
            messages.append({"role": "tool", "tool_name": a["tool"],
                             "content": json.dumps(a["result"], ensure_ascii=False)})
    messages.append({"role": "assistant", "content": record.get("reply", "")})
    return messages


def interruption_note(heard):
    tail = heard.strip()[-30:]
    return (f"\n（这段话只念到“{tail}”就被用户打断了，后面的用户没听到）" if tail
            else "\n（这段话刚开口就被用户打断了，用户没听到）")


# ---------------------------------------------------------------------------
# ② 历史记录
# ---------------------------------------------------------------------------


def _summary(record):
    """把一条历史记录写成一句话，例如：2026-11-20 14:05 用户说「把音量调到30」→ 音量已从 14% 设为 30%"""
    when = record["time"].replace("T", " ")[:16]
    did = "；".join(a["result"].get("message", "") for a in record.get("actions") or [])
    return f"{when} 用户说「{record['you']}」→ {did or record.get('reply') or record.get('note', '')}"


class History:
    """硬盘上的历史记录：每天一个 .jsonl 文件，每句话一行。"""

    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()  # 复盘在后台线程里写，加把锁防止两边同时写乱
        os.makedirs(folder, exist_ok=True)

    def append(self, record):
        path = os.path.join(self.folder, f"{_time(record):%Y-%m-%d}.jsonl")
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _files(self):
        return sorted(name for name in os.listdir(self.folder) if name.endswith(".jsonl"))

    def _read(self, name):
        """读一天的记录，按时间排好（复盘时补写的记录可能排在后面）。"""
        records = []
        with open(os.path.join(self.folder, name), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # 被手动改坏的行，跳过，不让程序崩溃
        return sorted(records, key=_time)

    def recent(self, minutes, now=None):
        """最近 minutes 分钟内的记录（从旧到新）。"""
        since = (now or datetime.now()) - timedelta(minutes=minutes)
        records = []
        for name in self._files()[-2:]:  # 只看最近两天的文件就够了（跨过午夜也没问题）
            records += [r for r in self._read(name) if _time(r) >= since]
        return records

    def search(self, keyword="", date="", limit=SEARCH_LIMIT):
        """
        查历史记录（这是给模型用的工具）。
            keyword  关键词，可以用空格隔开几个，命中任意一个就算
            date     日期，格式 YYYY-MM-DD；不填就查所有日期
        返回最新的若干条。
        """
        keywords = str(keyword or "").split()
        date = str(date or "").strip()
        try:
            limit = max(1, min(int(limit or SEARCH_LIMIT), 20))
        except (TypeError, ValueError):
            limit = SEARCH_LIMIT

        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return {"count": 0, "records": [], "message": f"日期格式不对：{date}，应该像 2026-11-20 这样"}
            names = [n for n in self._files() if n == f"{date}.jsonl"]
        else:
            names = self._files()

        found = []
        for name in reversed(names):                  # 从最新的日期往前找
            for record in reversed(self._read(name)):  # 每天里也从最新往前
                text = _summary(record)
                if not keywords or any(k in text for k in keywords):
                    found.append(text)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break

        message = f"找到 {len(found)} 条记录" if found else "没有找到相关的历史记录"
        return {"count": len(found), "records": found, "message": message}


# ---------------------------------------------------------------------------
# ③ 长期记忆
# ---------------------------------------------------------------------------


class Profile:
    """memory\\profile.md：关于你的长期信息，一行一条，以“- ”开头。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(PROFILE_HEADER)

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return f.readlines()

    def facts(self):
        return [line.strip()[2:] for line in self._lines() if line.strip().startswith("- ")]

    def remember(self, fact, source="记"):
        """记住一件关于用户的事（这是给模型用的工具）。"""
        fact = str(fact or "").strip().rstrip("。.")
        if not fact:
            return {"message": "没有要记住的内容"}
        with self._lock:
            if any(fact in existing for existing in self.facts()):
                return {"message": f"这件事我已经记得了：{fact}"}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"- {fact}（{datetime.now():%Y-%m-%d} {source}）\n")
        return {"message": f"好的，我记住了：{fact}"}

    def forget(self, keyword):
        """忘掉和关键词有关的记忆（这是给模型用的工具）。"""
        keyword = str(keyword or "").strip()
        chars = [c for c in keyword if c.strip() and c not in "，。、的了"]
        if not chars:
            return {"message": "没说要忘掉什么"}

        def matches(line):
            # 关键词原样出现，或者关键词里大部分字都出现在这条记忆里（模型转述时字词可能略有不同）
            return keyword in line or sum(c in line for c in chars) / len(chars) >= 0.75

        with self._lock:
            lines = self._lines()
            removed = [line for line in lines if line.strip().startswith("- ") and matches(line)]
            if not removed:
                return {"message": f"没有找到和「{keyword}」有关的记忆"}
            with open(self.path, "w", encoding="utf-8") as f:
                f.writelines(line for line in lines if line not in removed)
        return {"message": "已经忘掉：" + "；".join(line.strip()[2:] for line in removed)}


# ---------------------------------------------------------------------------
# 把几层记忆放在一起
# ---------------------------------------------------------------------------


class Memory:
    def __init__(self, folder=DEFAULT_DIR, resume=True):
        """
        folder  记忆文件夹（测试时会用临时文件夹，不影响你真正的记忆）
        resume  是否把最近 RESUME_MINUTES 分钟的对话接回短期记忆（模拟“重启后还记得”）
        """
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.short_term = ShortTermMemory()
        self.history = History(os.path.join(folder, "history"))
        self.profile = Profile(os.path.join(folder, "profile.md"))
        self._lock = threading.Lock()
        self.window_records = []  # 这个对话窗口里执行过的指令（已写进历史记录，复盘时作参考）
        self.pending = []         # ④ 暂存区：这个对话窗口里“不是对助手说的话”，只在内存里
        if resume:
            for record in self.history.recent(RESUME_MINUTES):
                self.short_term.add(record_to_messages(record))
        self.resumed = len(self.short_term)

    def add_turn(self, text, actions, reply, turn_messages, when, *, track_window=True):
        """对助手说的一轮对话结束：放进短期记忆，并马上写进历史记录。"""
        record = {"time": when.isoformat(timespec="seconds"), "you": text, "actions": actions, "reply": reply}
        self.history.append(record)
        self.short_term.add(turn_messages)
        if track_window:
            with self._lock:
                self.window_records.append(record)

    def mark_interrupted(self, heard):
        """
        刚才的回答念到一半被你打断了（像 ChatGPT 通话模式那样）：在短期记忆里注明你只听到了哪部分，
        下一句模型就知道“我刚才的话没说完”，不会以为你全听到了。历史记录里还是完整的回答。
        """
        if not self.short_term.turns:
            return False
        turn = self.short_term.turns[-1]
        note = interruption_note(heard)
        for i in range(len(turn) - 1, -1, -1):
            message = turn[i]
            try:
                if message.get("role") == "assistant" and message.get("content"):
                    turn[i] = {**dict(message), "content": message.get("content") + note}
                    return True
            except Exception:
                return False
        return False

    def add_pending(self, text, when, reply=""):
        """可能不是对助手说的话：先放进暂存区（内存），等复盘决定。reply 是助手当时的回答（如果有）。"""
        with self._lock:
            self.pending.append({"time": when.isoformat(timespec="seconds"), "you": text, "reply": reply})

    def take_window(self):
        """取出这个对话窗口的内容交给复盘，并清空，准备迎接下一个窗口。"""
        with self._lock:
            records, pending = self.window_records, self.pending
            self.window_records, self.pending = [], []
        return records, pending

    def status(self):
        """一句话说明当前记忆的情况（程序启动时显示）。"""
        return (f"短期记忆接上了最近 {self.resumed} 轮对话；长期记忆 {len(self.profile.facts())} 条；"
                f"历史记录保存在 {os.path.relpath(self.history.folder, PROJECT_DIR)}")
