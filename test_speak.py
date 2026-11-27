"""
test_speak.py —— 测试语音回复（不会真的放出声音）

    1. 两种声音（微软在线 / 本地离线）都能合成，速度够快
    2. “贾维斯风格”的润色：把机械的执行结果变成口语，而且数字不能说错、不能编造
    3. 念之前会去掉 Markdown 符号和表情

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_speak.py
"""

import time

import assistant
import speak

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


print("===== 1. 合成声音 =====")
for voice in ("edge:zh-CN-YunyangNeural", "kokoro:52"):
    speaker = speak.Speaker(voice)
    speaker.synthesize("你好。")  # 第一次会加载模型 / 建立连接，不计时
    t = time.time()
    samples, rate = speaker.synthesize("好的，音量已经调到百分之三十了。")
    seconds = time.time() - t
    duration = len(samples) / rate
    check(f"{speak.CANDIDATES[voice]}", duration > 1 and seconds < 3,
          f"合成用时 {seconds:.2f} 秒，声音长 {duration:.1f} 秒")

speaker = speak.Speaker("edge:zh-CN-YunyangNeural")
speaker.prepare(["我在。"])
t = time.time()
speaker.synthesize("我在。")
check("提前合成好的“我在”，取用时 0 等待", time.time() - t < 0.01, f"{(time.time() - t) * 1000:.1f} 毫秒")

print("\n===== 2. 贾维斯风格的润色 =====")
assistant.warm_up()
cases = [
    ({"text": "音量调到30", "reply": "音量已从 14% 设为 30%",
      "actions": [{"tool": "set_volume"}]}, ["30", "三十"]),
    ({"text": "静音", "reply": "已静音", "actions": [{"tool": "mute"}]}, ["静音"]),
    ({"text": "帮我把数学作业做了", "reply": "已经交给 Claude，它会在新窗口里开始干活。任务文件夹：D:\\AI任务\\2026-11-20 1429 数学作业",
      "actions": [{"tool": "delegate_task"}]}, ["Claude", "克劳德"]),
]
for result, must in cases:
    t = time.time()
    words = assistant.spoken_reply(result)
    seconds = time.time() - t
    ok = any(m in words for m in must) and len(words) <= 60 and words != result["reply"]
    check(f"“{result['reply'][:20]}…”", ok, f"→ “{words}”（{seconds:.1f} 秒）")

natural = {"text": "讲个笑话", "reply": "为什么程序员总分不清万圣节和圣诞节？因为 Oct 31 = Dec 25。",
           "actions": [], "natural": True}
check("模型自己说的话直接念，不再润色", assistant.spoken_reply(natural) == natural["reply"])

print("\n===== 3. 念之前清理符号 =====")
check("去掉 Markdown 和表情", speak.clean("**好的** 😀 已经 `完成` 了") == "好的 已经 完成 了",
      f"→ “{speak.clean('**好的** 😀 已经 `完成` 了')}”")

print("\n===== 4. 换声音 =====")
import os  # noqa: E402
import shutil  # noqa: E402

import memory  # noqa: E402
import settings  # noqa: E402
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

saved = settings.load()  # 测完要恢复你原来的设置
TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_memory_voice")
shutil.rmtree(TMP, ignore_errors=True)
try:
    for said, expect in [("换成晓晓的声音", "晓晓"), ("换个男声", "云扬"), ("用离线的女声吧", "离线女声"),
                         ("换回云扬", "云扬")]:
        r = assistant.handle(said, memory.Memory(TMP, resume=False))
        now = speak.VOICES[speak.current_voice()][0]
        check(f"说“{said}”", now == expect, f"→ 现在是 {now}（应为 {expect}），回复：{r['reply']}")

    speak.set_voice("晓晓")
    check("选择记在 settings.json 里（重启后不变）", settings.load().get("voice") == "edge:zh-CN-XiaoxiaoNeural")
    follower = speak.Speaker()  # 不指定声音的“嘴巴”，会跟着当前选择走
    check("不指定声音时，跟着当前选择走", follower.voice == "edge:zh-CN-XiaoxiaoNeural")
    check("没有的声音会提示可选的", "可以选" in speak.set_voice("不存在的声音")["message"])

    broken = speak.Speaker("edge:zh-CN-XiaoxiaoNeural")
    broken._edge = lambda name, text: (_ for _ in ()).throw(ConnectionError("断网了"))  # 假装断网
    samples, rate = broken.synthesize("网络断了也要能说话。")
    check("微软语音失败时，自动换成同性别的离线声音", len(samples) / rate > 1, f"离线声音长 {len(samples) / rate:.1f} 秒")
finally:
    with open(settings.PATH, "w", encoding="utf-8") as f:
        import json  # noqa: E402
        json.dump(saved, f, ensure_ascii=False, indent=2)
    if not saved:
        os.remove(settings.PATH)
    shutil.rmtree(TMP, ignore_errors=True)
    print("  （已恢复测试前的声音设置）")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
