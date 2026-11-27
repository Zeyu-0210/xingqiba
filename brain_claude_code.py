"""
brain_claude_code.py —— 用“你自己的 Claude 账号”当大脑（通过电脑上的 Claude Code）

    不需要 API 密钥：借用已经装好的 Claude Code（claude.exe），用你登录的 Claude 账号（会员额度）来回答。
    做法：在后台运行一次 “claude -p”（只回答、不干活的模式），把问题交给它，拿回答案。
        --tools ""            不给它任何工具（不读文件、不运行命令），它只负责回答
        --system-prompt       换成我们自己的说明（它是“星期八”，回答要简短、适合念出来）
        --no-session-persistence  不在 Claude Code 里留下对话记录
    第一次用之前要登录一次（只要一次）：
        开始菜单打开“终端”，输入 claude 回车，按提示登录你的 Claude 账号（或者输入 /login）
"""

import json
import os
import re
import shutil
import subprocess
import sys

from login_status import LoginCache, LoginResult

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(PROJECT_DIR, ".claude_brain")  # 一个空文件夹：在这里运行，它就不会去读项目里的文件
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 120
LOGIN_HINT = "还没登录你的 Claude 账号"  # 每次用不了都会念，所以要短；怎么登录在换大脑时说一次

class NotReady(Exception):
    """Claude Code 没装或者没登录。"""


def find_exe():
    """找 claude.exe：先看官方安装位置，再看 PATH。"""
    if sys.platform == "darwin":
        return (shutil.which("claude") or shutil.which(os.path.expanduser("~/.local/bin/claude"))
                or shutil.which("/opt/homebrew/bin/claude"))
    default = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    if os.path.isfile(default):
        return default
    return shutil.which("claude")


def _env():
    """在 Claude Code 里面测试时，去掉“我在 Claude Code 里”的标记，免得它拒绝启动。"""
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    return env


def _check_login():
    exe = find_exe()
    if not exe:
        return LoginResult(False, False)
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, timeout=30,
                             creationflags=NO_WINDOW, env=_env())
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        return LoginResult(True, isinstance(data, dict) and bool(data.get("loggedIn")))
    except (OSError, subprocess.SubprocessError, ValueError):
        return LoginResult(True, False)


_login_cache = LoginCache("claude", _check_login, signed_out_ttl=30)


def cached_login():
    """返回上次结果，首次检查完成前返回 None。"""
    return _login_cache.get()


def logged_in(refresh=False):
    """发言或执行前复查旧的未登录状态。"""
    return _login_cache.get(wait=True, refresh=refresh, recheck_signed_out=True).logged_in


def _conversation(messages):
    """把对话整理成一段文字交给 Claude（它这次只回答最后一句）。"""
    *history, last = messages
    if not history:
        return last["content"]
    lines = ["【最近的对话】"]
    for m in history:
        who = "用户" if m["role"] == "user" else "你"
        lines.append(f"{who}：{m['content']}")
    lines += ["", "【用户现在说】", last["content"]]
    return "\n".join(lines)


def answer(system, messages, model="claude-sonnet-5-5", effort="low", timeout=TIMEOUT, info=None):
    """让 Claude（你的账号）回答，返回回答文字。没装、没登录时抛出 NotReady。
    info：传一个字典进来，会填上这次的花费 cost（按 API 价格折算的美元）。"""
    exe = find_exe()
    if not exe:
        raise NotReady("Mac 上没找到 Claude Code（claude）" if sys.platform == "darwin" else
                       "电脑上没找到 Claude Code（claude.exe）")
    if not logged_in():
        raise NotReady(LOGIN_HINT)
    os.makedirs(WORK_DIR, exist_ok=True)
    command = [exe, "-p", "--system-prompt", system, "--tools", "", "--model", model, "--effort", effort,
               "--no-session-persistence", "--output-format", "json", "--strict-mcp-config",
               "--disable-slash-commands"]
    result = subprocess.run(command, input=_conversation(messages).encode("utf-8"), capture_output=True,
                            timeout=timeout, cwd=WORK_DIR, creationflags=NO_WINDOW, env=_env())
    output = result.stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(output)
    except ValueError:
        data = {"result": output}  # 万一不是 JSON，就当是纯文字
    text = str(data.get("result") or "").strip()
    if info is not None:
        info["cost"] = data.get("total_cost_usd")
    if result.returncode != 0 or not text or data.get("is_error"):
        error = (result.stderr.decode("utf-8", "replace") or text).strip()
        if "login" in error.lower() or "auth" in error.lower():
            _login_cache.mark_signed_out()
            raise NotReady(LOGIN_HINT)
        raise RuntimeError(error[:200] or f"Claude Code 返回码 {result.returncode}")
    return clean(text)


def clean(text):
    """去掉回答开头偶尔多出来的标签（实测出现过“消息:我是星期八……”），不然念出来很怪。"""
    return re.sub(r"^\s*(消息|回答|答|回复|星期八|助手|你)\s*[:：]\s*", "", text).strip()
