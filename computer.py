"""
computer.py —— 星期八的“手”：操作电脑

    能做的事（都是给秘书模型用的工具，见文件末尾的 TOOLS）：
        open_app     打开软件（从开始菜单里找名字，和你在开始菜单里点一下一样）
        window       切换 / 关闭 / 最小化 / 最大化 / 还原窗口，或者列出开着的窗口
        read_screen  看看当前窗口里有哪些按钮、输入框、菜单、链接，上面写着什么字
        click        点击按钮、菜单、链接（填编号或者按钮上的字）
        type_text    打字（支持中文）
        press_keys   按键盘快捷键（ctrl+s、alt+f4、win+d、播放/暂停……）
        scroll       滚动窗口
        open_web     打开网址，或者在网上搜索
        system_info  电量、CPU、内存、硬盘、开机多久
      危险操作（不会直接做，先登记，等你说“确定”才执行；你说“取消”或说别的事就作废）：
        run_command  运行一条 PowerShell 命令（改注册表的命令一律不执行）
        delete_file  把文件/文件夹删到回收站
        power        睡眠、关机、重启（锁屏和取消关机不用确认）

    全部是我们自己的代码，直接在助手程序里运行：不用装 MCP、不用开别的服务、不用截图。
    思路参考了开源项目 Windows-MCP（CursorTouch，MIT 许可证）：
        - 软件列表：PowerShell 的 Get-StartApps，打开时用 shell:AppsFolder\\软件ID
        - 看窗口：用 Windows 自带的“界面自动化”（UI Automation，读屏软件也用它），
          把按钮、输入框整理成一张文字清单——模型看文字就够了，比发截图省得多
        - 把窗口切到最前面：AttachThreadInput 的办法
    坐标：屏幕缩放是 150% 时，“真实像素”和“缩放后的像素”不一样，点击会点歪。
          所以读坐标、移动鼠标都在“按真实像素计算”的模式下进行（_dpi_aware）。
"""

import ctypes
import ctypes.wintypes as wt
import difflib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

import psutil

import friendly
import settings

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = 0x08000000  # 运行 PowerShell 时不弹黑色窗口
MAC_UNSUPPORTED = "Mac 上暂时不支持这个操作。"


def _windows_tool(func):
    @functools.wraps(func)
    def call(*args, **kwargs):
        if sys.platform == "darwin":
            return {"ok": False, "message": MAC_UNSUPPORTED}
        return func(*args, **kwargs)
    return call

# ---------------------------------------------------------------------------
# 可以调的设置
# ---------------------------------------------------------------------------

WAIT_SECONDS = 8         # 打开软件后最多等几秒，看窗口出来没有
APPS_CACHE_SECONDS = 600  # 开始菜单的软件列表缓存多久（10 分钟；新装的软件找不到时会自动刷新）
READ_TIMEOUT = 10        # 读窗口内容最多等几秒（有的软件卡住不回应，不能把助手也卡死）
MAX_ELEMENTS = 60        # read_screen 最多列出多少个可以操作的东西（太多了模型看不过来，也费时间）
MAX_TEXTS = 30           # 最多列出多少段文字
CONFIRM_SECONDS = 60     # 危险操作等你确认多久，超时自动作废
SEARCH_ENGINES = {
    "必应": "https://www.bing.com/search?q={}",
    "百度": "https://www.baidu.com/s?wd={}",
    "谷歌": "https://www.google.com/search?q={}",
}
DEFAULT_SEARCH_ENGINE = "必应"  # 想换成百度：settings.json 里加 "search_engine": "百度"

# 常用网站：用户说网站名，就打开对应网址
SITES = {
    "b站": "https://www.bilibili.com", "哔哩哔哩": "https://www.bilibili.com", "bilibili": "https://www.bilibili.com",
    "百度": "https://www.baidu.com", "淘宝": "https://www.taobao.com", "京东": "https://www.jd.com",
    "知乎": "https://www.zhihu.com", "抖音": "https://www.douyin.com", "微博": "https://weibo.com",
    "小红书": "https://www.xiaohongshu.com", "爱奇艺": "https://www.iqiyi.com", "腾讯视频": "https://v.qq.com",
    "优酷": "https://www.youku.com", "网易云音乐": "https://music.163.com", "github": "https://github.com",
    "youtube": "https://www.youtube.com", "油管": "https://www.youtube.com", "谷歌": "https://www.google.com",
    "必应": "https://www.bing.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
    "豆包": "https://www.doubao.com", "deepseek": "https://chat.deepseek.com", "gemini": "https://gemini.google.com",
}

# 说法 → 开始菜单里的名字（按顺序找，装了哪个用哪个）
APP_ALIASES = {
    "浏览器": ["Google Chrome", "Microsoft Edge", "Firefox"], "谷歌浏览器": ["Google Chrome"],
    "谷歌": ["Google Chrome"], "chrome": ["Google Chrome"], "edge": ["Microsoft Edge"],
    "edge浏览器": ["Microsoft Edge"], "文件管理器": ["文件资源管理器"], "资源管理器": ["文件资源管理器"],
    "此电脑": ["文件资源管理器"], "我的电脑": ["文件资源管理器"], "文件夹": ["文件资源管理器"],
    "命令行": ["终端", "命令提示符"], "cmd": ["命令提示符"], "powershell": ["Windows PowerShell"],
    "vscode": ["Visual Studio Code"], "截图": ["截图工具"], "截屏": ["截图工具"],
    "商店": ["Microsoft Store"], "应用商店": ["Microsoft Store"], "画板": ["画图"],
    "便签": ["便笺", "Sticky Notes (new)"], "相册": ["照片"], "图片": ["照片"],
    "wps": ["WPS Office"], "表格": ["WPS表格", "Excel"], "ppt": ["WPS演示", "PowerPoint"],
    "幻灯片": ["WPS演示", "PowerPoint"], "英雄联盟": ["League of Legends"], "lol": ["League of Legends"],
    "无畏契约": ["VALORANT"], "瓦罗兰特": ["VALORANT"], "网易云": ["网易云音乐"],
}

# 说法 → 窗口所属程序（找已经开着的窗口时用）
WINDOW_ALIASES = {
    "浏览器": ["chrome", "msedge", "firefox", "360se", "qqbrowser", "sogouexplorer"],
    "谷歌浏览器": ["chrome"], "谷歌": ["chrome"], "edge": ["msedge"],
    "文件管理器": ["explorer"], "资源管理器": ["explorer"], "文件夹": ["explorer"],
    "此电脑": ["explorer"], "我的电脑": ["explorer"], "微信": ["weixin", "wechat"],
    "qq音乐": ["qqmusic"], "vscode": ["code"], "终端": ["windowsterminal"],
    "命令行": ["windowsterminal", "cmd", "powershell"],
}
THIS_WINDOW = {"", "这个", "这个窗口", "当前", "当前窗口", "现在的窗口", "前面的窗口", "最前面的窗口", "它", "这个软件"}

# ---------------------------------------------------------------------------
# Windows 系统函数（ctypes：Python 自带的“直接调用 Windows 功能”的办法，不用装东西）
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None

    ULONG_PTR = ctypes.c_size_t


    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]


    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]


    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wt.LONG
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wt.UINT
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    if dwmapi:
        dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

DPI_PER_MONITOR_V2 = ctypes.c_void_p(-4)
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
GW_OWNER = 4
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
WM_CLOSE = 0x0010
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSE_LEFT = (0x0002, 0x0004)
MOUSE_RIGHT = (0x0008, 0x0010)
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _dpi_aware(func):
    """让这个函数里的坐标都按“真实像素”算（屏幕缩放 150% 时也不会点歪）。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        old = None
        try:
            old = user32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_V2)
        except Exception:
            pass
        try:
            return func(*args, **kwargs)
        finally:
            if old:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
    return wrapper


def _norm(text):
    """比较名字前先统一一下：去掉空格和符号、英文变小写（“QQ 音乐”和“qq音乐”算一样）。"""
    return re.sub(r"[\s\-_·•《》®™()（）\[\]【】“”\"'.:：]", "", str(text or "")).lower()


def _bool(value):
    return value in (True, 1, "true", "True", "1", "是", "yes")


def _short(text, n=30):
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _cloaked(hwnd):
    """Windows 会把一些“看不见”的窗口藏起来（比如后台的设置界面），这种不算。"""
    if not dwmapi:
        return False
    value = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        return False
    return value.value != 0


def _info(hwnd):
    pid = _pid(hwnd)
    return {"hwnd": hwnd, "title": _title(hwnd), "class": _class(hwnd), "pid": pid,
            "process": _process_name(pid), "minimized": bool(user32.IsIconic(hwnd))}


def _windows():
    """现在开着的“正常”窗口（任务栏、Alt+Tab 里能看到的那些），最上面的排第一。"""
    found = []

    def callback(hwnd, _):
        try:
            if not hwnd or not user32.IsWindowVisible(hwnd):
                return True
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
                return True
            if user32.GetWindow(hwnd, GW_OWNER) and not ex & WS_EX_APPWINDOW:
                return True
            if not _title(hwnd).strip() or _cloaked(hwnd):
                return True
            info = _info(hwnd)
            if info["pid"] != os.getpid() and info["class"] not in DESKTOP_CLASSES:
                found.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def _foreground():
    """最前面的窗口（你正在用的那个）。是桌面或者助手自己的话返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    info = _info(hwnd)
    if info["pid"] == os.getpid() or info["class"] in DESKTOP_CLASSES:
        return None
    return info


def _rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _wait(condition, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


@_dpi_aware
def _activate(hwnd):
    """把窗口放到最前面。Windows 不太让后台程序抢前台，所以要用点办法。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True
    current = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    threads = {user32.GetWindowThreadProcessId(hwnd, None)}
    if fg:
        threads.add(user32.GetWindowThreadProcessId(fg, None))
    attached = []
    try:
        # 办法一（Windows-MCP 的做法）：把我们的线程和前台窗口、目标窗口“连”在一起，再切前台
        for t in threads:
            if t and t != current and user32.AttachThreadInput(current, t, True):
                attached.append(t)
        user32.AllowSetForegroundWindow(-1)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for t in attached:
            user32.AttachThreadInput(current, t, False)
    if _wait(lambda: user32.GetForegroundWindow() == hwnd, 1.0):
        return True
    # 办法二：最小化再还原（还原的窗口 Windows 一定放到最前面）
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    user32.ShowWindow(hwnd, SW_RESTORE)
    return _wait(lambda: user32.GetForegroundWindow() == hwnd, 1.5)


def _find_window(name, fuzzy=True):
    """按名字找开着的窗口：可以是窗口标题里的字、软件名，或者“这个窗口”。找不到返回 None。
    fuzzy=False 时不做“差不多就行”的匹配（关窗口时用，免得关错）。"""
    q = _norm(name)
    wins = _windows()
    if q in {_norm(t) for t in THIS_WINDOW}:
        return _foreground() or (wins[0] if wins else None)

    def proc(w):
        return _norm(w["process"].rsplit(".", 1)[0])

    for alias, procs in WINDOW_ALIASES.items():
        if q == _norm(alias):
            for w in wins:
                if proc(w) in procs:
                    return w
    for w in wins:  # 标题里有这几个字（最上面的优先）
        if q in _norm(w["title"]):
            return w
    for w in wins:  # 程序名（比如 notepad、chrome）
        if q == proc(w) or (len(q) >= 3 and q in proc(w)):
            return w
    app = find_app(name)  # 说的是软件名（比如“谷歌”→ Google Chrome）：用开始菜单里的名字再找一遍
    if app:
        key = _norm(app[0])
        for w in wins:
            title = _norm(w["title"])
            if key in title or (len(title) >= 2 and title in key):
                return w
    if not fuzzy:
        return None
    titles = {_norm(w["title"]): w for w in wins}
    close = difflib.get_close_matches(q, list(titles), n=1, cutoff=0.5)
    return titles[close[0]] if close else None


def _window_list_text(wins, limit=15):
    return "；".join(f"{_short(w['title'], 25)}" + ("（最小化了）" if w["minimized"] else "")
                    for w in wins[:limit]) or "（没有）"


@_windows_tool
@_dpi_aware
def window(action="switch", name=""):
    """切换 / 关闭 / 最小化 / 最大化 / 还原窗口，或者列出开着的窗口。"""
    action = str(action or "switch").strip().lower()
    action = {"切换": "switch", "关闭": "close", "关掉": "close", "最小化": "minimize", "最大化": "maximize",
              "还原": "restore", "列出": "list"}.get(action, action)
    if action == "list":
        wins = _windows()
        return {"ok": True, "message": f"现在开着 {len(wins)} 个窗口：{_window_list_text(wins)}"}
    win = _find_window(name, fuzzy=action != "close")
    if not win:
        what = f"叫“{name}”的窗口" if str(name or "").strip() else "在最前面的窗口"
        return {"ok": False, "message": f"没找到{what}。现在开着的窗口有：{_window_list_text(_windows())}"}
    hwnd, title = win["hwnd"], _short(win["title"])
    if action == "switch":
        if _activate(hwnd):
            return {"ok": True, "message": f"已切换到“{title}”"}
        return {"ok": False, "message": f"Windows 不让我把“{title}”切到前面，需要你点一下任务栏上的它"}
    if action == "close":
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # 和点右上角的 × 一样，软件可以问你要不要保存
        if _wait(lambda: not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd), 2.0):
            return {"ok": True, "message": f"已关闭“{title}”"}
        return {"ok": True, "message": f"已经让“{title}”关闭，它可能在问你要不要保存"}
    if action == "minimize":
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return {"ok": True, "message": f"已把“{title}”最小化"}
    if action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        _activate(hwnd)
        return {"ok": True, "message": f"已把“{title}”最大化"}
    if action == "restore":
        user32.ShowWindow(hwnd, SW_RESTORE)
        _activate(hwnd)
        return {"ok": True, "message": f"已把“{title}”还原"}
    return {"ok": False, "message": f"不认识的窗口操作：{action}（可以是 switch/close/minimize/maximize/restore/list）"}


# ---------------------------------------------------------------------------
# 打开软件
# ---------------------------------------------------------------------------

_apps_cache = {"time": 0.0, "apps": {}}
_apps_lock = threading.Lock()


def _load_apps():
    """读开始菜单里的所有软件：名字 → 软件ID（PowerShell 的 Get-StartApps，约 1 秒）。"""
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "$a=@(); try { $a=@(Get-StartApps | Select-Object Name,AppID) } catch {};"
              "$b=@(); try { $b=@((New-Object -ComObject Shell.Application).NameSpace('shell:AppsFolder').Items()"
              " | Select-Object Name,@{n='AppID';e={$_.Path}}) } catch {};"
              "ConvertTo-Json -Compress -Depth 3 -InputObject @{start=$a; folder=$b}")
    apps = {}
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, timeout=30, creationflags=NO_WINDOW)
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        for group in (data.get("start"), data.get("folder")):
            for item in ([group] if isinstance(group, dict) else group or []):
                if isinstance(item, dict) and item.get("Name") and item.get("AppID"):
                    apps.setdefault(str(item["Name"]).strip(), str(item["AppID"]))
    except Exception:
        pass
    if not apps:  # 备用办法：直接翻开始菜单文件夹里的快捷方式
        for base in (os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
                     os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")):
            for folder, _, files in os.walk(base):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        apps.setdefault(f[:-4], os.path.join(folder, f))
    return apps


def list_apps(refresh=False):
    with _apps_lock:
        if refresh or not _apps_cache["apps"] or time.time() - _apps_cache["time"] > APPS_CACHE_SECONDS:
            _apps_cache["apps"] = _load_apps()
            _apps_cache["time"] = time.time()
        return dict(_apps_cache["apps"])


def prefetch():
    """在后台先把软件列表读好（唤醒时调用），第一次说“打开某某”就不用等。"""
    if sys.platform != "darwin":
        threading.Thread(target=list_apps, daemon=True).start()


def _usable(name):
    n = _norm(name)
    return not n.startswith(("卸载", "uninstall")) and "星期八" not in n  # 别把助手自己关掉


def _match_app(name, apps):
    q = _norm(name)
    for suffix in ("软件", "应用", "程序", "客户端", "app"):
        if q.endswith(suffix) and len(q) > len(suffix):
            q = q[: -len(suffix)]
    if not q:
        return None
    by_norm = {_norm(n): n for n in apps if _usable(n)}
    if q in by_norm:
        return by_norm[q]
    for alias, targets in APP_ALIASES.items():
        if q == _norm(alias):
            for t in targets:
                if _norm(t) in by_norm:
                    return by_norm[_norm(t)]
    contains = [k for k in by_norm if q in k]
    if contains:
        return by_norm[min(contains, key=lambda k: (not k.startswith(q), len(k)))]
    inside = [k for k in by_norm if len(k) >= 2 and k in q]
    if inside:
        return by_norm[max(inside, key=len)]
    close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=0.6)
    return by_norm[close[0]] if close else None


def find_app(name):
    """按你说的名字在开始菜单里找软件，返回 (开始菜单里的名字, 软件ID)，找不到返回 None。"""
    apps = list_apps()
    match = _match_app(name, apps)
    if match is None and time.time() - _apps_cache["time"] > 30:
        apps = list_apps(refresh=True)  # 可能是刚装的软件：刷新一次再找
        match = _match_app(name, apps)
    return (match, apps[match]) if match else None


@_windows_tool
@_dpi_aware
def open_app(name):
    """打开软件。已经开着的软件，一般会直接切到它。"""
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "message": "要打开哪个软件？"}
    found = find_app(name)
    before = {w["hwnd"] for w in _windows()}
    try:
        if found:
            label, app_id = found
            if os.path.isfile(app_id) or "://" in app_id:
                os.startfile(app_id)  # 快捷方式文件、steam:// 这类链接
            else:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])  # 和在开始菜单里点一样
        elif re.fullmatch(r"[\w.\-]+", name) and shutil.which(name):
            label = name
            subprocess.Popen([shutil.which(name)], cwd=os.path.expanduser("~"))  # 比如 notepad、mspaint
        else:
            close = difflib.get_close_matches(_norm(name), [_norm(n) for n in list_apps() if _usable(n)], n=3, cutoff=0.3)
            hint = f"，名字差不多的有：{'、'.join(close)}" if close else ""
            return {"ok": False, "message": f"开始菜单里没找到叫“{name}”的软件{hint}"}
    except Exception as exc:
        friendly.LOG.exception("打开软件失败")
        return {"ok": False, "message": friendly.MESSAGES["error"], "detail": f"打开“{name}”失败：{exc}"[:300]}

    key = _norm(label)

    def appeared():
        fg = _foreground()
        return fg if fg and (fg["hwnd"] not in before or key in _norm(fg["title"])
                             or key in _norm(fg["process"])) else None

    deadline = time.time() + WAIT_SECONDS
    win = None
    while time.time() < deadline and not win:
        time.sleep(0.3)
        win = appeared()
    if win:
        return {"ok": True, "message": f"已打开{label}（窗口：{_short(win['title'])}）"}
    return {"ok": True, "message": f"已经让电脑打开{label}，窗口可能还在加载"}


# ---------------------------------------------------------------------------
# 看窗口里有什么（UI Automation）
# ---------------------------------------------------------------------------

CONTROL_TYPES = {  # 可以操作的东西：UI Automation 里的类型编号 → 中文名
    50000: "按钮", 50031: "按钮", 50002: "复选框", 50003: "下拉框", 50004: "输入框", 50030: "文档",
    50005: "链接", 50007: "列表项", 50011: "菜单项", 50013: "单选框", 50015: "滑块", 50016: "数值框",
    50019: "标签页", 50024: "树节点", 50029: "数据项", 50035: "表头",
}
TEXT_TYPE = 50020
P_NAME, P_TYPE, P_RECT, P_ENABLED, P_OFFSCREEN = 30005, 30003, 30001, 30010, 30022
P_FOCUS, P_PASSWORD, P_VALUE, P_TOGGLE = 30008, 30019, 30045, 30086
TREE_SCOPE_DESCENDANTS = 4

_last_screen = {"hwnd": None, "title": "", "elements": []}  # 上一次 read_screen 的结果（按编号点击时用）


def _collect(hwnd):
    """读一个窗口里的按钮、输入框、文字（在单独的线程里调用）。只返回普通的 Python 数据。"""
    import comtypes.client
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    root = uia.ElementFromHandle(hwnd)
    condition = None
    for ct in list(CONTROL_TYPES) + [TEXT_TYPE]:
        c = uia.CreatePropertyCondition(P_TYPE, ct)
        condition = c if condition is None else uia.CreateOrCondition(condition, c)
    cache = uia.CreateCacheRequest()  # 一次性把要用的属性都取回来（一个一个问会慢很多）
    for prop in (P_NAME, P_TYPE, P_RECT, P_ENABLED, P_OFFSCREEN, P_FOCUS, P_PASSWORD, P_VALUE, P_TOGGLE):
        cache.AddProperty(prop)
    found = root.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, condition, cache)

    left, top, right, bottom = _rect(hwnd)
    elements, texts, seen = [], [], set()
    for i in range(found.Length):
        el = found.GetElement(i)
        try:
            ctype = el.CachedControlType
            r = el.CachedBoundingRectangle
            if r.right - r.left <= 0 or r.bottom - r.top <= 0 or not el.CachedIsEnabled:
                continue
            x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if not (left - 5 <= x <= right + 5 and top - 5 <= y <= bottom + 5):
                continue  # 不在窗口范围里（被滚出去了）
            if el.CachedIsOffscreen and ctype != 50004:
                continue
            name = re.sub(r"\s+", " ", el.CachedName or "").strip()
            if ctype == TEXT_TYPE:
                if name and (name, x, y) not in seen:
                    seen.add((name, x, y))
                    texts.append({"name": name, "type": "文字", "x": x, "y": y})
                continue
            kind = CONTROL_TYPES.get(ctype, "其他")
            if not name and kind not in ("输入框", "文档"):
                continue  # 没名字的按钮，模型也不知道是干嘛的
            if (kind, name, x, y) in seen:
                continue
            seen.add((kind, name, x, y))
            item = {"name": name, "type": kind, "x": x, "y": y, "focused": bool(el.CachedHasKeyboardFocus)}
            if kind in ("输入框", "文档") and not el.CachedIsPassword:
                value = el.GetCachedPropertyValue(P_VALUE)
                if isinstance(value, str) and value.strip():
                    item["value"] = value.strip()
            if kind == "复选框":
                toggle = el.GetCachedPropertyValue(P_TOGGLE)
                if isinstance(toggle, int):
                    item["checked"] = toggle == 1
            elements.append(item)
        except Exception:
            continue
    return elements, texts


def _read(hwnd):
    """在单独的线程里读窗口（有的软件卡住不回应，最多等 READ_TIMEOUT 秒，不会把助手卡死）。"""
    box = {}

    def work():
        import comtypes
        try:
            user32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_V2)
        except Exception:
            pass
        initialized = False
        try:
            comtypes.CoInitialize()
            initialized = True
        except OSError:
            pass
        try:
            box["result"] = _collect(hwnd)
        except Exception as e:
            box["error"] = f"{type(e).__name__}: {e}"
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(READ_TIMEOUT)
    if t.is_alive():
        raise TimeoutError("窗口没有响应")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box["result"]


def _read_window(hwnd):
    """读窗口。Chrome、Edge、微信这类“网页内核”的软件，第一次被问时只给几个按钮，
    之后才打开完整的界面信息（实测：第一次 3 个，第二次 87 个），所以读到太少时隔一会儿再读。"""
    elements, texts = _read(hwnd)
    if _class(hwnd).startswith("Chrome_WidgetWin"):
        for _ in range(3):
            if len(elements) > 5:
                break
            time.sleep(0.4)
            elements, texts = _read(hwnd)
    return elements, texts


def _describe(e, number):
    line = f"[{number}] {e['type']}「{_short(e['name'], 30)}」" if e["name"] else f"[{number}] {e['type']}"
    if e.get("value"):
        line += f" 内容：{_short(e['value'], 40)}"
    if "checked" in e:
        line += " 已勾选" if e["checked"] else " 未勾选"
    if e.get("focused"):
        line += " （光标在这）"
    return line


def _target_window(name=""):
    if str(name or "").strip():
        win = _find_window(name)
        if win and user32.GetForegroundWindow() != win["hwnd"]:
            _activate(win["hwnd"])
        return win
    win = _foreground()
    if win:
        return win
    if _last_screen["hwnd"] and user32.IsWindow(_last_screen["hwnd"]):
        return _info(_last_screen["hwnd"])
    wins = _windows()
    return wins[0] if wins else None


@_windows_tool
@_dpi_aware
def read_screen(name=""):
    """看看窗口里有哪些可以点的东西、写着什么字。"""
    win = _target_window(name)
    if not win:
        return {"ok": False, "message": f"没找到{'叫“' + name + '”的' if name else '可以看的'}窗口"}
    try:
        elements, texts = _read_window(win["hwnd"])
    except Exception as exc:
        friendly.LOG.exception("读取窗口内容失败")
        return {"ok": False, "message": friendly.MESSAGES["error"], "detail": f"读不出窗口内容：{exc}"[:300]}
    elements = elements[:MAX_ELEMENTS]
    _last_screen.update(hwnd=win["hwnd"], title=win["title"], elements=elements)
    lines = [f"窗口：{_short(win['title'], 40)}（{win['process']}）"]
    if elements:
        lines.append("可以操作的（点击时 target 填编号或名字）：")
        lines += [_describe(e, i) for i, e in enumerate(elements, 1)]
    else:
        lines.append("（没读到按钮之类的东西，可能是游戏或者特殊界面，可以试试快捷键）")
    names = {e["name"] for e in elements}
    words, total = [], 0
    for t in texts:
        if t["name"] in names or total > 600:
            continue
        words.append(_short(t["name"], 60))
        total += len(words[-1])
        if len(words) >= MAX_TEXTS:
            break
    if words:
        lines.append("窗口里的文字：" + " | ".join(words))
    return {"ok": True, "message": "\n".join(lines), "count": len(elements)}


TYPE_WORDS = re.compile(r"(那个|这个|一下|按钮|按键|链接|菜单项|菜单|选项|标签页|标签|图标|输入框|搜索框|文本框|列表项)")
# 名字里有这些词的按钮，只有你的话里也有这个词才会点。
# 实测教训：说“计算器”，模糊匹配挑中了“关闭计算器”，一点就把计算器关了。
DANGER_WORDS = re.compile(r"关闭|删除|退出|卸载|清空|移除|注销|关机|重启|格式化|close|delete|exit|quit|remove|uninstall", re.I)


def _match_element(target, elements, texts=()):
    """按名字找要点的东西：完全一样 > 包含 > 差不多。模糊找的时候绝不挑出“关闭”“删除”这类按钮。"""
    named = [e for e in elements if e["name"]]
    pools = (named, list(texts))  # 先找按钮之类，再找文字
    t = _norm(target)
    cands = [c for c in dict.fromkeys([t, _norm(TYPE_WORDS.sub("", target))]) if c]
    for cand in cands:  # 1. 名字完全一样
        for pool in pools:
            for e in pool:
                if _norm(e["name"]) == cand:
                    return e

    def safe(e):
        return not DANGER_WORDS.search(e["name"]) or DANGER_WORDS.search(str(target))

    for cand in cands:  # 2. 名字里包含你说的字，或者你说的话里包含它的名字
        for pool in pools:
            contains = [e for e in pool if cand in _norm(e["name"]) and safe(e)]
            if contains:
                return min(contains, key=lambda e: len(e["name"]))
            inside = [e for e in pool if len(_norm(e["name"])) >= 2 and _norm(e["name"]) in cand and safe(e)]
            if inside:
                return max(inside, key=lambda e: len(e["name"]))
    if re.search("输入|搜索|文本框|地址栏", str(target)):  # 3. 说的是“输入框”“搜索框”
        edits = [e for e in elements if e["type"] == "输入框"]
        searching = [e for e in edits if re.search("搜索|search|地址", e["name"], re.I)]
        if searching or edits:
            return (searching or edits)[0]
    by_name = {_norm(e["name"]): e for e in named if safe(e)}  # 4. 差不多的
    close = difflib.get_close_matches(cands[-1] if cands else t, list(by_name), n=1, cutoff=0.6)
    return by_name[close[0]] if close else None


def _is_window_name(target):
    """target 说的是不是一个窗口本身（比如“计算器”“记事本”）。是的话返回那个窗口。"""
    if re.search("输入|搜索|框|栏|按钮|链接|菜单", str(target)):
        return None
    win = _find_window(target, fuzzy=False)
    return win if win and _norm(target) and _norm(target) in _norm(win["title"]) else None


# ---------------------------------------------------------------------------
# 鼠标和键盘
# ---------------------------------------------------------------------------


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk=0, scan=0, flags=0):
    i = INPUT(type=INPUT_KEYBOARD)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def _mouse(flags, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


@_dpi_aware
def _click_at(x, y, right=False, double=False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    down, up = MOUSE_RIGHT if right else MOUSE_LEFT
    events = [_mouse(down), _mouse(up)] * (2 if double else 1)
    return _send(events) == len(events)


@_windows_tool
@_dpi_aware
def click(target, double=False, right=False):
    """点击窗口里的按钮、菜单、链接。target：read_screen 给的编号，或者按钮上的字。"""
    target = str(target or "").strip()
    double, right = _bool(double), _bool(right)
    number = re.fullmatch(r"\[?(\d+)\]?", target)
    if number and _last_screen["elements"]:
        index = int(number.group(1))
        if not 1 <= index <= len(_last_screen["elements"]):
            return {"ok": False, "message": f"没有编号 {index}，上次看到的编号是 1 到 {len(_last_screen['elements'])}"}
        hwnd = _last_screen["hwnd"]
        if not user32.IsWindow(hwnd):
            return {"ok": False, "message": "上次看的窗口已经关了，请重新 read_screen"}
        element, title = _last_screen["elements"][index - 1], _last_screen["title"]
    else:
        win = _target_window()
        if not win:
            return {"ok": False, "message": "现在没有可以点的窗口"}
        hwnd, title = win["hwnd"], win["title"]
        try:
            elements, texts = _read_window(hwnd)
        except Exception as exc:
            friendly.LOG.exception("读取要点击的窗口失败")
            return {"ok": False, "message": friendly.MESSAGES["error"], "detail": f"读不出要点击的窗口：{exc}"[:300]}
        element = _match_element(target, elements, texts)
        if not element:
            names = "、".join(dict.fromkeys(_short(e["name"], 12) for e in elements if e["name"]))
            return {"ok": False, "message": f"在“{_short(title)}”里没找到“{target}”。能点的有：{_short(names, 300)}"}
    if user32.GetForegroundWindow() != hwnd:
        _activate(hwnd)
    if not _click_at(element["x"], element["y"], right, double):
        return {"ok": False, "message": f"“{_short(title)}”不接受模拟点击（可能是用管理员权限运行的软件）"}
    time.sleep(0.3)
    verb = "双击了" if double else "右键点击了" if right else "点击了"
    return {"ok": True, "message": f"已{verb}「{_short(element['name'] or element['type'])}」（{_short(title)}）",
            "element_type": element["type"]}


VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "capslock": 0x14, "menu": 0x5D,
    "playpause": 0xB3, "play": 0xB3, "pause": 0xB3, "nexttrack": 0xB0, "next": 0xB0,
    "prevtrack": 0xB1, "previous": 0xB1, "prev": 0xB1, "stop": 0xB2,
    # 中文说法
    "回车": 0x0D, "空格": 0x20, "退格": 0x08, "删除": 0x2E, "上": 0x26, "下": 0x28, "左": 0x25, "右": 0x27,
    "播放": 0xB3, "暂停": 0xB3, "播放暂停": 0xB3, "下一首": 0xB0, "上一首": 0xB1, "制表": 0x09,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D, 0x2C,
            0xB0, 0xB1, 0xB2, 0xB3}


def _vk(name):
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper())
    return None


def _parse_keys(keys):
    """“ctrl+shift+esc down enter” → [[ctrl, shift, esc], [down], [enter]]。不认识的键返回它的名字。"""
    text = re.sub(r"\s*\+\s*", "+", str(keys or "").strip())
    combos = []
    for combo in re.split(r"[\s,，、]+", text):
        if not combo:
            continue
        codes = []
        for part in combo.split("+"):
            code = _vk(part)
            if code is None:
                return part
            codes.append(code)
        combos.append(codes)
    return combos


def _press(codes):
    def flags(c, up=False):
        return (KEYEVENTF_EXTENDEDKEY if c in EXTENDED else 0) | (KEYEVENTF_KEYUP if up else 0)
    events = [_key(c, 0, flags(c)) for c in codes] + [_key(c, 0, flags(c, True)) for c in reversed(codes)]
    return _send(events) == len(events)


@_windows_tool
def press_keys(keys):
    """按快捷键：“ctrl+s”“alt+f4”“win+d”；多个依次按就用空格隔开：“down down enter”。"""
    combos = _parse_keys(keys)
    if isinstance(combos, str):
        return {"ok": False, "message": f"不认识的按键：{combos}（可以用 ctrl、alt、shift、win、enter、esc、tab、"
                                        "up/down/left/right、f1~f12、字母数字、playpause、nexttrack、prevtrack）"}
    if not combos:
        return {"ok": False, "message": "要按什么键？"}
    for codes in combos:
        if not _press(codes):
            return {"ok": False, "message": "当前窗口不接受模拟按键（可能是用管理员权限运行的软件）"}
        time.sleep(0.05)
    win = _foreground()
    where = f"（在“{_short(win['title'])}”里）" if win else ""
    return {"ok": True, "message": f"已按下 {keys}{where}"}


def _type_unicode(text):
    """一个字一个字地“敲”进去（直接发文字，不经过输入法，所以中文也行）。"""
    for start in range(0, len(text), 20):
        events = []
        for ch in text[start:start + 20]:
            if ch == "\r":
                continue
            if ch == "\n":
                events += [_key(0x0D), _key(0x0D, 0, KEYEVENTF_KEYUP)]
                continue
            code = ord(ch)
            units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
            for u in units:
                events += [_key(0, u, KEYEVENTF_UNICODE), _key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        if events and _send(events) != len(events):
            return False
        time.sleep(0.01)
    return True


@_windows_tool
def type_text(text, target="", enter=False, clear=False):
    """在当前光标所在的地方打字。target：先点一下哪个输入框；enter：打完按回车；
    clear：先清空 target 这个输入框原来的内容。
    注意：清空只对 target 指定的“输入框”（搜索框、地址栏这种一行的框）有效。
    在整篇文档里（记事本、Word）绝不清空——实测模型会自作主张加 clear，那样会把你写的东西全删了。"""
    text = str(text or "")
    clicked_type = None
    window_itself = _is_window_name(target) if str(target or "").strip() else None
    if window_itself:  # target 说的是窗口本身（“计算器”）：切到它就行，不去点里面的按钮
        if user32.GetForegroundWindow() != window_itself["hwnd"]:
            _activate(window_itself["hwnd"])
        target = ""
    if str(target or "").strip():
        result = click(target)
        if not result["ok"]:
            return result
        clicked_type = result.get("element_type")
        time.sleep(0.2)
    win = _foreground()
    if not win:
        return {"ok": False, "message": "现在没有可以打字的窗口"}
    if _bool(clear) and clicked_type == "输入框":
        _press([0x11, ord("A")])
        _press([0x2E])
    if text and not _type_unicode(text):
        return {"ok": False, "message": f"“{_short(win['title'])}”不接受模拟打字（可能是用管理员权限运行的软件）"}
    if _bool(enter):
        time.sleep(0.05)
        _press([0x0D])
    return {"ok": True, "message": f"已在“{_short(win['title'])}”里输入：{_short(text, 40)}"
                                   + ("，并按了回车" if _bool(enter) else "")}


@_windows_tool
@_dpi_aware
def scroll(direction="down", amount=5):
    """滚动当前窗口：把鼠标移到窗口中间，再滚滚轮。"""
    try:
        amount = max(1, min(int(amount), 30))
    except (TypeError, ValueError):
        amount = 5
    direction = {"上": "up", "下": "down", "左": "left", "右": "right"}.get(str(direction), str(direction).lower())
    win = _foreground()
    if not win:
        return {"ok": False, "message": "现在没有可以滚动的窗口"}
    left, top, right, bottom = _rect(win["hwnd"])
    user32.SetCursorPos((left + right) // 2, (top + bottom) // 2)
    time.sleep(0.05)
    if direction in ("left", "right"):
        _send([_mouse(MOUSEEVENTF_HWHEEL, 120 * amount * (1 if direction == "right" else -1))])
    else:
        _send([_mouse(MOUSEEVENTF_WHEEL, 120 * amount * (1 if direction == "up" else -1))])
    words = {"up": "往上", "down": "往下", "left": "往左", "right": "往右"}.get(direction, direction)
    return {"ok": True, "message": f"已把“{_short(win['title'])}”{words}滚了 {amount} 格"}


# ---------------------------------------------------------------------------
# 网页、电脑状态
# ---------------------------------------------------------------------------


@_windows_tool
def open_web(url="", search=""):
    """用默认浏览器打开网址，或者搜索。"""
    url, search = str(url or "").strip(), str(search or "").strip()
    if url:
        key = _norm(url)
        if key in SITES:
            url = SITES[key]
        elif re.fullmatch(r"(?i)https?://\S+", url):
            pass
        elif re.fullmatch(r"[\w\-]+(\.[\w\-]+)+(:\d+)?(/\S*)?", url):
            url = "https://" + url
        else:
            search, url = search or url, ""  # 不像网址：当成要搜索的内容
    if url:
        os.startfile(url)
        return {"ok": True, "message": f"已用浏览器打开 {url}"}
    if not search:
        return {"ok": False, "message": "要打开哪个网址，或者搜什么？"}
    engine = settings.get("search_engine", DEFAULT_SEARCH_ENGINE)
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_SEARCH_ENGINE])
    os.startfile(template.format(urllib.parse.quote(search)))
    return {"ok": True, "message": f"已用{engine if engine in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE}搜索“{search}”"}


@_windows_tool
def system_info():
    """电量、CPU、内存、硬盘、开机多久。"""
    parts = []
    battery = psutil.sensors_battery()
    if battery:
        line = f"电量 {battery.percent:.0f}%，" + ("正在充电" if battery.power_plugged else "没插电源")
        if not battery.power_plugged and 0 < battery.secsleft < 100 * 3600:
            line += f"，大约还能用 {battery.secsleft // 3600} 小时 {battery.secsleft % 3600 // 60} 分钟"
        parts.append(line)
    parts.append(f"CPU 占用 {psutil.cpu_percent(interval=0.3):.0f}%")
    mem = psutil.virtual_memory()
    parts.append(f"内存用了 {mem.used / 1024 ** 3:.1f} GB / 共 {mem.total / 1024 ** 3:.0f} GB")
    disks = []
    for p in psutil.disk_partitions():
        if "fixed" in p.opts or p.fstype:
            try:
                disks.append(f"{p.device[0]}盘剩 {psutil.disk_usage(p.mountpoint).free / 1024 ** 3:.0f} GB")
            except Exception:
                pass
    if disks:
        parts.append("，".join(disks))
    up = int(time.time() - psutil.boot_time())
    parts.append(f"已经开机 {up // 3600} 小时 {up % 3600 // 60} 分钟")
    return {"ok": True, "message": "；".join(parts)}


# ---------------------------------------------------------------------------
# 危险操作：先登记，等你说“确定”才做
# ---------------------------------------------------------------------------

_pending = {"action": None}
REGISTRY = re.compile(r"\breg(\.exe)?\s+(add|delete|import|copy|restore|load|unload|save)\b|regedit|\bHK(LM|CU|CR|U|CC)\b"
                      r"|HKEY_|Registry::|ItemProperty", re.I)
YES = re.compile(r"确定|确认|执行|可以|是的|没问题|好的|好啊|^好|^行|^对|同意|开始吧|做吧|删吧|关吧|ok|yes", re.I)
NO = re.compile(r"不|别|取消|算了|停|等等|等一下|no|cancel", re.I)


def _ask_confirm(tool, args, say):
    _pending["action"] = {"tool": tool, "args": args, "say": say, "time": time.time()}
    return {"ok": True, "needs_confirm": True,
            "message": f"{say}，确定吗？说“确定”我就做，说“取消”就不做。"}


def pending():
    """正在等你确认的危险操作（超时的自动作废），没有就返回 None。"""
    if sys.platform == "darwin":
        return None
    action = _pending["action"]
    if action and time.time() - action["time"] > CONFIRM_SECONDS:
        _pending["action"] = action = None
    return action


def answer(text):
    """你对“确定吗？”的回答：'yes'（确定）、'no'（取消），说的是别的事返回 None。"""
    t = re.sub(r"[\s，。！？、,.!?～~]", "", str(text or ""))
    if not t or len(t) > 15:
        return None
    if NO.search(t):
        return "no"
    if YES.search(t):
        return "yes"
    return None


@_windows_tool
def confirm():
    """你说了“确定”：执行等待中的危险操作。"""
    action = pending()
    _pending["action"] = None
    if not action:
        return {"ok": False, "message": "没有等你确认的操作了（可能已经超时作废）"}
    try:
        return EXECUTORS[action["tool"]](**action["args"])
    except Exception as exc:
        friendly.LOG.exception("执行已确认的操作失败")
        return {"ok": False, "message": friendly.MESSAGES["error"], "detail": f"执行时出错：{exc}"[:300]}


def cancel():
    had = pending()
    _pending["action"] = None
    return {"ok": True, "message": "好的，已经取消，不做了。" if had else "没有要取消的操作"}


@_windows_tool
def run_command(command, purpose=""):
    """运行 PowerShell 命令（先登记，等你确认）。"""
    command = str(command or "").strip()
    if not command:
        return {"ok": False, "message": "要运行什么命令？"}
    if REGISTRY.search(command):
        return {"ok": False, "message": "这条命令会动注册表，按规定我不执行"}
    about = f"（{purpose}）" if str(purpose or "").strip() else ""
    return _ask_confirm("run_command", {"command": command}, f"要运行命令「{_short(command, 80)}」{about}")


def _run_command_now(command):
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + command
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=120, creationflags=NO_WINDOW, cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        friendly.LOG.exception("命令运行超时")
        return {"ok": False, "message": friendly.MESSAGES["error"], "detail": "命令运行超过 2 分钟，不等了"}
    output = r.stdout.decode("utf-8", "replace").strip()
    errors = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode:
        friendly.LOG.error("命令执行失败，返回码 %s\n%s\n%s", r.returncode, output, errors)
        # message 念给人听；detail 只交给模型，让它知道哪里错了、能改了再试
        return {"ok": False, "message": friendly.MESSAGES["error"],
                "detail": f"命令出错了（返回码 {r.returncode}）：{_short(errors or output, 300)}"}
    if errors:
        friendly.LOG.warning("命令运行时的诊断信息：%s", errors)
    message = "命令执行完了"
    if output:
        message += f"，输出：{_short(output, 300)}"
    return {"ok": True, "message": message}


def _protected(path):
    """系统文件夹、整个盘、用户文件夹本身、助手自己的文件夹：不让删。"""
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    if re.fullmatch(r"[a-z]:", p):
        return True
    if p.startswith(os.path.normcase(os.path.join(PROJECT_DIR, "tests")) + "\\"):
        return False  # 助手文件夹里只有 tests（测试用的临时文件）可以删
    roots = [os.environ.get(k, "") for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
    exact = [os.path.expanduser("~"), os.path.join(os.path.expanduser("~"), "Desktop"),
             os.path.join(os.path.expanduser("~"), "Documents"), os.path.join(os.path.expanduser("~"), "Downloads")]
    for r in roots + [PROJECT_DIR]:
        r = os.path.normcase(os.path.abspath(r)).rstrip("\\") if r else ""
        if r and (p == r or p.startswith(r + "\\") or r.startswith(p + "\\")):
            return True
    return any(p == os.path.normcase(os.path.abspath(e)).rstrip("\\") for e in exact)


@_windows_tool
def delete_file(path):
    """把文件或文件夹删到回收站（先登记，等你确认）。"""
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"'))))
    if not str(path or "").strip() or not os.path.exists(p):
        return {"ok": False, "message": f"找不到“{path}”"}
    if _protected(p):
        return {"ok": False, "message": f"“{p}”是系统或重要的文件夹，不能删"}
    kind = "文件夹" if os.path.isdir(p) else "文件"
    return _ask_confirm("delete_file", {"path": p}, f"要把{kind}“{p}”删到回收站")


def _delete_now(path):
    buf = ctypes.create_unicode_buffer(path + "\0\0")  # 这个接口要求以两个 \0 结尾
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # 删除
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # 放进回收站、不弹确认框、不显示进度、不弹错误框
    result = shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or os.path.exists(path):
        return {"ok": False, "message": f"删除“{path}”失败（错误码 {result}），可能文件正被别的软件占用"}
    return {"ok": True, "message": f"已把“{os.path.basename(path)}”删到回收站，后悔了可以从回收站还原"}


POWER_WORDS = {"锁屏": "lock", "锁定": "lock", "睡眠": "sleep", "休眠": "sleep", "关机": "shutdown",
               "重启": "restart", "重新启动": "restart", "取消": "cancel", "取消关机": "cancel"}


@_windows_tool
def power(action):
    """锁屏、睡眠、关机、重启、取消关机。睡眠/关机/重启要你确认。"""
    action = POWER_WORDS.get(str(action or "").strip(), str(action or "").strip().lower())
    if action == "lock":
        return _power_now("lock")
    if action == "cancel":
        return _power_now("cancel")
    if action in ("sleep", "shutdown", "restart"):
        say = {"sleep": "要让电脑睡眠", "shutdown": "要关机（30 秒后关，期间说“取消关机”还来得及）",
               "restart": "要重启电脑（30 秒后重启，期间说“取消关机”还来得及）"}[action]
        return _ask_confirm("power", {"action": action}, say)
    return {"ok": False, "message": f"不认识的操作：{action}（可以是 lock/sleep/shutdown/restart/cancel）"}


def _power_now(action):
    if action == "lock":
        ctypes.windll.user32.LockWorkStation()
        return {"ok": True, "message": "已锁屏"}
    if action == "cancel":
        r = subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
        return {"ok": True, "message": "已取消关机" if r.returncode == 0 else "现在没有要执行的关机或重启"}
    if action == "sleep":
        ctypes.windll.powrprof.SetSuspendState(False, False, False)
        return {"ok": True, "message": "电脑睡眠了"}
    flag = "/s" if action == "shutdown" else "/r"
    subprocess.run(["shutdown", flag, "/t", "30"], capture_output=True, creationflags=NO_WINDOW)
    word = "关机" if action == "shutdown" else "重启"
    return {"ok": True, "message": f"电脑会在 30 秒后{word}，想取消就说“取消关机”"}


EXECUTORS = {"run_command": _run_command_now, "delete_file": _delete_now, "power": _power_now}

# ---------------------------------------------------------------------------
# 给秘书模型的工具说明书
# ---------------------------------------------------------------------------


def _tool(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


TOOLS = [
    _tool("open_app", "打开电脑上的软件（从开始菜单里找），例如“打开微信”“打开记事本”“打开浏览器”“启动 Steam”。"
                      "软件已经开着的话，一般会直接切到它。",
          {"name": {"type": "string", "description": "软件名字，照用户说的填，例如 微信、记事本、QQ音乐、浏览器、计算器"}},
          ["name"]),
    _tool("window", "管理已经开着的窗口：切换到它、关闭、最小化、最大化、还原，或者列出现在开着哪些窗口。",
          {"action": {"type": "string", "enum": ["switch", "close", "minimize", "maximize", "restore", "list"],
                      "description": "switch=切换到它（放到最前面）；close=关闭；minimize=最小化；maximize=最大化；"
                                     "restore=还原；list=列出所有窗口"},
           "name": {"type": "string", "description": "窗口名字或软件名，例如 微信、记事本、浏览器。"
                                                     "用户说“这个窗口”“当前窗口”就留空"}},
          ["action"]),
    _tool("read_screen", "看看当前窗口（或指定的窗口）里有哪些按钮、输入框、菜单、链接，以及上面写的字。"
                         "不知道该点哪里、或者用户问屏幕上有什么时用它。每个能操作的东西都有编号，点击时可以用编号。",
          {"name": {"type": "string", "description": "要看的窗口名字；不填就看当前最前面的窗口"}}),
    _tool("click", "点击当前窗口里的按钮、菜单、链接、列表项等。",
          {"target": {"type": "string", "description": "read_screen 给的编号（如 3），或者按钮上的字（如 保存、发送）"},
           "double": {"type": "boolean", "description": "是否双击（打开文件时常用）"},
           "right": {"type": "boolean", "description": "是否右键点击"}},
          ["target"]),
    _tool("type_text", "在当前光标所在的输入框里打字（支持中文）。可以用 target 先点一下要输入的框。",
          {"text": {"type": "string", "description": "要输入的文字"},
           "target": {"type": "string", "description": "先点哪个输入框（编号或名字，如 搜索）；光标已经在框里就不填"},
           "enter": {"type": "boolean", "description": "打完后按回车。只有用户要求“发送”“搜索”“确认”时才填 true；"
                                                       "用户只说“写”“输入”就不要填（在微信等聊天软件里，回车会直接把消息发出去）"},
           "clear": {"type": "boolean", "description": "先清空 target 这个输入框里原来的内容（只对搜索框这种一行的框有效；"
                                                       "用户没要求替换原来的内容就不要填）"}},
          ["text"]),
    _tool("press_keys", "按键盘按键或快捷键。例如 ctrl+c 复制、ctrl+v 粘贴、ctrl+s 保存、ctrl+z 撤销、alt+f4 关闭、"
                        "win+d 显示桌面、alt+tab 切换、enter、esc、tab、f5 刷新、up/down/left/right 方向键；"
                        "媒体键 playpause 播放/暂停、nexttrack 下一首、prevtrack 上一首。多个依次按用空格隔开，如“down down enter”。",
          {"keys": {"type": "string", "description": "按键，例如 ctrl+s"}}, ["keys"]),
    _tool("scroll", "滚动当前窗口（往下翻、往上翻）。",
          {"direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "方向"},
           "amount": {"type": "integer", "description": "滚几格，默认 5"}},
          ["direction"]),
    _tool("open_web", "用浏览器打开网址，或者在网上搜索东西。用户说“打开B站”“搜一下……”时用。",
          {"url": {"type": "string", "description": "网址（如 bilibili.com），或者网站名（如 B站、淘宝、知乎）"},
           "search": {"type": "string", "description": "要搜索的内容（只想打开网站就不填）"}}),
    _tool("system_info", "查看电脑状态：电量、是否在充电、CPU 和内存占用、硬盘剩余空间、开机多久了。"),
    _tool("run_command", "在电脑上运行一条 PowerShell 命令。只有别的工具都做不到时才用。危险操作：系统会先请用户确认。",
          {"command": {"type": "string", "description": "PowerShell 命令"},
           "purpose": {"type": "string", "description": "用一句中文说明这条命令要做什么（会念给用户听）"}},
          ["command", "purpose"]),
    _tool("delete_file", "把文件或文件夹删到回收站。危险操作：系统会先请用户确认。",
          {"path": {"type": "string", "description": "文件或文件夹的完整路径"}}, ["path"]),
    _tool("power", "锁屏、睡眠、关机、重启，或者取消关机。睡眠、关机、重启是危险操作，系统会先请用户确认；关机和重启在 30 秒后执行。",
          {"action": {"type": "string", "enum": ["lock", "sleep", "shutdown", "restart", "cancel"],
                      "description": "lock=锁屏；sleep=睡眠；shutdown=关机；restart=重启；cancel=取消关机/重启"}},
          ["action"]),
]

FUNCTIONS = {"open_app": open_app, "window": window, "read_screen": read_screen, "click": click,
             "type_text": type_text, "press_keys": press_keys, "scroll": scroll, "open_web": open_web,
             "system_info": system_info, "run_command": run_command, "delete_file": delete_file, "power": power}

if sys.platform == "darwin":
    TOOLS = []

NAMES = {"open_app": "打开软件", "window": "管理窗口", "read_screen": "看窗口里有什么", "click": "点击",
         "type_text": "打字", "press_keys": "按快捷键", "scroll": "滚动", "open_web": "打开网页",
         "system_info": "查看电脑状态", "run_command": "运行命令", "delete_file": "删除文件", "power": "电源"}


def brief(name, result):
    """存进记忆/历史时用的简短版本：read_screen 的整张清单（可能有屏幕上的文字）不存，只留第一行。"""
    if name != "read_screen" or not result.get("ok"):
        return result
    first = result["message"].splitlines()[0]
    return {"ok": True, "message": f"{first}（看了一眼，共 {result.get('count', 0)} 个可操作的东西，清单没有保存）"}
