"""
takeover.py —— “Claude 接管”：你说的事整个交给 Claude Code，让它用自己的工具动手

    你说“让 Claude 接管电脑”之后，星期八只负责听和说：
        你的话 → 星期八 → 在后台悄悄运行 Claude Code（claude.exe，不弹窗口，走你登录的 Pro 账号）
              → Claude 用它自己的工具做事：PowerShell 命令、读写文件、现写脚本、上网搜索
              → 做完把一两句话的结果交回来 → 星期八念给你听
    同一段对话里 Claude 记得前面说过什么（10 分钟内接着同一个“会话”）。

    权限和你单独用 Claude Code 一样，没有程序拦（2026-11-21 按你的要求删掉了“门卫”）。
    只在说明里叮嘱它：删除你的文件、格式化、改注册表、关机这类改不回来的事，先问你一句“确定吗？”。
    你说“确定”，这句话接着交给同一个会话，它就去做。

    额度：每说一句都会用你的 Pro 额度（和网页、桌面版 Claude 共用）。实测一句简单的事约等于 API 价格 0.1～0.2 美元。
    为了省，只给 Claude 留它真正用得上的几个工具（TOOLS）。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime

import brain_claude_code
import status
import workers
from run_control import RunControl, RunStopped

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(PROJECT_DIR, ".takeover")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")
WORK_DIR = os.path.join(workers.TASKS_DIR, "星期八接管")  # Claude 自己生成的文件默认放这里
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MODEL = "claude-sonnet-5-5"  # 没指定时用的模型；平时由 claude_models 按难度挑（简单 Haiku / 一般 Sonnet / 复杂 Opus）
EFFORT = "medium"
TOOLS = "PowerShell,Read,Write,Edit,Glob,Grep,WebSearch"  # 只留用得上的（工具越少，每句话占的额度越少）
SESSION_IDLE = 10 * 60    # 10 分钟内接着同一个会话（记得前面说过什么）
TIMEOUT = 15 * 60         # 一件事最多做 15 分钟
# 给所有能动手的 AI（Claude、GPT、其他 AI 的手）的同一句叮嘱：没有程序拦，只有改不回来的事先问一句
SAFETY = ("- 你的权限和用户单独用你的桌面版一样，直接做。只有不可挽回的操作（删除用户的文件、格式化、改注册表、关机重启、"
          "卸载软件）先用一句话问用户“确定吗？”，然后结束这一轮，等他说“确定”再做。")

SYSTEM_PROMPT = """你是“{name}”，用户的私人语音助手兼管家（像钢铁侠的贾维斯）。现在用户让你“接管电脑”：你在用户的 Windows 11 笔记本上，用你自己的工具（PowerShell 命令、读写文件、自己写脚本、上网搜索）帮用户把事情做完。
- 用户的话来自中文语音识别，可能有错别字或口语，按真实意图理解。
- 你在后台运行，用户看不到你的过程，只能听到你最后那段话。最后那段话会被念出来：用口语，一到三句，不用 Markdown、列表、代码、网址、表情；说清楚做了什么、结果怎样；没做成就照实说原因。
- 能用命令完成的就用命令。要操作软件界面（点按钮、输入）时，可以用 PowerShell 调用 Windows 的 UI Automation 写脚本；开始菜单里的软件可以用 Get-StartApps 查名字和 AppID，再用 explorer.exe "shell:AppsFolder\\<AppID>" 打开。
{safety}
- 不要改语音助手自己的程序文件夹 {project}，除非用户明确要求。
- 你自己生成的文件默认放在 {work_dir}。
- 被问到你是谁，就说你是{name}。
现在时间：{now}

【关于用户的长期记忆】
{profile}"""


class NotReady(Exception):
    """Claude Code 没装或者没登录。"""


def available():
    """现在能不能接管（装了 Claude Code 并且登录了）。"""
    if sys.platform == "darwin":
        return False
    return bool(brain_claude_code.find_exe()) and brain_claude_code.logged_in()


# ---------------------------------------------------------------------------
# 会话：同一段对话接着聊（Claude 记得前面的事）
# ---------------------------------------------------------------------------

def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def end_session():
    """结束当前会话（换回本地、或者超过 10 分钟没说话时，下次就重新开始）。"""
    _remove(SESSION_FILE)


# ---------------------------------------------------------------------------
# 运行 Claude Code
# ---------------------------------------------------------------------------

_ENCODING_PREFIX = re.compile(
    r"^(?:(?:\$OutputEncoding|\[Console\]::OutputEncoding)\s*=\s*)+"
    r"(?:\[(?:System\.)?Text\.Encoding\]::UTF8|\[(?:System\.)?Text\.UTF8Encoding\]::new\([ \t]*(?:\$(?:false|true))?[ \t]*\))"
    r"[ \t]*(?:;|\r?\n|$)\s*", re.IGNORECASE)


def command_preview(command, limit):
    """群聊标题跳过开头的 UTF-8 设置，详情仍显示完整命令。"""
    script = command.lstrip()
    while match := _ENCODING_PREFIX.match(script):
        script = script[match.end():]
    return " ".join((script or command).split())[:limit]


def _describe(block):
    """把 Claude 正在做的一步变成状态窗里的一句话。"""
    name, args = block.get("name", ""), block.get("input") or {}
    if "command" in args:
        return f"运行命令：{command_preview(args.get('description') or args['command'], 40)}"
    path = os.path.basename(str(args.get("file_path") or args.get("path") or ""))
    if name.startswith("mcp__claude-in-chrome__"):  # 操控 Chrome 浏览器
        what = args.get("action") or name.rsplit("__", 1)[-1]
        target = args.get("url") or args.get("text") or args.get("query") or ""
        return f"操控 Chrome：{what} {str(target)[:30]}".rstrip()
    if name.startswith("mcp__"):
        return f"用连接器 {name[5:].replace('__', ' 的 ')}"
    return {"Read": f"看文件 {path}", "Write": f"写文件 {path}", "Edit": f"改文件 {path}",
            "Glob": "找文件", "Grep": "在文件里搜", "WebSearch": f"上网搜：{args.get('query', '')[:30]}",
            "WebFetch": f"看网页 {str(args.get('url', ''))[:40]}",
            "Agent": f"派小助手去做：{str(args.get('description') or args.get('prompt', ''))[:30]}",
            "Skill": f"用技能 {args.get('skill', '')}", "TodoWrite": "列待办清单",
            }.get(name, f"用工具 {name}")


def _context_text(history):
    if not history:
        return ""
    lines = ["【刚才的对话（本地助手处理的）】"]
    for m in history[-8:]:
        lines.append(f"{'用户' if m['role'] == 'user' else '你'}：{m['content']}")
    return "\n".join(lines) + "\n\n【用户现在说】\n"


def run(text, history=None, name="星期八", profile="（暂无）", note="", model=MODEL, effort=EFFORT,
        system=None, session_file=SESSION_FILE, on_step=None, tools=TOOLS, cwd=WORK_DIR, full=False, control=None):
    """
    把一句话交给 Claude Code 去做。返回字典：
        reply          要念给你听的话
        steps          它做了哪几步（状态窗、记录里用）
        needs_confirm  Claude 最后在问你“确定吗”（等你回答）
        cost           按 API 价格折算的花费（美元；走 Pro 额度时不扣钱，只代表占了多少额度）
    AI 群聊里用时：system 换成群聊的说明；session_file=None 每次重新开始（群聊记录已经放在 text 里）；
    on_step(这一步的说明) 每做一步调用一次（群里实时显示它在干什么）；
    tools 只给哪些工具（群里没有执行权的只给看文件、搜索的工具）；cwd 在哪个文件夹里运行；
    full=True：功能全开，和你单独用 Claude Code 一样（群里有执行权的 Claude 用）：
        全部自带工具（开小助手、技能……）、你配的 MCP 连接器、Chrome 浏览器操控（Claude in Chrome）。
    没装、没登录时抛出 NotReady。
    """
    control = control or RunControl()
    control.check()
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return {"reply": MAC_UNSUPPORTED, "steps": [], "files": [], "needs_confirm": False,
                "cost": None, "error": True}
    exe = brain_claude_code.find_exe()
    if not exe:
        raise NotReady("电脑上没找到 Claude Code")
    if not brain_claude_code.logged_in():
        raise NotReady(brain_claude_code.LOGIN_HINT)
    os.makedirs(cwd, exist_ok=True)

    session = (_load(session_file) or {}) if session_file else {}
    resume = bool(session.get("id")) and time.time() - session.get("time", 0) < SESSION_IDLE
    sid = session["id"] if resume else str(uuid.uuid4())
    prompt = (session.get("note", "") + "\n" if resume else _context_text(history)) + (note + "\n" if note else "") + text

    system = system or SYSTEM_PROMPT.format(name=name, project=PROJECT_DIR, work_dir=WORK_DIR, profile=profile,
                                            safety=SAFETY, now=f"{datetime.now():%Y-%m-%d %H:%M}")
    command = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model, "--effort", effort,
               "--tools", "default" if full else tools, "--permission-mode", "bypassPermissions",
               "--system-prompt", system]
    command += ["--chrome"] if full else ["--strict-mcp-config", "--disable-slash-commands"]
    command += ["--resume", sid] if resume else ["--session-id", sid]

    if not on_step:
        status.set("思考中", "Claude 接管中：正在想")
    env = brain_claude_code._env()
    steps, final, texts, files = [], None, [], []
    pending = {}    # 正在做的工具：id → (说明, 细节)
    thought = []    # 它边做边说的话：等下一步出来再显示（最后一句是回答）

    def flush_thought():
        if thought and on_step:
            text = thought.pop()
            on_step(text[:80], detail=text, kind="think")
        thought.clear()

    def report_unfinished():
        if on_step:
            for desc, detail in pending.values():
                on_step(f"{desc}（结果未返回）", detail=detail, kind="interrupted")

    try:
        with tempfile.TemporaryFile() as stderr:
            with control.process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                 cwd=cwd, creationflags=NO_WINDOW, env=env, timeout=TIMEOUT) as proc:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
                for raw in proc.stdout:
                    try:
                        event = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if event.get("type") == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "tool_use":
                                flush_thought()
                                steps.append(_describe(block))
                                pending[block.get("id")] = (steps[-1], _detail(block))
                                args = block.get("input") or {}
                                if block.get("name") in ("Write", "Edit") and args.get("file_path"):
                                    files.append(args["file_path"])
                                if not on_step:
                                    status.set("执行", f"Claude：{steps[-1]}")
                            elif block.get("type") == "text" and block.get("text", "").strip():
                                texts.append(block["text"].strip())
                                thought.append(block["text"].strip())
                    elif event.get("type") == "user" and on_step:
                        for block in event.get("message", {}).get("content") or []:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                desc, detail = pending.pop(block.get("tool_use_id"), ("用了一个工具", ""))
                                out = block.get("content")
                                if isinstance(out, list):
                                    out = "\n".join(str(x.get("text", "")) for x in out if isinstance(x, dict))
                                out = str(out or "").strip()
                                if detail.startswith("$ "):
                                    if block.get("is_error"):
                                        desc += "（命令报错）"
                                    on_step(desc, detail=command_detail(detail[2:], out), kind="cmd")
                                else:
                                    on_step(desc, detail=f"{detail}\n\n{out[:3000] or '（没有输出）'}",
                                            kind="cmd" if desc.startswith(("操控", "派", "用"))
                                            else "file" if desc.startswith(("写", "改")) else "read")
                    elif event.get("type") == "result":
                        final = event
                proc.wait(timeout=30)
            stderr.seek(0)
            error = stderr.read(4096).decode("utf-8", "replace").strip()
    except (RunStopped, OSError, subprocess.TimeoutExpired):
        report_unfinished()
        raise

    if final is None:
        report_unfinished()
        if "login" in error.lower() or "auth" in error.lower():
            raise NotReady(brain_claude_code.LOGIN_HINT)
        raise RuntimeError(error[:200] or "Claude Code 退出时没有返回最终结果")
    reply = brain_claude_code.clean(str(final.get("result") or (texts[-1] if texts else "")))
    if session_file:
        _save(session_file, {"id": final.get("session_id") or sid, "time": time.time()})
    return {"reply": reply or "做完了。", "steps": steps, "needs_confirm": asks_confirm(reply), "model": model,
            "error": bool(final.get("is_error")), "cost": final.get("total_cost_usd"), "session": sid,
            "files": files}


ASKS = re.compile(r"确定吗|可以吗|要不要|是否继续|行吗")


def asks_confirm(reply):
    """AI 最后是不是在问你“确定吗”（等你回答）。"""
    return bool(ASKS.search(str(reply or "")[-40:]))


COMMAND_OUTPUT = "\n\n命令输出\n"


def command_output_excerpt(output, limit):
    """长输出保留开头和结尾，测试总数通常在结尾。"""
    if len(output) <= limit:
        return output
    separator = "\n……（中间输出已省略）\n"
    tail = limit // 2
    return output[:limit - tail - len(separator)] + separator + output[-tail:]


def command_detail(command, output):
    return f"$ {command}{COMMAND_OUTPUT}{command_output_excerpt(output or '（没有输出）', 3000)}"


def _detail(block):
    """一步操作的细节（群里点开能看）：完整命令，或者改了哪个文件、改成什么。"""
    args = block.get("input") or {}
    if "command" in args:
        return f"$ {args['command']}"
    path = args.get("file_path") or args.get("path") or ""
    if block.get("name") == "Write":
        return f"写文件 {path}\n\n{str(args.get('content', ''))[:1500]}"
    if block.get("name") == "Edit":
        return f"改文件 {path}\n\n- {str(args.get('old_string', ''))[:700]}\n+ {str(args.get('new_string', ''))[:700]}"
    return f"{block.get('name')} {json.dumps(args, ensure_ascii=False)[:500]}"


# 你在接管模式里说这些话时，交给本地处理（换大脑、退出接管），不发给 Claude
SWITCH_WORDS = re.compile(r"(换|切|改)回|回到本地|(退出|取消|结束|停止|关掉|不要|不用了?)\s*接管|脑子|大脑"
                          r"|换成\s*(GPT|本地|gemma|杰玛|Claude)", re.I)
