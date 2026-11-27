"""
group_memory.py —— AI 群聊的长期记忆（像秘书的笔记本）

    群里的 AI 每次回答，都会带上：这本笔记 + 最近 30 条群消息。
    笔记怎么来的：群里每多出 EVERY 条新消息，就让本地 Gemma（免费、不联网、不用额度）
        把“旧笔记 + 这些新消息”整理成新笔记（关于你、项目进度、定下来的事和分工、待办……）。
    你说“记住：……”，会原样记进笔记。你也可以在群聊页面右上角的“记忆”里直接看、直接改。
    清空聊天时，没整理的消息会先整理进笔记，所以清空了它也还记得。
    笔记存在 groupchat/memory.md；完整的聊天记录一直留在 groupchat/history*.jsonl，AI 需要时可以去翻。
"""

import json
import os
import re
import threading
from datetime import datetime

EVERY = 20         # 攒够这么多条新消息就整理一次
BATCH = 40         # 一次最多整理多少条（太多了本地模型看不过来）
MAX_CHARS = 3000   # 笔记最长多少字（每次回答都会带上，太长了费额度）
MSG_CHARS = 500    # 每条消息最多看多少字
SECTIONS = ["关于用户", "项目进度", "定下来的决定和分工", "待办和没解决的问题", "用户交代要记住的"]
REMEMBER = "用户交代要记住的"

PROMPT = """你是一个 AI 群聊的“秘书”，负责维护群里的长期记忆笔记。群里有用户（群主，显示为“我”）和几个 AI（{members}）。
下面是【旧笔记】和【新的群消息】。请把新消息里值得长期记住的内容合并进笔记，输出完整的新笔记。
要求：
- 用简体中文、Markdown，固定用这几个二级标题（## 开头）：{sections}
- 只记长期有用的：用户的习惯和要求、项目做到哪一步、改了哪些文件、定下来的决定和分工、还没做完的事。闲聊、客套话不记。
- 旧笔记里的内容要保留，除非新消息说明它已经过时或者做完了（做完的从“待办”挪到“项目进度”）。
- 写上日期（比如 11-21）；不要编造没出现过的事；“{remember}”这一节原样保留，不要改。
- 一共不超过 {max_chars} 字，写不下就把最旧、最不重要的合并精简。
- 只输出笔记本身，不要解释，不要用代码块包起来。

【旧笔记】
{old}

【新的群消息（从旧到新）】
{messages}"""

_lock = threading.Lock()
_running = {"on": False, "error": ""}


def _paths():
    import group_projects   # 每个项目一本笔记（默认项目还是原来的 groupchat/memory.md）
    folder = group_projects.data_dir()
    return os.path.join(folder, "memory.md"), os.path.join(folder, "memory_state.json")


def read():
    path, _ = _paths()
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save(text, publish=True):
    """把笔记存起来（你在界面上改的，或者整理好的）。"""
    path, _ = _paths()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write((text or "").strip() + "\n")
    os.replace(tmp, path)
    if publish:
        import status
        status.publish({"type": "group_memory"})
    return {"ok": True}


def _state():
    _, path = _paths()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(**changes):
    _, path = _paths()
    state = _state()
    state.update(changes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def pending(messages=None):
    """还没整理进笔记的群消息（不算“每一步”的细节）。"""
    import groupchat
    messages = groupchat.history(1000) if messages is None else messages
    done = _state().get("done")
    ids = [m.get("id") for m in messages]
    start = ids.index(done) + 1 if done in ids else 0
    return [m for m in messages[start:] if m.get("role") in ("user", "ai", "system")]


def info():
    """给界面看：还有多少条没整理、上次什么时候整理的、正在整理没有。"""
    state = _state()
    return {"pending": len(pending()), "every": EVERY, "updated": state.get("time", ""),
            "running": _running["on"], "error": _running["error"]}


def remember(text):
    """你说“记住：……”：原样记进笔记的“用户交代要记住的”那一节。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False}
    with _lock:
        note = read()
        line = f"- {datetime.now():%m-%d} {text}"
        head = f"## {REMEMBER}"
        if head in note:
            parts = note.split(head, 1)
            rest = parts[1].split("\n## ", 1)
            body = rest[0].rstrip() + "\n" + line
            note = parts[0] + head + body + ("\n\n## " + rest[1] if len(rest) > 1 else "")
        else:
            note = (note + "\n\n" if note else "") + head + "\n" + line
        save(note)
    return {"ok": True}


def _line(msg):
    when = str(msg.get("time", ""))[5:16].replace("T", " ")
    who = "我（群主）" if msg["role"] == "user" else "（系统）" if msg["role"] == "system" else msg.get("name", "某个 AI")
    text = re.sub(r"\s+", " ", str(msg.get("text", "")))[:MSG_CHARS]
    return f"[{when}] {who}：{text}"


def _summarize(old, batch):
    """让本地 Gemma 把“旧笔记 + 新消息”整理成新笔记。"""
    import local_models

    import assistant  # 用和星期八一样的模型和设置，免得模型反复重新加载
    import groupchat
    names = "、".join(m["name"] for m in groupchat.members() if m.get("enabled")) or "无"
    prompt = PROMPT.format(members=names, sections="、".join(SECTIONS), remember=REMEMBER, max_chars=MAX_CHARS,
                           old=old or "（还没有）", messages="\n".join(_line(m) for m in batch))
    response = local_models.chat(think=False, messages=[{"role": "user", "content": prompt}],
                           options=assistant.OPTIONS, keep_alive=assistant.KEEP_ALIVE)
    text = (response.message.content or "").strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    if len(text) < 20 or "##" not in text:
        raise RuntimeError("整理出来的笔记不像样，这次先不换")
    # “用户交代要记住的”一节以原来的为准（本地模型偶尔会改写或丢掉）
    head = f"## {REMEMBER}"
    if head in (old or ""):
        kept = head + old.split(head, 1)[1].split("\n## ", 1)[0]
        text = text.split(head, 1)[0].rstrip() if head in text else text
        text = text.rstrip() + "\n\n" + kept.strip()
    return text[:int(MAX_CHARS * 1.5)]


def update(force=False, messages=None):
    """整理笔记（会花几十秒，在后台调用）。messages：清空聊天前没整理的那些消息。返回整理了几条。"""
    with _lock:
        todo = pending() if messages is None else [m for m in messages if m.get("role") in ("user", "ai", "system")]
        if not todo or (len(todo) < EVERY and not force):
            return 0
        count = 0
        try:
            while todo and (force or len(todo) >= EVERY):
                batch, todo = todo[:BATCH], todo[BATCH:]
                save(_summarize(read(), batch), publish=False)
                count += len(batch)
                _save_state(done="" if messages is not None else batch[-1].get("id"),
                            time=datetime.now().isoformat(timespec="seconds"))
            _running["error"] = ""
        except Exception as e:  # 本地模型没开、出错：下次再整理
            _running["error"] = f"整理记忆没成功：{e}"[:120]
        import status
        status.publish({"type": "group_memory"})
        return count


def maybe_update(force=False, messages=None):
    """攒够了就在后台整理一次（同一时间只整理一个）。"""
    if _running["on"]:
        return False
    if messages is None and not force and len(pending()) < EVERY:
        return False

    def work():
        _running["on"] = True
        try:
            update(force, messages)
        finally:
            _running["on"] = False

    _running["on"] = True
    threading.Thread(target=work, daemon=True).start()
    return True
