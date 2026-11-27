"""
group_projects.py —— AI 群聊的“项目”：每个项目各有自己的聊天记录、长期记忆（秘书笔记）、任务卡和关联文件夹

    为什么要分项目：所有事混在一条聊天里，背景互相干扰；分开后切回某个项目，AI 接着那个项目的记录和笔记往下做。
    成员和执行权是全群共用的（同一批 AI，执行权同一时间仍只给一个）。

    存哪儿：
        groupchat/projects.json          项目列表和“当前项目”
        默认项目「星期八开发」          直接用原来的 groupchat/ 下的 history.jsonl、memory.md、tasks.json（不搬文件）
        其他项目                         groupchat/projects/<编号>/ 下同名的几个文件
    有 AI 正在回话、正在整理记忆/任务卡时不让切换（免得回复写进别的项目）。
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime

DEFAULT_ID = "xingqiba"
DEFAULT_NAME = "星期八开发"
MAX_NAME = 30
MAX_FOLDERS = 8

_lock = threading.RLock()


def _gc():
    import groupchat
    return groupchat


def _file():
    return os.path.join(_gc().DATA_DIR, "projects.json")


def _default_project():
    return {"id": DEFAULT_ID, "name": DEFAULT_NAME, "folders": [_gc().PROJECT_DIR],
            "created": datetime.now().isoformat(timespec="seconds")}


def _load():
    try:
        with open(_file(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise ValueError
    except (OSError, ValueError):
        data = {"current": DEFAULT_ID, "projects": []}
    projects = [p for p in data["projects"] if isinstance(p, dict) and p.get("id")]
    if not any(p["id"] == DEFAULT_ID for p in projects):
        projects.insert(0, _default_project())
    data["projects"] = projects
    if not any(p["id"] == data.get("current") for p in projects):
        data["current"] = DEFAULT_ID
    return data


def _save(data):
    path = _file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _publish():
    import status
    status.publish({"type": "group_project"})


def current_id():
    with _lock:
        return _load()["current"]


def current():
    with _lock:
        data = _load()
        return next(p for p in data["projects"] if p["id"] == data["current"])


def find(project_id):
    with _lock:
        return next((p for p in _load()["projects"] if p["id"] == project_id), None)


def data_dir(project_id=None):
    """这个项目的聊天记录、笔记、任务卡放在哪个文件夹。默认项目就是原来的 groupchat/。"""
    project_id = project_id or current_id()
    gc = _gc()
    if project_id == DEFAULT_ID:
        return gc.DATA_DIR
    return os.path.join(gc.DATA_DIR, "projects", project_id)


def history_file(project_id=None):
    project_id = project_id or current_id()
    if project_id == DEFAULT_ID:
        return _gc().HISTORY_FILE   # 测试会改这个路径，默认项目跟着它走
    return os.path.join(data_dir(project_id), "history.jsonl")


def _last_active(project_id):
    try:
        return datetime.fromtimestamp(os.path.getmtime(history_file(project_id))).isoformat(timespec="seconds")
    except OSError:
        return ""


def public():
    """给界面看：项目列表（当前的标出来）。"""
    with _lock:
        data = _load()
    return {"current": data["current"],
            "list": [{**p, "current": p["id"] == data["current"], "active": _last_active(p["id"])}
                     for p in data["projects"]]}


def _clean_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:MAX_NAME]
    return name


def _clean_folders(folders):
    if isinstance(folders, str):
        folders = [x for x in re.split(r"[\r\n;；]+", folders)]
    out = []
    for f in folders or []:
        f = str(f).strip().strip('"').strip()
        if f and f not in out:
            out.append(os.path.normpath(f))
    return out[:MAX_FOLDERS]


def busy_reason():
    """现在能不能切换项目：有 AI 在回话、在整理记忆/任务卡时不行。返回原因（能切就返回空字符串）。"""
    gc = _gc()
    import group_memory
    import group_tasks
    if gc._runs or any(gc._busy.values()):
        return "群里还有 AI 在回话或动手，等它说完（或点“强制停止”）再切换项目"
    if group_memory._running["on"] or group_tasks._running:
        return "正在整理记忆或任务卡，稍等几十秒再切换"
    return ""


def _switch_to(data, project_id):
    gc = _gc()
    data["current"] = project_id
    _save(data)
    gc._awaiting["member"] = None
    import group_tasks
    group_tasks._focus["id"] = None


def switch(project_id):
    with _lock:
        data = _load()
        if not any(p["id"] == project_id for p in data["projects"]):
            return {"ok": False, "message": "没有这个项目"}
        if data["current"] == project_id:
            return {"ok": True}
        why = busy_reason()
        if why:
            return {"ok": False, "message": why}
        _switch_to(data, project_id)
        name = next(p["name"] for p in data["projects"] if p["id"] == project_id)
    _publish()
    return {"ok": True, "message": f"切换到项目「{name}」"}


def create(name, folders=None, switch_now=True):
    name = _clean_name(name)
    if not name:
        return {"ok": False, "message": "项目名不能为空"}
    with _lock:
        data = _load()
        if any(p["name"] == name for p in data["projects"]):
            return {"ok": False, "message": f"已经有叫「{name}」的项目了"}
        if switch_now:
            why = busy_reason()
            if why:
                return {"ok": False, "message": why}
        project = {"id": uuid.uuid4().hex[:8], "name": name, "folders": _clean_folders(folders),
                   "created": datetime.now().isoformat(timespec="seconds")}
        data["projects"].append(project)
        os.makedirs(data_dir(project["id"]), exist_ok=True)
        if switch_now:
            _switch_to(data, project["id"])
        else:
            _save(data)
    _publish()
    return {"ok": True, "project": project, "message": f"建好了项目「{name}」" + ("，已切换过去" if switch_now else "")}


def update(project_id, name=None, folders=None):
    with _lock:
        data = _load()
        project = next((p for p in data["projects"] if p["id"] == project_id), None)
        if project is None:
            return {"ok": False, "message": "没有这个项目"}
        if name is not None:
            name = _clean_name(name)
            if not name:
                return {"ok": False, "message": "项目名不能为空"}
            if any(p["name"] == name and p["id"] != project_id for p in data["projects"]):
                return {"ok": False, "message": f"已经有叫「{name}」的项目了"}
            project["name"] = name
        if folders is not None:
            project["folders"] = _clean_folders(folders)
        _save(data)
    _publish()
    return {"ok": True, "message": "项目设置已保存"}


def prompt_text(project=None):
    """告诉 AI 现在在哪个项目、关联哪些文件夹。"""
    project = project or current()
    folders = "、".join(project.get("folders") or []) or "（还没关联文件夹）"
    return f"当前项目：「{project['name']}」，关联文件夹：{folders}。这次只谈这个项目的事。"
