"""
status.py —— 助手“现在在干嘛”的广播站

    程序每走到一步，就调用 status.set("在听", "请说话……")；
    状态窗（overlay.py）、托盘图标（app.py）收到后马上更新显示；
    同时把“哪一步、用了几秒”写进 logs\\timing-日期.log，方便以后排查哪里慢。
    这个日志只记步骤名称和用时，不记你说的话、也不记助手回答的内容。

    status.level(0.05)   正在听的时候，报告麦克风音量（状态窗里的音量条用）
"""

import atexit
import json
import os
import queue
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
KEEP_LOG_DAYS = 7
LOG_ENABLED = False   # 助手真正运行时（listen.main）才打开，跑测试时不记

# 每个状态在状态窗里的样子：(颜色, 动画)
#   动画：breathe 呼吸 / bars 音量条 / spin 转圈 / wave 声波 / still 静止
STYLES = {
    "启动中": ((90, 160, 200), "spin"),
    "待命中": ((0, 210, 255), "breathe"),
    "已暂停": ((140, 140, 140), "still"),
    "群聊语音输入": ((140, 140, 140), "still"),   # 群聊在录语音输入，助手这段时间不听
    "确认唤醒词": ((0, 210, 255), "spin"),
    "在听": ((60, 220, 120), "bars"),
    "识别中": ((60, 220, 120), "spin"),
    "思考中": ((255, 170, 0), "spin"),
    "执行": ((255, 120, 60), "spin"),
    "组织语言": ((255, 170, 0), "spin"),
    "合成语音": ((180, 120, 255), "spin"),
    "说话中": ((180, 120, 255), "wave"),
    "没听清": ((255, 200, 60), "still"),
    "复盘中": ((120, 160, 255), "spin"),
    "卸载模型": ((120, 160, 255), "spin"),
    "出错": ((255, 80, 80), "still"),
    "正在退出": ((140, 140, 140), "still"),
}

_listeners = []
_lock = threading.Lock()
_current = {"state": "启动中", "detail": "", "since": time.time(), "level": 0.0}
_last_log_day = None
_barge_queue = queue.SimpleQueue()
_barge_lock = threading.Lock()
_barge_writer = None
barge_source = ContextVar("barge_source", default=(None, None))
# 这一回合：(录音 ID, 是不是对这句话的回答)。录音结束时生成 ID，回答的播放首包也带上它，日志里才能一一配对
barge_turn = ContextVar("barge_turn", default=(None, None))
_last_recording_id = None


def new_recording_id(*, remember=True):
    """生成跨进程唯一的录音 ID；调用方可选择把它记成最近一段录音。"""
    global _last_recording_id
    recording_id = uuid.uuid4().hex[:12]
    if remember:
        _last_recording_id = recording_id
    return recording_id


def take_recording_id():
    """取走最近一段录音的 ID（取一次就清空，避免配到后面别的回合）。"""
    global _last_recording_id
    rid, _last_recording_id = _last_recording_id, None
    return rid


def barge_event(event, playback_id=None, *, at=None, **details):
    """从普通线程提交打断日志。音频回调仅保存时钟值，不调用这里。"""
    global _barge_writer
    if not LOG_ENABLED:
        return
    now = time.monotonic()
    at = now if at is None else at
    wall = time.time() + (at - now)
    source_id, source_run = barge_source.get()
    if source_id is not None:
        details.update(source_playback_id=source_id, source_run=source_run)
    recording_id, is_reply = barge_turn.get()
    if recording_id is not None:
        details.setdefault("recording_id", recording_id)
    if is_reply is not None:
        details.setdefault("is_reply", is_reply)
    if _barge_writer is None:
        with _barge_lock:
            if _barge_writer is None:
                worker = threading.Thread(target=_write_barge_events, name="barge-log", daemon=True)
                try:
                    worker.start()
                except RuntimeError:
                    return
                _barge_writer = worker
    _barge_queue.put((LOG_DIR, wall, at, playback_id, event, details))


def _write_barge_events():
    while (entry := _barge_queue.get()) is not None:
        log_dir, wall, at, playback_id, event, details = entry
        try:
            when = datetime.fromtimestamp(wall).astimezone()
            row = dict(time=when.isoformat(timespec="milliseconds"), mono_ms=round(at * 1000, 3),
                       playback_id=playback_id, event=event, **details)
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, f"barge-events-{when:%Y-%m-%d}.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError):
            pass


@atexit.register
def _stop_barge_writer():
    if _barge_writer is not None:
        _barge_queue.put(None)
        _barge_writer.join(timeout=1)


def subscribe(callback):
    """登记一个“听众”：状态变化时调用 callback(state, detail)。"""
    _listeners.append(callback)


def current():
    return dict(_current)


def style(state):
    return STYLES.get(state, ((0, 210, 255), "still"))


def set(state, detail=""):  # noqa: A001
    """进入新的一步。"""
    with _lock:
        now = time.time()
        previous, took = _current["state"], now - _current["since"]
        _current.update(state=state, detail=detail, since=now, level=0.0)
    _log(previous, took, state)
    for callback in list(_listeners):
        try:
            callback(state, detail)
        except Exception:
            pass  # 显示出问题不能影响助手本身


def level(value):
    """正在听的时候，报告麦克风音量（0~1）。"""
    _current["level"] = float(value)


# ---- 其他消息（对话内容、群聊消息……）：发给窗口界面（ui_server.py）显示 ----
_event_listeners = []


def subscribe_events(callback):
    """登记一个“听众”：有新消息时调用 callback(事件字典)。"""
    _event_listeners.append(callback)


def publish(event):
    """广播一条消息，比如 {"type": "turn", "you": "...", "reply": "..."}。不会写进日志。"""
    for callback in list(_event_listeners):
        try:
            callback(event)
        except Exception:
            pass


def _log(previous, took, state):
    global _last_log_day
    if not LOG_ENABLED:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now()
        if _last_log_day != today.date():
            _last_log_day = today.date()
            _clean_old_logs(today)
        with open(os.path.join(LOG_DIR, f"timing-{today:%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
            f.write(f"{today:%H:%M:%S.%f}"[:-3] + f"  {previous}（用了 {took:.2f} 秒）→ {state}\n")
    except OSError:
        pass


def _clean_old_logs(today):
    """只保留最近 KEEP_LOG_DAYS 天的用时日志。"""
    cutoff = (today - timedelta(days=KEEP_LOG_DAYS)).strftime("%Y-%m-%d")
    for name in os.listdir(LOG_DIR):
        if name.startswith("timing-") and name[7:17] < cutoff:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass
