"""
test_takeover.py —— 测试“Claude 接管”：切换、过滤闲聊、它问“确定吗”你回答、本地做不成时自动求助

    第 1～4 部分用“假 Claude”，不花额度。
    第 5 部分真的让 Claude 做两件事（会用掉一些 Pro 额度）：
        查一下电脑上开着哪些窗口；删一个测试文件（它会先问“确定吗”，说确定才删）。
    （2026-11-21 删掉了“门卫”：没有程序拦，只靠说明让它在删除这类改不回来的事之前先问一句。）
    不想用额度：加 --fake-only 只跑前四部分。
    测完恢复你原来选的大脑和设置。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_takeover.py
"""

import json
import os
import shutil
import sys

import assistant
import brains
import claude_models
import memory
import settings
import takeover

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_takeover")
claude_models.USAGE_FILE = os.path.join(TMP, "usage.json")  # 测试的用量不算进你真正的用量记录
FAKE_ONLY = "--fake-only" in sys.argv
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def fresh():
    shutil.rmtree(os.path.join(TMP, "memory"), ignore_errors=True)
    return memory.Memory(os.path.join(TMP, "memory"), resume=False)


def clear_state():
    assistant._claude_asked["time"] = 0.0
    if os.path.exists(takeover.SESSION_FILE):
        os.remove(takeover.SESSION_FILE)


def say(text, mem, first=True):
    r = assistant.handle(text, mem, first=first)
    tools = [a["tool"] for a in r["actions"]]
    shown = "（不是对助手说的，没理会）" if r.get("ignored") else r["reply"][:120]
    print(f"  你：{text}\n    → 工具：{tools or '（无）'}  回复：{shown}（{r['seconds']:.1f} 秒）")
    return r


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
saved_settings = settings.load()
real_run, real_available = takeover.run, takeover.available
calls = []


def fake_run(text, history=None, name="", profile="", note="", **kw):
    calls.append({"text": text, "note": note, "model": kw.get("model")})
    return {"reply": "（假Claude）办好了。", "steps": ["运行命令：假的"], "needs_confirm": False, "error": False,
            "cost": 0.0, "session": "fake", "model": kw.get("model")}


assistant.warm_up()
try:
    takeover.run, takeover.available = fake_run, lambda: True
    clear_state()
    brains.set_brain("本地")
    settings.set("auto_help", True)

    print("===== 1. 说一句话进入 / 退出接管 =====")
    m = fresh()
    say("让 Claude 接管电脑", m)
    check("“让 Claude 接管电脑”→ 进入接管", brains.current() == brains.TAKEOVER)
    calls.clear()
    r = say("帮我看看D盘还剩多少空间", m)
    check("接管时：这句话交给 Claude（本地不动手）", len(calls) == 1 and r["brain"] == brains.TAKEOVER
          and [a["tool"] for a in r["actions"]] == ["claude_takeover"])
    calls.clear()
    say("换回本地", m)
    check("“换回本地”→ 本地处理、退出接管（不发给 Claude）", brains.current() == "本地" and not calls)

    print("\n===== 2. 接管时，闲聊不发给 Claude（本地先过滤，省额度）=====")
    brains.set_brain(brains.TAKEOVER)
    m = fresh()
    say("帮我查一下C盘还剩多少", m)
    calls.clear()
    r = say("兄弟你那边卡不卡啊", m, first=False)
    check("“兄弟你那边卡不卡啊”→ 不理会，不发给 Claude", r.get("ignored") and not calls)
    r = say("再帮我看一下D盘", m, first=False)
    check("“再帮我看一下D盘”→ 发给 Claude", len(calls) == 1 and not r.get("ignored"))

    print("\n===== 3. Claude 自己问“确定吗”：你说确定，接着交给它去做 =====")
    m = fresh()

    def asking_run(text, history=None, name="", profile="", note="", **kw):  # 假装 Claude 在问“确定吗”
        calls.append({"text": text})
        return {"reply": "我要删掉 D:\\AI任务\\旧文件.txt，确定吗？", "steps": [], "needs_confirm": True,
                "error": False, "cost": 0.0, "session": "fake"}

    takeover.run = asking_run
    say("把AI任务里的旧文件删掉", m)
    takeover.run = fake_run
    calls.clear()
    say("确定", m, first=False)
    check("说“确定”→ 接着交给 Claude 去做（告诉它你同意了）", len(calls) == 1 and "同意" in calls[0]["text"])
    clear_state()
    takeover.run = asking_run
    say("把AI任务里的旧文件删掉", m)
    takeover.run = fake_run
    calls.clear()
    r = say("算了不删了", m, first=False)
    check("说“算了不删了”→ 不发给 Claude，回一句“不做了”", not calls and r["reply"] == "好的，不做了。")
    check("Claude 问“确定吗”的话能认出来", takeover.asks_confirm("我要删掉旧文件.txt，确定吗？")
          and not takeover.asks_confirm("D 盘还剩 300 GB。"))

    print("\n===== 4. 自动求助：本地做不成，交给 Claude 接手 =====")
    brains.set_brain("本地")
    m = fresh()
    calls.clear()
    r = say("看一下英雄联盟窗口里有没有更新按钮", m)
    check("本地找不到窗口 → 自动交给 Claude", len(calls) == 1 and r["reply"].startswith("本地没做成")
          and "没做成" in calls[0]["note"], f"→ 告诉 Claude：{calls[0]['note'][:60] if calls else '（没交）'}")
    settings.set("auto_help", False)
    calls.clear()
    r = say("看一下英雄联盟窗口里有没有更新按钮", fresh())
    check("托盘里关掉自动求助 → 不交给 Claude", not calls)
    settings.set("auto_help", True)
    takeover.available = lambda: False
    brains.set_brain(brains.TAKEOVER)
    calls.clear()
    r = say("现在音量是多少", fresh())
    check("Claude 用不了（没登录）时：照常本地做", not calls and any(a["tool"] == "get_volume" for a in r["actions"]))
    takeover.run, takeover.available = real_run, real_available

    if FAKE_ONLY:
        print("\n[跳过] 第 5 部分（真的用 Claude）：你加了 --fake-only")
    elif not takeover.available():
        print("\n[跳过] 第 5 部分：Claude Code 还没登录")
    else:
        print("\n===== 5. 真的让 Claude 接管（用一些 Pro 额度）=====")
        clear_state()
        brains.set_brain(brains.TAKEOVER)
        m = fresh()
        r = say("现在电脑上开着哪些程序的窗口？简单说几个名字就行", m)
        cost = r["actions"][-1]["result"].get("cost_usd")
        check("让 Claude 查电脑（它自己用 PowerShell）", r["actions"][-1]["result"].get("ok") and "运行命令" in
              r["actions"][-1]["result"]["message"], f"→ 约 {cost or 0:.2f} 美元额度")

        victim = os.path.join(TMP, "要删的测试文件.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("test")
        r = say(f"把这个文件删掉：{victim}", m)
        asked = os.path.exists(victim) and "确定" in r["reply"]
        check("删文件：Claude 先问“确定吗”（靠说明，没有程序拦），文件还在", asked, f"→ {r['reply'][:60]}")
        if asked:
            r = say("确定", m, first=False)
        check("说“确定”→ 真的删了", not os.path.exists(victim), f"→ {r['reply'][:60]}")
finally:
    takeover.run, takeover.available = real_run, real_available
    clear_state()
    with open(settings.PATH, "w", encoding="utf-8") as f:
        json.dump(saved_settings, f, ensure_ascii=False, indent=2)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n（已恢复：大脑 = {brains.current()}，自动求助 = {settings.get('auto_help', True)}）")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
