"""
test_review.py —— 测试“持续记录 + 复盘”：
    1. 唤醒后，模型能不能分清“对助手说的”和“闲聊 / 队友语音 / 游戏声音”
    2. 复盘时，闲聊有没有被删掉、有用的信息有没有留下、新习惯有没有记住

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_review.py

用临时记忆文件夹 tests\\_tmp_review，测完删除；会改变音量（最高 30%），测完恢复。
"""

import json
import os
import shutil
from datetime import datetime

import assistant
import memory
import volume
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_review")
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'[通过]' if ok else '[失败]'} {name}  {detail}\n")


original = volume.get_volume()
shutil.rmtree(TMP, ignore_errors=True)
print(f"测试前：{original['message']}")
print(f"加载模型……用时 {assistant.warm_up():.1f} 秒\n")

try:
    print("===== 1. 分清“对助手说的”和“闲聊” =====")
    m = memory.Memory(TMP, resume=False)
    volume.set_volume(20)
    # (说的话, 是不是唤醒后第一句, 应该被当成对助手说的吗)
    lines = [
        ("音量调到30", True, True),
        ("快快快，去B点，B点有人", False, False),
        ("这把我们赢定了哈哈", False, False),
        ("敌人在中路，小心狙击", False, False),
        ("帮我把音量调小一点", False, True),
        ("兄弟你那边网卡不卡", False, False),
        ("现在音量是多少", False, True),
        # 电脑正在播放的声音（就是试听样音里的话，之前被误当成了用户）
        ("你好，我是小姨。已经帮您把音量调到30%了，明天下午3点您还有个会议，别忘了。", False, False),
        ("你好，我是黎线女生。", False, False),
    ]
    correct = 0
    for text, first, should_address in lines:
        r = assistant.handle(text, m, first=first)
        got = not r["ignored"] and not r["uncertain"]
        correct += got == should_address
        tools = "、".join(a["tool"] for a in r["actions"]) or "（无）"
        where = "对助手说的（执行）" if got else ("拿不准（放进暂存区）" if r["uncertain"] else "闲聊（放进暂存区）")
        print(f"    {'✓' if got == should_address else '✗'} {text:<16} → {where}  工具：{tools}")
    check(f"分对了 {correct}/{len(lines)} 句", correct == len(lines))
    check(f"暂存区里有 {len(m.pending)} 句闲聊和播放的声音，没有写进硬盘", len(m.pending) == 6)
    check("指令照常执行（30% 再调小 10 = 20%）", volume.get_volume()["volume"] == 20)
    check("播放的“我是某某”没有被记进长期记忆", not m.profile.facts(), f"长期记忆：{m.profile.facts()}")

    # 唤醒后第一句（一定当成对助手说的）就是一句“我是某某”：也不能直接记，要先问
    m2 = memory.Memory(TMP + "_2", resume=False)
    r = assistant.handle("你好，我是黎线女生。", m2, first=True)
    print(f"    唤醒后第一句就是“你好，我是黎线女生。” → 工具：{[a['tool'] for a in r['actions']] or '（无）'}，"
          f"回复：{r['reply']}")
    check("没说“记住”就不写进长期记忆", not m2.profile.facts(), f"长期记忆：{m2.profile.facts()}")
    shutil.rmtree(TMP + "_2", ignore_errors=True)

    print("===== 2. 复盘 =====")
    # 再往暂存区放两句“有用”的话，看复盘能不能挑出来
    now = datetime.now()
    m.add_pending("我以后打游戏的时候音量都开到四十，太大声会吵到室友", now)
    m.add_pending("明天下午三点要去开会，别忘了", now)
    print(f"    暂存区：{[p['you'] for p in m.pending]}")

    result = assistant.review(m)
    print(f"    复盘结果：{result['message']}")

    history_text = ""
    for name in os.listdir(os.path.join(TMP, "history")):
        with open(os.path.join(TMP, "history", name), encoding="utf-8") as f:
            history_text += f.read()
    kept = [f"{json.loads(line)['you']}  ← {json.loads(line)['note']}" for line in history_text.splitlines()
            if line.strip() and json.loads(line).get("note", "").startswith("复盘时保留")]
    print(f"    复盘后留在历史记录里的闲聊：{kept}")
    print(f"    长期记忆：{m.profile.facts()}")

    chatter = ["快快快", "赢定了", "小心狙击", "网卡不卡", "我是小姨", "黎线女生"]
    check("闲聊、队友的话、播放的声音都被删掉，没有写进硬盘", not any(c in history_text for c in chatter))
    check("记住了新习惯：打游戏时音量开到 40", any("40" in f or "四十" in f for f in m.profile.facts()))
    check("没有从播放的声音里瞎猜习惯（比如“音量 30%”）",
          not any("30" in f or "三十" in f or "黎线" in f for f in m.profile.facts()))
    check("开会的安排被留了下来", any("开会" in k for k in kept))
    check("暂存区已清空", not m.pending)

finally:
    volume.set_volume(original["volume"])
    if original["muted"]:
        volume.mute()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"已恢复：{volume.get_volume()['message']}；临时记忆文件夹已删除")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
