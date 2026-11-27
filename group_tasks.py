"""
group_tasks.py —— AI 群聊的“任务卡”（把一条群消息变成一件要推进的事）

    点消息旁边的“转为任务”：同一条消息只建一张卡。卡上记：
        原话、谁负责（沿用点名：@了谁就是谁；没 @ 就是群里所有 AI）、
        目前进展、分歧（谁和谁意见不一样）、等你拍板的问题、状态（进行中 / 待拍板 / 已完成）
    摘要怎么来：这张卡是“当前任务”时，每轮群聊结束后，本地 Gemma 把相关消息整理进卡片（免费、不联网）；
        也可以点“更新摘要”马上整理。
    “继续任务”：把卡片的要点作为你的一句话发进群里，点名负责的 AI 接着推进（有执行权的那个能直接动手）。
        也可以直接在群里说“继续任务：打断”（按标题找卡）。
    清空聊天不影响任务卡；数据存在 groupchat/tasks.json。
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime

MSG_CHARS = 600     # 整理时每条消息最多看多少字
BATCH = 40          # 整理时最多看多少条消息

PROMPT = """你是一个 AI 群聊的秘书，负责维护一张“任务卡”。群里有用户（群主，显示为“我”）和几个 AI（{members}）。
下面是【旧任务卡】和这件事相关的【群消息】。请更新任务卡，只输出一个 JSON 对象，字段：
- "title"：任务标题，不超过 20 个字，说清楚要做什么
- "progress"：目前进展，2～4 句：做到哪一步了、查到了什么、改了哪些文件；没有进展就写“还没开始”
- "disagreements"：数组，每条一句话写清“谁主张什么、谁主张什么”；没有分歧就是 []
- "decisions"：数组，需要用户（群主）拍板才能往下走的问题，每条一句话；用户已经回答过的不要再列；没有就是 []
- "next"：下一步该谁做什么，一句话
要求：用简体中文；只根据消息内容，不要编造；旧卡片里仍然成立的内容保留。

【旧任务卡】
{old}

【群消息（从旧到新）】
{messages}"""

_lock = threading.Lock()
_running = set()     # 正在整理的任务 id
_focus = {"id": None}  # 当前任务：每轮群聊结束后自动整理它


def _path():
    import group_projects   # 每个项目各自的任务卡
    return os.path.join(group_projects.data_dir(), "tasks.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(data):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _publish():
    import status
    status.publish({"type": "group_tasks"})


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _status(task):
    if task.get("done"):
        return "已完成"
    return "待拍板" if task.get("decisions") else "进行中"


def public():
    """给界面看的任务列表（新的在前）。"""
    out = []
    for t in reversed(_load()):
        out.append({**t, "status": _status(t), "running": t["id"] in _running, "focus": _focus["id"] == t["id"]})
    return out


def find(task_id):
    return next((t for t in _load() if t["id"] == task_id), None)


def _update(task_id, **changes):
    with _lock:
        data = _load()
        for t in data:
            if t["id"] == task_id:
                t.update(changes)
                t["updated"] = _now()
                _save(data)
                return t
    return None


def _title_from(text):
    lines = (text or "").strip().splitlines()
    line = re.sub(r"@\S+\s*", "", lines[0] if lines else "")
    line = re.sub(r"\s+", " ", line).strip(" ，。,.!！?？")
    return (line[:24] + "…") if len(line) > 24 else (line or "未命名任务")


def _owners(msg):
    """谁负责：你的话 → @了谁 / 叫了谁就是谁，没点名就是群里所有 AI；AI 的话 → 它自己 + 它 @ 的。"""
    import groupchat
    group = [m for m in groupchat.members() if m.get("enabled")]
    if msg.get("role") == "user":
        picked = groupchat.addressed(msg.get("text", ""), group) or group
    else:
        speaker = next((m for m in group if m["id"] == msg.get("member_id")), None)
        picked = ([speaker] if speaker else []) + groupchat.mentioned(msg.get("text", ""), group)
        picked = picked or group
    seen, out = set(), []
    for m in picked:
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append({"id": m["id"], "name": m["name"], "color": m.get("color", "#888")})
    return out


def create(msg_id):
    """把一条群消息转成任务卡。同一条消息只建一张（再点一次返回原来那张）。"""
    import groupchat
    if not isinstance(msg_id, str) or not msg_id:
        return {"ok": False, "message": "没有消息编号"}
    msg = next((m for m in groupchat.history(5000) if m.get("id") == msg_id), None)
    if msg is None or msg.get("role") not in ("user", "ai"):
        return {"ok": False, "message": "找不到这条消息（可能已经清空）"}
    with _lock:
        data = _load()
        old = next((t for t in data if t.get("msg_id") == msg_id), None)
        if old:
            _focus["id"] = old["id"]
            return {"ok": True, "task": old, "existed": True, "message": "这条消息已经有任务卡了"}
        who = groupchat.USER_NAME if msg["role"] == "user" else msg.get("name", "某个 AI")
        task = {"id": uuid.uuid4().hex[:8], "msg_id": msg_id, "title": _title_from(msg.get("text", "")),
                "origin": msg.get("text", ""), "origin_by": who, "origin_time": msg.get("time", ""),
                "owners": _owners(msg), "progress": "", "disagreements": [], "decisions": [], "next": "",
                "done": False, "created": _now(), "updated": _now(), "summarized": "", "error": ""}
        data.append(task)
        _save(data)
    _focus["id"] = task["id"]
    groupchat._note(f"📌 建了任务卡「{task['title']}」，负责：{'、'.join(o['name'] for o in task['owners'])}。"
                    f"之后每轮聊完会自动更新进展；点“任务”里的“继续任务”接着推进。")
    refresh(task["id"])  # 后台先整理一次（把这条消息之后已经聊过的也算进去）
    _publish()
    return {"ok": True, "task": task, "message": "已转为任务"}


def _related(task):
    """和这张卡有关的消息：原消息开始往后（清空过聊天就从现在的记录开头算）。"""
    import groupchat
    msgs = groupchat.history(2000)
    ids = [m.get("id") for m in msgs]
    if task.get("msg_id") in ids:
        msgs = msgs[ids.index(task["msg_id"]):]
    else:
        start = task.get("created", "")
        msgs = [m for m in msgs if str(m.get("time", "")) >= start] or msgs[-BATCH:]
    return [m for m in msgs if m.get("role") in ("user", "ai")]


def _line(msg):
    who = "我（群主）" if msg["role"] == "user" else msg.get("name", "某个 AI")
    return f"[{str(msg.get('time', ''))[5:16].replace('T', ' ')}] {who}：" + re.sub(r"\s+", " ", str(msg.get("text", "")))[:MSG_CHARS]


def _card_text(task):
    return json.dumps({k: task.get(k) for k in ("title", "progress", "disagreements", "decisions", "next")},
                      ensure_ascii=False)


def _summarize(task, msgs):
    import local_models

    import assistant  # 和星期八、群聊记忆用同一个本地模型，免得反复加载
    import groupchat
    names = "、".join(m["name"] for m in groupchat.members() if m.get("enabled")) or "无"
    first = [msgs[0]] if msgs and msgs[0].get("id") == task.get("msg_id") else []
    batch = first + [m for m in msgs[-BATCH:] if m not in first]  # 原话一直留着，加最近的
    prompt = PROMPT.format(members=names, old=_card_text(task), messages="\n".join(_line(m) for m in batch))
    response = local_models.chat(think=False, format="json",
                           messages=[{"role": "user", "content": prompt}],
                           options=assistant.OPTIONS, keep_alive=assistant.KEEP_ALIVE)
    data = json.loads((response.message.content or "").strip())
    if not isinstance(data, dict):
        raise RuntimeError("整理结果不像样")

    def items(v):
        return [str(x).strip() for x in v if str(x).strip()][:6] if isinstance(v, list) else []

    out = {"progress": str(data.get("progress") or task.get("progress") or "").strip()[:600],
           "disagreements": items(data.get("disagreements")), "decisions": items(data.get("decisions")),
           "next": str(data.get("next") or "").strip()[:200]}
    title = str(data.get("title") or "").strip()
    if title and not task.get("title_locked"):
        out["title"] = title[:30]
    return out


def refresh(task_id, force=True):
    """在后台用本地 Gemma 整理这张卡。force=False：没有新消息就不整理。返回是否开始整理。"""
    task = find(task_id)
    if task is None or task_id in _running:
        return False
    msgs = _related(task)
    if not msgs or (not force and msgs[-1].get("id") == task.get("summarized")):
        return False
    _running.add(task_id)

    def work():
        try:
            changes = _summarize(task, msgs)
            _update(task_id, **changes, summarized=msgs[-1].get("id"), error="")
        except Exception as e:  # 本地模型没开、出错：卡片不动，下次再整理
            why = "本地模型没开（Ollama 没在运行），开了以后点“更新摘要”" if "connect" in str(e).lower() else str(e)[:80]
            _update(task_id, error=f"整理没成功：{why}")
        finally:
            _running.discard(task_id)
            _publish()

    threading.Thread(target=work, daemon=True).start()
    _publish()
    return True


def after_turns():
    """每轮群聊结束后调用：当前任务有新消息就整理一次。"""
    task = find(_focus["id"]) if _focus["id"] else None
    if task and not task.get("done"):
        refresh(task["id"], force=False)


CONTINUE_HINT = ("（用户点了“继续任务”，要接着上面这张任务卡往下推进。先看进展和分歧，需要就去查证；"
                 "有执行权的按分工动手，其他人补充、反驳或核对。需要用户拍板的问题放在最后单独列出来。）")


def continue_task(task_id, wait=False, *, generation=None):
    """把卡片要点作为你的一句话发进群里，点名负责的 AI 接着推进。"""
    import groupchat
    if generation is None:
        generation = groupchat._run_generation
    task = find(task_id)
    if task is None:
        return {"ok": False, "message": "没有这张任务卡"}
    if task.get("done"):
        _update(task_id, done=False)
    _focus["id"] = task_id
    group = {m["id"]: m for m in groupchat.members() if m.get("enabled")}
    owners = [group[o["id"]] for o in task.get("owners") or [] if o["id"] in group] or list(group.values())
    parts = [f"▶ 继续任务「{task['title']}」" + "".join(f" @{m['name']}" for m in owners),
             f"原话（{task.get('origin_by', '')}）：{_short(task.get('origin', ''), 300)}"]
    if task.get("progress"):
        parts.append(f"目前进展：{task['progress']}")
    if task.get("disagreements"):
        parts.append("分歧：\n" + "\n".join(f"- {x}" for x in task["disagreements"]))
    if task.get("decisions"):
        parts.append("等我拍板：\n" + "\n".join(f"- {x}" for x in task["decisions"]))
    if task.get("next"):
        parts.append(f"下一步：{task['next']}")
    groupchat._append(groupchat._message("user", "\n".join(parts), name=groupchat.USER_NAME, task_id=task_id))
    _publish()
    result = groupchat._ask_all(owners, CONTINUE_HINT, wait, from_user=True, generation=generation)
    return {**result, "message": f"继续任务：{task['title']}"}


def _short(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


CONTINUE_RE = re.compile(r"^\s*(继续|接着)任务\s*[:：]?\s*(.*)$")


def match_continue(text):
    """群里说“继续任务：打断”：按标题 / 原话找卡（不写名字 = 当前任务或最近一张没完成的）。找不到返回 None。"""
    m = CONTINUE_RE.match(text or "")
    if not m:
        return None
    key = m.group(2).strip(" 。！!")
    tasks = list(reversed(_load()))
    if not key:
        cur = find(_focus["id"]) if _focus["id"] else None
        return cur or next((t for t in tasks if not t.get("done")), None)
    for t in tasks:
        if key in t.get("title", "") or key in t.get("origin", ""):
            return t
    return None


def set_done(task_id, done):
    task = _update(task_id, done=bool(done))
    if task is None:
        return {"ok": False, "message": "没有这张任务卡"}
    if done and _focus["id"] == task_id:
        _focus["id"] = None
    _publish()
    return {"ok": True, "message": "标为已完成" if done else "重新打开了"}


def rename(task_id, title):
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:30]
    if not title:
        return {"ok": False, "message": "标题不能为空"}
    task = _update(task_id, title=title, title_locked=True)  # 你改过的标题，自动整理不再覆盖
    _publish()
    return {"ok": bool(task)}


def delete(task_id):
    with _lock:
        data = _load()
        left = [t for t in data if t["id"] != task_id]
        if len(left) == len(data):
            return {"ok": False, "message": "没有这张任务卡"}
        _save(left)
    if _focus["id"] == task_id:
        _focus["id"] = None
    _publish()
    return {"ok": True}
