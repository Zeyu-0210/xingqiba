"""
test_research.py —— 测试“夜间研究”：研究记录、早上提一句、“昨晚研究了什么”
    （2026-11-21 删掉了“门卫”，原来测门卫夜间模式的第 1 部分一起删了）

    不花额度：Claude 用假的。加 --smoke 会再真的跑一次小测试（只看一个项目的 README，最多 1 美元）。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_research.py
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta

import assistant
import claude_models
import memory
import research
import takeover

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_research")
REPORTS = os.path.join(TMP, "日报")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(REPORTS)
takeover.available = lambda: False
claude_models.USAGE_FILE = os.path.join(TMP, "usage.json")
research.REPORT_DIR = REPORTS
research.STATE_DIR = os.path.join(TMP, "state")
research.STATE_FILE = os.path.join(TMP, "state", "state.json")
research.MORNING_FILE = os.path.join(TMP, "state", "morning.json")
research.LOCK_FILE = os.path.join(TMP, "state", "running.lock")


print("===== 1. 研究一次（假 Claude）：写日报、记下研究过的、早上要提的那句话 =====")
prompts = []


def fake_claude(prompt, report_dir, budget):
    prompts.append((prompt, budget))
    day = f"{datetime.now():%Y-%m-%d}"
    with open(os.path.join(report_dir, f"{day}.md"), "w", encoding="utf-8") as f:
        f.write("# 研究日报（假的）\n")
    with open(os.path.join(report_dir, f"{day}.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": ["Pipecat"], "summary": "找到三个值得借鉴的点", "report": "x",
                   "top": ["加思考提示音", "支持打断", "用 smart-turn 断句"]}, f, ensure_ascii=False)
    os.makedirs(os.path.join(report_dir, "_下载", "pipecat-main"), exist_ok=True)  # 假装下载了源码
    return 0.42, ""


r = research.run(runner=fake_claude)
check("跑完：有日报、有总结", r["ok"] and r["projects"] == ["Pipecat"] and len(r["top"]) == 3)
prompt, budget = prompts[-1]
check("交给 Claude 的说明里：带着已经研究过的 LiveKit，不让它重复",
      "LiveKit Agents" in prompt.split("【候选项目】")[0] and "Pipecat" in prompt.split("【候选项目】")[1])
check("每晚花费上限 5 美元", budget == research.BUDGET_USD == 5.0)
check("下载的源码研究完就清理掉", not os.path.exists(os.path.join(REPORTS, "_下载")))
check("记下 Pipecat 研究过了", any(c["name"] == "Pipecat" for c in research.state()["covered"]))
research.run(runner=fake_claude)
check("第二次：候选里不再有 Pipecat", "pipecat-ai" not in prompts[-1][0])
check("花费记进了用量（算在 Opus 上）", claude_models.today()["models"].get("opus", {}).get("runs") == 2)

print("\n===== 2. 早上第一次叫醒时提一句，之后不再重复 =====")
news = research.morning_news()
check("有昨晚的研究要提", news and news["projects"] == ["Pipecat"])
research.mark_spoken()
check("提过一次就不再提", research.morning_news() is None)
old = json.load(open(research.MORNING_FILE, encoding="utf-8"))
old.update(spoken=False, time=(datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"))
json.dump(old, open(research.MORNING_FILE, "w", encoding="utf-8"), ensure_ascii=False)
check("三天前的研究，早上就不提了", research.morning_news() is None)

print("\n===== 3. 问“昨晚研究了什么”：本地直接念（不用模型、不花钱）=====")
m = memory.Memory(os.path.join(TMP, "memory"), resume=False)
r = assistant.handle("星期八，昨晚研究了什么", m)
check("念出项目、总结和最值得做的三条", "Pipecat" in r["reply"] and "思考提示音" in r["reply"]
      and r["seconds"] == 0.0 and not r["actions"], f"→ {r['reply'][:70]}…")

print("\n===== 4. 假 Claude 没写出报告：不算研究过，也不提 =====")


def broken_claude(prompt, report_dir, budget):
    return 0.1, "额度用完了"


os.remove(research.MORNING_FILE)
r = research.run(runner=broken_claude)
check("没完成：说明原因，不记成研究过，早上不提", not r["ok"] and r["error"] == "额度用完了"
      and research.morning_news() is None)
log_text = open(os.path.join(REPORTS, "运行记录.md"), encoding="utf-8").read()
check("运行记录里写清楚了", "没完成：额度用完了" in log_text)

if "--smoke" in sys.argv:
    print("\n===== 5. 真的跑一次小测试（Opus 超高，最多 1 美元）=====")
    research.REPORT_DIR = os.path.join(r"D:\AI任务", "星期八研究日报")
    claude_models.USAGE_FILE = os.path.join(memory.PROJECT_DIR, "logs", "claude_usage.json")  # 真的用量要记上
    r = research.run(smoke=True)
    check("小测试跑通：Claude 下载读了资料、写了报告", r["ok"],
          f"→ {r.get('summary')}（约 {r.get('cost') or 0:.2f} 美元，{r.get('minutes')} 分钟）{r.get('error', '')}")
    if r["ok"]:
        print("  报告内容：\n    " + open(r["report"], encoding="utf-8").read().strip().replace("\n", "\n    "))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{sum(results)}/{len(results)} 项通过")
