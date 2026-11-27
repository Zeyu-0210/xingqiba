"""
test_delegate.py —— 测试“秘书把任务交给干活的 AI”

    1. 秘书分派得对不对：作业交给 Claude、点名 DeepSeek/豆包/Gemini 就交给它们、
       调音量和闲聊不交出去
    2. 转述全不全：你说的要求有没有漏
    3. 任务文件夹和“任务说明.md”写得对不对
    4. 启动新窗口的机制能不能用（用 Python 假装成 Claude，确认它在任务文件夹里、收到了中文指令）

用“演练模式”（不真的打开 Claude 和网页），任务文件夹建在 tests\\_tmp_tasks，测完删除。
会改变音量（调到 30%），测完恢复。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_delegate.py
"""

import json
import os
import shutil
import sys
import time

import assistant
import delegate
import memory
import volume
import workers
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP_TASKS = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_tasks")
TMP_MEMORY = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_memory_delegate")
delegate.DRY_RUN = True
workers.TASKS_DIR = TMP_TASKS
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'[通过]' if ok else '[失败]'} {name}  {detail}")


original = volume.get_volume()
for d in (TMP_TASKS, TMP_MEMORY):
    shutil.rmtree(d, ignore_errors=True)
print(f"测试前：{original['message']}")
print(f"加载模型……用时 {assistant.warm_up():.1f} 秒\n")

# (你说的话, 应该交给谁（None = 不该交出去）, 转述里必须包含的词)
CASES = [
    ("帮我把这周的数学作业做了，一共十道题，每道都要写详细步骤，周五之前交", "Claude", [["十", "10"], ["步骤"], ["周五"]]),
    ("Cloud 帮我写一个贪吃蛇小游戏，用 Python 写", "Claude", [["贪吃蛇"], ["Python", "python"]]),
    ("让 DeepSeek 帮我查一下明天北京的天气", "DeepSeek", [["北京"], ["天气"]]),
    ("用豆包帮我写一段给妈妈的生日祝福，五十个字左右", "豆包", [["生日"], ["妈妈", "母亲"], ["五十", "50"]]),
    ("让 Gemini 帮我把这篇英语作文改一下语法错误", "Gemini", [["语法"]]),
    ("音量调到30", None, []),
    ("给我讲个笑话", None, []),
]

try:
    print("===== 1~3. 分派、转述、任务文件夹 =====")
    for text, expect_worker, must_have in CASES:
        mem = memory.Memory(TMP_MEMORY, resume=False)
        r = assistant.handle(text, mem)
        calls = [a for a in r["actions"] if a["tool"] == "delegate_task"]
        got = calls[0]["result"].get("worker") if calls else None
        print(f"    你：{text}")
        if calls:
            print(f"      → 交给 {got}，任务：{calls[0]['args'].get('task')}")
        else:
            tools = "、".join(a["tool"] for a in r["actions"]) or "（无）"
            print(f"      → 没有交出去，工具：{tools}，回复：{r['reply'][:40]}")
        check("交给的对象对", got == expect_worker, f"（应为 {expect_worker or '不交出去'}，实际 {got or '没交出去'}）")

        if calls and must_have:
            task = calls[0]["args"].get("task", "")
            missing = [group[0] for group in must_have if not any(w in task for w in group)]
            check("转述没有漏掉要求", not missing, f"漏了：{missing}" if missing else "")

        folder = calls[0]["result"].get("folder") if calls else None
        if folder:
            with open(os.path.join(folder, delegate.TASK_FILE), encoding="utf-8") as f:
                content = f.read()
            check("任务文件夹和任务说明.md 都建好了，里面有原话",
                  text in content and calls[0]["args"]["task"] in content, f"→ {os.path.basename(folder)}")
        print()

    print("===== 4. 启动新窗口的机制 =====")
    folder = delegate.make_task_folder("窗口测试", __import__("datetime").datetime.now())
    fake_claude = [sys.executable, "-c",
                   "import os,sys; open('收到的指令.txt','w',encoding='utf-8').write(os.getcwd()+'\\n'+sys.argv[1])"]
    delegate.launch_in_new_window(fake_claude + [delegate.CLI_PROMPT], folder, keep_open=False)
    out = os.path.join(folder, "收到的指令.txt")
    for _ in range(50):
        if os.path.exists(out):
            break
        time.sleep(0.2)
    got = open(out, encoding="utf-8").read().splitlines() if os.path.exists(out) else ["", ""]
    check("新窗口在任务文件夹里启动", os.path.normcase(got[0]) == os.path.normcase(folder), f"→ {got[0]}")
    check("收到了完整的中文指令", got[1] == delegate.CLI_PROMPT, f"→ {got[1]}")

finally:
    volume.set_volume(original["volume"])
    if original["muted"]:
        volume.mute()
    for d in (TMP_TASKS, TMP_MEMORY):
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n已恢复：{volume.get_volume()['message']}；临时文件夹已删除")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
