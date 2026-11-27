"""
test_computer.py —— 测试“操作电脑”的工具（computer.py）

    会真的打开计算器和记事本操作一下，测完关掉：
      - 计算器：按名字点“一 加 二 等于”，读屏幕看是不是 3；再直接打字“7*6=”看是不是 42
      - 记事本：只打开 tests\\_tmp_computer 里的测试文件，打一句中文、Ctrl+S 保存、读文件核对，
        最后只关掉这个标签页（你记事本里原来的内容不会碰）
    危险操作：只测“先登记 → 你说确定才做 / 说取消就不做”；关机用假的代替，绝对不会真关机。
    删除测试会把一个测试用的小文件放进回收站。
    测试期间请不要动鼠标键盘（大约 30 秒）。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_computer.py
"""

import os
import shutil
import time

import computer

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "_tmp_computer")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def screen_text():
    r = computer.read_screen()
    return r["message"] if r["ok"] else ""


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("===== 1. 按名字找软件（不打开）=====")
for said, expect in [("记事本", "记事本"), ("计算器", "计算器"), ("画图软件", "画图"), ("文件管理器", "文件资源管理器"),
                     ("星期八", None), ("不存在的软件xyz", None)]:
    found = computer.find_app(said)
    check(f"“{said}”", (found[0] if found else None) == expect, f"→ {found[0] if found else '没找到'}")

print("\n===== 1b. 按名字找按钮：模糊匹配绝不挑出“关闭”“删除”（不动屏幕）=====")
fake = [{"name": n, "type": "按钮", "x": 0, "y": 0} for n in ["最小化计算器", "关闭计算器", "一", "等于", "删除聊天记录", "发送(S)"]]
fake_texts = [{"name": "计算器", "type": "文字", "x": 0, "y": 0}]
for target, expect in [("计算器", "计算器"), ("关闭", "关闭计算器"), ("关掉计算器", None), ("等于", "等于"),
                       ("发送", "发送(S)"), ("聊天记录", None), ("删除聊天记录", "删除聊天记录")]:
    found = computer._match_element(target, fake, fake_texts)
    name = found["name"] if found else None
    ok = name == expect if expect else (name is None or not computer.DANGER_WORDS.search(name))
    check(f"点“{target}”", ok, f"→ {name or '没找到'}")

print("\n===== 2. 计算器：打开、按名字点击、打字、读屏幕 =====")
r = computer.open_app("计算器")
check("打开计算器", r["ok"] and "计算器" in r["message"], f"→ {r['message']}")
time.sleep(1.0)
computer.press_keys("esc")  # 清零
for button in ["一", "加", "二", "等于"]:
    r = computer.click(button)
    if not r["ok"]:
        check(f"点击“{button}”", False, f"→ {r['message']}")
        break
text = screen_text()
check("点了“一 加 二 等于”，屏幕显示 3", "显示为 3" in text.replace("\u202d", "").replace("\u202c", ""),
      f"→ {[line for line in text.splitlines() if '显示' in line][:2]}")
r = computer.type_text("7*6=")
time.sleep(0.5)
text = screen_text()
check("直接打字“7*6=”，屏幕显示 42", "显示为 42" in text.replace("\u202d", "").replace("\u202c", ""),
      f"→ {r['message']}")
r = computer.window("minimize", "计算器")
check("最小化计算器", r["ok"], f"→ {r['message']}")
r = computer.window("switch", "计算器")
check("切回计算器", r["ok"] and computer._foreground() and "计算器" in computer._foreground()["title"],
      f"→ {r['message']}")
r = computer.window("close", "计算器")
time.sleep(0.5)
check("关闭计算器", r["ok"] and not any("计算器" in w["title"] for w in computer._windows()), f"→ {r['message']}")

print("\n===== 3. 记事本：打字（中文）、保存、核对文件 =====")
path = os.path.join(TMP, "星期八测试.txt")
open(path, "w", encoding="utf-8").close()
ok = False
if any(w["process"].lower() == "notepad.exe" for w in computer._windows()):
    print("[跳过] 你开着记事本：Windows 11 会把测试文件塞进你的记事本窗口，为了不碰你的标签页，这项先跳过")
else:
    os.startfile(path)
    ok = computer._wait(lambda: computer._foreground() and "星期八测试" in computer._foreground()["title"], 10)
    check("用记事本打开测试文件", ok, f"→ {computer._foreground()['title'] if computer._foreground() else '没有窗口'}")
if ok:
    time.sleep(0.8)
    words = "你好，我是星期八。Hello 123"
    r = computer.type_text(words)
    computer.press_keys("ctrl+s")
    time.sleep(1.0)
    with open(path, encoding="utf-8-sig") as f:
        saved = f.read()
    check("打的字和保存下来的一样（含中文）", saved == words, f"→ 文件内容：{saved!r}")
    r = computer.read_screen()
    check("read_screen 能看到记事本的菜单", r["ok"] and "文件" in r["message"], f"→ {r.get('count')} 个可操作")
    if "星期八测试" in (computer._foreground() or {}).get("title", ""):
        computer.press_keys("ctrl+w")  # 只关这个标签页
    time.sleep(0.8)
    check("关掉测试标签页", not any("星期八测试" in w["title"] for w in computer._windows()))

print("\n===== 4. 危险操作：先登记，说“确定”才做 =====")
real_power = computer.EXECUTORS["power"]
computer.EXECUTORS["power"] = lambda action: {"ok": True, "message": f"（假的）{action}"}  # 保险：绝不真关机
try:
    r = computer.run_command("Write-Output 星期八测试", "输出一句测试文字")
    check("运行命令：先登记、不直接做", r.get("needs_confirm") and computer.pending(), f"→ {r['message']}")
    r = computer.confirm() if computer.answer("确定") == "yes" else {}
    check("说“确定”后才执行", r.get("ok") and "星期八测试" in r.get("message", ""), f"→ {r.get('message')}")

    r = computer.run_command(r"reg add HKCU\Software\Test /v x /d 1", "改注册表")
    check("改注册表的命令直接拒绝", not r["ok"] and not computer.pending(), f"→ {r['message']}")

    victim = os.path.join(TMP, "要删的测试文件.txt")
    with open(victim, "w", encoding="utf-8") as f:
        f.write("test")
    r = computer.delete_file(victim)
    check("删文件：先登记", r.get("needs_confirm") and os.path.exists(victim), f"→ {r['message']}")
    computer.cancel() if computer.answer("算了不用了") == "no" else None
    check("说“算了”就不删", os.path.exists(victim) and not computer.pending())
    computer.delete_file(victim)
    r = computer.confirm() if computer.answer("好的，删吧") == "yes" else {}
    check("说“好的，删吧”才删（进回收站）", r.get("ok") and not os.path.exists(victim), f"→ {r.get('message')}")

    r = computer.delete_file(r"C:\Windows")
    check("系统文件夹不让删", not r["ok"] and not computer.pending(), f"→ {r['message']}")

    r = computer.power("shutdown")
    check("关机：先登记", r.get("needs_confirm"), f"→ {r['message']}")
    computer.cancel()
    check("取消后没有等待的操作", computer.pending() is None)

    computer.run_command("Write-Output x", "测试超时")
    computer._pending["action"]["time"] -= computer.CONFIRM_SECONDS + 1
    check("超过 60 秒没确认，自动作废", computer.pending() is None)
finally:
    computer.EXECUTORS["power"] = real_power
    computer.cancel()

print("\n===== 5. 打开网页（不真的打开浏览器）=====")
opened = []
real_startfile = os.startfile
os.startfile = lambda target: opened.append(target)
try:
    for kwargs, expect in [({"url": "B站"}, "https://www.bilibili.com"), ({"url": "github.com/ollama"}, "https://github.com/ollama"),
                           ({"search": "明天天气"}, "bing.com/search?q=%E6%98%8E"), ({"url": "怎么做红烧肉"}, "search?q=")]:
        opened.clear()
        r = computer.open_web(**kwargs)
        check(f"open_web({kwargs})", opened and expect in opened[0], f"→ {opened[0] if opened else r['message']}")
finally:
    os.startfile = real_startfile

if any("星期八测试" in w["title"] for w in computer._windows()):
    print("（测试文件还在记事本里开着，先不删测试文件夹，免得记事本弹“找不到路径”）")
else:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{sum(results)}/{len(results)} 项通过")
