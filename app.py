"""
app.py —— “植入”电脑的语音助手：在后台运行，没有黑色窗口

    右下角托盘有个“八”字图标，光环颜色表示状态：
        青色 = 待命中（说“星期八”唤醒）   橙色 = 对话中   灰色 = 已暂停
    左键点图标：打开星期八的窗口（状态、打字聊天、AI 群聊、所有设置，见 ui_server.py 和 ui 文件夹）
    右键菜单：打开星期八、暂停/继续监听、退出（彻底关闭）

    启动：桌面或开始菜单里的“星期八 语音助手”（开机也会自动启动；已经在运行时再点一次 = 打开窗口）
    关闭：托盘菜单“退出”，或者“关闭 星期八 语音助手”快捷方式

    后台没有窗口，所以普通输出直接丢掉（你说的话不会写进任何日志），
    只有出错信息会写进 logs\\errors.log。想看详细过程，用命令行版：listen.py
"""

import ctypes
import os
import sys
import threading
import time
import traceback
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
STOP_FILE = os.path.join(PROJECT_DIR, ".stop_request")  # 关闭快捷方式会创建这个文件，请助手退出
TITLE = "星期八 语音助手"


def setup_output():
    os.makedirs(LOG_DIR, exist_ok=True)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.path.join(LOG_DIR, "errors.log"), "a", encoding="utf-8", buffering=1)
    print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====", file=sys.stderr)


def message(text, seconds=5):
    """弹一个小提示框，几秒后自动消失。"""
    if sys.platform == "darwin":
        print(text, file=sys.stderr)
        return
    try:
        ctypes.windll.user32.MessageBoxTimeoutW(0, text, TITLE, 0x40, 0, seconds * 1000)
    except Exception:
        pass


def main():
    mac = sys.platform == "darwin"
    if "--console" not in sys.argv:  # 加 --console 可以在黑色窗口里看过程（调试用）
        setup_output()

    import housekeeping
    import ui_server

    if housekeeping.find_instances(__file__):
        # 已经在后台运行了（又点了一次快捷方式）：打开它的窗口
        if not ui_server.open_running():
            message("语音助手已经在后台运行了，点菜单栏里的“八”字图标打开界面。" if mac else
                    "语音助手已经在后台运行了，点右下角托盘里的“八”字图标打开窗口。")
        return
    # 命令行版（listen.py）在运行的话先关掉，免得两个程序抢麦克风和显存
    housekeeping.close_old_instances(os.path.join(PROJECT_DIR, "listen.py"))
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    import pystray

    import appicon
    import listen
    import settings
    import status

    if mac:
        from PyObjCTools.AppHelper import callAfter
    else:
        import overlay

    paused = listen.PAUSED  # 暂停信号：对话进行到一半点“暂停”，也会马上停
    state = {"status": "启动中", "greeted": False}

    def notify(text):
        try:
            icon.notify(text, TITLE)
        except Exception:
            pass

    def update_icon(new_state):
        state["status"] = new_state
        if new_state in ("待命中", "已暂停", "正在退出", "启动中"):
            color = appicon.COLORS[new_state]
        else:
            color = appicon.COLORS["对话中"]
        icon.icon = appicon.make_image(color)
        icon.title = f"{TITLE}：{new_state}"
        icon.update_menu()

    def on_status(new_state, detail):
        """助手每走一步，托盘图标的颜色和提示文字跟着变。"""
        if mac:
            callAfter(update_icon, new_state)
        else:
            update_icon(new_state)
        if not mac and new_state == "待命中" and not state["greeted"]:
            state["greeted"] = True
            notify(f"已经在后台待命，说“{'、'.join(listen.wakeword.WAKE_WORDS)}”就能叫我。点托盘里的“八”字图标打开窗口。")

    def set_status(new_state):
        status.set(new_state)

    def toggle_pause():
        if paused.is_set():
            paused.clear()
        else:
            paused.set()
            set_status("已暂停")

    def quit_app():
        listen.STOP.set()
        set_status("正在退出")

    # 窗口界面：所有设置、打字聊天、AI 群聊都在这里（托盘菜单只留最常用的三项）
    ui_server.quit_app = quit_app
    ui_address = None

    def start_ui():
        nonlocal ui_address
        try:
            ui_address = ui_server.start()
        except Exception:
            traceback.print_exc()

    if not mac:
        start_ui()

    def open_ui():
        if ui_address:
            ui_server.open_window(ui_address)
        else:
            notify("窗口界面没能启动，详情在 logs/errors.log。" if mac else
                   "窗口界面没能启动，详情在 logs\\errors.log。")

    icon = pystray.Icon(
        "voice-assistant",
        appicon.make_image(appicon.COLORS["启动中"]),
        f"{TITLE}：启动中",
        pystray.Menu(
            pystray.MenuItem("打开星期八", open_ui, default=True),  # 左键点托盘图标 = 打开窗口
            pystray.MenuItem(lambda item: f"状态：{state['status']}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("暂停监听", toggle_pause, checked=lambda item: paused.is_set()),
            pystray.MenuItem("退出（彻底关闭）", quit_app),
        ),
    )
    def watch_stop_file():
        """有人运行了关闭快捷方式（stop.py）：体面地退出（先复盘、卸载模型）。"""
        while not listen.STOP.is_set():
            if os.path.exists(STOP_FILE):
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
                quit_app()
                break
            time.sleep(0.5)

    def run_assistant(status_window=None):
        try:
            if mac:
                start_ui()
                if ui_address:
                    open_ui()
            threading.Thread(target=watch_stop_file, daemon=True).start()

            # 调度员挂起的定时任务到点用语音提醒，也会在托盘弹一条。
            try:
                import bridge_pending
                bridge_pending.start(listen.STOP, notify=notify)
            except Exception:
                traceback.print_exc()

            time.sleep(1)  # 等托盘图标出来
            listen.main(paused=paused, notify=notify)
        except Exception:
            traceback.print_exc()
            if not mac:
                from speak import play_notice
                play_notice("error")
                time.sleep(3)
        finally:
            try:
                print(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 退出 =====", file=sys.stderr)
                ui_server.broadcast({"type": "status", "state": "已退出", "detail": "星期八已经退出，可以关掉这个窗口了",
                                     "since": time.time(), "style": [[140, 140, 140], "still"]})
                ui_server.stop()
                if status_window is not None:
                    status_window.stop()
            finally:
                if mac:
                    callAfter(icon.stop)
                else:
                    icon.stop()

    if mac:
        worker = threading.Thread(target=run_assistant, name="语音助手")

        def show_icon():
            icon.visible = True
            worker.start()

        status.subscribe(on_status)
        try:
            # pystray 的 setup 回调在后台线程，需要把 AppKit 操作排到主线程。
            icon.run(setup=lambda _icon: callAfter(show_icon))
        finally:
            listen.STOP.set()
            if worker.ident is not None:
                worker.join()
    else:
        threading.Thread(target=icon.run, daemon=True).start()
        status.subscribe(on_status)
        status_window = overlay.Overlay(mode=lambda: settings.get("overlay_mode", "auto")).start()
        # 开机 5 分钟内自动启动时只在后台待命。
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        if ui_address and ctypes.windll.kernel32.GetTickCount64() > 5 * 60 * 1000:
            threading.Timer(1.5, open_ui).start()
        run_assistant(status_window)


if __name__ == "__main__":
    main()
