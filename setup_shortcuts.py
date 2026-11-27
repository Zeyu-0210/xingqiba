"""
setup_shortcuts.py —— 把语音助手“装进”电脑，或者“拿出来”

    .venv\\Scripts\\python.exe setup_shortcuts.py install
        桌面 + 开始菜单：“星期八 语音助手”（启动）、“关闭 星期八 语音助手”（彻底关闭）
        开机自动启动
    .venv\\Scripts\\python.exe setup_shortcuts.py uninstall
        删除上面这些快捷方式和开机自启（项目文件、记忆、任务文件夹都不动）

开机自启也可以在托盘图标的右键菜单里随时开关。
"""

import os
import sys

if sys.platform == "win32":
    import comtypes.client

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(PROJECT_DIR, ".venv", "Scripts", "pythonw.exe")  # 不弹黑色窗口的 Python
ICON = os.path.join(PROJECT_DIR, "assets", "assistant.ico")
NAME_START = "星期八 语音助手"
NAME_STOP = "关闭 星期八 语音助手"
MENU_FOLDER = "星期八 语音助手"


def _shell():
    # 每个线程用 Windows 的系统组件（COM）之前都要先“初始化”一次（托盘菜单在另一个线程里调用这里）
    comtypes.CoInitialize()
    return comtypes.client.CreateObject("WScript.Shell", dynamic=True)


def special_folder(name):
    """Windows 的特殊文件夹：Desktop（桌面）、Programs（开始菜单）、Startup（开机启动）。"""
    return _shell().SpecialFolders.Item(name)


def _create(path, script, description):
    shortcut = _shell().CreateShortcut(path)
    shortcut.TargetPath = PYTHONW
    shortcut.Arguments = f'"{os.path.join(PROJECT_DIR, script)}"'
    shortcut.WorkingDirectory = PROJECT_DIR
    shortcut.IconLocation = f"{ICON},0"
    shortcut.Description = description
    shortcut.Save()


def shortcut_paths():
    """所有快捷方式的位置：(路径, 运行哪个脚本, 说明)。"""
    if sys.platform == "darwin":
        return []
    desktop = special_folder("Desktop")
    menu = os.path.join(special_folder("Programs"), MENU_FOLDER)
    start = ("app.py", "启动星期八语音助手（后台运行）")
    stop = ("stop.py", "彻底关闭星期八语音助手")
    return [
        (os.path.join(desktop, f"{NAME_START}.lnk"), *start),
        (os.path.join(desktop, f"{NAME_STOP}.lnk"), *stop),
        (os.path.join(menu, f"{NAME_START}.lnk"), *start),
        (os.path.join(menu, f"{NAME_STOP}.lnk"), *stop),
    ]


def autostart_path():
    return os.path.join(special_folder("Startup"), f"{NAME_START}.lnk")


def autostart_enabled():
    return sys.platform != "darwin" and os.path.exists(autostart_path())


def set_autostart(on):
    if sys.platform == "darwin":
        return
    path = autostart_path()
    if on:
        _make_icon()
        _create(path, "app.py", "开机自动启动星期八语音助手")
    elif os.path.exists(path):
        os.remove(path)


def _make_icon():
    if not os.path.exists(ICON):
        import appicon
        appicon.save_ico(ICON)


def install():
    if sys.platform == "darwin":
        return []
    _make_icon()
    created = []
    for path, script, description in shortcut_paths():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _create(path, script, description)
        created.append(path)
    set_autostart(True)
    created.append(autostart_path())
    return created


def uninstall():
    if sys.platform == "darwin":
        return []
    removed = []
    for path, _script, _description in shortcut_paths():
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    menu = os.path.join(special_folder("Programs"), MENU_FOLDER)
    if os.path.isdir(menu) and not os.listdir(menu):
        os.rmdir(menu)
    if autostart_enabled():
        removed.append(autostart_path())
        set_autostart(False)
    return removed


if __name__ == "__main__":
    if sys.platform == "darwin":
        print("Mac 请使用 start_mac.command 和 stop_mac.command。")
        sys.exit(0)
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "install":
        for p in install():
            print(f"已创建：{p}")
    elif action == "uninstall":
        for p in uninstall():
            print(f"已删除：{p}")
    else:
        print(__doc__)
