"""
settings.py —— 记住你的选择（比如用哪个声音），存在项目里的 settings.json，重启后不变

    settings.get("voice")            读取
    settings.set("voice", "...")     保存
"""

import json
import os
import threading

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(PROJECT_DIR, "settings.json")
_lock = threading.Lock()


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}  # 还没有设置过，或者文件被改坏了：当作空的


def get(key, default=None):
    return load().get(key, default)


def set(key, value):  # noqa: A001  （和内置 set 同名，但只在 settings.set 这样用）
    with _lock:
        data = load()
        data[key] = value
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PATH)  # 先写临时文件再替换，防止写到一半断电把文件写坏
    # 通知窗口界面刷新（比如你用语音换了大脑，界面上马上跟着变）
    import status
    status.publish({"type": "settings", "key": key})
