"""
test_memory.py —— 测试三层记忆：A 短期记忆、B 历史记录、C 长期记忆、D 回头查历史

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_memory.py

注意：
  - 用的是临时记忆文件夹 tests\\_tmp_memory，测完自动删除，不会影响你真正的记忆
  - 会真的改变系统音量（最高 50%），测完恢复成测试前的状态
"""

import json
import os
import shutil
from datetime import datetime, timedelta

import assistant
import memory
import volume
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_memory")
results = []


def fresh(clear=False, resume=True):
    """新建一个记忆。clear=True 先清空临时文件夹；同一个文件夹再建一次 = 模拟“程序重启”。"""
    if clear:
        shutil.rmtree(TMP, ignore_errors=True)
    return memory.Memory(TMP, resume=resume)


def say(mem, text):
    r = assistant.handle(text, mem)
    tools = "、".join(f"{a['tool']}({json.dumps(a['args'], ensure_ascii=False)})" for a in r["actions"])
    print(f"    你：{text}")
    print(f"      工具：{tools or '（无）'}")
    print(f"      助手：{r['reply']}")
    return r


def check(name, ok):
    results.append(ok)
    print(f"  {'[通过]' if ok else '[失败]'} {name}\n")


original = volume.get_volume()
print(f"测试前：{original['message']}")
print(f"加载模型……用时 {assistant.warm_up():.1f} 秒\n")

try:
    # ------------------------------------------------------------------
    print("===== A. 短期记忆 =====")
    volume.set_volume(25)
    m = fresh(clear=True)
    say(m, "音量调到40")
    say(m, "调回去")
    check("调到 40 → 调回去 = 回到原来的 25%", volume.get_volume()["volume"] == 25)

    say(m, "静音")
    say(m, "调回去")
    check("静音 → 调回去 = 取消静音", not volume.get_volume()["muted"])

    volume.set_volume(20)
    say(m, "大一点")
    say(m, "再大一点")
    check("大一点 → 再大一点 = 连续调大两次（20% → 40%）", volume.get_volume()["volume"] == 40)

    # ------------------------------------------------------------------
    print("===== B. 历史记录 =====")
    files = os.listdir(os.path.join(TMP, "history"))
    with open(os.path.join(TMP, "history", files[0]), encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    print(f"    历史文件：{files}，第一行：{json.dumps(lines[0], ensure_ascii=False)[:120]}……")
    check(f"6 句话都写进了历史记录（实际 {len(lines)} 行）",
          len(lines) == 6 and all({"time", "you", "actions", "reply"} <= set(r) for r in lines))

    volume.set_volume(30)
    m = fresh()          # 模拟重启
    say(m, "静音")
    m = fresh()          # 再重启一次
    check(f"重启后接回了最近的对话（{m.resumed} 轮，最多 6 轮）", m.resumed == 6)
    say(m, "调回去")
    check("重启后说“调回去” = 取消静音", not volume.get_volume()["muted"])

    # ------------------------------------------------------------------
    print("===== C. 长期记忆 =====")
    m = fresh(resume=False)
    say(m, "记住，我晚上一般把音量开到20")
    print(f"    profile.md：{m.profile.facts()}")
    check("写进了长期记忆", any("20" in f for f in m.profile.facts()))

    volume.set_volume(50)
    m = fresh(resume=False)  # 重启，而且不带短期记忆 → 只能靠长期记忆
    say(m, "现在是晚上了，按我的习惯调一下音量")
    check("重启后按习惯调到 20%", volume.get_volume()["volume"] == 20)

    say(m, "把晚上音量那条记忆忘掉")
    print(f"    profile.md：{m.profile.facts()}")
    check("忘掉了", not any("20" in f for f in m.profile.facts()))

    # ------------------------------------------------------------------
    print("===== D. 回头查历史 =====")
    m = fresh(clear=True, resume=False)
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    m.history.append({
        "time": yesterday.isoformat(timespec="seconds"),
        "you": "把音量调到35",
        "actions": [{"tool": "set_volume", "args": {"percent": 35},
                     "result": {"volume": 35, "muted": False, "message": "音量已从 20% 设为 35%"}}],
        "reply": "音量已从 20% 设为 35%",
    })
    print(f"    （事先放了一条“昨天 22:00 把音量调到 35”的历史记录）")

    m = fresh(resume=False)
    r = say(m, "我昨天晚上把音量调到多少了？")
    check("查了历史，回答里有 35",
          any(a["tool"] == "search_history" for a in r["actions"]) and "35" in r["reply"])

    volume.set_volume(15)
    m = fresh(resume=False)
    say(m, "把音量调到我昨天晚上用的那个")
    check("查完历史后调到 35%", volume.get_volume()["volume"] == 35)

finally:
    volume.set_volume(original["volume"])
    if original["muted"]:
        volume.mute()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"已恢复：{volume.get_volume()['message']}；临时记忆文件夹已删除")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
