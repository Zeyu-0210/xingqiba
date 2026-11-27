"""
test_assistant.py —— 用一组中文句子测试“模型 + 音量工具”是否配合正确

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_assistant.py

注意：测试会真的改变系统音量，最高到 45% 左右；
测试结束后（即使中途出错）一定会恢复成测试前的音量和静音状态。
"""

import os
import shutil

import assistant
import memory
import volume
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

# 用临时记忆文件夹，测完删除，不会写进你真正的历史记录
TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_memory_assistant")
shutil.rmtree(TMP, ignore_errors=True)
MEM = memory.Memory(TMP, resume=False)

# 测试时不真的打开浏览器（问天气时模型可能会用浏览器搜一下）：只记下它想打开什么
os.startfile = lambda target, *args, **kwargs: print(f"    （测试中没有真的打开：{target}）")

# 每一项：(要说的话, 检查函数)
# 检查函数拿到 handle() 的结果和执行后的音量状态，返回 True 表示符合预期
CASES = [
    ("现在音量是多少",
     lambda r, v: [a["tool"] for a in r["actions"]] == ["get_volume"]),
    ("把音量调到35",
     lambda r, v: v["volume"] == 35),
    ("声音大一点",
     lambda r, v: v["volume"] == 45),
    ("太吵了，小声点",
     lambda r, v: v["volume"] == 35),
    ("静音",
     lambda r, v: v["muted"]),
    ("取消静音",
     lambda r, v: not v["muted"]),
    ("帮我把音量调成百分之二十",
     lambda r, v: v["volume"] == 20),
    ("音量三十",
     lambda r, v: v["volume"] == 30),
    ("把音量调到二十五然后静音",
     lambda r, v: v["volume"] == 25 and v["muted"]),
    # 本地模型不知道天气：直接回答，或者用浏览器帮你搜一下（有了 open_web 以后），都算对
    ("今天天气怎么样",
     lambda r, v: all(a["tool"] == "open_web" for a in r["actions"])),
    ("给我讲个很短的笑话",
     lambda r, v: r["actions"] == []),
]

original = volume.get_volume()
print(f"测试前：{original['message']}")
print(f"正在加载模型 {assistant.MODEL} ……用时 {assistant.warm_up():.1f} 秒\n")

passed = 0
times = []
try:
    for i, (text, check) in enumerate(CASES, 1):
        result = assistant.handle(text, MEM)
        status = volume.get_volume()
        ok = check(result, status)
        passed += ok
        times.append(result["seconds"])
        print(f"{i:>2}. {'[通过]' if ok else '[失败]'} 你：{text}")
        assistant.print_result(result)
        print()
finally:
    volume.set_volume(original["volume"])
    if original["muted"]:
        volume.mute()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"已恢复：{volume.get_volume()['message']}")

print(f"\n结果：{passed}/{len(CASES)} 项通过，模型平均用时 {sum(times) / len(times):.1f} 秒")
