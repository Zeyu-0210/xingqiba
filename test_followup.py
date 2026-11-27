"""
test_followup.py —— 测试“让它退下 / 回答完只再听一会儿 / 界面同步”

    1. 哪些话会让它马上回去待命（“静默”“退下吧”“拜拜”……），哪些不会（“静音”是调音量）
    2. 回答完再听多久：平时 20 秒；它在问你问题时 60 秒；设置里可以改
    3. 对话窗口：回答完 20 秒没再说话就关；模型判断你想结束（end_conversation）就马上关
    4. 真的问本地模型：换种说法让它退下，它会调用 end_conversation；“帮我静音”不会
    5. 改设置（比如用语音换大脑）会马上通知窗口界面

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_followup.py
    加 --quick 不跑第 4 部分（不用本地模型）
"""

import contextlib
import io
import os
import shutil
import sys

import numpy as np

import listen
import memory
import settings
import status
import takeover

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_followup")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
settings.PATH = os.path.join(TMP, "settings.json")
takeover.available = lambda: False
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


print("===== 1. 哪些话让它马上回去待命 =====")
YES = ["静默", "进入静默模式。", "静默模式", "你先静默吧", "回去待命吧", "待命", "退下吧", "你先休息吧",
       "拜拜", "好了，再见", "晚安", "我不跟你说了", "没你事了", "别听了", "星期八，退下吧", "退出", "关闭", "好了。"]
NO = ["静音", "取消静音", "把手机调成静默模式", "进入睡眠模式", "帮我关闭记事本", "休息一下再玩",
      "现在音量是多少", "我去休息一下，你帮我把音量调小"]
for text in YES:
    check(f"“{text}” → 回去待命", listen.is_end_phrase(text))
for text in NO:
    check(f"“{text}” → 不是让它退下", not listen.is_end_phrase(text))

print("\n===== 2. 回答完再听多久 =====")
check("它在问你问题（问号结尾）", listen.asks_question("需要我帮你打开吗？"))
check("它在问你问题（“……呢”结尾）", listen.asks_question("你想听哪一首呢"))
check("普通回答不算问题", not listen.asks_question("好的，音量调到百分之三十了。"))
check("默认：回答完再听 20 秒", listen.follow_up_seconds("好的。") == 20)
check("它问了你问题：等 60 秒", listen.follow_up_seconds("要我帮你保存吗？") == 60)
settings.set("follow_up_seconds", 180)
check("设置改成 3 分钟：就听 3 分钟（问问题也不会变短）",
      listen.follow_up_seconds("好的。") == 180 and listen.follow_up_seconds("要保存吗？") == 180)
settings.set("follow_up_seconds", 10)
check("设置改成 10 秒", listen.follow_up_seconds("好的。") == 10)

print("\n===== 3. 对话窗口（假录音、假时钟）=====")


def window(lines, answers, follow=20):
    """lines：[(文字, 过了几秒)，None=一直没人说话]；answers：on_text 对每句话的回答。"""
    now = [0.0]
    lines = list(lines)
    current = [None]
    passed = []

    class Stt:
        def transcribe(self, audio):
            return current[0]

    def record(start_timeout, prompt):
        item = lines.pop(0) if lines else None
        if item is None:
            now[0] += start_timeout
            return None
        current[0], seconds = item
        now[0] += seconds
        return np.zeros(1600, np.float32)

    def on_text(text, first):
        passed.append(text)
        return answers.get(text, True)

    with contextlib.redirect_stdout(io.StringIO()):
        listen.conversation_window(Stt(), on_text, record=record, clock=lambda: now[0],
                                   follow_up=lambda: follow)
    return now[0], passed


closed, passed = window([("音量调到30", 3), None], {})
check("回答完 20 秒没再说话：回去待命", closed == 3 + 20, f"→ 第 {closed:.0f} 秒关闭")
closed, passed = window([("音量调到30", 3), ("快去B点", 10), None], {"快去B点": False})
check("中间的闲聊不会让它多听", closed == 3 + 20, f"→ 第 {closed:.0f} 秒关闭")
closed, passed = window([("音量调到30", 3), ("静默", 5), ("这句不该被听到", 1)], {})
check("说“静默”：马上回去待命，后面的话不会被听写", closed == 8 and passed == ["音量调到30"])
closed, passed = window([("我去打游戏了，你先歇着吧", 3), ("这句不该被听到", 1)], {"我去打游戏了，你先歇着吧": "end"})
check("模型判断你想结束（end_conversation）：说完告别马上回去待命", closed == 3 and len(passed) == 1)

if "--quick" not in sys.argv:
    print("\n===== 4. 真的问本地模型：换种说法让它退下 =====")
    import assistant
    import volume
    assistant.MEMORY = memory.Memory(os.path.join(TMP, "memory"), resume=False)
    was_muted = volume.get_volume().get("muted")
    try:
        for text, expect in [("我要去打游戏了，你先歇着吧", True), ("行了，先这样，有事再叫你", True),
                             ("帮我静音", False), ("现在音量是多少", False)]:
            r = assistant.handle(text, first=False)
            tools = [a["tool"] for a in r["actions"]]
            check(f"“{text}” → {'结束对话' if expect else '照常处理'}", bool(r.get("end")) == expect,
                  f"→ 工具 {tools or '无'}，回答：{r['reply']}")
    finally:
        if not was_muted:
            volume.unmute()  # 测试里“帮我静音”真的会静音，测完恢复

print("\n===== 5. 改设置会马上通知窗口界面 =====")
got = []
status.subscribe_events(got.append)
import brains  # noqa: E402
brains.set_brain("Claude")
brains.set_brain("本地")
check("用语音换大脑：窗口界面收到“设置变了”的通知", [e for e in got if e.get("type") == "settings" and e["key"] == "brain"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{sum(results)}/{len(results)} 项通过")
