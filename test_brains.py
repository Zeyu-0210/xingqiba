"""
test_brains.py —— 测试“大脑”：说一句就换、操作本地做（不花钱）、动脑交给大脑、用不了时退回本地

    不需要任何 API 密钥、也不花钱：云端大脑用“假大脑”代替；
    GPT 那条代码用本地 Ollama 冒充 OpenAI 的服务真跑一遍。
    会改变音量（调到 30%），测完恢复；测完恢复你原来选的大脑和声音。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_brains.py
"""

import json
import os
import shutil

import assistant
import brain_openai
import brains
import delegate
import friendly
import memory
import settings
import volume
import workers
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_brains")
delegate.DRY_RUN = True
workers.TASKS_DIR = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_brains_tasks")
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    return memory.Memory(TMP, resume=False)


saved_settings = settings.load()
original_volume = volume.get_volume()
real_ask = brains.ask
assistant.warm_up()

try:
    print("===== 1. 说一句就换大脑 =====")
    for said, expect in [("让 Claude 当我的脑子", "Claude"), ("换成 GPT 回答我", "GPT"),
                         ("用 Claude 回答我", "Claude"), ("换回本地", "本地"), ("换回 Gemma", "本地")]:
        r = assistant.handle(said, fresh())
        tools = [a["tool"] for a in r["actions"]]
        check(f"“{said}”", brains.current() == expect and "set_brain" in tools,
              f"→ 现在是 {brains.current()}（应为 {expect}）  回复：{r['reply']}")

    brains.set_brain("本地")
    r = assistant.handle("让 Claude 帮我用 Python 写一个贪吃蛇小游戏", fresh())
    tools = [a["tool"] for a in r["actions"]]
    check("“让 Claude 帮我写贪吃蛇”是交任务，不是换大脑",
          "delegate_task" in tools and brains.current() == "本地", f"→ 工具 {tools}")

    print("\n===== 2. 操作本地做、动脑交给大脑（假大脑，不花钱）=====")
    calls = []

    def fake_ask(system, messages, brain=None):
        calls.append({"system": system, "messages": messages, "brain": brain})
        return "（假GPT）这是大脑的回答。"

    brains.ask = fake_ask
    brains.set_brain("GPT")
    m = fresh()

    r = assistant.handle("音量调到30", m)
    check("调音量：本地直接做，不找大脑（不花钱）",
          volume.get_volume()["volume"] == 30 and not calls and r["brain"] == "本地",
          f"→ 工具 {[a['tool'] for a in r['actions']]}，找了大脑 {len(calls)} 次")

    r = assistant.handle("给我讲个很短的笑话", m)
    check("讲笑话：交给大脑回答", len(calls) == 1 and "假GPT" in r["reply"] and r["brain"] == "GPT",
          f"→ 回复：{r['reply']}")
    check("大脑知道自己叫星期八、也知道最近的对话",
          "星期八" in calls[-1]["system"] and any("音量调到30" in msg["content"] for msg in calls[-1]["messages"]),
          f"→ 带过去 {len(calls[-1]['messages'])} 条对话")

    def broken_ask(system, messages, brain=None):
        raise brains.BrainError("密钥不对或者没填")

    brains.ask = broken_ask
    r = assistant.handle("天空为什么是蓝色的", fresh())
    check("大脑用不了时，说明原因并退回本地回答",
          r["reply"].startswith(friendly.MESSAGES["offline"]) and r["reply"] != friendly.MESSAGES["offline"]
          and r["brain"] == "本地",
          f"→ 回复：{r['reply'][:60]}…")
    brains.ask = real_ask

    print("\n===== 3. GPT 那条代码真跑一遍（本地 Ollama 冒充 OpenAI 服务，免费）=====")
    question = [{"role": "user", "content": "用一句话介绍你自己。"}]
    for api in ("responses", "chat"):
        try:
            text = brain_openai.answer("你叫星期八，是语音助手。回答要简短。", question,
                                       model=assistant.MODEL, api_key="ollama",
                                       base_url="http://127.0.0.1:11434/v1", api=api)
            check(f"OpenAI 接口（{api}）走得通", bool(text), f"→ {text}")
        except Exception as e:
            check(f"OpenAI 接口（{api}）走得通", False, f"→ 出错：{type(e).__name__}: {str(e)[:80]}")

    print("\n===== 4. Claude 没填密钥时 =====")
    if brains.has_key("Claude") or os.environ.get("ANTHROPIC_API_KEY"):
        print("[跳过] Claude 已经能用了（登录了账号或填了密钥），这项测试会真的用掉额度，所以不测")
    else:
        try:
            brains.ask("测试", [{"role": "user", "content": "你好"}], "Claude")
            check("没密钥时给出能看懂的原因", False, "→ 居然成功了？")
        except brains.BrainError as e:
            check("没密钥时给出能看懂的原因（不会崩溃）", True, f"→ {e}")

finally:
    brains.ask = real_ask
    with open(settings.PATH, "w", encoding="utf-8") as f:
        json.dump(saved_settings, f, ensure_ascii=False, indent=2)
    if not saved_settings:
        os.remove(settings.PATH)
    volume.set_volume(original_volume["volume"])
    if original_volume["muted"]:
        volume.mute()
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(workers.TASKS_DIR, ignore_errors=True)
    print(f"\n（已恢复：大脑 = {brains.current()}，{volume.get_volume()['message']}）")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
