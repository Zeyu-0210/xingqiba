"""
agent_codex.py —— 让 GPT 用你的 ChatGPT 账号干活（借你装的 Codex 桌面版自带的命令行程序 codex.exe）

    不用 API 密钥，走你的 ChatGPT 会员额度（和 Claude 用 Claude Code 走 Pro 额度一样）。
    不用另外安装：Codex 桌面版里自带 codex.exe，登录也是共用的（你在桌面版登录过就行）。

    三种用法：
        chat  只聊天：只读沙盒，在“项目副本”文件夹里
        look  只看不改（群里没有执行权、或者 AI 之间接话时）：只读沙盒，
              能看电脑上的文件、运行只看不改的命令；不能动鼠标、不能操控浏览器
        do    动手（有执行权）：和你单独用 Codex 完全一样，功能全开、能动整台电脑，没有程序拦
              （2026-11-21 按你的要求删掉了“门卫”）：操控屏幕（computer_use）、浏览器、插件、连接器、
              Node 脚本、一直开着的命令窗口都能用——它是真的在你屏幕上动鼠标，你看得到，随时能接管
    实测（2026-11-21，codex-cli 0.171）：它改文件用 apply_patch；命令有时用 cmd、有时用 PowerShell
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import takeover
from login_status import LoginCache, LoginResult
from run_control import RunControl, RunStopped

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 15 * 60       # 动手最多做多久
LOOK_TIMEOUT = 6 * 60   # 只看（回一句话）最多多久（2026-11-21 实测有一次网络卡住，等了 15 分钟才报错）
LOGIN_HINT = "还没登录 ChatGPT 账号（打开 Codex 桌面版登录一次）"
_LOGIN_ERROR = re.compile(
    r"\b(?:not logged in|(?:re-)?run [`'\"]?codex login|your access token could not be refreshed"
    r"|unexpected status 401 unauthorized)\b", re.IGNORECASE)

# 只看时关掉的功能（没有执行权的不许动鼠标、不许操控浏览器；动手时什么都不关）
LOOK_DISABLED = ["unified_exec", "unified_exec_tty", "computer_use", "apps", "plugins", "browser_use",
                 "browser_use_external"]
EFFORTS = {"low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高", "ultra": "极限"}

def models():
    """你的 ChatGPT 账号能用哪些模型（从 Codex 的模型列表缓存里读，Codex 更新了这里也跟着变）。
    返回 [{"id", "name", "efforts": [...], "default_effort"}]，按 Codex 推荐的顺序。"""
    try:
        with open(os.path.expanduser("~/.codex/models_cache.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for m in sorted(data.get("models") or [], key=lambda m: m.get("priority", 99)):
        if not isinstance(m, dict) or m.get("visibility") != "list" or not m.get("slug"):
            continue
        efforts = [e.get("effort") if isinstance(e, dict) else e for e in m.get("supported_reasoning_levels") or []]
        out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"], "efforts": [e for e in efforts if e],
                    "default_effort": m.get("default_reasoning_level") or ""})
    return out


_exe_cache = {"path": None}


def _exe_from_appx():
    """WindowsApps 文件夹列不出来（拒绝访问）时：问系统 Codex 装在哪，再拼出 codex.exe 的路径。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | "
             "Select-Object -First 1).InstallLocation"],
            capture_output=True, timeout=20, creationflags=NO_WINDOW)
        location = out.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not location:
        return None
    exe = os.path.join(location, "app", "resources", "codex.exe")
    return exe if os.path.isfile(exe) else None


def find_exe():
    """找 codex.exe：Codex 桌面版里自带的（取最新版），或者另外装的命令行版。
    2026-11-22：WindowsApps 文件夹突然列不出来（拒绝访问），glob 变成空的，GPT 就接不上了；
    所以找不到时再问系统 Codex 装在哪（找到后记住，Codex 更新后旧路径没了会重新找）。"""
    if sys.platform == "darwin":
        return (shutil.which("codex") or shutil.which(os.path.expanduser("~/.local/bin/codex"))
                or shutil.which("/opt/homebrew/bin/codex")
                or shutil.which("/Applications/Codex.app/Contents/Resources/codex")
                or shutil.which(os.path.expanduser("~/Applications/Codex.app/Contents/Resources/codex")))
    cached = _exe_cache["path"]
    if cached and os.path.isfile(cached):
        return cached
    found = glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe")
    if found:
        exe = max(found, key=os.path.getmtime)
    else:
        exe = _exe_from_appx() or shutil.which("codex.exe") or shutil.which("codex")
    _exe_cache["path"] = exe
    return exe


def _check_login():
    exe = find_exe()
    if not exe:
        return LoginResult(False, False)
    try:
        out = subprocess.run([exe, "login", "status"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        lines = (out.stdout + b"\n" + out.stderr).decode("utf-8", "replace").splitlines()
        ok = out.returncode == 0 and any(line.strip().lower().startswith("logged in") for line in lines)
    except (OSError, subprocess.SubprocessError):
        ok = False
    return LoginResult(True, ok)


_login_cache = LoginCache("codex", _check_login, signed_out_ttl=30)


def cached_login():
    """返回上次结果，首次检查完成前返回 None。"""
    return _login_cache.get()


def logged_in(refresh=False):
    """发言或执行前复查旧的未登录状态。"""
    return _login_cache.get(wait=True, refresh=refresh, recheck_signed_out=True).logged_in


def _short(command):
    """Codex 报告的命令是 "powershell.exe" -Command '……' 或 "cmd.exe" /c "……"：只留里面真正的命令（一行）。"""
    return takeover.command_preview(_full(command), 70)


def run(prompt, mode="chat", model="", cwd=None, on_step=None, effort="", control=None):
    """
    让 GPT（Codex）做一件事。prompt 里写清楚身份、群聊记录和要求。
        mode    chat 只聊天 / look 看项目（只读） / do 动手（和你单独用 Codex 一样）
        effort  思考深度：low / medium / high / xhigh / max / ultra（空着用模型默认）
        on_step(一句话说明, detail=细节, kind=种类)：每做一步调用（群里显示过程，点开能看命令和输出）
    返回 {"reply": 最后的话, "steps": [...], "files": [改过的文件], "needs_confirm": 它最后在问你“确定吗”}
    """
    control = control or RunControl()
    control.check()
    if sys.platform == "darwin" and mode == "do":
        from computer import MAC_UNSUPPORTED
        return {"reply": MAC_UNSUPPORTED, "steps": [], "files": [], "needs_confirm": False, "error": True}
    exe = find_exe()
    if not exe:
        raise RuntimeError("电脑上没找到 Codex（装一下 Codex 桌面版就有）")
    if not logged_in():
        raise RuntimeError(LOGIN_HINT)
    cwd = cwd or takeover.WORK_DIR
    os.makedirs(cwd, exist_ok=True)
    sandbox = "danger-full-access" if mode == "do" else "read-only"  # 动手时和 Claude 一样能动整台电脑
    command = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", sandbox, "-C", cwd]
    for feature in ([] if mode == "do" else LOOK_DISABLED):
        command += ["--disable", feature]
    if model:
        command += ["-m", model]
    if effort:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    command.append("-")
    step = on_step or (lambda desc, detail="", kind="": None)
    limit = TIMEOUT if mode == "do" else LOOK_TIMEOUT
    steps, texts, errors, files = [], [], [], []
    thought = []  # 它边做边说的话：等下一步出来再显示（最后一句是回答，不当成过程）
    pending = {}

    def flush_thought():
        if thought:
            text = thought.pop()
            steps.append(text)
            step(text[:80], text, "think")

    def report_unfinished():
        for item in pending.values():
            if item.get("type") == "command_execution":
                desc = f"运行命令：{_short(item.get('command'))}"
                detail = f"$ {_full(item.get('command'))}"
            else:
                desc, detail = _other_step(item)
            step(f"{desc}（结果未返回）", detail, "interrupted")

    failed = False
    try:
        with (tempfile.TemporaryFile() as stderr,
              control.process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                              cwd=cwd, creationflags=NO_WINDOW, timeout=limit) as proc):
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            for raw in proc.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                item = event.get("item") or {}
                kind = item.get("type")
                event_type = event.get("type")
                if event_type == "item.started" and kind in ("command_execution", "mcp_tool_call", "collab_tool_call"):
                    pending[item.get("id")] = item
                elif event_type == "item.completed":
                    pending.pop(item.get("id"), None)
                    flush_thought()
                    if kind == "command_execution":
                        command_text = _full(item.get("command"))
                        cmd = _short(item.get("command"))
                        code = item.get("exit_code")
                        output = str(item.get("aggregated_output") or "").strip()
                        desc = f"运行命令：{cmd}" + ("" if code in (0, None, "0") else f"（出错，退出码 {code}）")
                        if code in (0, None, "0") and item.get("status") == "failed":
                            desc += "（命令报错）"
                        elif code is None:
                            desc += "（未返回退出码）"
                        steps.append(desc)
                        step(desc, takeover.command_detail(command_text, output), "cmd")
                    elif kind == "file_change":
                        changes = item.get("changes") or []
                        names = {"add": "新建", "update": "修改", "delete": "删除"}
                        lines = [f"{names.get(c.get('kind'), c.get('kind'))} {c.get('path')}" for c in changes]
                        files += [c.get("path") for c in changes if c.get("path")]
                        desc = "改文件：" + "、".join(os.path.basename(str(c.get("path"))) for c in changes)
                        if item.get("status") == "failed":
                            desc += "（没改成）"
                        steps.append(desc)
                        step(desc, "\n".join(lines), "file")
                    elif kind == "agent_message" and item.get("text"):
                        texts.append(item["text"].strip())
                        thought.append(item["text"].strip())
                    elif kind in ("mcp_tool_call", "web_search", "todo_list", "collab_tool_call", "image_generation"):
                        desc, detail = _other_step(item)
                        steps.append(desc)
                        step(desc, detail, "cmd")
                elif event_type in ("turn.failed", "error"):
                    failed = failed or event_type == "turn.failed"
                    errors.append(str(event.get("error") or event.get("message") or event)[:200])
            returncode = proc.wait(timeout=30)
            stderr.seek(0)
            error = stderr.read(4096).decode("utf-8", "replace").strip()
    except (RunStopped, OSError, subprocess.TimeoutExpired):
        report_unfinished()
        raise
    if failed or returncode or not texts:
        report_unfinished()
        if any(_LOGIN_ERROR.search(message) for message in (*errors, error)):
            _login_cache.mark_signed_out()
            raise RuntimeError(LOGIN_HINT)
        raise RuntimeError((errors[-1] if errors else error.splitlines()[-1] if error else "Codex 没有返回最终结果")[:200])
    return {"reply": texts[-1], "steps": steps, "files": files, "needs_confirm": takeover.asks_confirm(texts[-1])}


def _other_step(item):
    """命令、改文件以外的一步（操控屏幕、浏览器、插件、上网搜……）：一句话说明 + 点开能看的细节。"""
    kind = item.get("type")
    if kind == "web_search":
        return f"上网搜：{str(item.get('query') or '')[:40]}", json.dumps(item, ensure_ascii=False)[:2000]
    if kind == "mcp_tool_call":
        server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
        args = item.get("arguments")
        text = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
        result = item.get("result") or item.get("error") or ""
        what = "操控屏幕" if "cua" in server or "computer" in server else "用插件/连接器"
        failed = "（出错了）" if item.get("status") == "failed" or item.get("error") else ""
        return (f"{what}：{server} {tool}{failed}",
                f"{server}.{tool}\n\n{text[:2500]}\n\n结果：{json.dumps(result, ensure_ascii=False)[:1500]}")
    names = {"todo_list": "列待办清单", "collab_tool_call": "派小助手 / 和小助手沟通", "image_generation": "画图"}
    return names.get(kind, kind), json.dumps(item, ensure_ascii=False)[:2000]


def _full(command):
    """完整的命令（去掉外面那层 powershell.exe / cmd.exe 的包装），给“点开看细节”用。"""
    text = (command or "").strip()
    shell = re.match(r"""(?:["'][^"']*[\\/]|[^\s"']*[\\/]|["'])?(powershell|pwsh|cmd)(?:\.exe)?["']?\s+""",
                     text, re.IGNORECASE)
    if not shell:
        return text
    marker = "/c" if shell[1].lower() == "cmd" else "-command"
    args = text[shell.end():]
    wrapped = re.match(rf"(?:-executionpolicy\s+\S+\s+|-[\w-]+\s+)*{marker}\s+", args, re.IGNORECASE)
    if not wrapped:
        return text
    script = args[wrapped.end():]
    if len(script) > 1 and script[0] in "'\"" and script[0] == script[-1]:
        script = script[1:-1]
    return script.strip()
