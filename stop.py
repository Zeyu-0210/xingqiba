"""
stop.py —— 彻底关闭后台的语音助手（快捷方式“关闭 星期八 语音助手”运行的就是它）

    1. 先请助手自己退出（它会做完复盘、卸载模型）
    2. 30 秒还没退出，就强制关闭
    3. 保险起见，再确认一次大模型已经从显存里卸掉

加 --quiet 不弹提示框（测试用）。
"""

import ctypes
import os
import sys
import time

import housekeeping

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(PROJECT_DIR, "app.py")
LISTEN = os.path.join(PROJECT_DIR, "listen.py")
STOP_FILE = os.path.join(PROJECT_DIR, ".stop_request")
QUIET = "--quiet" in sys.argv


def message(text):
    print(text)
    if sys.platform == "win32" and not QUIET:
        ctypes.windll.user32.MessageBoxTimeoutW(0, text, "星期八 语音助手", 0x40, 0, 5000)


def main():
    if not housekeeping.find_instances(APP) and not housekeeping.find_instances(LISTEN):
        message("语音助手本来就没有在运行。")
        return

    if housekeeping.find_instances(APP):
        open(STOP_FILE, "w").close()  # 请它自己体面地退出
        deadline = time.time() + 30
        while time.time() < deadline and housekeeping.find_instances(APP):
            time.sleep(0.5)

    forced = housekeeping.close_old_instances(APP) + housekeeping.close_old_instances(LISTEN)
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    try:  # 保险：确认大模型已经从显存里卸掉
        import assistant
        assistant.unload()
    except Exception:
        pass

    message("语音助手已经彻底关闭了。" + ("（30 秒没响应，是强制关闭的）" if forced else ""))


if __name__ == "__main__":
    main()
