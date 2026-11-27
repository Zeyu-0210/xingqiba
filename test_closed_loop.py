"""
test_closed_loop.py —— 测试整个闭环：说一句话 → 秘书（本地 gemma4）听懂 → 真的操作电脑 → 换大脑

    像语音一样，把一句句中文交给秘书（assistant.handle），再检查电脑上是不是真的发生了：
      1. 操作电脑：打开计算器、在里面算 12×3、关掉；用记事本在测试文件里写字并保存
      2. 危险操作：运行命令要先问“确定吗”，说“确定”才做；关机说“取消”就不做（关机用假的，绝不会真关）
      3. 换大脑：换成 Claude 后，操作类的话还是本地做（不花钱），聊天的话交给大脑（这里用假大脑，不花钱）
      4. Claude（你的账号）：没登录时会提示云端连不上，并退回本地回答；已登录时真问一句（会用一点点会员额度）
    测试期间请不要动鼠标键盘（大约 1 分钟）。测完恢复你原来选的大脑。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_closed_loop.py
"""

import json
import os
import shutil
import time

import assistant
import brain_claude_code
import brains
import computer
import friendly
import memory
import settings
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_loop")
import claude_models  # noqa: E402

claude_models.USAGE_FILE = os.path.join(TMP, "usage.json")  # 测试的用量不算进你真正的用量记录
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def say(text, mem, first=True):
    r = assistant.handle(text, mem, first=first)
    tools = [f"{a['tool']}({json.dumps(a['args'], ensure_ascii=False)})" for a in r["actions"]]
    print(f"  你：{text}\n    → 工具：{'、'.join(tools) or '（无）'}\n    → 回复：{r['reply']}（{r['seconds']:.1f} 秒）")
    return r


def calculator_shows(number):
    r = computer.read_screen("计算器")
    clean = r["message"].replace("\u202d", "").replace("\u202c", "")
    return f"显示为 {number}" in clean


def calculator_open():
    return any("计算器" in w["title"] for w in computer._windows())


def notepads():
    return {w["hwnd"] for w in computer._windows() if w["process"].lower() == "notepad.exe"}


def close_test_tab(hwnd, marks):
    """只关测试用的那一页：先切过去，确认标题是测试页才按 Ctrl+W；问要不要保存就选“不保存”。
    你自己的记事本标签页绝不碰。"""
    for _ in range(3):
        if not computer.user32.IsWindow(hwnd):
            return True
        if (computer._foreground() or {}).get("title") == "另存为":
            computer.press_keys("esc")
            time.sleep(0.5)
        if not any(m in computer._title(hwnd) for m in marks):
            return False
        computer._activate(hwnd)
        time.sleep(0.3)
        if not any(m in (computer._foreground() or {}).get("title", "") for m in marks):
            return False
        computer.press_keys("ctrl+w")
        time.sleep(1.0)
        if computer.user32.IsWindow(hwnd):
            elements, _ = computer._read(hwnd)
            if any(e["name"] == "不保存" for e in elements):
                computer.click("不保存")
                time.sleep(0.8)
    return not any(m in computer._title(hwnd) for m in marks) if computer.user32.IsWindow(hwnd) else True


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
saved_settings = settings.load()
real_ask, real_power = brains.ask, computer.EXECUTORS["power"]
computer.EXECUTORS["power"] = lambda action: {"ok": True, "message": f"（假的，没有真的执行）{action}"}
brains.set_brain("本地")
mem = memory.Memory(os.path.join(TMP, "memory"), resume=False)
assistant.warm_up()

try:
    print("===== 1. 说一句话，真的操作电脑 =====")
    r = say("帮我打开计算器", mem)
    time.sleep(1.0)
    check("“帮我打开计算器”", calculator_open() and any(a["tool"] == "open_app" for a in r["actions"]))

    computer.press_keys("esc")
    r = say("在计算器里算一下12乘以3", mem)
    time.sleep(0.5)
    check("“在计算器里算一下12乘以3”→ 屏幕显示 36", calculator_shows(36))

    r = say("把计算器关掉", mem)
    time.sleep(0.5)
    check("“把计算器关掉”", not calculator_open())

    r = say("打开计算器，然后算一下7乘以8", mem)
    time.sleep(0.5)
    check("多步：“打开计算器，然后算一下7乘以8”→ 显示 56", calculator_shows(56))
    computer.window("close", "计算器")

    # “这个记事本”：用假的工具只看模型想做什么（不真的打字，免得字打到别的窗口里）
    planned = []
    real_functions = dict(computer.FUNCTIONS)
    for tool in ("open_app", "window", "type_text", "press_keys", "click", "read_screen"):
        computer.FUNCTIONS[tool] = (lambda tool: lambda **args: planned.append((tool, args)) or
                                    {"ok": True, "message": f"（假的）{tool} 完成（窗口：无标题 - 记事本）"})(tool)
    try:
        say("在这个记事本里写一句：你好，然后保存", mem)
    finally:
        computer.FUNCTIONS.update(real_functions)
    names = [t for t, _ in planned]
    check("“这个记事本”：不再多开一个，直接打字并保存（不乱清空、不乱按回车）",
          "open_app" not in names and "type_text" in names
          and any(t == "press_keys" and "ctrl+s" in str(a).lower() for t, a in planned)
          and not any(a.get("clear") or a.get("enter") for t, a in planned if t == "type_text"), f"→ {planned}")

    planned.clear()
    for tool in ("open_app", "window", "type_text", "press_keys", "click", "read_screen"):
        computer.FUNCTIONS[tool] = (lambda tool: lambda **args: planned.append((tool, args)) or
                                    {"ok": True, "message": f"（假的）{tool} 完成（窗口：微信）"})(tool)
    try:
        say("在这个微信聊天窗口里发一句：晚上好", mem)
    finally:
        computer.FUNCTIONS.update(real_functions)
    check("“在这个微信聊天窗口里发一句”：打字并发送（按回车）",
          any(t == "type_text" and a.get("enter") for t, a in planned)
          or any(t == "press_keys" and "enter" in str(a).lower() for t, a in planned), f"→ {planned}")

    path = os.path.join(TMP, "闭环测试.txt")
    open(path, "w", encoding="utf-8").close()
    before = notepads()
    if before:
        print("[跳过] 你开着记事本：Windows 11 会把测试文件塞进你的记事本窗口，为了不碰你的标签页，真实的记事本测试先跳过")
        opened = False
    else:
        os.startfile(path)
        opened = computer._wait(lambda: "闭环测试" in ((computer._foreground() or {}).get("title", "")), 10)
        time.sleep(0.8)
    if before:
        pass
    elif opened:
        test_window = computer._foreground()["hwnd"]
        r = say("在这个记事本里写一句：星期八闭环测试成功，然后保存", mem)
        time.sleep(1.0)
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        check("“写一句……然后保存”（多步：打字 + 保存）", "星期八闭环测试成功" in content, f"→ 文件内容：{content!r}")
        check("没有多开记事本", notepads() <= before | {test_window}, f"→ 新开了 {len(notepads() - before - {test_window})} 个")
        for hwnd in notepads() - before - {test_window}:  # 模型多开的记事本：里面只有测试内容，不保存关掉
            close_test_tab(hwnd, ["星期八闭环测试", "无标题"])
        closed = close_test_tab(test_window, ["闭环测试.txt"])
        check("测试标签页关掉了（你自己的标签页不碰）", closed)
    else:
        check("用记事本打开测试文件", False)

    r = say("现在电脑还剩多少电", mem)
    check("“还剩多少电”→ 查电脑状态", any(a["tool"] == "system_info" for a in r["actions"]))

    print("\n===== 2. 危险操作：先问，说“确定”才做 =====")
    r = say("帮我运行一个命令：Write-Output 闭环测试", mem)
    check("运行命令：先问“确定吗”，还没执行", computer.pending() is not None and "确定" in r["reply"])
    r = say("确定", mem, first=False)
    check("说“确定”→ 执行了", "闭环测试" in r["reply"] and computer.pending() is None)

    r = say("帮我关机", mem)
    check("关机：先问", computer.pending() is not None and computer.pending()["tool"] == "power")
    r = say("算了，取消", mem, first=False)
    check("说“算了，取消”→ 不关机", computer.pending() is None and "取消" in r["reply"])

    r = say("帮我重启一下电脑", mem)
    pending_before = computer.pending() is not None
    r = say("现在音量是多少", mem, first=False)
    check("没回答“确定”而是说了别的 → 重启作废，正常回答音量",
          pending_before and computer.pending() is None and any(a["tool"] == "get_volume" for a in r["actions"]))

    print("\n===== 3. 换大脑：操作本地做，动脑交给大脑 =====")
    calls = []

    def fake_ask(system, messages, brain=None):
        calls.append(brain)
        return "（假Claude）这是大脑的回答。"

    brains.ask = fake_ask
    r = say("让 Claude 当我的脑子", mem)
    check("“让 Claude 当我的脑子”→ 换成 Claude", brains.current() == "Claude")
    r = say("打开计算器", mem)
    time.sleep(1.0)
    check("换了大脑后，“打开计算器”还是本地做（不找大脑）", calculator_open() and not calls and r["brain"] == "本地")
    computer.window("close", "计算器")
    r = say("给我讲个很短的笑话", mem)
    check("聊天交给 Claude", calls == ["Claude"] and "假Claude" in r["reply"], f"→ {r['reply']}")
    brains.ask = real_ask

    print("\n===== 4. Claude（你的账号，通过 Claude Code）=====")
    if brain_claude_code.logged_in(refresh=True):
        start = time.time()
        try:
            text = brains.ask("你叫星期八，是语音助手。回答要简短。", [{"role": "user", "content": "用一句话介绍你自己。"}], "Claude")
            check("用你的账号问 Claude 一句", bool(text), f"→ {text}（{time.time() - start:.1f} 秒）")
        except brains.BrainError as e:
            check("用你的账号问 Claude 一句", False, f"→ {e}")
    else:
        r = say("天空为什么是蓝色的", mem)
        check("没登录时：提示云端连不上，并退回本地回答",
              r["reply"].startswith(friendly.MESSAGES["offline"]) and r["reply"] != friendly.MESSAGES["offline"]
              and r["brain"] == "本地", f"→ {r['reply'][:80]}…")
    r = say("换回本地", mem)
    check("“换回本地”", brains.current() == "本地")

finally:
    brains.ask, computer.EXECUTORS["power"] = real_ask, real_power
    computer.cancel()
    if calculator_open():
        computer.window("close", "计算器")
    with open(settings.PATH, "w", encoding="utf-8") as f:
        json.dump(saved_settings, f, ensure_ascii=False, indent=2)
    if any("闭环测试" in w["title"] for w in computer._windows()):
        print("（测试文件还在记事本里开着，先不删测试文件夹，免得记事本弹“找不到路径”）")
    else:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n（已恢复：大脑 = {brains.current()}）")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
