"""
agent_hands.py —— 给“用 API 密钥的 AI”装上手：GPT、DeepSeek、通义千问、Kimi、智谱、Gemini、本地 Gemma……

    Claude 有自己的“手”（Claude Code，用你登录的账号）。其他 AI 只有“脑子”（API 接口），
    这里给它们三样工具，AI 通过“工具调用”告诉我们要做什么，真正动手的是这里的代码：
        run_powershell  在你电脑上运行一条 PowerShell 命令（默认在 D:\\AI任务\\星期八接管 里）
        read_file       读一个文本文件
        write_file      写一个文本文件

    没有程序拦（2026-11-21 按你的要求删掉了“门卫”），只在说明里叮嘱它：
        删除你的文件、格式化、改注册表、关机这类改不回来的事，先在群里问你一句“确定吗？”

    支持三种接口：
        OpenAI Responses 接口（GPT 用）、OpenAI Chat Completions 接口（DeepSeek、豆包、千问等兼容的都用这个）、
        Ollama（本地 Gemma，免费）
"""

import json
import os
import shutil
import subprocess
import sys

import takeover
from run_control import RunControl

WORK_DIR = takeover.WORK_DIR
# 给群里的 AI “看项目”用的代码副本：只复制代码和说明文件，不含记忆、日志、密钥、聊天记录
SNAPSHOT_DIR = os.path.join(WORK_DIR, "星期八项目副本")
CODE_EXT = (".py", ".md", ".txt", ".html", ".css", ".js", ".svg", ".toml", ".yaml", ".yml", ".ps1")
SKIP_DIRS = {".venv", "models", "memory", "logs", "tests", "voices", "assets", "groupchat", "__pycache__"}
SKIP_FILES = {"api_keys.json", "settings.json"}
MAX_STEPS = 12          # 一件事最多来回几步
COMMAND_TIMEOUT = 120   # 一条命令最多跑多久（秒）
OUTPUT_LIMIT = 4000     # 命令输出最多交回去多少字（太长会占很多 token）
NO_WINDOW = 0x08000000

SYSTEM = """你是“{name}”，在一个 AI 群聊里被用户指派“接管电脑”：你可以用工具在用户的 Windows 11 电脑上动手（运行 PowerShell 命令、读写文件），把用户交代的事情做完。
- 先想清楚再动手；一步一步来，每步看结果再决定下一步。能用一条命令完成的就别分成很多步。
- 你自己生成的文件默认放在 {work_dir}。语音助手自己的程序文件夹 {project}：用户明确要求时才改，没要求就别动它。
{safety}
- 全部做完后，用简体中文简短地说做了什么、结果怎样（会发到群里，不用列“Sources/来源”）；没做成就照实说原因，不要假装做成了。
现在时间：{now}"""

TOOLS = [
    ("run_powershell", "在用户的 Windows 电脑上运行一条 PowerShell 命令，返回输出（太长会被截断）。",
     {"command": {"type": "string", "description": "要运行的 PowerShell 命令"}}, ["command"]),
    ("read_file", "读一个文本文件的内容。",
     {"path": {"type": "string", "description": "文件路径（相对路径从工作文件夹算起）"}}, ["path"]),
    ("write_file", "写一个文本文件（已有的会被覆盖）。",
     {"path": {"type": "string", "description": "文件路径（相对路径从工作文件夹算起）"},
      "content": {"type": "string", "description": "要写进去的完整内容"}}, ["path", "content"]),
]


# “看项目”时只给这两样（只能看代码副本，什么都改不了）
LOOK_TOOLS = [
    ("list_files", "列出项目副本里的文件（可以指定子文件夹）。",
     {"path": {"type": "string", "description": "子文件夹，不填就是整个项目"}}, []),
    ("read_file", "读项目副本里的一个文件。",
     {"path": {"type": "string", "description": "文件路径（相对项目副本）"}}, ["path"]),
]


def project_snapshot():
    """把星期八的代码复制一份给群里的 AI 看（只复制代码和说明；记忆、日志、密钥、聊天记录都不复制）。"""
    project = takeover.PROJECT_DIR
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.lower() in SKIP_FILES or not name.lower().endswith(CODE_EXT):
                continue
            src = os.path.join(root, name)
            dst = os.path.join(SNAPSHOT_DIR, os.path.relpath(src, project))
            try:
                if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
            except OSError:
                pass
    return SNAPSHOT_DIR


def _chat_tools(spec=None):
    return [{"type": "function", "function": {"name": n, "description": d,
                                              "parameters": {"type": "object", "properties": p, "required": r}}}
            for n, d, p, r in (spec or TOOLS)]


def _responses_tools(spec=None):
    return [{"type": "function", "name": n, "description": d,
             "parameters": {"type": "object", "properties": {k: {**v, "type": ["string", "null"]} if k not in r else v
                                                             for k, v in p.items()},
                            "required": list(p), "additionalProperties": False},
             "strict": True} for n, d, p, r in (spec or TOOLS)]


# ---------------------------------------------------------------------------
# 真正动手
# ---------------------------------------------------------------------------


def _full_path(path):
    path = os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"')))
    return path if os.path.isabs(path) else os.path.join(WORK_DIR, path)


def _look_tool(name, args):
    """“看项目”的两样工具：只在代码副本里看。"""
    rel = str(args.get("path") or "").strip().strip("/\\")
    full = os.path.normpath(os.path.join(SNAPSHOT_DIR, rel))
    if not (full == SNAPSHOT_DIR or full.startswith(SNAPSHOT_DIR + os.sep)):
        return "只能看项目副本里的文件。", f"想看副本以外的地方（没让看）：{rel[:40]}"
    if name == "list_files":
        found = []
        for root, _dirs, files in os.walk(full):
            for f in files:
                found.append(os.path.relpath(os.path.join(root, f), SNAPSHOT_DIR))
        found.sort()
        text = "\n".join(found[:300]) + (f"\n……一共 {len(found)} 个文件" if len(found) > 300 else "")
        return text or "（没有文件）", f"看了文件清单 {rel or '（整个项目）'}"
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read(OUTPUT_LIMIT * 3 + 1)
    except OSError as e:
        return f"打不开：{e}", f"看文件 {rel}（打不开）"
    if len(text) > OUTPUT_LIMIT * 3:
        text = text[:OUTPUT_LIMIT * 3] + "\n……（太长，后面省略了）"
    return text, f"看文件 {rel}"


def run_tool(name, args, look=False, *, control=None):
    """执行一个工具，返回交给 AI 看的文字，以及给人看的一句说明。look=True：只能看代码副本。"""
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return MAC_UNSUPPORTED, MAC_UNSUPPORTED
    control = control or RunControl()
    control.check()
    args = args if isinstance(args, dict) else {}
    if look:
        if name in ("list_files", "read_file"):
            return _look_tool(name, args)
        return "现在只是在看项目，不能动手。", f"想用 {name}（看项目时不让动手）"
    if name == "run_powershell":
        command = str(args.get("command") or "")
        one_line = takeover.command_preview(command, 60)
        os.makedirs(WORK_DIR, exist_ok=True)
        try:
            with control.process(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; " + command],
                cwd=WORK_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW) as proc:
                stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
            text = (stdout + stderr).decode("utf-8", "replace").strip()
            if proc.returncode:
                text = f"（退出码 {proc.returncode}）\n{text}"
        except subprocess.TimeoutExpired:
            text = f"命令超过 {COMMAND_TIMEOUT} 秒还没结束，已经停下。"
        text = takeover.command_output_excerpt(text, OUTPUT_LIMIT)
        return text or "（运行完了，没有输出）", f"运行命令：{one_line}"
    if name == "read_file":
        path = _full_path(args.get("path"))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(OUTPUT_LIMIT + 1)
        except OSError as e:
            return f"打不开这个文件：{e}", f"看文件 {os.path.basename(path)}（打不开）"
        if len(text) > OUTPUT_LIMIT:
            text = text[:OUTPUT_LIMIT] + "\n……（太长，后面省略了）"
        return text, f"看文件 {os.path.basename(path)}"
    if name == "write_file":
        path = _full_path(args.get("path"))
        try:
            os.makedirs(os.path.dirname(path) or WORK_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(args.get("content") or ""))
        except OSError as e:
            return f"写不进去：{e}", f"写文件 {os.path.basename(path)}（失败）"
        return f"已写入 {path}", f"写文件 {os.path.basename(path)}"
    return f"没有叫 {name} 的工具", f"用了不存在的工具 {name}"


# ---------------------------------------------------------------------------
# 动手循环：AI 说要用工具 → 我们执行 → 结果交回去 → 直到它给出最后的回答
# ---------------------------------------------------------------------------


LOOK_SYSTEM = """你是“{name}”，在一个 AI 群聊里。用户让大家先看看他的项目：语音助手“星期八”（Python 写的，跑在他的 Windows 电脑上）。
你可以用 list_files、read_file 看项目的只读副本（不含用户的记忆、日志、密钥）。按用户的要求挑相关的文件看，不用全看。
看完用简体中文在群里说说你的理解：要点、结构、你注意到的问题或改进点。简洁，一般不超过 10 句；可以引用文件名和函数名。不要假装看过没看的文件。"""


def run(member, messages, api_key="", on_step=None, look=False, *, control=None):
    """
    让一个“用 API 的 AI”动手完成任务。
        member    群成员（kind = openai / ollama；model、base_url、api）
        messages  [{"role": "user"/"assistant", "content": ...}]：群聊记录和任务（最后一条是 user）
        on_step   每做一步调用：on_step(给人看的一句说明)
    返回 {"reply": 最后的回答, "steps": [...], "needs_confirm": 它最后在问你“确定吗”}
    """
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return {"reply": MAC_UNSUPPORTED, "steps": [], "files": [], "needs_confirm": False, "error": True}
    from datetime import datetime
    control = control or RunControl()
    control.check()
    if look:
        project_snapshot()
        system = LOOK_SYSTEM.format(name=member["name"])
    else:
        system = SYSTEM.format(name=member["name"], work_dir=WORK_DIR, project=takeover.PROJECT_DIR,
                               safety=takeover.SAFETY, now=f"{datetime.now():%Y-%m-%d %H:%M}")
    spec = LOOK_TOOLS if look else TOOLS
    steps, files = [], []

    def step(desc, detail="", kind=""):
        """记一步；on_step(一句话说明, detail=细节, kind=种类) 让群里实时显示（点开能看命令和输出）。"""
        steps.append(desc)
        if desc.startswith("写文件") and not desc.endswith("（失败）"):
            files.append(detail.split("\n", 1)[0].replace("已写入 ", ""))
        if on_step:
            on_step(desc, detail=detail, kind=kind)

    messages = list(messages)
    kind = member.get("kind")
    if kind == "ollama":
        reply = _run_ollama(member, system, messages, step, spec, look, control)
    elif member.get("api") == "responses":
        reply = _run_responses(member, system, messages, api_key, step, spec, look, control)
    else:
        reply = _run_chat(member, system, messages, api_key, step, spec, look, control)
    if not reply:  # 照实说，不替它说“做完了”
        reply = f"（做了 {len(steps)} 步，但没有给出说明）" if steps else "（没有动手，也没有回答）"
    return {"reply": reply, "steps": steps, "files": files, "needs_confirm": takeover.asks_confirm(reply)}


def _report(step, name, args, result, desc):
    """把一步工具调用报告给群里：一句话说明 + 细节（命令 / 文件 + 结果）。"""
    if name == "run_powershell":
        detail = takeover.command_detail(args.get("command", ""), result)
        step(desc, detail, "cmd")
        return
    elif name == "write_file":
        detail, kind = f"{result}\n\n{str(args.get('content', ''))[:1500]}", "file"
    else:
        detail, kind = f"{name} {args.get('path', '')}\n\n{str(result)[:1500]}", "read"
    step(desc, detail[:3500], kind)



def _run_chat(member, system, messages, api_key, step, spec, look, control):
    """Chat Completions 接口（DeepSeek、豆包、千问、Kimi、智谱、Gemini…… 以及 Ollama 的兼容接口）。"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key or "none", base_url=member.get("base_url") or None, timeout=120.0, max_retries=1)
    history = [{"role": "system", "content": system}] + list(messages)
    for _ in range(MAX_STEPS):
        control.check()
        response = client.chat.completions.create(model=member["model"], messages=history, tools=_chat_tools(spec))
        control.check()
        msg = response.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            return (msg.content or "").strip()
        if (msg.content or "").strip():
            step(msg.content.strip()[:80], msg.content.strip(), "think")
        history.append({"role": "assistant", "content": msg.content or "",
                        "tool_calls": [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            result, desc = run_tool(c.function.name, args, look, control=control)
            _report(step, c.function.name, args, result, desc)
            history.append({"role": "tool", "tool_call_id": c.id, "content": result})
    return "步骤太多了，我先停下。"


def _run_responses(member, system, messages, api_key, step, spec, look, control):
    """OpenAI 的 Responses 接口（GPT 官方推荐）。"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=member.get("base_url") or None, timeout=120.0, max_retries=1)
    items = [{"role": m["role"], "content": m["content"]} for m in messages]
    for _ in range(MAX_STEPS):
        control.check()
        response = client.responses.create(model=member["model"], instructions=system, input=items,
                                           tools=_responses_tools(spec))
        control.check()
        calls = [o for o in response.output if getattr(o, "type", "") == "function_call"]
        if not calls:
            return (response.output_text or "").strip()
        for o in response.output:  # 把它这一轮的输出原样接回去（推理、工具调用都要带上）
            items.append(o.model_dump(exclude_none=True) if hasattr(o, "model_dump") else o)
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except ValueError:
                args = {}
            result, desc = run_tool(c.name, args, look, control=control)
            _report(step, c.name, args, result, desc)
            items.append({"type": "function_call_output", "call_id": c.call_id, "output": result})
    return "步骤太多了，我先停下。"


def _run_ollama(member, system, messages, step, spec, look, control):
    """本地 Ollama（免费、不联网）。"""
    import ollama

    import assistant
    history = [{"role": "system", "content": system}] + list(messages)
    for _ in range(MAX_STEPS):
        control.check()
        response = ollama.chat(model=member.get("model") or assistant.MODEL, messages=history, tools=_chat_tools(spec),
                               think=False, options={**assistant.OPTIONS, "temperature": 0.1},
                               keep_alive=assistant.KEEP_ALIVE)
        control.check()
        msg = response.message
        calls = msg.tool_calls or []
        if not calls:
            return (msg.content or "").strip()
        if (msg.content or "").strip():
            step(msg.content.strip()[:80], msg.content.strip(), "think")
        history.append({"role": "assistant", "content": msg.content or "",
                        "tool_calls": [{"function": {"name": c.function.name,
                                                     "arguments": dict(c.function.arguments or {})}} for c in calls]})
        for c in calls:
            args = dict(c.function.arguments or {})
            result, desc = run_tool(c.function.name, args, look, control=control)
            _report(step, c.function.name, args, result, desc)
            history.append({"role": "tool", "tool_name": c.function.name, "content": result})
    return "步骤太多了，我先停下。"
