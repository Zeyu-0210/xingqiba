"""
test_claude_models.py —— 测试“Claude 用哪个模型”：按难度智能选、你自己选、没做成换高一档、记用量

    第 1～4 部分不花额度（难度判断用本地 gemma，Claude 用假的）。
    第 5 部分真的问一次：简单的事应该自动用最省的 Haiku（花费很少）。加 --fake-only 跳过。
    测完恢复你原来的设置，测试的用量不算进你真正的用量记录。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_claude_models.py
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

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_models")
FAKE_ONLY = "--fake-only" in sys.argv
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
claude_models.USAGE_FILE = os.path.join(TMP, "usage.json")
saved_settings = settings.load()
real_run, real_available = takeover.run, takeover.available
assistant.warm_up()

try:
    settings.set("claude_model", claude_models.AUTO)
    print("===== 1. 本地 gemma 判断难度（免费），挑模型 =====")
    for text, expect in [("现在几点了", "haiku"), ("D盘还剩多少空间", "haiku"), ("帮我打开微信", "haiku"),
                         ("把下载文件夹里的图片按月份整理到不同的文件夹里", "sonnet"),
                         ("帮我写一个Python程序，每天自动抓取B站排行榜存成Excel，再做个图表", "opus"),
                         ("我的程序一运行就报内存错误，帮我排查一下是哪里的问题", "opus")]:
        key, why = claude_models.choose(text)
        check(f"“{text[:24]}”→ {claude_models.MODELS[key]['name']}", key == expect,
              f"（{why}，应为 {claude_models.MODELS[expect]['name']}）")
    key, _ = claude_models.choose("现在几点了", at_least="sonnet")
    check("本地没做成才交过来的：至少用 Sonnet", key == "sonnet")

    print("\n===== 2. 你自己选 =====")
    for choice in ("haiku", "opus"):
        claude_models.set_choice(choice)
        check(f"选了 {claude_models.MODELS[choice]['name']}：不管难度都用它",
              claude_models.choose("帮我写个复杂程序")[0] == choice and claude_models.choose("几点了")[0] == choice)
    claude_models.set_choice(claude_models.AUTO)

    print("\n===== 3. 便宜的没做成：自动换高一档再试一次 =====")
    used = []

    def flaky_run(text, history=None, name="", profile="", note="", model="", effort=""):
        used.append(model)
        ok = model != "claude-haiku-5-5"  # 假装 Haiku 没做成
        return {"reply": "办好了。" if ok else "抱歉，这件事我没做成。", "steps": ["运行命令：假的"], "needs_confirm": False,
                "error": False, "cost": 0.01, "session": "fake", "model": model}

    takeover.run, takeover.available = flaky_run, lambda: True
    brains.set_brain(brains.TAKEOVER)
    m = memory.Memory(os.path.join(TMP, "memory"), resume=False)
    r = assistant.handle("现在几点了", m)
    check("Haiku 没做成 → 换 Sonnet 再试，做成了", used == ["claude-haiku-5-5", "claude-sonnet-5-5"]
          and r["reply"] == "办好了。", f"→ 用了 {r['actions'][-1]['result'].get('model')}")
    used.clear()
    settings.set("claude_model", "haiku")
    r = assistant.handle("现在几点了", m)
    check("你固定选了 Haiku：没做成也不偷偷换贵的", used == ["claude-haiku-5-5"])
    settings.set("claude_model", claude_models.AUTO)
    for reply, failed in [("删好了。", False), ("这件事我没做成，因为找不到文件。", True),
                          ("改注册表是被禁止的，我做不到。", False), ("我要删掉这个文件，确定吗？", False)]:
        check(f"“{reply}”算没做成吗？{'是' if failed else '不是'}",
              claude_models.looks_failed({"reply": reply}) == failed)

    print("\n===== 4. 记用量 =====")
    day = claude_models.today()
    check("每次都记下了（次数、花费、哪个模型）", day["runs"] == 3 and abs(day["usd"] - 0.03) < 1e-6
          and set(day["models"]) == {"haiku", "sonnet"}, f"→ {claude_models.today_text()}")
    takeover.run, takeover.available = real_run, real_available

    if FAKE_ONLY or not takeover.available():
        print("\n[跳过] 第 5 部分（真的问 Claude）" + ("：你加了 --fake-only" if FAKE_ONLY else "：没登录"))
    else:
        print("\n===== 5. 真的问一次：简单的事自动用 Haiku =====")
        takeover.end_session()
        r = assistant.handle("现在几点了？", memory.Memory(os.path.join(TMP, "memory2"), resume=False))
        result = r["actions"][-1]["result"]
        check("简单的事用了 Haiku，而且做成了", "Haiku" in result.get("model", "") and result.get("ok"),
              f"→ {r['reply'][:50]}（{result.get('model')}，约 {result.get('cost_usd') or 0:.4f} 美元）")
        brains.set_brain("Claude")
        text = brains.ask("你叫星期八，是语音助手。回答要简短。", [{"role": "user", "content": "用一句话说说你是谁"}])
        day = claude_models.today()
        check("“Claude 当脑子”聊天也按难度挑模型（简单的用 Haiku）", bool(text) and day["models"].get("haiku", {}).get("runs", 0) >= 2,
              f"→ {text[:40]}；{claude_models.today_text()}")
finally:
    takeover.run, takeover.available = real_run, real_available
    takeover.end_session()
    with open(settings.PATH, "w", encoding="utf-8") as f:
        json.dump(saved_settings, f, ensure_ascii=False, indent=2)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n（已恢复：大脑 = {brains.current()}，Claude 模型 = {claude_models.label(claude_models.current())}）")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
