"""
groupchat.py —— AI 群聊：把几个 AI 拉进一个群，你说一句，在群里的 AI 各回一句（像微信 / WhatsApp 群）

    成员：Claude（用你登录的 Claude 账号，或 API 密钥）、GPT、本地 Gemma（免费不联网）、
          DeepSeek、豆包、通义千问、Kimi、智谱 GLM、Gemini，或者任何“兼容 OpenAI 接口”的服务
    操作：拉进群 / 移出群（暂时不让它说话，设置还留着）/ 删除成员 / 改模型和密钥
    @某人：只让被 @ 的那几个回答（比如“@Claude 你怎么看”“问你呢 GPT”）；不 @ 就是群里所有 AI 轮流回
    轮流回：后说的能看到先说的，可以接话；AI 在话里 @ 了另一个 AI，被 @ 的会接着回它（每句话之后最多几次）
    “接着聊一轮”：让每个 AI 看看别人刚才说了什么，再各说一句（可以补充、反驳）

    权限（像你单独用 Claude Code / Codex 桌面版一样）：
        Claude、GPT 每次回答都能看电脑上的文件（星期八的代码看最新副本），不用特别说“去看看”
        “执行权”= 能改东西的权限，同一时间只给一个 AI，只有你能给、能收回：
            “@Claude 全权接管电脑”“让 GPT 执行”，或者点群聊里的“交给某某” → 执行权交给它
            有执行权的那个：你说的话它像桌面版一样直接干（运行命令、改文件……）；其他 AI 只能看、不能改
            AI 之间互相 @ 时，谁都只看不改（只有你的话能让它动手）
        “你们讨论一下怎么做，讨论完让 Claude 去执行” → 大家先轮流讨论两轮，然后 Claude 按讨论出的方案动手
        “收回执行权”“取消接管”         → 收回，大家都只能看
    Claude 用你登录的 Claude Code；GPT 用你登录的 ChatGPT 账号（Codex）；其他 AI 用 agent_hands.py 里的“手”。
    没有程序拦（2026-11-21 按你的要求删掉了“门卫”）：权限和你单独用它们的桌面版一样，
        只在说明里叮嘱它们：删除你的文件、格式化这类改不回来的事，先在群里问你一句“确定吗？”，你回“确定”它才做

    数据存在项目里的 groupchat 文件夹：members.json（成员）、history.jsonl（聊天记录）。
    密钥存在 api_keys.json（和“大脑”用的同一个文件，只在你电脑上）。
    注意：发到群里的话，会发给群里每一个云端 AI 的服务器。
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime

import status
from run_control import RunControl, RunStopped

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "groupchat")
MEMBERS_FILE = os.path.join(DATA_DIR, "members.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
CONTEXT_MESSAGES = 30     # 每个 AI 回答时，看最近多少条群消息
TIMEOUT = 120             # 一个 AI 最多等多久
USER_NAME = "我"

# 可以拉进群的 AI（预设）。模型名都能在界面里改；标了“要填”的，得先去对应平台开通拿到模型名
PRESETS = {
    "claude": {"name": "Claude", "kind": "claude", "model": "sonnet", "color": "#D97757",
               "about": "用你登录的 Claude 账号（会员额度）；填了 Anthropic 密钥就按用量付费"},
    "codex": {"name": "GPT", "kind": "codex", "model": "", "color": "#10A37F",
              "about": "用你登录的 ChatGPT 账号（借 Codex 桌面版），走会员额度，不用填密钥。模型留空 = ChatGPT 默认"},
    "gpt": {"name": "GPT（API）", "kind": "openai", "api": "responses", "model": "gpt-6.2-sol", "key": "openai",
            "color": "#0E8A6B", "about": "OpenAI 接口，按用量付费，要填 OpenAI 密钥"},
    "gemma": {"name": "本地 Gemma", "kind": "ollama", "model": "gemma4:12b", "color": "#4F8DF7",
              "about": "就是星期八自己的本地模型：免费、不联网（会占显存）"},
    "deepseek": {"name": "DeepSeek", "kind": "openai", "api": "chat", "base_url": "https://api.deepseek.com",
                 "model": "deepseek-chat", "key": "deepseek", "color": "#4D6BFE",
                 "about": "深度求索，便宜，platform.deepseek.com 申请密钥"},
    "doubao": {"name": "豆包", "kind": "openai", "api": "chat",
               "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "", "key": "doubao",
               "color": "#3C8CFF", "about": "字节火山方舟：开通模型后，把模型名（或接入点 ID）填进“模型”"},
    "qwen": {"name": "通义千问", "kind": "openai", "api": "chat",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "key": "qwen",
             "color": "#615CED", "about": "阿里百炼，bailian.console.aliyun.com 申请密钥"},
    "kimi": {"name": "Kimi", "kind": "openai", "api": "chat", "base_url": "https://api.moonshot.cn/v1",
             "model": "kimi-latest", "key": "kimi", "color": "#7C83FD", "about": "月之暗面，platform.moonshot.cn 申请密钥"},
    "glm": {"name": "智谱 GLM", "kind": "openai", "api": "chat", "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4-flash", "key": "zhipu", "color": "#3859FF", "about": "智谱开放平台，有免费模型"},
    "gemini": {"name": "Gemini", "kind": "openai", "api": "chat",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.5-flash",
               "key": "gemini", "color": "#8E75F2", "about": "Google，aistudio.google.com 申请密钥（国内要特殊网络）"},
    "custom": {"name": "自定义 AI", "kind": "openai", "api": "chat", "base_url": "", "model": "", "key": "",
               "color": "#888888", "about": "任何兼容 OpenAI 接口的服务：填地址、模型名和密钥"},
}

# 第一次打开群聊时默认有的成员（Claude、GPT 都用你登录的账号）
DEFAULT_MEMBERS = [("claude", True), ("codex", True), ("gemma", True), ("deepseek", False)]

SYSTEM_PROMPT = """你在一个像微信群一样的群聊里，群名叫“AI 群聊”。
群里有：用户（群主，名字显示为“我”）{others}。你是“{name}”。
回复规则：
- 像在群里聊天一样说话：简短自然，一般 1～4 句；用户要求详细时再写长
- 用简体中文（用户用别的语言时跟着用）
- 可以接别人的话，赞同或者反驳其他 AI，但不要替别人发言；想让另一个 AI 回答你，就在话里写“@它的名字”
- 不要在开头写自己的名字，也不要模仿“【某某】：”这种格式
- 你这次只能聊天，不能操作电脑，绝对不要说“我已经做了”这类话。
  能改东西的“执行权”同一时间只给一个 AI，只有用户能指定，现在在：{hands}。
  讨论怎么做时，可以出主意、写清楚步骤和命令，方便有执行权的成员去做。
你的职责：{role}
{project_line}

【群聊长期记忆（秘书笔记）】
{memory}"""

# Claude、GPT（用你登录的账号）的说明：它们每次回答都能用工具（像你单独用它们的桌面版一样）
AGENT_PROMPT = """你是“{name}”，在用户的“AI 群聊”里（像微信群一样）。群里有：用户（群主，显示为“我”）{others}。
这个群就像一个小公司：用户是老板，几个 AI 是同事，分工合作把他的事做好；项目的进度都在这个群里推进。
你的职责：{role}
你用的模型：{model}（用户在星期八的“设置”里选，你自己换不了）。
你的权限：{power}
执行权：能改东西的“执行权”同一时间只给一个 AI，只有用户能给、能收回（他说“让某某执行”，或者点群聊里的按钮）；AI 自己不能拿，也不能转交。现在执行权在：{hands}。
用户的电脑是 Windows 11。语音助手“星期八”（Python 写的）的程序在 {project}。{where}
{project_line}

【群聊长期记忆（秘书笔记：自动整理，用户能看能改）】
{memory}

去哪儿翻以前的事：
- 更早的完整群聊记录：{history}（history*.jsonl，每行一条消息）
- 用户和桌面版 Claude 的聊天记录：{claude_records}（*.jsonl，很大，用搜索找关键词，别整个读）
- 用户和 Codex（GPT）的聊天记录：{codex_records}
规矩：
- 不知道的先去看（看文件、翻记录、搜一搜），没看过的别说得像看过一样；不要编造进度和结果。
- 想让群里另一个 AI 回答你，就在话里写“@它的名字”（比如 @{example}），它会接着回你；不需要它回就别 @。
{privacy}
- 像在群里说话：简短自然，一般 1～5 句，用户要详细再写长；用简体中文；不要在开头写自己的名字；不用列“Sources/来源”。
现在时间：{now}"""
POWER_LOOK = ("只能看、不能改。你可以{tools}；不能改文件，也不能运行会改东西的命令。"
              "用户要你动手改东西时，告诉他执行权现在在谁手里；想让你来做，请他说“让{name}执行”或者点群里的“交给{name}”。")
POWER_LOOK_MINE = "执行权在你手里，但这次是群里的 AI 在问你（不是用户让你做事），所以这次只看不改：你可以{tools}，回答它就好。"
PROJECT_WORK_RULES = """- 单次动手上限 15 分钟。每次只做 10 分钟内能收尾的一小步，留出时间核对和汇报。
- 找代码先用 Select-String（这里没有 rg）；没有命令行时用搜索工具。定位后只读相关的几十行。
- 同一种做法失败两次就换方法；换方法仍失败就停下汇报。
- 不建备份副本，不逐步请示群主。改动需要重启星期八才生效时，汇报写明“需要重启”，由群主重启。"""
POWER_REVIEW = """这次你当检查员：只看不改。你可以{tools}。
- 项目分工是 GPT 写代码并跑测试，Claude 负责复查。
- 只看本次实际改动的代码片段和 GPT 已有的测试结果。不要通读文件、扩大检查范围或重跑整套测试。
- 复查结论最多 200 字。通过就只写“通过”；发现问题就以“⚠️ 有问题”开头，指出文件、位置和具体问题。
{work_rules}"""
POWER_DO = """执行权在你手里：你可以像在桌面版里一样动手（运行命令、读写文件……），能动整台电脑。
- 用户明确让你做事（改、修、写、建、装、执行、整理……）就去做：一步一步来，每步看结果再决定下一步，做完简短汇报做了什么、结果怎样；没做成就照实说。
- 项目分工是 GPT 写代码并跑测试，Claude 只做复查。
- 这一步结束前，汇报改了哪些文件、测试结果、下一步和卡点；无卡点就写“无”。
{work_rules}
- 用户只是聊天、提问、讨论，或者说了“只看不做”：只看不改，直接回答。
{safety}
- 要操作软件界面时，可以像人一样操控屏幕（真的在用户屏幕上动鼠标、打字，他看得到，随时能接管）：{desktop}
  动之前先截图看清楚，动完再截图确认结果；能用命令或文件做到的，优先用命令（更快更稳）。
- 你自己生成的文件默认放在 {work_dir}。"""
SAFETY = ("- 你的权限和用户单独用你的桌面版一样，直接做（包括改星期八自己的程序）。只有不可挽回的操作"
          "（删除用户的文件、格式化、改注册表、关机重启、卸载软件）先用一句话问用户“确定吗？”，然后结束这一轮，等他说“确定”再做。")
PRIVACY = "- 密钥、口令、登录凭证的原文不要贴到群里。"
DESKTOP_CTL = ("运行 `{python} {project}\\desktop_ctl.py screenshot` 截图（会告诉你图片存在哪），看图片，"
               "再用 `click-xy X Y`（图片上的坐标）、`type \"文字\" enter`、`keys ctrl+s`、`scroll down 5`、"
               "`read`（列出窗口里的按钮、输入框）、`click 编号`、`windows`、`open 软件名` 操作（`help` 看全部用法）")
DESKTOP_TEXT = {"claude": "用 Read 看截图图片。" + DESKTOP_CTL + "；网页也可以用你的 Chrome 工具。",
                "codex": "用你自带的操控屏幕工具（computer use）；或者 " + DESKTOP_CTL + "，截图用 view_image 看。"}
LOOK_TOOLS_TEXT = {"claude": "看电脑上的文件、找文件、在文件里搜、上网查",
                   "codex": "看电脑上的文件、运行只看不改的命令（列目录、搜内容、查状态）"}
CLAUDE_LOOK_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch"   # Claude 只看时给的工具（没有命令行、不能写文件）

AT_HINT = "（{name} 在群里 @ 了你，请回应它。）"
AI_CHAIN_LIMIT = 4   # 你说一句话之后，AI 之间最多互相 @ 着接几次话（免得没完没了、费额度）
# 不 @ 也能叫到：话的开头或结尾叫了名字（“问你呢gpt”“Claude，你看看”）。同一种 AI 只有一个时才认这些叫法
ALIASES = {"codex": ("GPT", "ChatGPT", "Codex"), "claude": ("Claude", "克劳德"), "ollama": ("Gemma",)}

DISCUSS_HINTS = [
    "（用户让大家先讨论这件事怎么做，现在还不动手。请提出你的方案，或者补充、纠正别人的方案。需要的话先去看看相关文件。"
    "简短具体，可以写出关键命令。）",
    "（请看看大家的方案，取长补短，用几条简短的步骤写出你认为最终该怎么做。）",
]
EXEC_AFTER_DISCUSS = "（讨论结束了。现在轮到你动手：按群里讨论出的最好的方案，把用户要的事情做完。）"

# 协作：一个动手做完，另一个当“检查员”看一遍；有问题，动手的那个按意见再改一次
REVIEW_HINT = ("（{executor} 的本次改动和测试结果见最后的汇报。{files}"
               "只核对这次改动的代码片段和已有测试结果，不写代码、不重跑整套测试。"
               "结论最多 200 字，无问题只回“通过”；有问题以“⚠️ 有问题”开头，并指出具体位置。）")
FIX_HINT = "（{reviewer} 检查了你刚才做的事，提出了问题（见上面）。请按它的意见修正，做完简短汇报。）"

# 群里的话是不是在让某个 AI 接管 / 动手 / 收回
TAKEOVER_RE = re.compile(r"接管|执行权|操控(一下)?(我的)?电脑|控制(一下)?(我的)?电脑")
RELEASE_RE = re.compile(r"(取消|退出|结束|停止|不用|别再?|不要再?)\s*(接管|动手|操作电脑|控制电脑|碰(我的)?电脑)"
                        r"|收回(执行权|权限|接管)|(取消|撤销|拿回)(执行权|权限)")
STOP_RE = re.compile(
    r"(?:(?:@\S+\s+|@?(?:Claude|GPT|ChatGPT|Codex|克劳德)|你们|大家)[\s，,:：]*)?"
    r"(?:(?:请|先|马上|立即|现在|都)\s*)*(?:强制\s*|强)?"
    r"(?:停止|停下|停|中止|终止|截止)\s*(?:一下|下来|掉)?\s*"
    r"(?:当前任务|群聊任务|所有任务|任务|执行|@?(?:Claude|GPT|Codex))?\s*[吧了]?", re.I)
EXEC_RE = re.compile(r"执行|动手|实施|落实|操作一下|干活|搞定|去做|做吧|开干|弄一下|处理一下")
DISCUSS_RE = re.compile(r"讨论|商量|研究一下|一起想想|出个方案|想个办法|聊聊怎么")
# 让大家先看看项目 / 代码（只读：看的是项目的代码副本，不含记忆、日志、密钥）
LOOK_RE = re.compile(r"(看看|看一下|看一看|了解|熟悉|读一下|读读|研究一下|分析一下|过一遍|摸清|搞清楚).{0,15}"
                     r"(项目|代码|源码|程序|文件|星期八|仓库)|(项目|代码|源码).{0,6}(看看|了解|熟悉|读一下|研究)")
LOOK_HINT = "（用户让你先去看看项目代码。按他的要求挑相关的文件看，看完在群里说说你的理解和注意到的问题。）"
REMEMBER_RE = re.compile(r"^\s*(你们|大家)?\s*(也|都)?\s*(要)?(记住|记一下|记下来|帮我记(住|一下|下来)?)\s*[:：，,。]?\s*")
DISPATCH_RE = re.compile(r"^调度\s*[:：]\s*")
PENDING_LIST_RE = re.compile(r"^\s*(待办|挂起的?任务|看看待办)\s*[?？。!！]?\s*$")
PENDING_CANCEL_RE = re.compile(r"^\s*取消待办\s*(?:第\s*)?(\d+)\s*(?:条|个)?\s*$")
ASSIGN_WORDS = r"(让|叫|由|交给|请)\s*@?"
DO_WORDS = r"\s*(来|去|负责|帮我|帮忙|你)?\s*(全权|全员)?\s*(执行|动手|做|操作|接管|干|搞|处理|实施)"

_lock = threading.Lock()
_busy = {}   # 成员 id → 正在打字（或正在动手）
_exec_lock = threading.Lock()   # 同一时间只让一个 AI 动手
_awaiting = {"member": None}    # 在群里问你“确定吗”、等你回答的那个执行者
_run_lock = threading.RLock()
_runs = set()
_run_generation = 0  # 停止后，弃掉停止前还在准备的轮次


# ---------------------------------------------------------------------------
# 成员
# ---------------------------------------------------------------------------


def _new_member(preset_id, enabled=True):
    preset = PRESETS[preset_id]
    member = {k: v for k, v in preset.items() if k != "about"}
    member.update(id=uuid.uuid4().hex[:8], preset=preset_id, enabled=enabled)
    return member


def members():
    """所有成员（包括暂时移出群的）。第一次用时建好默认成员。"""
    with _lock:
        if not os.path.exists(MEMBERS_FILE):
            os.makedirs(DATA_DIR, exist_ok=True)
            data = [_new_member(p, on) for p, on in DEFAULT_MEMBERS]
            _save_members(data)
            return data
        try:
            with open(MEMBERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if sys.platform == "darwin":
                for member in data:
                    member["hands"] = False
            return data
        except (OSError, ValueError):
            return []


def _save_members(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MEMBERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMBERS_FILE)


def find_member(member_id):
    return next((m for m in members() if m["id"] == member_id), None)


def add_member(preset_id):
    if preset_id not in PRESETS:
        return {"ok": False, "message": f"没有这种 AI：{preset_id}"}
    data = members()
    member = _new_member(preset_id)
    names = {m["name"] for m in data}
    base, n = member["name"], 2
    while member["name"] in names:  # 同一种拉两个：Claude 2、Claude 3……
        member["name"], n = f"{base} {n}", n + 1
    data.append(member)
    with _lock:
        _save_members(data)
    _publish({"type": "group_members"})
    return {"ok": True, "member": member, "message": f"已把 {member['name']} 拉进群"}


EDITABLE = {"name", "model", "base_url", "api", "enabled", "color", "key", "effort", "role"}


def update_member(member_id, changes):
    data = members()
    for m in data:
        if m["id"] == member_id:
            for k, v in (changes or {}).items():
                if k in EDITABLE:
                    m[k] = v.strip() if isinstance(v, str) else v
            if m.get("preset") == "custom" and not m.get("key"):
                m["key"] = f"custom_{m['id']}"  # 自定义 AI 各用各的密钥
            with _lock:
                _save_members(data)
            _publish({"type": "group_members"})
            return {"ok": True, "member": m}
    return {"ok": False, "message": "没找到这个成员"}


def remove_member(member_id):
    data = members()
    left = [m for m in data if m["id"] != member_id]
    if len(left) == len(data):
        return {"ok": False, "message": "没找到这个成员"}
    with _lock:
        _save_members(left)
    _publish({"type": "group_members"})
    return {"ok": True}


def readiness(member, *, cached_login=False):
    """这个成员现在能不能说话：(能不能, 缺什么)。"""
    kind = member.get("kind")
    if kind == "ollama":
        return True, ""
    if kind == "codex":
        import agent_codex
        if cached_login:
            login = agent_codex.cached_login()
            if login is None:
                return False, "正在检查 ChatGPT 登录状态"
            installed, logged_in = login
        else:
            installed = bool(agent_codex.find_exe())
            logged_in = installed and agent_codex.logged_in()
        if not installed:
            return False, "电脑上没找到 Codex（装 Codex 桌面版就有）"
        return (True, "") if logged_in else (False, "还没登录 ChatGPT 账号（打开 Codex 桌面版登录一次）")
    if not member.get("model"):
        return False, "还没填模型名"
    if kind == "claude":
        if _key("anthropic"):
            return True, ""
        import brain_claude_code
        if cached_login:
            login = brain_claude_code.cached_login()
            if login is None:
                return False, "正在检查 Claude 登录状态"
            logged_in = login.logged_in
        else:
            logged_in = brain_claude_code.logged_in()
        return (True, "") if logged_in else (False, "还没登录 Claude 账号（或填 Anthropic 密钥）")
    if kind == "openai":
        if member.get("preset") == "custom" and not member.get("base_url"):
            return False, "还没填服务地址"
        return (True, "") if _key(member.get("key", "")) else (False, "还没填密钥")
    return False, "不认识的类型"


def can_execute(member):
    """这个成员能不能动手（接管电脑）：(能不能, 缺什么)。"""
    ok, why = readiness(member)
    if not ok:
        return False, why
    return _execution_readiness(member)


def _execution_readiness(member, *, cached_login=False):
    if sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return False, MAC_UNSUPPORTED
    if member.get("kind") == "claude":
        if cached_login:
            import brain_claude_code
            login = brain_claude_code.cached_login()
            if login is None:
                return False, "正在检查 Claude 登录状态"
            available = login.logged_in
        else:
            import takeover  # Claude 动手用的是你登录的 Claude Code（它自带工具）
            available = takeover.available()
        if not available:
            return False, "Claude 动手要用你登录的 Claude Code（现在没登录或没安装）"
    return True, ""


def hands_member():
    """现在接管电脑的是谁（没有就是 None）。"""
    return next((m for m in members() if m.get("hands")), None)


def set_hands(member_id):
    """指定谁接管电脑（同一时间只有一个）；member_id 为空 = 收回，大家只聊天。"""
    if member_id and sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return {"ok": False, "message": MAC_UNSUPPORTED}
    data = members()
    for m in data:
        m["hands"] = bool(member_id) and m["id"] == member_id
        if m["hands"]:
            m["enabled"] = True  # 让它接管，就顺便拉进群
    with _lock:
        _save_members(data)
    _publish({"type": "group_members"})
    return {"ok": True}


def give_hands(member_id, *, generation=None):
    """你把执行权交给某个 AI（member_id 为空 = 收回）。在群里说一声，大家都知道现在谁能动手。"""
    with _run_lock:
        if generation is not None and generation != _run_generation:
            return {"ok": False, "message": "群聊已经叫停，请重新指定执行权。"}
        before = hands_member()
        result = set_hands(member_id)
        if not result["ok"]:
            return result
        now = hands_member()
        if now and (not before or before["id"] != now["id"]):
            _awaiting["member"] = None
            _note(f"🖐 执行权交给了 {now['name']}。你让它做的事它会真的去做，其他 AI 只能看、不能改。"
                  "说“停”可立即中断群聊任务并收回执行权。")
        elif not now:
            _awaiting["member"] = None
            _note(f"已收回 {before['name']} 的执行权，现在大家都只能看、不能改。" if before else "现在没有谁有执行权。")
    return {"ok": True}


def force_stop():
    """群主随时可停下这之前开始的群聊轮次，包括检查和返修。"""
    global _run_generation
    with _run_lock:
        _run_generation += 1
        _awaiting["member"] = None
        failed = 0
        for control in _runs:
            try:
                control.stop("用户叫停")
            except OSError:
                failed += 1
        if failed:
            message = f"已取消后续群聊任务，{failed} 个本地进程未能强制停止。"
        elif _runs:
            message = "已发出强制停止指令，取消这轮尚未开始的回复、复查和返修。"
        else:
            message = "已取消尚未开始的群聊任务。"
        try:
            _note(message)
        finally:
            give_hands(None)
    return {"ok": not failed, "message": message}


def public_members():
    """给界面看的成员列表（带“能不能说话、能不能动手”，不带密钥）。"""
    out = []
    for m in members():
        ok, why = readiness(m, cached_login=True)
        can, can_why = _execution_readiness(m, cached_login=True) if ok else (False, why)
        out.append({**m, "ready": ok, "why": why, "about": PRESETS.get(m.get("preset"), {}).get("about", ""),
                    "typing": bool(_busy.get(m["id"])), "can_exec": can, "exec_why": can_why,
                    "hands": bool(m.get("hands"))})
    return out


# ---------------------------------------------------------------------------
# 聊天记录
# ---------------------------------------------------------------------------


def history_file():
    """当前项目的聊天记录文件（默认项目「星期八开发」就是原来的 history.jsonl）。"""
    import group_projects
    return group_projects.history_file()


def history(limit=200):
    path = history_file()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _append(message, *, once=False):
    path = history_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock, open(path, "a+" if once else "a", encoding="utf-8") as f:
        if once:
            # 用同一把锁检查并写入，避免收到重复请求时再次执行指令。
            f.seek(0)
            for line in f:
                if message["id"] not in line:
                    continue
                try:
                    saved = json.loads(line)
                except ValueError:
                    continue
                if isinstance(saved, dict) and saved.get("id") == message["id"]:
                    return saved
        f.write(json.dumps(message, ensure_ascii=False) + "\n")
    _publish({"type": "group_message", "message": message})


def _message(role, text, member=None, **extra):
    msg = {"id": uuid.uuid4().hex[:10], "time": datetime.now().isoformat(timespec="seconds"), "role": role,
           "text": text}
    if member:
        msg.update(member_id=member["id"], name=member["name"], color=member.get("color", "#888"))
    msg.update(extra)
    return msg


def clear():
    """清空聊天（旧记录改名存档，不真删）。还没整理进长期记忆的消息，先在后台整理进去（清空了也记得）。"""
    import group_memory
    left = group_memory.pending()
    path = history_file()
    if os.path.exists(path):
        os.replace(path, os.path.join(os.path.dirname(path), f"history-{datetime.now():%Y%m%d-%H%M%S}.jsonl"))
    if left:
        group_memory.maybe_update(force=True, messages=left)
    _publish({"type": "group_cleared"})
    return {"ok": True}


# ---------------------------------------------------------------------------
# 发消息、AI 回答
# ---------------------------------------------------------------------------


def mentioned(text, group):
    """@ 了谁：返回被 @ 的成员；@所有人 或没 @ 就返回 []（表示所有人）。"""
    if re.search(r"@(所有人|全体|大家|all)", text, re.I):
        return []
    hits = []
    for m in sorted(group, key=lambda m: -len(m["name"])):  # 长名字先匹配（“Claude 2”先于“Claude”）
        pattern = "@" + re.escape(m["name"]).replace(r"\ ", r"\s*")
        if re.search(pattern, text, re.I) and m not in hits:
            hits.append(m)
            text = re.sub(pattern, "", text, flags=re.I)
    return hits


def addressed(text, group):
    """你这句话是对谁说的：@ 了谁就是谁；没 @ 时，看开头或结尾有没有叫名字（“问你呢gpt”“Claude，你看看”）。
    返回 [] 表示对所有人说。"""
    if re.search(r"@(所有人|全体|大家|all)", text, re.I):
        return []
    hits = mentioned(text, group)
    if hits:
        return hits
    found = []
    for m in group:
        names = [m["name"]]
        if sum(1 for o in group if o.get("kind") == m.get("kind")) == 1:
            names += ALIASES.get(m.get("kind"), ())
        for name in names:
            p = re.escape(name).replace(r"\ ", r"\s*")
            if (re.search(r"^\s*" + p + r"(?![A-Za-z0-9])", text, re.I)
                    or re.search(r"(?<![A-Za-z0-9])" + p + r"[\s,，。.!！?？~～…]*$", text, re.I)):
                found.append(m)
                break
    return found


def _name_pattern(member):
    return re.escape(member["name"]).replace(r"\ ", r"\s*")


def exec_target(text, everyone):
    """这句话是不是在点名让某个 AI 动手 / 接管：返回那个成员，没有就返回 None。"""
    hits = mentioned(text, everyone)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None
    # 没有 @：看“让 Claude 去执行”“交给 GPT 做”这种说法
    assigned = [m for m in sorted(everyone, key=lambda m: -len(m["name"]))
                if re.search(ASSIGN_WORDS + _name_pattern(m) + DO_WORDS, text, re.I)]
    assigned = [m for m in assigned if not any(o is not m and m["name"] in o["name"] for o in assigned)]
    if len(assigned) == 1:
        return assigned[0]
    named = [m for m in everyone if re.search(_name_pattern(m), text, re.I)]
    if len(named) == 1 and (TAKEOVER_RE.search(text) or EXEC_RE.search(text)):
        return named[0]
    return None


def _only_takeover(text, member):
    """只是说“@Claude 接管电脑”，还没交代要做什么。"""
    rest = re.sub("@?" + _name_pattern(member), "", text, flags=re.I)
    rest = re.sub(r"全权|全员|接管|执行权|交给|操控|控制|电脑|我的|这台|来|帮我|一下|你|吧|了|的|请|现在|开始|把|给"
                  r"|[\s，。！？、,.!?~～]", "", rest)
    return len(rest) < 3


def _note(text):
    _append(_message("system", text))


def _stop_command(text):
    command = text.strip("。！？.!?，, \t")
    return STOP_RE.fullmatch(command) or RELEASE_RE.fullmatch(command.removeprefix("请").strip())


def dispatch_generation():
    with _run_lock:
        return _run_generation


def post_dispatch(messages, targets, hint, *, wait, generation, summarize=True):
    """按调度清单写群消息并点名成员，沿用群聊的叫停规则。"""
    with _run_lock:
        if generation != _run_generation:
            return None
        for text in messages:
            _append(_message("dispatcher", text, name="调度员"))
    if not targets:
        return {"asked": []}
    return _ask_all(targets, hint, wait, chain=False, generation=generation, summarize=summarize)


def send(text, wait=False, *, request_id=None):
    """
    你在群里发一句话。
        平时：在群里、能说话的 AI 轮流回（被 @ 或被叫到名字的只有它们回）；有执行权的那个最后说，而且能直接动手
        “@Claude 接管电脑”“让 GPT 执行”：执行权交给它
        “讨论一下……让 Claude 去执行”：先讨论两轮，再让 Claude 按方案动手
        “收回执行权”“取消接管”：收回，大家都只能看
        有执行权的在问你“确定吗”时：“确定”它就去做，“取消”就不做
    """
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return {"ok": False, "saved": False, "message": "没有内容"}
    if request_id is not None and (
        not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
    ):
        return {"ok": False, "saved": False, "message": "发送编号无效"}

    message = _message("user", text, name=USER_NAME)
    if request_id is not None:
        message["id"] = request_id
    stop = _stop_command(text)
    with _run_lock:
        try:
            previous = _append(message, once=request_id is not None)
        except OSError:
            if stop:
                return {**force_stop(), "saved": False}
            return {"ok": False, "saved": False, "message": "群消息保存失败，请稍后重试"}
        if previous is not None:
            if previous.get("role") != "user" or previous.get("text") != text:
                return {"ok": False, "saved": False, "message": "发送编号已被使用"}
            return {"ok": True, "saved": True}

        generation = _run_generation
        if stop:
            return {**force_stop(), "saved": True}

    try:
        dispatch = DISPATCH_RE.match(text)
        cancel_pending = PENDING_CANCEL_RE.match(text)
        if dispatch:
            result = _manual_dispatch(text[dispatch.end():], wait, generation)
        elif PENDING_LIST_RE.match(text) or cancel_pending:
            # 调度员挂起的任务：看列表 / 取消一条（不叫 AI，不花额度）
            import bridge_pending
            _note(bridge_pending.cancel(int(cancel_pending.group(1))) if cancel_pending
                  else bridge_pending.listing())
            result = {"ok": True}
        else:
            result = _handle_user_message(text, wait, generation)
    except Exception as error:
        result = {"ok": False, "message": str(error)[:200] or "群消息已保存，后续处理失败"}
    return {**result, "saved": True}


def _manual_dispatch(text, wait, generation):
    import bridge

    if not text:
        return {"ok": False, "message": "请在“调度：”后面加上要安排的事。"}

    def work(control):
        try:
            reply = bridge.route(text, wait=wait, generation=generation)
            with _run_lock:
                control.check()
                _note(reply)
        except RunStopped:
            raise
        except Exception:
            with _run_lock:
                control.check()
                _note("调度没完成，请稍后重试。")

    _background(work, wait, generation)
    return {"ok": True, "message": bridge.ACKNOWLEDGEMENT}


def _handle_user_message(text, wait, generation):
    with _run_lock:
        if generation != _run_generation:
            return {"ok": True}
        everyone = members()
        hands = next((m for m in everyone if m.get("hands")), None)
        awaited = _awaiting["member"]
        _awaiting["member"] = None
    group = [m for m in everyone if m.get("enabled")]

    # 1. 执行者在问你“确定吗”
    if hands and awaited == hands["id"]:
        import computer

        decision = computer.answer(text)
        if decision == "yes":
            return _start_exec(hands, "（用户说“确定”，同意了你刚才说的操作。请直接去执行，做完简短汇报。）",
                               wait, generation)
        if decision == "no":
            _note("好的，不做了。")
            return {"ok": True}

    # 2.5 “记住：……”：原样记进群聊的长期记忆
    remember = REMEMBER_RE.match(text)
    if remember and text[remember.end():].strip():
        import group_memory
        group_memory.remember(text[remember.end():].strip())
        _note("📝 记进群聊的长期记忆了（右上角“记忆”里能看、能改）。")
        return {"ok": True, "remembered": True}

    # 2.6 “继续任务：打断”：按任务卡接着推进（你这句话已经在群里了，卡片要点再发一条）
    import group_tasks
    if group_tasks.CONTINUE_RE.match(text):
        task = group_tasks.match_continue(text)
        if task is None:
            _note("没找到对应的任务卡（点右上角“任务”看看有哪些）。")
            return {"ok": False, "message": "没找到任务卡"}
        return group_tasks.continue_task(task["id"], wait, generation=generation)

    # 3. 点名让某个 AI 接管 / 动手：执行权交给它
    target = exec_target(text, everyone)
    asking_hands = (target is not None and hands is not None and target["id"] == hands["id"]
                    and len(mentioned(text, everyone)) == 1)
    explicit = bool(TAKEOVER_RE.search(text) or EXEC_RE.search(text))  # 明说了让它动手（做完会让另一个检查）
    if target is not None and (explicit or asking_hands):
        ok, why = can_execute(target)
        if not ok:
            _note(f"{target['name']} 现在不能动手：{why}")
            return {"ok": False, "message": why}
        with _run_lock:
            if generation != _run_generation:
                return {"ok": True}
            current = hands_member()
            if not current or current["id"] != target["id"]:
                give_hands(target["id"])
        if DISCUSS_RE.search(text) and len(group) > 1:
            return _discuss(wait, generation, executor=target, look_first=bool(LOOK_RE.search(text)))
        if TAKEOVER_RE.search(text) and _only_takeover(text, target):
            return {"ok": True, "hands": target["name"]}
        return _ask_all([find_member(target["id"]) or target], None, wait,
                        from_user=True, explicit=explicit, generation=generation)

    targets = addressed(text, group) or group
    # 4. 只讨论、不动手（“你们讨论一下……”）：轮流讨论两轮
    if DISCUSS_RE.search(text) and len(targets) > 1:
        return _discuss(wait, generation, look_first=bool(LOOK_RE.search(text)), group=targets)
    # 5. 平时：轮流回（Claude、GPT 随时能看文件；其他 AI 说了“看看项目”才去看）；有执行权的那个能直接动手
    look = bool(LOOK_RE.search(text))
    return _ask_all(targets, LOOK_HINT if look else None, wait, look=look, from_user=True, generation=generation)


def _discuss(wait, generation, executor=None, look_first=False, group=None):
    """轮流讨论两轮（群里能说话的都参加；讨论时谁都只看不改）。
    executor：讨论完让它按方案动手。"""
    def work(control):
        people = _ready(group or [m for m in members() if m.get("enabled")], control)
        for i, hint in enumerate(DISCUSS_HINTS):
            if i == 0 and look_first:
                hint = LOOK_HINT[:-1] + "然后提出你的方案。）"
            _turns([(m, hint, False) for m in people], control, look=(i == 0 and look_first))
        with _run_lock:
            control.check()
            if executor is not None:
                current = find_member(executor["id"])
                if not current or not current.get("hands"):
                    return
                _note(f"讨论完了，{executor['name']} 开始按方案动手。")
            else:
                _note("讨论完了。想让谁按这个方案去做，就说“让某某执行”，或者点上面的“交给某某”。")
        if executor is not None:
            _turns([(current, EXEC_AFTER_DISCUSS, True)], control, explicit=True)

    _background(work, wait, generation)
    return {"ok": True, "discuss": True, "executor": executor["name"] if executor else None}


def _background(work, wait, generation):
    control = RunControl()

    def run():
        try:
            control.check()
            work(control)
        except RunStopped:
            pass  # 正在运行的阶段会把中断原因和已记录的步骤发到群里。
        finally:
            with _run_lock:
                _runs.discard(control)

    with _run_lock:
        if generation != _run_generation:
            return
        _runs.add(control)
        t = threading.Thread(target=run, daemon=True)
        t.start()
    if wait:
        t.join()


def _start_exec(member, hint, wait, generation):
    _background(lambda control: _exec(member, hint, control=control), wait, generation)
    return {"ok": True, "executing": member["name"]}


def _stepper(member, progress):
    """给 AI 用的“每做一步就在群里显示一行”的函数（点开能看命令、输出、改动）。"""
    def on_step(desc, detail="", kind=""):
        detail = str(detail or "")
        _append(_message("step", desc, member, detail=detail[:4000], kind=kind))
        progress.append((desc, kind, detail if kind in ("cmd", "interrupted") else ""))
    return on_step


_TEST_FILE = re.compile(r"(?:test(?:s|[-_].+)?|测试.*|.+_test)\.py$", re.I)
_TEST_TOTAL = re.compile(r"(?:结果[：:]\s*)?(\d+)\s*/\s*(\d+)\s*(?:项\s*)?(?:通过|passed)", re.I)
_PYTEST_COUNT = r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)"
_PYTEST_TOTAL = re.compile(rf"{_PYTEST_COUNT}(?:,\s*{_PYTEST_COUNT})*(?: in .+s)?")
_FAILED_TEST = re.compile(r"^(?:\[失败\][ \t]*|失败[ \t：:]+)(.+)$", re.M)
_TEST_EXCEPTION = re.compile(r"^(?:E[ \t]+)?(\w*(?:Error|Exception)(?::.*)?)$", re.M)
_CREDENTIAL = re.compile(r"api[-_ ]?key|token|secret|password|authorization|bearer |\bsk-\w|"
                         r"\bgh[pousr]_\w|密钥|口令|密码|凭证|://[^/\s]+@", re.I)


def _test_command(command):
    """取测试程序和用例名，群里省略目录、重定向、环境变量等命令细节。"""
    for part in re.split(r"[;&|\r\n]", command):
        words = re.findall(r'''"[^"]*"|'[^']*'|\S+''', part)
        if not words:
            continue
        runner, *args = [os.path.basename(word.strip("\"'")) for word in words]
        runner = runner.lower().removesuffix(".exe").removesuffix(".cmd")
        if runner in ("npm", "pnpm", "yarn", "go", "cargo", "dotnet") and args[:1] == ["test"]:
            return f"{runner} test"
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?|py", runner):
            while args and re.fullmatch(r"-[uBISOE]+|-X|-[23](?:\.\d+)?", args[0]):
                args = args[2:] if args[0] == "-X" else args[1:]
            if args[:2] in (["-m", "pytest"], ["-m", "unittest"]):
                runner, args = f"python -m {args[1]}", args[2:]
            elif args and _TEST_FILE.fullmatch(args[0]) and not _CREDENTIAL.search(args[0]):
                return f"python {args[0][:100]}"
            else:
                continue
        elif runner not in ("pytest", "py.test", "ctest"):
            continue
        test = next((arg for arg in args if _TEST_FILE.fullmatch(arg.split("::", 1)[0])), "")
        return f"{runner} {test[:100]}" if test and not _CREDENTIAL.search(test) else runner
    return ""


def _test_result(desc, output):
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    lines = [line.strip() for line in output.splitlines()]
    output = "\n".join(lines)
    count, failed, no_tests = "", False, False
    for line in reversed(lines):
        if match := _TEST_TOTAL.fullmatch(line):
            passed, total = map(int, match.groups())
            count, failed, no_tests = f"{passed}/{total} 项通过", passed < total, total == 0
            break
        if _PYTEST_TOTAL.fullmatch(line.strip("= ")):
            counts = {name: int(amount) for amount, name in re.findall(_PYTEST_COUNT, line)}
            passed = counts.get("passed", 0)
            errors = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
            failed, no_tests = errors > 0, passed == errors == 0
            count = f"{passed} 项通过" + (f"，{errors} 项失败" if errors else "")
            break
    case = _FAILED_TEST.search(output)
    failed_case = re.search(r"^(?:FAILED (?=[^\s(])|ERROR (?=\S+::|collecting )|FAIL: )(.+)$", output, re.M)
    code = (re.search(r"（出错，退出码 (-?\d+)）$", desc)
            or re.match(r"(?:（退出码\s*|Exit code:?\s+)(-?\d+)", output, re.I))
    timed_out = re.match(r"命令超过 \d+ 秒还没结束", output)
    failed = (failed or case or failed_case or desc.endswith("（命令报错）") or timed_out
              or (code and int(code[1]) != 0) or re.search(r"^FAILED \(", output, re.M))
    if not failed:
        if no_tests or re.search(r"^(?:Ran 0 tests\b|no tests ran\b|collected 0 items\b)", output, re.M):
            return "没有通过的用例", count, ""
        status = "结果未确认" if desc.endswith("（未返回退出码）") and not count else "通过"
        return status, count, ""
    for match in (case, _TEST_EXCEPTION.search(output), failed_case):
        if match and not _CREDENTIAL.search(match[1]):
            return "失败", count, match[1][:120].rstrip("。 ")
    reason = ""
    if code and int(code[1]):
        reason = f"退出码 {code[1]}"
    elif timed_out:
        reason = timed_out[0]
    return "失败", count, reason


def _test_progress(progress):
    from takeover import COMMAND_OUTPUT

    tests, unfinished = [], ""
    for desc, kind, detail in progress:
        if not detail.startswith("$ ") or kind not in ("cmd", "interrupted"):
            continue
        command, output = detail[2:], ""
        if kind == "cmd":
            separator = COMMAND_OUTPUT if COMMAND_OUTPUT in command else "\n\n"
            command, _, output = command.partition(separator)
        label = _test_command(command)
        if not label:
            continue
        if kind == "interrupted":
            unfinished = label
        else:
            tests.append((command.strip(), label, *_test_result(desc, output)))
    summary = ""
    if tests:
        command, label, status, count, reason = tests[-1]
        summary = f"最近一次测试「{label}」{status}{f'（{count}）' if count else ''}。"
        attempts = [result for cmd, _, result, _, _ in tests if cmd == command]
        if status == "失败" and len(attempts) > 1 and all(result == "失败" for result in attempts):
            summary += f"同一命令运行 {len(attempts)} 次均失败。"
        if reason:
            summary += f"失败原因是{reason}。"
    if unfinished:
        summary += f"中断时「{unfinished}」结果未返回。"
    return summary


def _progress_text(progress):
    completed = [(desc, kind) for desc, kind, _ in progress if kind not in ("think", "interrupted")]
    incomplete = sum(kind == "interrupted" for _, kind, _ in progress)
    summary = f"已记录 {len(completed)} 个工具步骤。"
    if incomplete:
        summary += f"还有 {incomplete} 个工具步骤的结果未返回。"
    files = list(dict.fromkeys(desc for desc, kind in completed if kind == "file"))
    if files:
        summary += f"最近的文件操作为{'、'.join(files[-3:])[:120]}。"
    return summary + _test_progress(progress)


def _typing(member, doing, on):
    if on:
        _busy[member["id"]] = doing or True
    else:
        _busy.pop(member["id"], None)
    _publish({"type": "group_typing", "member_id": member["id"], "name": member["name"],
              "on": on, "doing": doing})


def _claude_effort(member, use):
    """Claude 的思考深度：成员设置里选了就用选的；没选就按模型默认（use = chat / takeover）。"""
    import claude_models
    choice = member.get("model") if member.get("model") in claude_models.MODELS else "sonnet"
    return choice, claude_models.MODELS[choice], member.get("effort") or claude_models.MODELS[choice].get(use, "medium")


def _exec(member, hint=None, review=True, explicit=True, *, control):
    """有执行权的 AI 回你的话：像桌面版一样能直接动手。每一步在群里显示一行（点开看细节）；做完把结果发到群里。
    review：做完后让另一个 AI 检查一遍（设置里可以关）——只在它改了文件，或者你明说了让它动手（explicit）时检查。
    返回它在群里说的话。"""
    with _run_lock:
        control.check()
        current = find_member(member["id"])
        if not current or not current.get("hands"):
            return ""
        member = current
        if not _exec_lock.acquire(blocking=False):
            _note(f"上一件事还在做，{member['name']} 等它做完再来。")
            return ""
        if _busy.get(member["id"]):
            _exec_lock.release()
            return ""
        _typing(member, "do", True)
    t = time.monotonic()
    done = None
    reply = ""
    progress = []
    try:
        import agent_hands
        import takeover
        on_step = _stepper(member, progress)
        recent = history(CONTEXT_MESSAGES)
        hint = hint or "（现在轮到你：回应用户最后那句话。他让你做事就去做；只是聊天、提问就直接回答。）"
        agent_hands.project_snapshot()  # 让它看代码时看到的是最新的
        control.check()
        if member.get("kind") == "claude":
            import claude_models
            choice, model, effort = _claude_effort(member, "takeover")
            result = takeover.run(_transcript(member, recent, hint), system=_agent_prompt(member, "do"),
                                  session_file=None, on_step=on_step, model=model["id"], effort=effort,
                                  full=True, control=control)  # 功能全开（和你单独用 Claude Code 一样）
            claude_models.record(choice, result.get("cost"))
        elif member.get("kind") == "codex":
            import agent_codex
            result = agent_codex.run(_agent_prompt(member, "do") + "\n\n" + _transcript(member, recent, hint), "do",
                                     model=member.get("model") or "", effort=member.get("effort") or "",
                                     on_step=on_step, control=control)
        else:
            messages = _chat_messages(member, recent, hint)
            result = agent_hands.run(member, messages, _key(member.get("key", "")),
                                     on_step=on_step, control=control)
        with _run_lock:
            control.check()
            reply = _clean(result["reply"], member["name"])
            steps = len(result.get("steps") or [])
            _append(_message("ai", reply, member, exec=steps > 0, steps=steps,
                             seconds=round(time.monotonic() - t, 1)))
            if not result.get("error"):
                current = hands_member()
                if result.get("needs_confirm") or takeover.asks_confirm(reply):
                    if current and current["id"] == member["id"]:
                        _awaiting["member"] = member["id"]
                else:
                    done = result
    except RunStopped as e:
        _note(f"{member['name']} 的执行已中断（{e}，运行 {time.monotonic() - t:.0f} 秒）。{_progress_text(progress)}")
    except Exception as e:
        _note(f"{member['name']} 动手时出错了（{_explain(e)}）。{_progress_text(progress)}")
    finally:
        with _run_lock:
            _typing(member, "do", False)
            _exec_lock.release()
    if done is not None and review and (done.get("files") or (explicit and done.get("steps"))):
        _review_and_fix(member, done, control)
    return reply


def _reviewer_for(executor):
    """找一个检查员：群里另一个能动手的 AI（Claude、GPT 优先）。设置里关了就不检查。"""
    import settings
    if not settings.get("group_review", True):
        return None
    candidates = [m for m in members() if m.get("enabled") and m["id"] != executor["id"]]
    candidates.sort(key=lambda m: m.get("kind") not in ("claude", "codex"))
    for m in candidates:
        if m.get("kind") in ("claude", "codex") and can_execute(m)[0]:
            return m
    return None


def _review_and_fix(executor, result, control):
    """协作：另一个 AI 检查一遍；它说“⚠️ 有问题”，动手的那个按意见改一次（只改一次，不来回拉锯）。"""
    control.check()
    reviewer = _reviewer_for(executor)
    if reviewer is None:
        return
    files = result.get("files") or []
    files_text = f"它改过的文件：{'、'.join(dict.fromkeys(files))}。" if files else ""
    with _run_lock:
        control.check()
        _note(f"🔍 {reviewer['name']} 来检查 {executor['name']} 做的结果。")
    text = _reply(reviewer, REVIEW_HINT.format(executor=executor["name"], files=files_text),
                  look="review", control=control)
    if text and text.lstrip().startswith("⚠"):
        with _run_lock:
            control.check()
            current = find_member(executor["id"])
            if not current or not current.get("hands"):
                return
            _note(f"{reviewer['name']} 发现了问题，{executor['name']} 按意见再改一次。")
        _exec(current, FIX_HINT.format(reviewer=reviewer["name"]), review=False, control=control)


def continue_round(wait=False):
    """“接着聊一轮”：每个 AI 看看别人刚才说的，再说一句（这时谁都只看不改）。"""
    group = [m for m in members() if m.get("enabled")]
    hint = "（请看看群里其他人刚才说的话，接着聊一句：可以补充、赞同或者反驳。没什么要说的就简单回应一下。）"
    return _ask_all(group, hint, wait, chain=True)


def _ready(targets, control=None):
    """能说话的留下；不能说话的在群里说一声为什么。"""
    out = []
    for m in targets:
        if control:
            control.check()
        ok, why = readiness(m)
        if control:
            control.check()
        if ok:
            out.append(m)
        else:
            _note(f"{m['name']} 没法回答：{why}")
    return out


def _ask_all(targets, hint, wait, look=False, from_user=False, explicit=False, chain=None, *, generation=None,
             summarize=True):
    """让几个成员轮流回（后说的能看到先说的）。
    from_user：这是你说的话 → 有执行权的那个排最后（先听听别人怎么看），而且能直接动手；其他的只看不改。
    chain：回答里 @ 了别的 AI，被 @ 的接着回（你说的话之后默认允许）。"""
    if not targets:
        return {"ok": True, "asked": [], "message": "群里还没有 AI，先在“成员”里拉一个进来"}
    if generation is None:
        generation = _run_generation
    people = _ready(targets)
    hands = hands_member()
    if hands:
        people.sort(key=lambda m: m["id"] == hands["id"])
    turns = [(m, hint, from_user) for m in people]
    allow = AI_CHAIN_LIMIT if (from_user if chain is None else chain) else 0
    _background(lambda control: _turns(turns, control, look=look, chain=allow, explicit=explicit,
                                      summarize=summarize), wait, generation)
    return {"ok": True, "asked": [m["name"] for m in people]}


def _turns(turns, control, look=False, chain=0, explicit=False, summarize=True):
    """轮流说。turns：[(成员, 提示, 能不能动手)]。
    能动手 = 这是你说的话，而且它手里有执行权 → 它像桌面版一样直接干；其他情况都只看不改。
    chain：回答里 @ 了别的 AI，就让被 @ 的接着回（最多这么多次；AI 之间接话时谁都只看不改）。"""
    pending = list(turns)
    while pending:
        control.check()
        member, hint, act = pending.pop(0)
        member = find_member(member["id"]) or member
        if _busy.get(member["id"]):
            continue  # 上一句还没回完
        if act and member.get("hands"):
            text = _exec(member, hint, explicit=explicit, control=control)
        else:
            text = _reply(member, hint, look, control=control)
        control.check()
        if chain <= 0:
            continue
        for other in _at(text, member):
            if chain <= 0:
                break
            if any(o["id"] == other["id"] for o, _, _ in pending):
                continue  # 本来就还没轮到它说
            pending.append((other, AT_HINT.format(name=member["name"]), False))
            chain -= 1
    control.check()
    if summarize:
        import group_memory
        group_memory.maybe_update()  # 新消息攒够了：在后台整理进长期记忆
        import group_tasks
        group_tasks.after_turns()    # 当前任务卡有新进展：在后台整理进卡片


def _at(text, speaker):
    """AI 的话里 @ 了群里哪些（能说话的）AI。"""
    if not text or "@" not in text:
        return []
    group = [m for m in members() if m.get("enabled") and m["id"] != speaker["id"]]
    return [m for m in mentioned(text, group) if readiness(m)[0]]


def _reply(member, hint, look=False, *, control):
    """让一个成员只看不改地说一句（look=True 先看项目代码；look="review" 当检查员看动手的结果）。返回它说的话。"""
    doing = "review" if look == "review" else "look" if look else ""
    with _run_lock:
        control.check()
        if _busy.get(member["id"]):
            return ""
        _typing(member, doing, True)
    t = time.monotonic()
    text = ""
    progress = []
    try:
        text = _clean(ask(member, history(CONTEXT_MESSAGES), hint, look=look,
                          on_step=_stepper(member, progress), control=control),
                      member["name"])
        with _run_lock:
            control.check()
            _append(_message("ai", text or "（没说话）", member, seconds=round(time.monotonic() - t, 1),
                             review=look == "review"))
    except RunStopped as e:
        stage = "复查" if look == "review" else "回复"
        _note(f"{member['name']} 的{stage}已中断（{e}，运行 {time.monotonic() - t:.0f} 秒）。{_progress_text(progress)}")
    except Exception as e:
        _append(_message("system", f"{member['name']} 没回上：{_explain(e)}"))
    finally:
        with _run_lock:
            _typing(member, doing, False)
    return text


def _system_for(member):
    everyone = members()
    others = [m["name"] for m in everyone if m.get("enabled") and m["id"] != member["id"]]
    hands = next((m["name"] for m in everyone if m.get("hands")), None)
    hands_text = ("你自己（用户让你做事时，系统会交给你去做；这次只是在聊天）" if hands == member["name"]
                  else hands or "没有人（大家都只能看）")
    return SYSTEM_PROMPT.format(name=member["name"], others=_others_text(member, everyone), hands=hands_text,
                                role=_role_text(member), memory=_memory_text(), project_line=_project_line())


# 你和桌面版 Claude、Codex 的聊天记录存在哪（2026-11-21 你允许群里的 AI 去翻）
CLAUDE_RECORDS = os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", PROJECT_DIR))
CODEX_RECORDS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _others_text(member, everyone):
    """“、Claude（职责：定调把关）、GPT（职责：写代码）”"""
    return "".join(f"、{m['name']}" + (f"（职责：{m['role']}）" if m.get("role") else "")
                   for m in everyone if m.get("enabled") and m["id"] != member["id"])


def _role_text(member):
    return member.get("role") or "用户还没给你分配固定职责，按他每次的要求来"


def _project_dir():
    import group_projects
    return group_projects.data_dir()


def _project_line():
    import group_projects
    return group_projects.prompt_text()


def _memory_text():
    import group_memory
    return group_memory.read() or "（还没有。群里聊得多了会自动整理；用户说“记住：……”也会记进来）"


def _model_label(member, use="chat"):
    """告诉 AI 它自己用的是哪个模型（用户问起来能答对）。"""
    kind = member.get("kind")
    if kind == "claude":
        _choice, model, effort = _claude_effort(member, use)
        return f"{model['name']}（思考深度 {effort}）"
    if kind == "codex":
        effort = member.get("effort")
        return (member.get("model") or "ChatGPT 账号的默认模型") + (f"（思考深度 {effort}）" if effort else "")
    return member.get("model") or "默认模型"


def _agent_prompt(member, mode):
    """Claude / GPT 的说明。mode：do 有执行权、回你的话（能动手）/ look 只看不改 / review 当检查员。"""
    import agent_hands
    import takeover
    everyone = members()
    others = [m["name"] for m in everyone if m.get("enabled") and m["id"] != member["id"]]
    hands = next((m for m in everyone if m.get("hands")), None)
    mine = hands is not None and hands["id"] == member["id"]
    tools = LOOK_TOOLS_TEXT.get(member.get("kind"), "看文件")
    if mode == "do":
        desktop = DESKTOP_TEXT.get(member.get("kind"), DESKTOP_CTL).format(
            python=os.path.join(PROJECT_DIR, ".venv", "Scripts", "python.exe"), project=PROJECT_DIR)
        power = POWER_DO.format(work_dir=takeover.WORK_DIR, desktop=desktop, safety=SAFETY,
                                work_rules=PROJECT_WORK_RULES)
        where = (f"看星期八的代码可以看最新副本 {agent_hands.SNAPSHOT_DIR}；"
                 f"要改就直接改 {takeover.PROJECT_DIR} 里的文件。")
    else:
        power = (POWER_REVIEW if mode == "review" else POWER_LOOK_MINE if mine else POWER_LOOK).format(
            name=member["name"], tools=tools, work_rules=PROJECT_WORK_RULES)
        where = "它的代码在你现在所在的文件夹里有一份最新副本（不含记忆、日志、密钥），看代码就看这里。"
    return AGENT_PROMPT.format(
        name=member["name"], others=_others_text(member, everyone), role=_role_text(member),
        model=_model_label(member, "takeover" if mode == "do" else "chat"), power=power,
        hands="你自己" if mine else hands["name"] if hands else "没有人（大家都只能看）",
        project=takeover.PROJECT_DIR, where=where, example=others[0] if others else "GPT",
        memory=_memory_text(), history=_project_dir(), project_line=_project_line(),claude_records=CLAUDE_RECORDS, codex_records=CODEX_RECORDS,
        privacy=PRIVACY, now=f"{datetime.now():%Y-%m-%d %H:%M}")


def _has_tools(member):
    """这个 AI 能不能像桌面版一样用工具：GPT（Codex）能；Claude 要有你登录的 Claude Code。"""
    if sys.platform == "darwin":
        return False
    if member.get("kind") == "codex":
        return True
    if member.get("kind") == "claude":
        import takeover
        return takeover.available()
    return False


def _chat_messages(member, recent, hint):
    """把群消息整理成这个 AI 看的对话：它自己说的是 assistant，其他人说的标上名字作为 user。"""
    out = []
    for msg in recent:
        if msg["role"] in ("system", "step"):  # 系统提示、动手的每一步不发给 AI（省 token；动手的结果会发）
            continue
        if msg["role"] == "ai" and msg.get("member_id") == member["id"]:
            out.append({"role": "assistant", "content": msg["text"]})
            continue
        who = "我（群主）" if msg["role"] == "user" else msg.get("name", "某个 AI")
        line = f"【{who}】：{msg['text']}"
        if out and out[-1]["role"] == "user":
            out[-1]["content"] += "\n" + line   # 连续几条别人说的，并成一条（有的接口要求一问一答交替）
        else:
            out.append({"role": "user", "content": line})
    if hint:
        if out and out[-1]["role"] == "user":
            out[-1]["content"] += "\n" + hint
        else:
            out.append({"role": "user", "content": hint})
    if not out or out[-1]["role"] != "user":
        out.append({"role": "user", "content": "（轮到你说一句）"})
    return out


def _transcript(member, recent, hint):
    """给 Claude Code（只能收一段文字）用：把群聊写成一段聊天记录。"""
    lines = ["【群聊记录（从旧到新）】"]
    for msg in recent:
        if msg["role"] in ("system", "step"):
            continue
        if msg["role"] == "ai" and msg.get("member_id") == member["id"]:
            who = "你"
        else:
            who = "我（群主）" if msg["role"] == "user" else msg.get("name", "某个 AI")
        lines.append(f"{who}：{msg['text']}")
    lines += ["", hint or "【现在轮到你在群里回一句】"]
    return "\n".join(lines)


def _agent_look(member, recent, hint, on_step, review=False, *, control=None):
    """Claude / GPT 只看不改地回一句：能看电脑上的文件（星期八的代码看最新副本），不能改东西
    （执行权不在它手里：Claude 只给看文件、搜索的工具；GPT 用只读沙盒）。"""
    import agent_hands
    where = agent_hands.project_snapshot()  # 先把最新的代码复制一份给它看
    prompt = _agent_prompt(member, "review" if review else "look")
    text = _transcript(member, recent, hint)
    if member.get("kind") == "claude":
        import claude_models
        import takeover
        choice, model, effort = _claude_effort(member, "chat")
        result = takeover.run(text, system=prompt, session_file=None, on_step=on_step, model=model["id"],
                              effort=effort, tools=CLAUDE_LOOK_TOOLS, cwd=where, control=control)
        claude_models.record(choice, result.get("cost"))
        return result["reply"]
    import agent_codex
    return agent_codex.run(prompt + "\n\n" + text, "look", model=member.get("model") or "",
                           effort=member.get("effort") or "", cwd=where, on_step=on_step, control=control)["reply"]


def ask(member, recent, hint=None, look=False, on_step=None, *, control=None):
    """让一个成员根据最近的群消息只看不改地说一句话。
    Claude、GPT：每次都能看文件（像桌面版一样）；其他 AI：look=True 时才去看项目。look="review"：当检查员。"""
    if _has_tools(member):
        return _agent_look(member, recent, hint, on_step, review=look == "review", control=control)
    if look and sys.platform == "darwin":
        from computer import MAC_UNSUPPORTED
        return MAC_UNSUPPORTED
    if look:
        import agent_hands
        return agent_hands.run(member, _chat_messages(member, recent, hint), _key(member.get("key", "")),
                               on_step=on_step, look=True, control=control)["reply"]
    kind = member.get("kind")
    system = _system_for(member)
    if kind == "codex":
        import agent_codex
        return agent_codex.run(system + "\n\n" + _transcript(member, recent, hint), "chat",
                               model=member.get("model") or "", effort=member.get("effort") or "",
                               on_step=on_step, control=control)["reply"]
    if kind == "ollama":
        import local_models

        import assistant  # 用和星期八一样的设置，免得模型反复重新加载
        response = local_models.chat(model=member.get("model"), think=False,
                               messages=[{"role": "system", "content": system}] + _chat_messages(member, recent, hint),
                               options=assistant.OPTIONS, keep_alive=assistant.KEEP_ALIVE)
        return (response.message.content or "").strip()
    if kind == "claude":
        import claude_models
        choice, model, effort = _claude_effort(member, "chat")
        key = _key("anthropic")
        if key:
            import brain_claude
            return brain_claude.answer(system, _chat_messages(member, recent, hint), model["id"], effort, key)
        import brain_claude_code
        info = {}
        text = brain_claude_code.answer(system, [{"role": "user", "content": _transcript(member, recent, hint)}],
                                        model["id"], effort, timeout=TIMEOUT, info=info)
        claude_models.record(choice, info.get("cost"))
        return text
    if kind == "openai":
        import brain_openai
        return brain_openai.answer(system, _chat_messages(member, recent, hint), member["model"],
                                   _key(member.get("key", "")) or "none", member.get("base_url") or None,
                                   member.get("api", "chat"))
    raise RuntimeError("不认识的 AI 类型")


def _clean(text, name):
    """去掉开头多出来的“【Claude】：”“Claude：”。"""
    text = (text or "").strip()
    text = re.sub(r"^\s*【[^】]{1,20}】\s*[:：]\s*", "", text)
    return re.sub(r"^\s*" + re.escape(name) + r"\s*[:：]\s*", "", text).strip()


def _explain(error):
    import brains
    text = brains._explain(error)
    if isinstance(error, RuntimeError) and text.startswith("RuntimeError: "):
        return str(error)[:150]  # 我们自己写的说明，不用带英文前缀
    return text


# ---------------------------------------------------------------------------
# 密钥（和“大脑”共用 api_keys.json）
# ---------------------------------------------------------------------------

KEY_LABELS = {"anthropic": "Anthropic（Claude）", "openai": "OpenAI（GPT）", "deepseek": "DeepSeek",
              "doubao": "豆包（火山方舟）", "qwen": "通义千问（阿里百炼）", "kimi": "Kimi（月之暗面）",
              "zhipu": "智谱 GLM", "gemini": "Gemini（Google）"}


def _key(name):
    if not name:
        return ""
    import brains
    return str(brains.load_keys().get(name, "")).strip()


def key_status():
    """每个密钥填了没有（只给看开头和结尾几位，不给完整密钥）。"""
    import brains
    keys = brains.load_keys()
    names = list(KEY_LABELS) + [m["key"] for m in members() if m.get("key", "").startswith("custom_")]
    out = []
    for name in names:
        value = str(keys.get(name, "")).strip()
        label = KEY_LABELS.get(name) or next((f"{m['name']}（自定义）" for m in members() if m.get("key") == name), name)
        out.append({"name": name, "label": label, "set": bool(value),
                    "hint": f"{value[:4]}…{value[-4:]}" if len(value) > 12 else ("已填写" if value else "")})
    return out


def set_key(name, value):
    import brains
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", name or ""):
        return {"ok": False, "message": "密钥名字不对"}
    keys = brains.load_keys()
    keys[name] = (value or "").strip()
    tmp = brains.KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)
    os.replace(tmp, brains.KEYS_FILE)
    _publish({"type": "group_members"})
    return {"ok": True}


def _publish(event):
    status.publish(event)
