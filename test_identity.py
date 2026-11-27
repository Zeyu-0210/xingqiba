"""
test_identity.py —— 测试助手知不知道自己叫“星期八”、会不会乱说“记下了”、回答够不够短

（之前对话里说“你叫星期八”，名字被程序误删了，模型就说自己叫 Gemma 4）

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_identity.py
用临时记忆文件夹，测完删除。
"""

import os
import re
import shutil

import assistant
import memory
import takeover

takeover.available = lambda: False  # 测试本地流程：本地做不成时不自动去找真的 Claude（不花额度）

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_identity")
WRONG_NAMES = ["Gemma", "gemma", "GEMS", "Google", "谷歌"]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def ask(text, first=True):
    r = assistant.handle(text, memory.Memory(TMP, resume=False), first=first)
    tools = [a["tool"] for a in r["actions"]]
    print(f"    你：{text}\n      工具：{tools or '（无）'}  回复：{r['reply']}")
    return r, tools


shutil.rmtree(TMP, ignore_errors=True)
assistant.warm_up()
try:
    for question in ("你叫什么名字", "你是谁", "介绍一下你自己"):
        r, _ = ask(question)
        check(f"问“{question}”说自己是星期八", "星期八" in r["reply"] and not any(w in r["reply"] for w in WRONG_NAMES))
        check("回答够短（念出来不会太久）", len(r["reply"]) <= 60, f"{len(r['reply'])} 个字")

    # 之前出问题的那几句（现在“星期八”不会再被删掉了）
    for said in ("你的名字叫星期八", "记住一点，你叫星期八，不叫Gemma"):
        r, tools = ask(said, first=False)
        claims = any(w in r["reply"] for w in ("记住了", "记下了", "已经记"))
        # “我叫Gemma”“名字就是Gemma”算错；“不叫Gemma”是对的
        claims_wrong = re.search(r"(?<!不)(叫|是|名字就是)\s*(" + "|".join(WRONG_NAMES) + ")", r["reply"])
        check(f"“{said}”：不会说自己叫 Gemma", not claims_wrong)
        check("说“记下了”的话，必须真的调用了 remember", not claims or "remember" in tools,
              f"说了记下：{claims}，调用了：{tools}")

    r, _ = ask("你底层用的是什么模型")
    check("专门问底层模型时，可以说是 gemma4", "gemma" in r["reply"].lower() or "Gemma" in r["reply"])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
