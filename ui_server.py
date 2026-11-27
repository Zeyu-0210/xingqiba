"""
ui_server.py —— 星期八的窗口界面（后台部分）

    在本机开一个小网页服务（只有这台电脑能访问：127.0.0.1），界面文件在 ui 文件夹里，
    用 Windows 自带的 Edge 以“应用模式”打开（没有地址栏、标签页，看起来像一个独立的软件）。
    不用安装任何东西。

    安全：
      - 只监听 127.0.0.1，局域网里别的电脑连不上
      - 每次启动生成一个随机口令，界面打开时带着它；没有口令的请求一律拒绝
        （防止你浏览的其他网页偷偷调用这里的功能）
      - 密钥只会显示开头结尾几位，不会把完整密钥发给界面

    界面里能做的事：看状态、打字给星期八、AI 群聊、所有设置（以前在托盘右键菜单里的那些）。
"""

import ctypes
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import friendly
import settings
from bridge import VOICE_SETTING
import speech_endpoint
import status

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(PROJECT_DIR, "ui")
STATE_DIR = os.path.join(PROJECT_DIR, ".ui")
SERVER_FILE = os.path.join(STATE_DIR, "server.json")   # 端口和口令：再次点快捷方式时用来打开窗口
EDGE_PROFILE = os.path.join(STATE_DIR, "edge")         # 界面专用的 Edge 配置（和你平时上网的 Edge 分开）
PORT = 18089
WINDOW_TITLE = "星期八"
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
          "/icon.svg": "icon.svg"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
FOLDERS = {"tasks": ("workers", "TASKS_DIR"), "memory": ("memory", "DEFAULT_DIR"), "research": ("research", "REPORT_DIR")}

_clients = []          # 正在看界面的窗口（每个一个消息队列）
_clients_lock = threading.Lock()
_server = None
_token = None
_typing_lock = threading.Lock()
quit_app = None        # app.py 填进来：彻底退出的函数


# ---------------------------------------------------------------------------
# 推送消息给界面（Server-Sent Events）
# ---------------------------------------------------------------------------


def broadcast(event):
    data = json.dumps(event, ensure_ascii=False)
    with _clients_lock:
        for q in list(_clients):
            if q.qsize() < 500:
                q.put(data)


def _on_status(state, detail):
    st = status.current()
    broadcast({"type": "status", "state": state, "detail": detail, "since": st["since"],
               "style": list(status.style(state))})


def _level_pump():
    """在听的时候，每 0.1 秒把麦克风音量发给界面（音量条用）。"""
    while True:
        time.sleep(0.1)
        if _clients and status.style(status.current()["state"])[1] == "bars":
            broadcast({"type": "level", "value": round(status.current()["level"], 4)})


# ---------------------------------------------------------------------------
# 界面要的数据
# ---------------------------------------------------------------------------


def state():
    import brain_claude_code
    import brains
    import claude_models
    import dictation
    import duplex
    import listen
    import local_models
    import setup_shortcuts
    import speak
    import transcription
    st = status.current()
    research = {}
    try:
        import research as research_mod
        research = research_mod.latest() or {}
    except Exception:
        pass
    login = brain_claude_code.cached_login()
    claude_login = bool(login and login.logged_in)
    login_pending = login is None
    mac = sys.platform == "darwin"
    return {
        **({"mac": True} if mac else {}),
        "status": {"state": st["state"], "detail": st["detail"], "since": st["since"],
                   "style": list(status.style(st["state"]))},
        "styles": {k: list(v) for k, v in status.STYLES.items()},
        "paused": listen.PAUSED.is_set(),
        "brain": brains.current(),
        "brains": [{"name": n, "about": c["about"],
                    "note": brains.note(n, claude_login=claude_login, login_pending=login_pending).strip(" ←"),
                    **({"available": False} if mac and n == brains.TAKEOVER else {})}
                   for n, c in brains.BRAINS.items()],
        "claude_model": claude_models.current(),
        "claude_models": [{"id": c, "label": claude_models.label(c)} for c in [claude_models.AUTO] + claude_models.ORDER],
        "usage_today": claude_models.today_text(),
        "claude_login": claude_login,
        "claude_login_pending": login_pending,
        "voice_mode": listen.voice_mode(),
        "transcription_model": transcription.current(),
        "reasoning_model": local_models.reasoning_model(),
        "model_slots": {
            "voice": local_models.voice_choices(),
            "transcription": transcription.model_choices(),
            "reasoning": local_models.choices(local_models.REASONING_MODELS),
        },
        "dictation": dictation.state(),
        "voice": speak.current_voice(),
        "voices": [{"id": k, "name": n, "about": a} for k, (n, a) in speak.VOICES.items()],
        "overlay_mode": "off" if mac else settings.get("overlay_mode", "auto"),
        "barge_in": settings.get("barge_in", True),
        VOICE_SETTING: bool(settings.get(VOICE_SETTING, False)),
        "echo_reference": duplex.echo_reference(),
        "slow_speech": bool(settings.get(speech_endpoint.SLOW_SPEECH_SETTING, False)),
        "follow_up": settings.get("follow_up_seconds", listen.FOLLOW_UP),
        "follow_up_choices": [{"value": k, "label": v} for k, v in listen.FOLLOW_UP_CHOICES.items()],
        "barge_in_available": duplex.available(),
        "auto_help": settings.get("auto_help", True),
        "autostart": setup_shortcuts.autostart_enabled(),
        "wake_words": list(listen.wakeword.WAKE_WORDS),
        "research": {k: research.get(k) for k in ("time", "projects", "summary", "top", "report") if k in research},
    }


def chat_history(limit=60):
    import assistant
    records = assistant.MEMORY.history.recent(24 * 60)[-limit:]
    return [{"time": r.get("time"), "you": r.get("you"), "reply": r.get("reply", ""),
             "actions": [a.get("tool") for a in r.get("actions") or []]} for r in records]


def group_state():
    import agent_codex
    import claude_models
    import group_memory
    import group_projects
    import group_tasks
    import groupchat
    return {"members": groupchat.public_members(), "history": groupchat.history(),
            "presets": [{"id": k, "name": v["name"], "about": v["about"], "color": v["color"], "kind": v["kind"]}
                        for k, v in groupchat.PRESETS.items()],
            "keys": groupchat.key_status(),
            # 设置页里给群聊成员选模型、选思考深度用
            "claude_models": [{"id": c, "label": claude_models.label(c)} for c in claude_models.ORDER],
            "claude_efforts": ["low", "medium", "high", "xhigh", "max"],
            "codex_models": agent_codex.models(),
            "effort_names": {"low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高", "ultra": "极限"},
            "review": settings.get("group_review", True),
            "tasks": group_tasks.public(), "projects": group_projects.public(),
            "memory": group_memory.read(), "memory_info": group_memory.info()}


def typed_chat(text):
    """打字给星期八：和说话一样处理（能执行操作），回答显示在界面里。"""
    import assistant
    import local_models
    before = status.current()
    status.publish({"type": "turn_start", "source": "text", "you": text})
    try:
        with assistant.TURN_LOCK, local_models.reasoning_slot():
            result = assistant.handle(text, first=True)
            reply = assistant.spoken_reply(result)
    except Exception as exc:
        friendly.LOG.exception("文字对话处理失败")
        reply = friendly.MESSAGES[friendly.failure(exc)]
        result = {"actions": []}
    status.publish({"type": "turn", "source": "text", "you": text, "reply": reply,
                    "actions": [a["tool"] for a in result.get("actions") or []]})
    if before["state"] in ("待命中", "已暂停") and status.current()["state"] not in ("待命中", "已暂停"):
        status.set(before["state"], before["detail"])  # 打字聊完，回到原来的待命状态
    return {"ok": True, "reply": reply}


def open_login():
    """打开一个窗口运行 Claude Code：第一次会提示登录（也可以输入 /login），登录完关掉窗口就行。"""
    import brain_claude_code
    exe = brain_claude_code.find_exe()
    if not exe:
        return {"ok": False, "message": "电脑上没找到 Claude Code，先安装它"}
    if sys.platform == "darwin":
        return {"ok": True, "message": "请在终端运行 claude auth login，按提示登录后刷新本页。"}
    window = subprocess.Popen(["cmd.exe", "/k", exe], cwd=os.path.expanduser("~"),
                              creationflags=subprocess.CREATE_NEW_CONSOLE)

    def refresh_login():
        brain_claude_code.logged_in(refresh=True)
        window.wait()
        brain_claude_code.logged_in(refresh=True)

    threading.Thread(target=refresh_login, name="claude-login-window", daemon=True).start()
    return {"ok": True, "message": "已经打开登录窗口，按提示登录，完成后关掉那个黑色窗口"}


def open_folder(name):
    if name == "logs":
        path = os.path.join(PROJECT_DIR, "logs")
    elif name in FOLDERS:
        module, attr = FOLDERS[name]
        path = getattr(__import__(module), attr)
    else:
        return {"ok": False, "message": "没有这个文件夹"}
    os.makedirs(path, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=True, timeout=5)
    else:
        os.startfile(path)
    return {"ok": True}


def action(name, data):
    """界面上点了什么。"""
    import groupchat
    value = data.get("value")
    if name == "chat":
        text = str(data.get("text", "")).strip()
        if not text:
            return {"ok": False, "message": "没有内容"}
        threading.Thread(target=typed_chat, args=(text,), daemon=True).start()
        return {"ok": True}
    if name == "brain":
        import brains
        return {"ok": True, **brains.set_brain(value)}
    if name == "claude_model":
        import claude_models
        claude_models.set_choice(value)
        return {"ok": True, "message": f"Claude 以后用：{claude_models.label(value)}"}
    if name == "voice_mode":
        import local_models
        if not isinstance(value, str) or value not in local_models.VOICE_MODES:
            return {"ok": False, "message": "没有这个语音模式"}
        if sys.platform == "darwin" and value == local_models.MINICPM_MODE:
            return {"ok": False, "message": f"MiniCPM-o {local_models.MINICPM_MAC_MESSAGE}"}
        settings.set("voice_mode", value)
        if value == local_models.MINICPM_MODE:
            return {"ok": True, "message": "MiniCPM-o 显示「在听」后，可以直接说话"}
        return {"ok": True, "message": "正在切回唤醒词模式"}
    if name == "transcription_model":
        import transcription
        if not isinstance(value, str) or value not in transcription.MODELS:
            return {"ok": False, "message": "没有这个转写模型"}
        if not transcription.model_available(value):
            return {"ok": False, "message": f"{transcription.MODELS[value]['label']}模型文件尚未下载完整"}
        settings.set(name, value)
        return {"ok": True, "message": "下一段录音起使用所选转写模型"}
    if name == "reasoning_model":
        import local_models
        if not isinstance(value, str) or value not in local_models.REASONING_MODELS:
            return {"ok": False, "message": "没有这个推理模型"}
        settings.set(name, value)
        return {"ok": True, "message": "下一轮对话使用所选推理模型"}
    if name == "voice":
        import listen
        import speak
        result = speak.set_voice(value)
        if data.get("preview") and "voice" in result and listen.voice_mode() == listen.WAKE_MODE:
            threading.Thread(target=lambda: speak.Speaker().say(f"你好，我是{result['voice']}，以后由我为您服务。"),
                             daemon=True).start()
        return {"ok": True, **result}
    if name == "overlay":
        if sys.platform == "darwin":
            return {"ok": False, "message": "Mac 上暂不支持悬浮状态窗。"}
        settings.set("overlay_mode", value if value in ("auto", "always", "off") else "auto")
        return {"ok": True}
    if name == "echo_reference":
        import duplex
        if value not in (duplex.PROGRAM_REFERENCE, duplex.SYSTEM_REFERENCE):
            return {"ok": False, "message": "没有这个回声参考选项"}
        if sys.platform == "darwin" and value == duplex.SYSTEM_REFERENCE:
            return {"ok": False, "message": "Mac 上使用程序声音作为回声参考。"}
        settings.set(duplex.ECHO_REFERENCE_SETTING, value)
        return {"ok": True, "message": "下次边说边听时使用所选回声参考"}
    if name in ("barge_in", "auto_help", VOICE_SETTING, speech_endpoint.SLOW_SPEECH_SETTING):
        settings.set(name, bool(value))
        return {"ok": True}
    if name == "follow_up":
        import listen
        seconds = int(value)
        if seconds not in listen.FOLLOW_UP_CHOICES:
            return {"ok": False, "message": "没有这个选项"}
        settings.set("follow_up_seconds", seconds)
        return {"ok": True, "message": f"以后回答完再听 {listen.FOLLOW_UP_CHOICES[seconds]}"}
    if name == "pause":
        import listen
        if value:
            listen.PAUSED.set()
            status.set("已暂停", "在界面里取消“暂停监听”就能继续")
        else:
            listen.PAUSED.clear()
        return {"ok": True}
    if name == "autostart":
        if sys.platform == "darwin":
            return {"ok": False, "message": "Mac 版请用启动脚本手动打开。"}
        import setup_shortcuts
        setup_shortcuts.set_autostart(bool(value))
        return {"ok": True}
    if name == "login_claude":
        return open_login()
    if name == "open_folder":
        return open_folder(value)
    if name == "quit":
        if quit_app:
            threading.Thread(target=quit_app, daemon=True).start()
        return {"ok": True}
    if name == "set_key":
        return groupchat.set_key(data.get("key", ""), data.get("value", ""))
    if name in ("dictate_start", "dictate_stop", "dictate_cancel"):  # 群聊输入框的语音输入（只转文字）
        import dictation
        return {"dictate_start": dictation.start, "dictate_stop": dictation.stop,
                "dictate_cancel": dictation.cancel}[name]()
    if name == "group_send":
        return groupchat.send(data.get("text", ""), request_id=data.get("request_id"))
    if name == "group_stop":
        return groupchat.force_stop()
    if name == "group_continue":
        return groupchat.continue_round()
    if name == "group_clear":
        return groupchat.clear()
    if name == "group_add":
        return groupchat.add_member(value)
    if name == "group_update":
        return groupchat.update_member(data.get("id"), data.get("changes") or {})
    if name == "group_remove":
        return groupchat.remove_member(data.get("id"))
    if name == "group_model":  # 设置页：给群聊成员选模型、思考深度
        changes = {k: str(data.get(k) or "") for k in ("model", "effort") if k in data}
        return groupchat.update_member(data.get("id"), changes)
    if name == "group_memory_save":  # 你在“记忆”里改了笔记
        import group_memory
        return group_memory.save(str(data.get("text") or "")[:20000])
    if name == "group_memory_update":  # 点了“现在整理”
        import group_memory
        if not group_memory.pending():
            return {"ok": True, "message": "没有新消息要整理"}
        started = group_memory.maybe_update(force=True)
        return {"ok": True, "message": "开始整理了，大约要半分钟到一分钟" if started else "正在整理中，稍等"}
    if name.startswith("task_"):  # 任务卡：转为任务 / 继续 / 更新摘要 / 完成 / 改标题 / 删除
        import group_tasks
        tid = str(data.get("id") or "")
        if name == "task_create":
            return group_tasks.create(str(data.get("msg_id") or ""))
        if name == "task_continue":
            return group_tasks.continue_task(tid)
        if name == "task_refresh":
            started = group_tasks.refresh(tid)
            return {"ok": True, "message": "开始用本地模型整理这张卡" if started else "正在整理中，稍等"}
        if name == "task_done":
            return group_tasks.set_done(tid, bool(value))
        if name == "task_rename":
            return group_tasks.rename(tid, data.get("title"))
        if name == "task_delete":
            return group_tasks.delete(tid)
    if name.startswith("project_"):  # 项目：切换 / 新建 / 改名和关联文件夹
        import group_projects
        if name == "project_switch":
            return group_projects.switch(str(value or ""))
        if name == "project_create":
            return group_projects.create(data.get("name"), data.get("folders"))
        if name == "project_update":
            return group_projects.update(str(data.get("id") or ""), data.get("name"), data.get("folders"))
    if name == "group_review":
        settings.set("group_review", bool(value))
        return {"ok": True, "message": "动手后会让另一个 AI 检查一遍" if value else "动手后不再检查"}
    if name == "group_hands":  # 点了“交给某某”/“收回”：执行权交给谁（同一时间只有一个）
        if not data.get("id"):
            return groupchat.force_stop()
        generation = groupchat._run_generation
        member = groupchat.find_member(data["id"])
        if member is None:
            return {"ok": False, "message": "没找到这个成员"}
        ok, why = groupchat.can_execute(member)
        if not ok:
            return {"ok": False, "message": f"{member['name']} 现在不能动手：{why}"}
        result = groupchat.give_hands(member["id"], generation=generation)
        if not result["ok"]:
            return result
        return {"ok": True, "message": f"执行权交给了 {member['name']}"}
    return {"ok": False, "message": f"不认识的操作：{name}"}


# ---------------------------------------------------------------------------
# 网页服务
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 不打印访问记录
        pass

    def _allowed(self, url):
        """只接受本机地址 + 正确的口令。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        token = self.headers.get("X-Token") or parse_qs(url.query).get("t", [""])[0]
        return secrets.compare_digest(token or "", _token or "")

    def _send(self, code, body, kind="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if not cache else "max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        try:
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # 界面请求超时或已重新发起，连接可能已经关闭。
            self.close_connection = True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in STATIC:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self._send(403, {"ok": False})
            name = STATIC[url.path]
            with open(os.path.join(UI_DIR, name), "rb") as f:
                body = f.read()
            headers_kind = TYPES[os.path.splitext(name)[1]]
            if name == "index.html":
                self.send_response(200)
                self.send_header("Content-Type", headers_kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 "connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(200, body, headers_kind)
        if not url.path.startswith("/api/") or not self._allowed(url):
            return self._send(403, {"ok": False, "message": "没有权限"})
        try:
            if url.path == "/api/state":
                return self._send(200, state())
            if url.path == "/api/chat":
                return self._send(200, chat_history())
            if url.path == "/api/group":
                return self._send(200, group_state())
            if url.path == "/api/events":
                return self._events()
        except Exception as e:
            return self._send(500, {"ok": False, "message": str(e)[:200]})
        return self._send(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/action/") or not self._allowed(url):
            return self._send(403, {"ok": False, "message": "没有权限"})
        if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
            return self._send(415, {"ok": False})
        length = min(int(self.headers.get("Content-Length") or 0), 200_000)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return self._send(200, action(url.path.rsplit("/", 1)[-1], data))
        except Exception as e:
            return self._send(500, {"ok": False, "message": str(e)[:200]})

    def _events(self):
        q = queue.Queue()
        with _clients_lock:
            _clients.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": hi\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 保持连接
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)
        self.close_connection = True


class _Server(ThreadingHTTPServer):
    """独占端口：Windows 上默认允许别的程序“重复占用”同一个端口，那样请求可能被别人截走，所以关掉。"""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _load_token():
    """口令只生成一次，存在项目的 .ui 文件夹里：重启星期八后，开着的窗口能自动重连，不用重新打开。
    （别的网页读不到你电脑上的文件，所以拿不到它。）"""
    path = os.path.join(STATE_DIR, "token")
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if len(token) >= 24:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def start():
    """启动网页服务（后台线程），返回打开界面用的地址。"""
    global _server, _token
    if _server is not None:
        return url()
    _token = _load_token()
    for port in [PORT] + list(range(PORT + 1, PORT + 20)):
        try:
            _server = _Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if _server is None:
        raise RuntimeError("找不到能用的端口")
    threading.Thread(target=_server.serve_forever, name="ui-server", daemon=True).start()
    status.subscribe(_on_status)
    status.subscribe_events(broadcast)
    threading.Thread(target=_level_pump, daemon=True).start()
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SERVER_FILE, "w", encoding="utf-8") as f:
        json.dump({"port": _server.server_address[1], "token": _token, "pid": os.getpid()}, f)
    return url()


def stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    try:
        os.remove(SERVER_FILE)
    except OSError:
        pass


def url():
    return f"http://127.0.0.1:{_server.server_address[1]}/?t={_token}"


# ---------------------------------------------------------------------------
# 打开窗口
# ---------------------------------------------------------------------------


def _find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LocalAppData")):
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(path):
                return path
    return None


def _existing_window():
    """界面窗口已经开着的话，找到它（标题是“星期八”的 Edge 窗口）。"""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if buf.value == WINDOW_TITLE and cls.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def open_window(address=None):
    """打开界面窗口；已经开着就把它拿到最前面。"""
    address = address or url()
    if sys.platform == "darwin":
        try:
            return bool(address and webbrowser.open(address))
        except (OSError, webbrowser.Error):
            return False
    hwnd = _existing_window()
    if hwnd:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # 从最小化恢复
        user32.SetForegroundWindow(hwnd)
        return True
    edge = _find_edge()
    if edge:
        os.makedirs(EDGE_PROFILE, exist_ok=True)
        subprocess.Popen([edge, f"--app={address}", f"--user-data-dir={EDGE_PROFILE}", "--no-first-run",
                          "--no-default-browser-check", "--disable-sync", "--window-size=1180,820",
                          "--disable-features=Translate,msEdgeSidebarV2"],
                         creationflags=0x08000000)
    else:
        os.startfile(address)  # 没有 Edge：用默认浏览器打开
    return True


def open_running():
    """星期八已经在后台运行时（再次点了快捷方式），打开它的界面。成功返回 True。"""
    try:
        with open(SERVER_FILE, encoding="utf-8") as f:
            info = json.load(f)
        return open_window(f"http://127.0.0.1:{info['port']}/?t={info['token']}")
    except (OSError, ValueError, KeyError):
        return False
