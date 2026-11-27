"""
test_group_exec.py —— 测试“群聊里的 AI 能看、能讨论、互相接话，有执行权的能真的动手”

    1. 假 AI（不花钱）：执行权（给 / 收回 / 同一时间只有一个）/ 轮流回 / 叫名字 / AI 之间 @ 接话 /
       有执行权的直接动手、其他只看 / 讨论 / 讨论完让 Claude 执行 / 它问“确定吗”你回答 / 检查一遍 /
       长期记忆、职责 / 功能全开、没有门卫
    2. 给 AI 看的项目副本
    3. 真的用本地 Gemma（免费）：看项目副本、查 D 盘、删文件、“兼容 OpenAI 接口”那条路、整理长期记忆
    加 --codex：真的让 GPT（你的 ChatGPT 账号）看项目、没执行权时不动手、拿到执行权后动手、截屏（用一点会员额度）
    加 --claude：真的让 Claude（你的账号，Haiku）看项目、接管查 D 盘、截屏（用一点 Pro 额度）
    （2026-11-21 删掉了“门卫”：没有程序拦，只靠说明让 AI 在删除这类改不回来的事之前先问一句）

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_group_exec.py [--codex] [--claude]
"""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

import brains
import groupchat
import memory
import settings
import takeover

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_group_exec")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
settings.PATH = os.path.join(TMP, "settings.json")
brains.KEYS_FILE = os.path.join(TMP, "api_keys.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def use_dir(name):
    groupchat.DATA_DIR = os.path.join(TMP, name)
    groupchat.MEMBERS_FILE = os.path.join(groupchat.DATA_DIR, "members.json")
    groupchat.HISTORY_FILE = os.path.join(groupchat.DATA_DIR, "history.jsonl")


def by_name(name):
    return next(m for m in groupchat.members() if m["name"] == name)


def notes():
    return [m["text"] for m in groupchat.history() if m["role"] == "system"]


print("===== 1. 假 AI：执行权、轮流回、互相接话、动手、讨论、确认 =====")
use_dir("fake")
import agent_hands  # noqa: E402
agent_hands.WORK_DIR = os.path.join(TMP, "work")      # 假装的工作文件夹
agent_hands.SNAPSHOT_DIR = os.path.join(TMP, "snapshot")
os.makedirs(agent_hands.WORK_DIR)
real_ask, real_can = groupchat.ask, groupchat.can_execute
chats, runs, order = [], [], []
scripted = {}   # 某个 AI 下一句要说什么（测“AI 之间 @ 接话”用）


def fake_ask(member, recent, hint=None, **kw):
    chats.append((member["name"], hint, kw.get("look")))
    order.append(member["name"])
    return scripted.get(member["name"]) or f"我是{member['name']}"


groupchat.ask = fake_ask
groupchat.can_execute = lambda m: (True, "") if m["kind"] != "openai" else real_can(m)
groupchat.readiness = lambda m: (True, "") if m["kind"] != "openai" else (False, "还没填密钥")


def fake_takeover_run(task, **kw):
    runs.append(("Claude", task))
    order.append("Claude动手")
    kw["on_step"]("运行命令：Get-PSDrive D")
    return {"reply": "D 盘还剩 120 GB。", "steps": ["运行命令：Get-PSDrive D"], "needs_confirm": False}


def reset():
    chats.clear(); runs.clear(); order.clear(); scripted.clear()


takeover.run = fake_takeover_run
takeover.available = lambda: True
settings.set("group_review", False)  # 先不检查（后面专门测“检查一遍、有问题再改一次”）
group = [m["name"] for m in groupchat.members() if m["enabled"]]
check("默认成员：Claude、GPT（都用你的账号）、本地 Gemma 在群里", group == ["Claude", "GPT", "本地 Gemma"]
      and by_name("GPT")["kind"] == "codex")

for text, expect in [("@Claude 来全权接管电脑", "Claude"), ("让 Claude 去执行", "Claude"), ("交给GPT做吧", "GPT"),
                     ("把执行权交给 GPT", "GPT"), ("你们讨论完让本地 Gemma 动手", "本地 Gemma"),
                     ("Claude 和 GPT 你们怎么看", None), ("@Claude @GPT 你们都来接管", None)]:
    got = groupchat.exec_target(text, groupchat.members())
    check(f"“{text}” → 点名动手的是 {expect or '没有'}", (got or {}).get("name") == expect, f"→ {(got or {}).get('name')}")
everyone = [m for m in groupchat.members() if m["enabled"]]
for text, expect in [("问你呢gpt", ["GPT"]), ("Claude，你看看这个", ["Claude"]), ("@GPT 你说呢", ["GPT"]),
                     ("你觉得GPT说得对吗", []), ("大家好", []), ("gemma 你在吗", ["本地 Gemma"]),
                     ("@所有人 早", [])]:
    got = [m["name"] for m in groupchat.addressed(text, everyone)]
    check(f"“{text}” 是对 {expect or '所有人'} 说的", got == expect, f"→ {got}")

# 没人有执行权：大家轮流回，都只看不改
reset()
groupchat.send("大家好", wait=True)
check("没人有执行权：三个轮流回（后说的能看到先说的），没人动手", order == group and not runs, f"→ {order}")
check("Claude 的说明：只能看、不能改，执行权在“没有人”",
      "只能看、不能改" in groupchat._agent_prompt(by_name("Claude"), "look")
      and "没有人" in groupchat._agent_prompt(by_name("Claude"), "look"))
reset()
groupchat.send("问你呢gpt", wait=True)
check("“问你呢gpt”（没 @，结尾叫了名字）：只有 GPT 回", order == ["GPT"], f"→ {order}")

# AI 之间 @ 接话：Claude 在话里 @GPT，GPT 接着回它
reset()
scripted["Claude"] = "@GPT 你能看到项目文件吗？"
groupchat.send("@Claude 你问问 GPT 能不能看文件", wait=True)
check("Claude 在话里 @GPT：GPT 接着回 Claude（不用你再催）",
      [(n, h) for n, h, _ in chats] == [("Claude", None), ("GPT", groupchat.AT_HINT.format(name="Claude"))],
      f"→ {[(n, h) for n, h, _ in chats]}")
reset()
scripted.update({"Claude": "@GPT 你说呢", "GPT": "@Claude 你说呢"})
groupchat.send("@Claude 开始", wait=True)
check(f"两个一直互相 @：最多接 {groupchat.AI_CHAIN_LIMIT} 次就停（免得没完没了）",
      len(chats) == 1 + groupchat.AI_CHAIN_LIMIT, f"→ 说了 {len(chats)} 句")

# 执行权交给 Claude
reset()
groupchat.send("@Claude 来全权接管电脑", wait=True)
check("“@Claude 来全权接管电脑”：执行权交给 Claude，还没交代事情就先不动手",
      groupchat.hands_member()["name"] == "Claude" and not runs and any("执行权交给了 Claude" in n for n in notes()))
groupchat.send("@DeepSeek 全员接管电脑", wait=True)
check("“@DeepSeek 接管电脑”但没填密钥：说明原因，执行权还在 Claude",
      groupchat.hands_member()["name"] == "Claude" and "DeepSeek 现在不能动手：还没填密钥" in notes()[-1])
groupchat.send("@Claude 帮我看看D盘还剩多少空间", wait=True)
hist = groupchat.history()
check("@有执行权的交代事情：Claude 真的去动手", len(runs) == 1 and "D盘还剩多少空间" in runs[0][1])
check("群里显示它做的每一步，最后发“动手结果”",
      [m["role"] for m in hist[-2:]] == ["step", "ai"] and hist[-1].get("exec") and "120 GB" in hist[-1]["text"])
check("Claude 的说明：执行权在你手里、能动整台电脑；GPT 的说明：执行权在 Claude",
      "执行权在你手里" in groupchat._agent_prompt(by_name("Claude"), "do")
      and "现在执行权在：Claude" in groupchat._agent_prompt(by_name("GPT"), "look"))

reset()
groupchat.send("大家好", wait=True)
check("有执行权时说“大家好”：GPT、Gemma 先只看不改地回，Claude 最后说、而且能直接动手",
      order == ["GPT", "本地 Gemma", "Claude动手"], f"→ {order}")
reset()
groupchat.send("你们先去看看星期八的项目代码，说说你们的理解", wait=True)
check("“先去看看项目代码”：GPT、Gemma 去看（只读），Claude 用自己的权限看",
      [(n, look) for n, _, look in chats] == [("GPT", True), ("本地 Gemma", True)]
      and len(runs) == 1 and groupchat.LOOK_HINT in runs[0][1], f"→ {chats}")
reset()
scripted["GPT"] = "@Claude 你那边能看到 listen.py 吗？"
groupchat.send("@GPT 你问问 Claude", wait=True)
check("AI 之间 @：Claude 虽然有执行权，回 GPT 时也只看不改（只有你的话能让它动手）",
      [n for n, _, _ in chats] == ["GPT", "Claude"] and not runs, f"→ {order}")
check("Claude 回 AI 时的说明：执行权在你手里，但这次只看不改",
      "这次只看不改" in groupchat._agent_prompt(by_name("Claude"), "look"))

reset()
groupchat.send("@Claude @GPT 先看看项目代码，然后讨论一下怎么改进打断功能", wait=True)
check("“@Claude @GPT 先看代码再讨论”：只有这两个，轮流说；第一轮看代码、第二轮讨论；讨论时谁都不动手",
      [(n, look) for n, _, look in chats] == [("Claude", True), ("GPT", True), ("Claude", False), ("GPT", False)]
      and not runs and "讨论完了" in notes()[-1], f"→ {[(n, look) for n, _, look in chats]}")
reset()
groupchat.send("你们两个现在去讨论一下这个东西该怎么做，然后讨论完之后让 Claude 去执行", wait=True)
n = len(group)
check("先轮流讨论两轮（群里的都参加），再让 Claude 按方案动手",
      [h for _, h, _ in chats] == [groupchat.DISCUSS_HINTS[0]] * n + [groupchat.DISCUSS_HINTS[1]] * n
      and len(runs) == 1 and groupchat.EXEC_AFTER_DISCUSS in runs[0][1])
check("交给 Claude 的任务里带着大家讨论的内容", "我是本地 Gemma" in runs[0][1] and "我是GPT" in runs[0][1])

# 它自己在群里问“确定吗”（说明里要求改不回来的事先问一句），你回“确定”它就接着去做
reset()
victim = os.path.join(agent_hands.WORK_DIR, "a.txt")
open(victim, "w").close()


def asking_run(task, **kw):
    runs.append(("Claude", task))
    if "用户说“确定”" in task:
        os.remove(victim)
        return {"reply": "已经删掉了。", "steps": ["运行命令：Remove-Item a.txt"], "needs_confirm": False}
    return {"reply": "我要删掉 a.txt，确定吗？", "steps": [], "needs_confirm": True}


takeover.run = asking_run
groupchat.send("@Claude 把 a.txt 删掉", wait=True)
check("改不回来的事：Claude 在群里问“确定吗”，文件还在", "确定吗" in groupchat.history()[-1]["text"]
      and groupchat._awaiting["member"] and os.path.exists(victim))
groupchat.send("确定", wait=True)
check("你回“确定”：接着交给 Claude 去做，文件删掉了", not os.path.exists(victim) and len(runs) == 2
      and groupchat.history()[-1]["text"] == "已经删掉了。")
open(victim, "w").close()
groupchat.send("@Claude 把 a.txt 删掉", wait=True)
groupchat.send("算了", wait=True)
check("你回“算了”：不做，文件还在", notes()[-1] == "好的，不做了。" and len(runs) == 3 and os.path.exists(victim))
groupchat.send("取消接管", wait=True)
check("“取消接管”：收回执行权，大家都只能看", groupchat.hands_member() is None and "已收回 Claude 的执行权" in notes()[-1])

# 协作：Claude 做完，GPT 当检查员；GPT 说“⚠️ 有问题”，Claude 按意见再改一次（只改一次）
settings.set("group_review", True)
reset()
reviews = iter(["⚠️ 有问题：文件名写错了，应该叫 b.txt", "✅ 没问题"])
groupchat.ask = lambda member, recent, hint=None, **kw: chats.append((member["name"], hint, kw.get("look"))) \
    or (next(reviews) if kw.get("look") == "review" else f"我是{member['name']}")
takeover.run = fake_takeover_run
groupchat.send("@Claude 接管电脑，帮我建一个 b.txt", wait=True)
hist = groupchat.history()
check("做完后 GPT 自动检查（只看不改）", any(n == "GPT" and look == "review" for n, _, look in chats)
      and any(m.get("review") for m in hist))
check("GPT 说有问题：Claude 按意见再改一次，改完不再来回检查",
      len(runs) == 2 and groupchat.FIX_HINT.format(reviewer="GPT") in runs[1][1]
      and sum(1 for _, _, look in chats if look == "review") == 1)
reset()
groupchat.send("大家好", wait=True)
check("只是聊天（没改文件、也没让它动手）：不去检查，省额度",
      not any(look == "review" for _, _, look in chats) and len(runs) == 1)
groupchat.give_hands(by_name("GPT")["id"])   # 界面上点“交给 GPT”
check("点“交给 GPT”：执行权换成 GPT（同一时间只有一个），群里说一声",
      [m["name"] for m in groupchat.members() if m.get("hands")] == ["GPT"] and "执行权交给了 GPT" in notes()[-1])
groupchat.give_hands(None)
check("点“收回”：没人有执行权", groupchat.hands_member() is None and "已收回 GPT 的执行权" in notes()[-1])

print("\n===== 1.5 长期记忆、职责 =====")
import group_memory  # noqa: E402
reset()
r = groupchat.send("记住：改星期八的代码之前，先在群里说清楚要改哪几个文件", wait=True)
check("“记住：……”：原样记进长期记忆，AI 不用回", r.get("remembered") and not chats
      and "先在群里说清楚要改哪几个文件" in group_memory.read() and "## 用户交代要记住的" in group_memory.read())
groupchat.send("你们也记一下：每天晚上十点提醒我休息", wait=True)
check("再记一条：两条都在同一节里", group_memory.read().count("## 用户交代要记住的") == 1
      and "十点提醒我休息" in group_memory.read())
groupchat.update_member(by_name("GPT")["id"], {"role": "写代码"})
groupchat.update_member(by_name("Claude")["id"], {"role": "定调和把关"})
p = groupchat._agent_prompt(by_name("GPT"), "look")
check("每个 AI 的说明里：带着长期记忆、自己的职责、别人的职责、以前聊天记录在哪",
      "十点提醒我休息" in p and "你的职责：写代码" in p and "Claude（职责：定调和把关）" in p
      and groupchat.CLAUDE_RECORDS in p and "D--voice-assistant" in groupchat.CLAUDE_RECORDS)
check("本地 AI（只聊天的）也带着长期记忆", "十点提醒我休息" in groupchat._system_for(by_name("本地 Gemma")))
summarized = []


def fake_summarize(old, batch):
    summarized.append(len(batch))
    return (old.split("## 用户交代要记住的")[0] if old else "") + f"## 项目进度\n- 整理了 {sum(summarized)} 条\n\n" \
        + ("## 用户交代要记住的" + old.split("## 用户交代要记住的")[1] if "## 用户交代要记住的" in old else "")


real_summarize = group_memory._summarize
group_memory._summarize = fake_summarize
group_memory.maybe_update = lambda **kw: False   # 先不让它在后台自己整理，下面手动整理
for i in range(group_memory.EVERY):
    groupchat.send(f"@GPT 第 {i} 句", wait=True)
before = len(group_memory.pending())
n = group_memory.update()
check(f"攒够 {group_memory.EVERY} 条就整理：新消息整理进笔记（剩下不到 {group_memory.EVERY} 条下次再说），记住的那一节还在",
      n >= group_memory.EVERY and n + len(group_memory.pending()) == before
      and len(group_memory.pending()) < group_memory.EVERY and "## 项目进度" in group_memory.read()
      and "十点提醒我休息" in group_memory.read(), f"→ 整理了 {n} 条，共 {before} 条 {group_memory._running['error']}")
check("没攒够不整理（省事）", group_memory.update() == 0)
importlib.reload(group_memory)
group_memory._summarize = fake_summarize
groupchat.send("@GPT 清空前最后一句", wait=True)
summarized.clear()
groupchat.clear()
for _ in range(100):
    if not group_memory._running["on"]:
        break
    time.sleep(0.05)
check("清空聊天：没整理的消息先整理进记忆（清空了也记得）", summarized and not groupchat.history()
      and "整理了" in group_memory.read())
groupchat.update_member(by_name("GPT")["id"], {"role": ""})
groupchat.update_member(by_name("Claude")["id"], {"role": ""})
group_memory._summarize = real_summarize
groupchat.ask, groupchat.can_execute = real_ask, real_can

print("\n===== 1.6 有执行权时功能全开，只看时关掉动鼠标的 =====")


class FakeProc:
    """假装是 codex / claude 程序：记下命令，吐出几行事件。"""
    last = None

    def __init__(self, command, **kw):
        FakeProc.last = command
        self.stdin = open(os.devnull, "wb")
        lines = FakeProc.events
        self.stdout = [json.dumps(e).encode("utf-8") + b"\n" for e in lines]
        self.stderr = type("E", (), {"read": lambda s: b""})()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@contextmanager
def fake_process(self, command, **kw):
    proc = FakeProc(command, **kw)
    try:
        yield proc
    finally:
        proc.stdin.close()


import agent_codex  # noqa: E402
import brain_claude_code  # noqa: E402
from run_control import RunControl
real_process = RunControl.process
saved = (agent_codex.logged_in, agent_codex.find_exe, brain_claude_code.find_exe, brain_claude_code.logged_in)
RunControl.process = fake_process
agent_codex.logged_in = lambda refresh=False: True
agent_codex.find_exe = lambda: "codex.exe"
FakeProc.events = [
    {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "cua_repl", "tool": "js",
                                        "arguments": {"code": "await screenshot()"}, "status": "completed"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "截好了"}}]
got_steps = []
r = agent_codex.run("截个图", "do", cwd=TMP, on_step=lambda d, detail="", kind="": got_steps.append(d))
cmd = FakeProc.last
check("GPT 有执行权：功能一个不关（操控屏幕、插件、浏览器、一直开着的命令窗口），也没有门卫钩子",
      "--disable" not in cmd and not any("hooks" in c for c in cmd), f"→ {cmd[cmd.index('-s'):][:12]}")
check("GPT 操控屏幕的每一步也显示在群里", any("操控屏幕" in s for s in got_steps) and r["reply"] == "截好了",
      f"→ {got_steps}")
agent_codex.run("看看", "look", cwd=TMP)
check("GPT 只看时：操控屏幕、插件、浏览器都关着", all(f in FakeProc.last for f in ("computer_use", "plugins", "browser_use")))
importlib.reload(takeover)
takeover.brain_claude_code.find_exe = lambda: "claude.exe"
takeover.brain_claude_code.logged_in = lambda: True
FakeProc.events = [{"type": "result", "result": "好了", "is_error": False}]
takeover.run("你好", session_file=None, full=True, cwd=TMP)
cmd = FakeProc.last
check("Claude 有执行权：全部工具、你的 MCP 连接器、技能、Chrome 浏览器都开着，也没有门卫钩子",
      cmd[cmd.index("--tools") + 1] == "default" and "--chrome" in cmd and "--strict-mcp-config" not in cmd
      and "--disable-slash-commands" not in cmd and "--settings" not in cmd)
takeover.run("你好", session_file=None, tools=groupchat.CLAUDE_LOOK_TOOLS, cwd=TMP)
cmd = FakeProc.last
check("Claude 只看时：只有看文件、搜索、上网查的工具，不连 Chrome", cmd[cmd.index("--tools") + 1] == groupchat.CLAUDE_LOOK_TOOLS
      and "--chrome" not in cmd and "--strict-mcp-config" in cmd)
p = groupchat._agent_prompt(by_name("GPT"), "do")
check("说明里写“权限和桌面版一样、直接做，只有改不回来的先问一句”，不再提门卫",
      groupchat.SAFETY in p and groupchat.PRIVACY in p and "门卫" not in p)
check("“Claude 接管”（语音）的说明也一样", "门卫" not in takeover.SYSTEM_PROMPT and takeover.SAFETY)
RunControl.process = real_process
agent_codex.logged_in, agent_codex.find_exe, brain_claude_code.find_exe, brain_claude_code.logged_in = saved
importlib.reload(takeover)

print("\n===== 2. 给 AI 看的项目副本 =====")
snap = agent_hands.project_snapshot()
copied = {os.path.relpath(os.path.join(r, f), snap).lower() for r, _, fs in os.walk(snap) for f in fs}
check("给 AI 看的项目副本：有代码，没有记忆、日志、密钥、设置",
      "listen.py" in copied and "ui\\app.js" in copied and not any(
          c.startswith(("memory\\", "logs\\", "groupchat\\", ".")) or c in ("api_keys.json", "settings.json")
          for c in copied),
      f"→ 复制了 {len(copied)} 个文件")
check("门卫已经删掉了：项目里没有 takeover_guard.py", not os.path.exists(os.path.join(memory.PROJECT_DIR,
                                                                               "takeover_guard.py")))

print("\n===== 3. 真的用本地 Gemma（免费）=====")
importlib.reload(takeover)
importlib.reload(agent_hands)
gemma = {"id": "g1", "name": "本地 Gemma", "kind": "ollama", "model": "gemma4:12b"}
steps = []
r = agent_hands.run(gemma, [{"role": "user", "content": "看看 wakeword.py，用两句话说说唤醒词是怎么确认的。"}],
                    on_step=lambda d, **k: steps.append(d), look=True)
check("Gemma 看项目副本：读了 wakeword.py，说出了自己的理解", any("wakeword.py" in s for s in steps) and len(r["reply"]) > 10,
      f"→ {steps[:2]}；{r['reply'][:60]}")
steps = []
r = agent_hands.run(gemma, [{"role": "user", "content": "用 PowerShell 查一下 D 盘还剩多少可用空间（GB），告诉我数字。"}],
                    on_step=lambda d, **k: steps.append(d))
check("Gemma 动手：运行了命令，说出 D 盘剩余空间", any("运行命令" in s for s in steps) and any(c.isdigit() for c in r["reply"]),
      f"→ {r['reply'][:50]}")
victim = os.path.join(agent_hands.WORK_DIR, "群聊测试_可以删.txt")
os.makedirs(agent_hands.WORK_DIR, exist_ok=True)
with open(victim, "w", encoding="utf-8") as f:
    f.write("测试用，可以删")
task = [{"role": "user", "content": f"把这个文件删掉：{victim}"}]
r = agent_hands.run(gemma, task, on_step=lambda d, **k: steps.append(d))
print(f"  Gemma 删文件前{'先问了一句' if r['needs_confirm'] else '没问，直接做了'}：{r['reply'][:60]}")
if r["needs_confirm"]:
    task += [{"role": "assistant", "content": r["reply"]}, {"role": "user", "content": "确定"}]
    r = agent_hands.run(gemma, task, on_step=lambda d, **k: steps.append(d))
check("Gemma 删文件（没有门卫拦；它问的话回“确定”）：文件删掉了", not os.path.exists(victim), f"→ {r['reply'][:50]}")
if os.path.exists(victim):
    os.remove(victim)
compat = {"id": "c1", "name": "兼容接口", "kind": "openai", "api": "chat", "base_url": "http://127.0.0.1:11434/v1",
          "model": "gemma4:12b"}
steps = []
r = agent_hands.run(compat, [{"role": "user", "content": "用 PowerShell 查一下现在电脑上有几个 explorer 进程，告诉我数字。"}],
                    api_key="ollama", on_step=lambda d, **k: steps.append(d))
check("“兼容 OpenAI 接口”这条路也能动手（DeepSeek、千问等用这条）", any("运行命令" in s for s in steps), f"→ {r['reply'][:50]}")
use_dir("memory_real")
t = time.time()
note = group_memory._summarize("## 用户交代要记住的\n- 11-21 改代码前先说清楚改哪几个文件", [
    {"role": "user", "time": "2026-11-21T14:00:00", "text": "以后 GPT 负责写代码，Claude 负责定调和把关"},
    {"role": "ai", "name": "GPT", "time": "2026-11-21T14:01:00", "text": "好的，我先给 listen.py 加打断日志"},
    {"role": "ai", "name": "GPT", "time": "2026-11-21T14:05:00", "text": "日志加好了，改了 listen.py 和 duplex.py"},
    {"role": "user", "time": "2026-11-21T14:06:00", "text": "下一步把唤醒词误触发的问题查一下"}])
print("  Gemma 整理的笔记：\n   ", note[:400].replace("\n", "\n    "))
check("本地 Gemma 整理长期记忆：有分工、有进度、有待办，记住的那条原样保留",
      "##" in note and "listen.py" in note and "唤醒" in note and "改代码前先说清楚改哪几个文件" in note,
      f"→ 用了 {time.time() - t:.0f} 秒")

if "--codex" in sys.argv:
    print("\n===== 4. 真的让 GPT（你的 ChatGPT 账号）看项目、没执行权不动手、拿到执行权后动手 =====")
    importlib.reload(groupchat)
    use_dir("codex")
    for m in groupchat.members():
        if m["name"] != "GPT":
            groupchat.update_member(m["id"], {"enabled": False})
    t = time.time()
    groupchat.send("问你呢gpt：你能看到星期八项目的代码吗？用三句话说说它是怎么听到唤醒词“星期八”的", wait=True)
    hist = groupchat.history()
    looked = [m["text"] for m in hist if m["role"] == "step"]
    reply = hist[-1]
    print(f"  看了：{looked[:4]}\n  GPT：{reply['text'][:150]}")
    check("GPT 平时说话就能看文件（不用特别说“去看看”）：真的读了代码并说了理解",
          looked and reply.get("name") == "GPT" and len(reply["text"]) > 20, f"→ 用了 {time.time() - t:.0f} 秒")
    target = os.path.join(takeover.WORK_DIR, "GPT群聊测试.txt")
    if os.path.exists(target):
        os.remove(target)
    groupchat.send("GPT 在工作文件夹里新建一个文件 GPT群聊测试.txt，内容写“你好，星期八”", wait=True)
    check("GPT 没有执行权：不动手（文件没建），说明执行权的事", not os.path.exists(target),
          f"→ {groupchat.history()[-1]['text'][:80]}")
    groupchat.send("@GPT 接管电脑", wait=True)
    groupchat.send("@GPT 在工作文件夹里新建一个文件 GPT群聊测试.txt，内容写“你好，星期八”", wait=True)
    check("GPT 拿到执行权后真的动手：建了文件", os.path.exists(target), f"→ {groupchat.history()[-1]['text'][:60]}")
    groupchat.send("@GPT 把工作文件夹里的 GPT群聊测试.txt 删掉", wait=True)
    asked = os.path.exists(target) and groupchat._awaiting["member"] is not None
    print(f"  GPT 删文件前{'先在群里问了“确定吗”' if asked else '没问'}：{groupchat.history()[-1]['text'][:60]}")
    if asked:
        groupchat.send("确定", wait=True)
    check("GPT 删文件（没有门卫拦；它问的话回“确定”）：文件删掉了", not os.path.exists(target),
          f"→ {groupchat.history()[-1]['text'][:60]}")
    if os.path.exists(target):
        os.remove(target)
    steps = [m for m in groupchat.history() if m["role"] == "step"]
    check("群里每一步都带着细节（完整命令和输出，点开能看）", steps and all(m.get("detail") for m in steps
                                                         if m.get("kind") in ("cmd", "file")),
          f"→ 例子：{(steps[0].get('detail') or '')[:80]!r}")
    # 没有门卫：GPT 能直接写进星期八的项目文件夹（不用再问）
    outside = os.path.join(TMP, "GPT放开权限测试.txt")
    groupchat.send(f"@GPT 新建文件 {outside}，内容写“放开权限测试”", wait=True)
    if not os.path.exists(outside) and groupchat._awaiting["member"]:
        groupchat.send("确定", wait=True)
    check("GPT 能直接写进星期八的项目文件夹（没有门卫拦）", os.path.exists(outside),
          f"→ {groupchat.history()[-1]['text'][:60]}")
    # 功能全开：GPT 能看屏幕（不点任何东西）
    mark = len(groupchat.history())
    groupchat.send("@GPT 截一张屏幕截图看一眼，用一句话告诉我屏幕上现在开着什么窗口。只看，不要点击、不要打字。", wait=True)
    new = groupchat.history()[mark:]
    shots = [m for m in new if m["role"] == "step" and re.search(r"操控屏幕|desktop_ctl|screenshot|view_image|截",
                                                                 m["text"] + (m.get("detail") or ""), re.I)]
    print("  它的步骤：", [m["text"][:50] for m in new if m["role"] == "step"][:6])
    check("GPT 有执行权：能截屏看屏幕（功能全开）", shots, f"→ {new[-1]['text'][:80]}")
    groupchat.send("取消接管", wait=True)

if "--claude" in sys.argv:
    print("\n===== 5. 真的让 Claude（Haiku）看项目、接管 =====")
    importlib.reload(groupchat)
    use_dir("claude")
    claude = by_name("Claude")
    groupchat.update_member(claude["id"], {"model": "haiku"})
    for m in groupchat.members():
        if m["name"] != "Claude":
            groupchat.update_member(m["id"], {"enabled": False})
    groupchat.send("Claude，你能看到项目代码吗？用两句话说说 duplex.py 是干什么的", wait=True)
    hist = groupchat.history()
    check("Claude 平时说话就能看文件：读了代码并说了理解", any(m["role"] == "step" for m in hist)
          and len(hist[-1]["text"]) > 10, f"→ {hist[-1]['text'][:80]}")
    groupchat.send("@Claude 接管电脑", wait=True)
    groupchat.send("@Claude 帮我看看D盘还剩多少可用空间", wait=True)
    last = groupchat.history()[-1]
    check("Claude 有执行权后真的动手查了，并在群里报告结果", last.get("exec"), f"→ {last['text'][:60]}")
    mark = len(groupchat.history())
    groupchat.send("@Claude 用 desktop_ctl 截一张屏幕截图看一眼，用一句话告诉我屏幕上现在开着什么窗口。只看，不要点击、不要打字。",
                   wait=True)
    new = groupchat.history()[mark:]
    steps = [m["text"] + (m.get("detail") or "") for m in new if m["role"] == "step"]
    print("  它的步骤：", [s[:50] for s in steps][:6])
    check("Claude 有执行权：用 desktop_ctl 截屏，再看图片", any("desktop_ctl" in s for s in steps)
          and any(".png" in s for s in steps), f"→ {new[-1]['text'][:80]}")
    groupchat.send("取消接管", wait=True)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{sum(results)}/{len(results)} 项通过")
