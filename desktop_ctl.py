"""
desktop_ctl.py —— 让 AI 群聊里有执行权的 AI 像人一样操控屏幕：截图看屏幕、动鼠标、打字、按快捷键

    Claude 的命令行版没有“操控电脑”的工具，就用这个（GPT 有自己的，也可以用这个）。
    它是真的在你屏幕上操作：你能看到鼠标在动，随时可以自己动鼠标、按键盘接管。
    用的是星期八自己的“手”（computer.py），不用装别的东西。

用法（在 PowerShell 里运行，python 用星期八的：D:\\voice-assistant\\.venv\\Scripts\\python.exe）：
    desktop_ctl.py screenshot [窗口名]            截图（整个屏幕，或者某个窗口），告诉你图片存在哪、多大
    desktop_ctl.py click-xy X Y [right|double]    点最近一张截图上的 (X, Y)（坐标就是图片上的像素位置）
    desktop_ctl.py move X Y                       把鼠标移到最近一张截图上的 (X, Y)
    desktop_ctl.py drag X1 Y1 X2 Y2               按住左键从 (X1, Y1) 拖到 (X2, Y2)
    desktop_ctl.py read [窗口名]                   列出窗口里能点的按钮、输入框（带编号）和文字（比截图省）
    desktop_ctl.py click 编号或按钮上的字          点 read 列出来的东西
    desktop_ctl.py type "文字" [enter]             在光标处打字（中文也行），加 enter 打完按回车
    desktop_ctl.py keys ctrl+s                    按快捷键（几个依次按用空格隔开：down down enter）
    desktop_ctl.py scroll down|up|left|right [格数]
    desktop_ctl.py windows                        列出开着的窗口
    desktop_ctl.py window switch|minimize|maximize|restore|close 名字
    desktop_ctl.py open 软件名                     打开软件
"""

import ctypes
import json
import os
import sys
import time

if sys.platform == "win32":
    try:  # 坐标都按“真实像素”算，要在动鼠标、截图之前设好。
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
import workers

SHOT_DIR = os.path.join(workers.TASKS_DIR, "星期八接管", "屏幕截图")
STATE_FILE = os.path.join(SHOT_DIR, "最近一次.json")
MAX_SIDE = 1568      # 截图最长边（太大的图 AI 看的时候也会被缩小，不如先缩好，坐标才对得上）
KEEP_SHOTS = 30      # 最多留多少张截图（多了删最旧的）


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(**changes):
    os.makedirs(SHOT_DIR, exist_ok=True)
    state = _load_state()
    state.update(changes)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, default=str)


def to_screen(x, y, shot):
    """截图上的 (x, y) → 屏幕上的真实像素位置。"""
    scale = shot.get("scale") or 1.0
    return round(shot.get("left", 0) + float(x) / scale), round(shot.get("top", 0) + float(y) / scale)


def screenshot(name=""):
    from PIL import ImageGrab

    import computer
    box = None
    where = "整个屏幕"
    if name:
        win = computer._find_window(name)
        if not win:
            return False, f"没找到叫“{name}”的窗口。现在开着的：{computer._window_list_text(computer._windows())}"
        computer._activate(win["hwnd"])
        time.sleep(0.3)
        box = computer._rect(win["hwnd"])
        where = f"窗口“{computer._short(win['title'], 30)}”"
    image = ImageGrab.grab(bbox=box)
    left, top = (box[0], box[1]) if box else (0, 0)
    scale = min(1.0, MAX_SIDE / max(image.size))
    if scale < 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, f"截图-{time.strftime('%H%M%S')}.png")
    image.save(path)
    _save_state(shot={"path": path, "left": left, "top": top, "scale": scale, "width": image.width,
                      "height": image.height, "time": time.time()})
    shots = sorted((f for f in os.listdir(SHOT_DIR) if f.endswith(".png")),
                   key=lambda f: os.path.getmtime(os.path.join(SHOT_DIR, f)))
    for old in shots[:-KEEP_SHOTS]:
        try:
            os.remove(os.path.join(SHOT_DIR, old))
        except OSError:
            pass
    return True, (f"已截{where}：{path}\n图片大小 {image.width}x{image.height}。用看图片的工具打开它；"
                  f"要点哪里，就用 click-xy 加图片上的坐标。")


def _shot():
    shot = _load_state().get("shot")
    if not shot:
        raise SystemExit(_fail("还没有截图：先运行 screenshot，再按图片上的坐标点"))
    return shot


def _fail(message):
    print(message)
    return 1


def main(argv):
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return _fail(MAC_UNSUPPORTED)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    action, args = argv[0].lower(), argv[1:]
    import computer
    if action == "screenshot":
        ok, message = screenshot(" ".join(args).strip())
    elif action in ("click-xy", "move", "drag"):
        shot = _shot()
        try:
            numbers = [float(a) for a in args if a.replace(".", "", 1).lstrip("-").isdigit()]
        except ValueError:
            numbers = []
        need = 4 if action == "drag" else 2
        if len(numbers) < need:
            return _fail(f"{action} 要 {need} 个数字坐标（按最近一张截图上的位置）")
        x, y = to_screen(numbers[0], numbers[1], shot)
        if action == "move":
            computer.user32.SetCursorPos(x, y)
            ok, message = True, f"鼠标已移到 ({numbers[0]:g}, {numbers[1]:g})"
        elif action == "drag":
            x2, y2 = to_screen(numbers[2], numbers[3], shot)
            computer.user32.SetCursorPos(x, y)
            time.sleep(0.05)
            computer._send([computer._mouse(computer.MOUSE_LEFT[0])])
            for i in range(1, 11):  # 慢慢拖过去（有的软件太快了认不出是拖动）
                computer.user32.SetCursorPos(round(x + (x2 - x) * i / 10), round(y + (y2 - y) * i / 10))
                time.sleep(0.02)
            computer._send([computer._mouse(computer.MOUSE_LEFT[1])])
            ok, message = True, f"已从 ({numbers[0]:g}, {numbers[1]:g}) 拖到 ({numbers[2]:g}, {numbers[3]:g})"
        else:
            extra = " ".join(args).lower()
            right, double = "right" in extra or "右键" in extra, "double" in extra or "双击" in extra
            ok = computer._click_at(x, y, right=right, double=double)
            verb = "双击了" if double else "右键点了" if right else "点了"
            message = (f"{verb}截图上的 ({numbers[0]:g}, {numbers[1]:g})。要看点完的样子，再截一张图"
                       if ok else "这个窗口不接受模拟点击（可能是用管理员权限运行的软件）")
    elif action == "read":
        result = computer.read_screen(" ".join(args).strip())
        ok, message = result["ok"], result["message"]
        if ok:
            _save_state(screen=dict(computer._last_screen))
    elif action == "click":
        screen = _load_state().get("screen")
        if screen:
            computer._last_screen.update(screen)
        result = computer.click(" ".join(args).strip())
        ok, message = result["ok"], result["message"]
    elif action == "type":
        enter = bool(args) and args[-1].lower() in ("enter", "回车")
        text = " ".join(args[:-1] if enter else args)
        result = computer.type_text(text, enter=enter)
        ok, message = result["ok"], result["message"]
    elif action == "keys":
        result = computer.press_keys(" ".join(args))
        ok, message = result["ok"], result["message"]
    elif action == "scroll":
        result = computer.scroll(args[0] if args else "down", args[1] if len(args) > 1 else 5)
        ok, message = result["ok"], result["message"]
    elif action == "windows":
        result = computer.window("list")
        ok, message = result["ok"], result["message"]
    elif action == "window":
        result = computer.window(args[0] if args else "switch", " ".join(args[1:]))
        ok, message = result["ok"], result["message"]
    elif action == "open":
        result = computer.open_app(" ".join(args).strip())
        ok, message = result["ok"], result["message"]
    else:
        return _fail(f"不认识的操作：{action}。运行 desktop_ctl.py help 看用法")
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
