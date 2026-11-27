"""
delegate.py —— 秘书把任务交给“干活的 AI”（谁来干活见 workers.py）

    delegate(任务说明, 交给谁, 简短名称, 你的原话)
        命令行 AI（Claude Code）：新建任务文件夹 → 写“任务说明.md” → 在新窗口里启动它
        网页 AI（DeepSeek、豆包……）：把任务复制到剪贴板 → 打开网页 → 你按 Ctrl+V 发送

交出去以后，秘书就不管了：AI 后面怎么做、问什么，都在它自己的窗口/网页里进行。
"""

import ctypes
import os
import re
import subprocess
import sys
import webbrowser
from ctypes import wintypes
from datetime import datetime

import workers

# 测试时设为 True：只建文件夹、写任务说明，不真的打开窗口和网页
DRY_RUN = False

TASK_FILE = "任务说明.md"
CLI_PROMPT = f"请先阅读当前文件夹里的“{TASK_FILE}”，然后完成里面的任务。生成的文件都放在当前文件夹里。"


def find_worker(name=None):
    """按名字或别名找到要交给的 AI；没点名或找不到时用默认的。返回 (名字, 配置)。"""
    if name:
        key = str(name).strip().lower()
        for worker, cfg in workers.WORKERS.items():
            if key == worker.lower() or key in (a.lower() for a in cfg.get("aliases", [])):
                return worker, cfg
    return workers.DEFAULT_WORKER, workers.WORKERS[workers.DEFAULT_WORKER]


def _folder_name(title, now):
    title = re.sub(r'[\\/:*?"<>|\s]+', "", str(title or "任务"))[:20] or "任务"
    return f"{now:%Y-%m-%d %H%M} {title}"


def make_task_folder(title, now):
    """新建任务文件夹，例如 D:\\AI任务\\2026-11-20 1405 数学作业（重名就加编号）。"""
    base = os.path.join(workers.TASKS_DIR, _folder_name(title, now))
    folder, n = base, 2
    while os.path.exists(folder):
        folder, n = f"{base} ({n})", n + 1
    os.makedirs(folder)
    return folder


def write_task_file(folder, task, said, worker, now):
    content = (
        "# 任务说明\n\n"
        f"- 交给：{worker}\n"
        f"- 时间：{now:%Y-%m-%d %H:%M}\n\n"
        "## 任务\n\n"
        f"{task}\n\n"
        "## 用户的原话（语音识别的结果，可能有错别字，以上面的“任务”为准）\n\n"
        f"> {said}\n\n"
        "## 说明\n\n"
        "- 这是用户用语音交代的任务，由本地语音助手转述。\n"
        "- 请在这个文件夹里完成任务，生成的文件也放在这里。\n"
    )
    with open(os.path.join(folder, TASK_FILE), "w", encoding="utf-8") as f:
        f.write(content)


def _ps_quote(text):
    """PowerShell 单引号字符串（里面的单引号要写两遍）。"""
    return "'" + str(text).replace("'", "''") + "'"


def launch_in_new_window(command, folder, keep_open=True):
    """在 folder 里打开一个新的 PowerShell 窗口运行 command（默认窗口不自动关掉，方便你看结果）。"""
    exe, *args = command
    line = "& " + " ".join(_ps_quote(part) for part in [exe, *args])
    options = ["-NoExit"] if keep_open else []
    return subprocess.Popen(
        ["powershell.exe", *options, "-NoProfile", "-Command", line],
        cwd=folder,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def copy_to_clipboard(text):
    """把文字放进剪贴板（支持中文）。"""
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    data = (str(text) + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        raise OSError("剪贴板被别的程序占用了")
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def delegate(task, worker=None, title=None, said=""):
    """把任务交给干活的 AI（这是给秘书模型用的工具）。"""
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return {"ok": False, "message": MAC_UNSUPPORTED}
    task = str(task or "").strip()
    if not task:
        return {"message": "没有要交出去的任务"}
    name, cfg = find_worker(worker)
    now = datetime.now()

    if cfg["type"] == "cli":
        command = [os.path.expandvars(part) for part in cfg["command"]]
        if not DRY_RUN and not os.path.exists(command[0]):
            return {"worker": name,
                    "message": f"还没安装 {name} 的命令行版（找不到 {command[0]}），这个任务没交出去"}
        folder = make_task_folder(title, now)
        write_task_file(folder, task, said, name, now)
        if not DRY_RUN:
            launch_in_new_window(command + [CLI_PROMPT], folder)
        return {"worker": name, "folder": folder,
                "message": f"已经交给 {name}，它会在新窗口里开始干活。任务文件夹：{folder}"}

    # 网页 AI：复制任务 + 打开网页
    if not DRY_RUN:
        copy_to_clipboard(task)
        webbrowser.open(cfg["url"])
    return {"worker": name,
            "message": f"已经打开 {name} 网页，任务复制好了，在输入框里按 Ctrl+V 再发送就行"}
