"""
test_volume.py —— 逐个测试 volume.py 里的函数

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_volume.py

注意：测试会真的改变系统音量，你会看到任务栏的音量图标在变化。
为了不吓到你，测试过程中音量最高只到 40%；
测试结束后（即使中途出错）一定会恢复成测试前的音量和静音状态。
"""

import time

import volume

results = []  # 记录每一项测试是否通过


def check(name, ok, result):
    """打印一项测试的结果。"""
    results.append(ok)
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name:<16} -> {result['message']}")
    time.sleep(0.4)  # 稍微停一下，方便你观察音量图标的变化


original = volume.get_volume()
print(f"测试前：{original['message']}\n")

try:
    r = volume.set_volume(30)
    check("设到 30%", r["volume"] == 30 and not r["muted"], r)

    r = volume.change_volume(10)
    check("调大 10", r["volume"] == 40, r)

    r = volume.change_volume(-25)
    check("调小 25", r["volume"] == 15, r)

    r = volume.mute()
    check("静音", r["muted"] and r["volume"] == 15, r)

    r = volume.unmute()
    check("取消静音", not r["muted"] and r["volume"] == 15, r)

    r = volume.mute()
    r = volume.set_volume(20)
    check("静音时设音量", r["volume"] == 20 and not r["muted"], r)

    r = volume.set_volume("25")
    check("文字数字 \"25\"", r["volume"] == 25, r)

    r = volume.change_volume(-500)
    check("调小超过下限", r["volume"] == 0, r)

    # 上限只检查计算逻辑，不真的把音量开到 100%
    ok = volume._clamp(150) == 100
    check("超过上限算成 100", ok, {"message": f"_clamp(150) = {volume._clamp(150)}"})

finally:
    # 无论测试成功与否，都恢复原来的状态
    volume.set_volume(original["volume"])
    if original["muted"]:
        volume.mute()
    print(f"\n已恢复：{volume.get_volume()['message']}")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
