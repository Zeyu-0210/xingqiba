"""
assistant.py —— 把一句话交给本地大模型，让它理解意图、调用工具，并且记住你

工作流程：
    你的一句话（语音识别的结果，或者打字）
      → 连同“系统提示词”（含长期记忆）、最近几轮对话（短期记忆）、“工具说明书”一起发给 gemma4
      → 模型决定调用哪个工具、参数是什么
      → Python 真正执行（音量在 volume.py，记忆在 memory.py，操作电脑在 computer.py）
      → 需要的话把结果再交给模型，让它接着做或者组织回答（比如查完历史再调音量）
      → 这一轮对话存进短期记忆，并写进历史记录

单独运行这个文件，可以打字和助手对话（直接回车或输入“退出”结束）：
    .venv\\Scripts\\python.exe assistant.py
"""

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import ollama

import brains
import claude_models
import computer
import delegate
import friendly
import local_models
import memory
import settings
import speak
import status
import takeover
import volume
import wakeword
import workers

# 状态窗里显示的工具名字
TOOL_NAMES = {
    "get_volume": "查看音量", "set_volume": "设置音量", "change_volume": "调大/调小音量",
    "mute": "静音", "unmute": "取消静音", "remember": "记住一件事", "forget": "忘掉一件事",
    "search_history": "翻历史记录", "delegate_task": "把任务交给 AI", "set_voice": "换声音",
    "set_brain": "换大脑", "claude_takeover": "Claude 接管", "end_conversation": "回去待命",
    **computer.NAMES,
}

# ---------------------------------------------------------------------------
# 基本设置
# ---------------------------------------------------------------------------

MODEL = local_models.DEFAULT_REASONING  # 旧的命令行测试仍可使用这个默认值

# 对话窗口关闭后让 Ollama 保留热模型，短时间再次唤醒时复用；切换语音模式时主动释放。
KEEP_ALIVE = "5m"

# num_ctx：上下文长度，模型一次最多能“看”多少内容。Ollama 默认按模型上限（256K）预留显存。
#          工具说明书（23 个工具）+ 系统提示词就有约 4400 个 token，再加上短期记忆、
#          read_screen 看到的窗口清单（约 1500）、多步操作的来回，所以设成 12288。
# temperature：随机程度。调低一点，模型调用工具更稳定，不容易“自由发挥”。
# Mac 上没有操作电脑的工具，提示词短很多；上下文开小一点，16GB 内存才不吃紧
OPTIONS = {"num_ctx": 8192 if sys.platform == "darwin" else 12288, "temperature": 0.3}

# 一句话最多让模型来回几次（比如：打开记事本 → 打字 → 保存 → 回答；
# 也包括“只说不做”被提醒后重来的次数）
MAX_ROUNDS = 8

# ======== 说话风格（念给你听的时候用） ========
USER_TITLE = ""   # 怎么称呼你，例如“先生”“老板”或你的名字；空着就不加称呼
# 注意：别加“不要反问确认”这类话——实测会让模型在“调回去”时只道歉、不调用工具（8 次里错 6 次）
PERSONA = ("说话风格像钢铁侠电影里的 AI 管家：沉稳、礼貌、简洁、有温度，偶尔带一点轻松的幽默。"
           "你的话会被念出来，所以尽量简短，一般一两句话；不用列表、不用表情符号。")

# 助手的名字：就是唤醒词（“星期八”）。以后在 wakeword.py 里换了唤醒词，名字也跟着变
ASSISTANT_NAME = next(iter(wakeword.WAKE_WORDS))


def persona_text():
    title = f"称呼用户为“{USER_TITLE}”，但不要每句都叫。" if USER_TITLE else "不用特别称呼用户。"
    return PERSONA + title

# 默认的记忆（存在项目的 memory 文件夹里）。测试时会换成临时文件夹。
MEMORY = memory.Memory()

# 同一时间只处理一句话：说话和在界面里打字同时来时，排队处理，免得记忆和确认流程乱掉
TURN_LOCK = threading.RLock()

WEEKDAYS = "一二三四五六日"

# “系统提示词”模板：每次对话前告诉模型它是谁、现在几点、关于用户它记得什么、该怎么做
SYSTEM_PROMPT = """你的名字叫“{name}”（用户也是喊这个名字叫醒你的），是运行在用户 {platform}上的中文语音助手兼私人秘书，会记住用户，越用越懂用户。
被问到你是谁、叫什么名字时，就说你是{name}。不要说自己是 Gemma、Google 或者其他公司、其他模型；只有用户专门问“你底层用的是什么模型”时，才说你目前用的是本地的 {model} 模型。
{persona}
用户的话来自语音识别，可能有错别字或口语化表达，请理解真实意图。

【关于用户的长期记忆】
{profile}

规则：
1. 查看或调整电脑音量时，调用对应的工具。中文数字（如“三十”“百分之五十”）要换成阿拉伯数字。问现在音量多少时，一定要调用 get_volume 查实际的值，不要凭前面的对话回答（音量可能已经被改过）。
2. 只说“大一点”“小声点”而没说具体数值时，调整 10；说“大很多”“小很多”时，调整 20。
3. 一句话里有多个操作时（如“调到二十然后静音”），按顺序调用多个工具。
4. “调回去”“恢复”“撤销”这类说法，要参考前面的对话：刚静音的就取消静音；刚改过音量的，就改回改之前的数值（工具结果里写着“从 X% 设为 Y%”）。
5. 只有用户明确说“记住……”“记一下……”时，才调用 remember，用一句简短完整的话记下来。如果只是听到“我是某某”“我喜欢……”这类话，不要直接记，先用一句话问用户要不要记住（麦克风可能录到别人或视频里的声音）。一次性的操作不要记。没有调用 remember，就绝对不能说“记住了”“记下了”。
6. 用户让你忘掉某件事时，调用 forget。
7. 用户问起以前做过什么、说过什么，而前面的对话里没有时，调用 search_history 查历史记录。
8. 用户说“按我的习惯”之类的话时，参考长期记忆行事。
9. 和以上都无关的请求，不要调用工具，直接用一两句简短的中文回答。
   重要：要改变电脑上的任何东西（音量、静音、记忆、交任务、换声音、打开软件、点击、打字……），必须真的调用工具。没有调用工具，就绝对不能说“已经调好了”“已恢复”“已打开”“记住了”这类话——只说不做，什么都不会发生。
10. 用户唤醒你之后麦克风会一直开着，所以后面听到的话不一定是对你说的。如果有 ignore 工具，按下面判断：
   - 对你说的：让你操作电脑（调音量、打开软件、点击、打字等）、让你记住/忘掉/查记录、明确问你的问题（如“现在音量多少”）。
   - 不是对你说的，调用 ignore：称呼别人的（兄弟、哥们、老铁、宝、队友的名字……）、游戏里的交流（报点、战术、输赢、“快去B点”）、问别人情况的（“你那边卡不卡”“你吃饭没”）、感叹和自言自语、游戏或视频里的声音。
   - 电脑、手机正在播放的声音也调用 ignore：比如用“帮您……”“为您……”这种助手、客服口吻说的话，“你好，我是某某”这种自我介绍，广告、新闻、视频台词。
   - 拿不准的，也调用 ignore（如果真是对你说的，用户会再说一遍）。
{desktop_rules}

现在时间：{now}（今天是 {today}，昨天是 {yesterday}）"""

WINDOWS_DESKTOP_RULES = """11. 你是用户的秘书。用户让你完成需要真正动手的工作（写作业、写代码、写文章、做表格、查资料、整理文件、做计划……），调用 delegate_task 交给“干活的 AI”；用户点名“让 DeepSeek/豆包/Claude……做”，就交给那个 AI。你只负责把任务转述清楚、交出去，不要自己去完成这类任务。打招呼、闲聊、讲笑话这类简单的话，你自己直接回答。
12. 用户想换“大脑”（换谁来回答问题、陪他聊天）时调用 set_brain，比如“让 Claude 当我的脑子”“换成 GPT 回答我”“用 Claude 回答我”“换回本地”“换回 Gemma”。用户说“让 Claude 接管电脑”“Claude 全权接管”时，set_brain 选 Claude接管。注意区别：让某个 AI 去完成一件具体的工作（写作业、写代码、查资料），是 delegate_task；只是想换一个更聪明的来回答问题，是 set_brain。
13. 用户让你操作电脑时（打开软件、切换或关闭窗口、点按钮、打字、按快捷键、翻页、打开网页、搜索、看电量），调用对应的工具：
   - 打开软件用 open_app；打开网站、在网上搜东西用 open_web；“这个窗口”“当前窗口”指最前面的窗口。
   - 用户说“这个记事本”“当前的浏览器”这类话，说明软件已经开着：不要再 open_app（会多开一个），直接打字或点击；它不在最前面就先用 window 切过去。
   - 要点窗口里的东西，知道按钮上写什么字就直接 click；不知道时先 read_screen 看一眼，再按编号 click。
   - 多步的事（比如“打开记事本，写一句你好”）一步一步做：每做一步看结果，再做下一步，全部做完再用一句话回答。某一步失败了，照实告诉用户。
   - 运行命令、删除文件、睡眠、关机、重启是危险操作：直接调用工具就行，系统会自动请用户确认，你不要自己先问。
   - 用户说“搜一下”“查一下”某个东西、想在浏览器里看，用 open_web；要 AI 写成文档、做长篇研究的工作才用 delegate_task。"""

MAC_DESKTOP_RULES = "11. Mac 版支持语音问答、音量、记忆和调度员。用户要求打开软件、点击、打字、委派到桌面或让 Claude 接管时，说明此操作暂不支持。"

# ---------------------------------------------------------------------------
# 工具说明书：告诉模型有哪些工具、各自做什么、需要什么参数
# 模型只能“看到”这份说明，看不到具体代码，所以描述要写清楚
# ---------------------------------------------------------------------------


def _tool(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


TOOLS = [
    _tool("get_volume", "查看电脑当前的音量百分比，以及是否处于静音状态。"),
    _tool("set_volume", "把电脑音量设置为指定的百分比。用户说出了具体数值时使用，例如“音量调到50”。",
          {"percent": {"type": "integer", "description": "目标音量，0 到 100 之间的整数"}},
          ["percent"]),
    _tool("change_volume", "在当前音量的基础上调大或调小。用户没说目标数值、只说调大调小时使用。",
          {"delta": {"type": "integer",
                     "description": "变化量：正数调大，负数调小。例如 10 表示调大 10，-10 表示调小 10"}},
          ["delta"]),
    _tool("mute", "把电脑静音。"),
    _tool("unmute", "取消电脑的静音。"),
    _tool("remember", "把关于用户的一条长期信息（习惯、喜好、称呼等）记进长期记忆，以后每次对话都会参考。",
          {"fact": {"type": "string",
                    "description": "要记住的内容，用一句简短完整的话，例如“晚上习惯把音量开到 20%”"}},
          ["fact"]),
    _tool("forget", "从长期记忆里删掉一条信息。",
          {"keyword": {"type": "string", "description": "要忘掉的那条记忆里的关键词，尽量直接用长期记忆里的原话"}},
          ["keyword"]),
    _tool("search_history", "查找以前的对话记录。用户问起以前做过什么、说过什么，而前面的对话里没有时使用。",
          {"keyword": {"type": "string", "description": "关键词，可以用空格隔开几个，例如“音量”。不填就不限"},
           "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD。不填就查所有日期"}}),
    _tool("delegate_task",
          "把需要真正动手的工作交给“干活的 AI”去做（写作业、写代码、写文章、做表格、查资料、整理文件等）。"
          "你只负责把任务转述清楚并交出去，交出去以后就不用管了。",
          {"task": {"type": "string",
                    "description": "转述后的完整任务说明：用通顺的中文写清楚用户要什么、有哪些要求（数量、格式、时间、风格等）。"
                                   "用户说的每个要求都要写进去，不能漏；用户没说的要求不要自己加。语音识别的错别字要改正。"},
           "title": {"type": "string", "description": "任务的简短名称，10 个字以内，例如“数学作业”“贪吃蛇游戏”"},
           "worker": {"type": "string", "enum": list(workers.WORKERS),
                      "description": f"交给哪个 AI。用户没点名就填 {workers.DEFAULT_WORKER}。"
                                     "语音识别可能把名字写成别的样子：" + "；".join(
                                         f"{w}（也可能是 {'、'.join(c['aliases'])}）：{c['about']}"
                                         for w, c in workers.WORKERS.items())}},
          ["task", "title"]),
    _tool("set_brain",
          "换“大脑”：换谁来回答用户的问题、陪用户聊天。用户说“让 Claude 当我的脑子”“换成 GPT 回答我”“换回本地”时使用；"
          "说“让 Claude 接管电脑”“Claude 全权接管”时选 Claude接管。（让某个 AI 去完成具体工作不是这个，是 delegate_task）",
          {"brain": {"type": "string", "enum": list(brains.BRAINS),
                     "description": "换成哪个大脑。语音识别可能写成别的样子：" + "；".join(
                         f"{b}（也可能是 {'、'.join(c['aliases'])}）：{c['about']}" for b, c in brains.BRAINS.items())}},
          ["brain"]),
    _tool("set_voice",
          "更换你说话的声音。用户说“换个声音”“换成晓晓”“换个女声/男声”“用离线的声音”之类的话时使用。"
          "用户只说男声/女声时，选列表里第一个符合的。",
          {"voice": {"type": "string", "enum": [name for name, _ in speak.VOICES.values()],
                     "description": "可选的声音：" + "；".join(f"{name}（{about}）"
                                                           for name, about in speak.VOICES.values())}},
          ["voice"]),
    *computer.TOOLS,  # 操作电脑的工具：打开软件、窗口、看屏幕、点击、打字、快捷键……（见 computer.py）
    # 参考小智（xiaozhi-esp32-server）的 handle_exit_intent：用户想结束对话时，说句告别就停止聆听
    _tool("end_conversation",
          "用户想结束这次对话、让你别再听了时调用：比如“进入静默模式”“你先歇着吧”“先这样，有事再叫你”“我去打游戏了，你别听了”。"
          "调用后你会说一句简短的告别，然后回去待命，要再喊你的名字才会醒。"
          "注意：“静音”“把声音关掉”是调电脑音量，不是这个。",
          {"say_goodbye": {"type": "string", "description": "一句很短的告别，比如“好的，有事再叫我。”"}}),
]

# “忽略”工具：只在对话窗口的后续几句提供（唤醒后的第一句一定是对助手说的，不给它忽略的选项）
IGNORE_TOOL = _tool("ignore", "这句话不是对你说的（闲聊、和队友说话、游戏或视频里的声音等），忽略它，不回答、不执行。")

VOLUME_FUNCTIONS = {
    "get_volume": volume.get_volume,
    "set_volume": volume.set_volume,
    "change_volume": volume.change_volume,
    "mute": volume.mute,
    "unmute": volume.unmute,
}

MAC_UNAVAILABLE_TOOLS = {*computer.FUNCTIONS, "delegate_task", "claude_takeover"}


def available_tools():
    if sys.platform != "darwin":
        return TOOLS
    tools = [tool for tool in TOOLS if tool["function"]["name"] not in MAC_UNAVAILABLE_TOOLS | {"set_brain"}]
    tools.append(_tool("set_brain", "选择回答问题、陪用户聊天的大脑。",
                       {"brain": {"type": "string", "enum": [name for name in brains.BRAINS if name != brains.TAKEOVER]}},
                       ["brain"]))
    return tools

# “只说不做”检查：模型没调用任何工具，回复却像是做了事（“音量已恢复到 25%”“已取消静音”“记住了”），
# 说明它在“演”，其实什么都没发生。这种情况让它重新来一次、真的调用工具。
ACTION_CLAIM = re.compile(
    r"音量[^。！？\n]{0,8}(调|设|恢复|改|降|升)[^。！？\n]{0,8}\d+"
    r"|(当前|现在)的?音量(是|为|设定在|在)?\s*\d+"
    r"|(已|已经)(为您|帮您|给您|帮你|为你)?(静音|取消静音|恢复|调|设置|记住|记下|交给|换成|切换)"
    r"|(已|已经)(为您|帮您|给您|帮你|为你)?(打开|关闭|关掉|点击|点了|输入|搜索|最小化|最大化|删除|运行|锁屏)"
    r"|(记住了|记下了)"
    r"|(帮您|帮你|为您|为你|给您|给你)(打开|关闭|关掉|点|输入|搜索|切换)[^。！？\n]{0,10}了"
    # 光答应不动手：“我这就为您恢复”“马上帮您调”“这就为您打开”
    r"|(这就|马上|立刻|现在就)(为您|帮您|给您|帮你|为你)?(恢复|调|设|静音|取消|记|交给|换|切换|打开|关|点|输入|搜)"
)
MAX_NUDGES = 2  # 最多提醒它几次
NUDGE = ("（系统提示：你刚才只是用文字说做了，但没有调用任何工具，实际上什么都没发生。"
         "请调用对应的工具真正去做；如果其实不需要做任何事，就照实回答，不要说已经做了。）")
FAILED_REPLY = "抱歉，刚才这件事我没能真正做到，请再说一次。"

# 这些工具执行完，直接把工具返回的中文说明当回复，不用再问一遍模型（更快）。
# 不在这里的工具（search_history），结果要交回给模型，让它看完再回答或接着做。
FAST_REPLY_TOOLS = set(VOLUME_FUNCTIONS) | {"remember", "forget", "ignore", "delegate_task", "set_voice",
                                            "set_brain", "end_conversation"}
GOODBYE = "好的，有事再叫我。"


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------


def build_system_prompt(mem, now, model=None):
    facts = mem.profile.facts()
    mac = sys.platform == "darwin"
    return SYSTEM_PROMPT.format(
        name=ASSISTANT_NAME,
        platform="Mac" if mac else "Windows 笔记本",
        desktop_rules=MAC_DESKTOP_RULES if mac else WINDOWS_DESKTOP_RULES,
        model=model or local_models.reasoning_model(),
        persona=persona_text(),
        now=f"{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}",
        today=f"{now:%Y-%m-%d}",
        yesterday=f"{now - timedelta(days=1):%Y-%m-%d}",
        profile="\n".join(f"- {f}" for f in facts) if facts else "（暂无）",
    )


def run_tool(name, args, mem, text=""):
    """执行模型要求调用的工具。出错时不让程序崩溃，而是返回错误说明。text 是用户的原话。"""
    if sys.platform == "darwin" and name in MAC_UNAVAILABLE_TOOLS:
        return {"ok": False, "message": computer.MAC_UNSUPPORTED}
    if name != "ignore":
        status.set("执行", TOOL_NAMES.get(name, name))
    if name == "delegate_task":
        args = {**args, "said": text}  # 把你的原话也写进任务说明，方便干活的 AI 对照
        func = delegate.delegate
    elif name in VOLUME_FUNCTIONS:
        func = VOLUME_FUNCTIONS[name]
    elif name == "remember":
        func = mem.profile.remember
    elif name == "forget":
        func = mem.profile.forget
    elif name == "search_history":
        func = mem.history.search
    elif name == "set_voice":
        func = speak.set_voice
    elif name == "set_brain":
        func = brains.set_brain
    elif name in computer.FUNCTIONS:
        func = computer.FUNCTIONS[name]
    elif name == "ignore":
        return {"message": ""}
    elif name == "end_conversation":
        words = str(args.get("say_goodbye") or "").strip() or GOODBYE
        return {"ok": True, "message": words[:40], "end": True}  # 对话窗口看到 end 就回去待命
    else:
        friendly.LOG.warning("请求了未知工具：%s", name)
        # message 是念给人听的；detail 只交给模型，让它知道错在哪、能改了再试
        return {"message": friendly.MESSAGES["error"], "detail": f"没有名为 {name} 的工具"}
    try:
        return func(**args)
    except Exception as exc:
        friendly.LOG.exception("工具执行失败：%s", name)
        return {"message": friendly.MESSAGES[friendly.failure(exc)],
                "detail": f"执行 {name} 时出错：{exc}"[:300]}  # 比如参数名不对、数值不是数字


def warm_up():
    """把模型加载进显存，这样说话时不用等加载。返回加载耗时（秒）。"""
    start = time.time()
    local_models.chat(messages=[], options=OPTIONS, keep_alive=KEEP_ALIVE)
    return time.time() - start


def load_in_background():
    """在后台加载模型（唤醒时调用：你说第一句话的同时，模型就在加载了）。"""
    def prepare():
        try:
            warm_up()
        except Exception:
            friendly.LOG.exception("后台准备本地模型失败")

    worker = threading.Thread(target=prepare, daemon=True)
    worker.start()
    computer.prefetch()  # 顺便把开始菜单的软件列表读好，说“打开某某”时不用等
    return worker


def unload():
    """把模型从显存里卸载（对话窗口关闭、复盘完成后调用），把显存还给游戏等其他程序。"""
    local_models.release()


def model_loaded():
    """大模型现在在不在显存里（状态窗用来提示“第一次要加载，约 5 秒”）。"""
    try:
        return any(m.model == local_models.reasoning_model() for m in ollama.ps().models)
    except Exception:
        return True


def _timed_voice_chat(stage, **kwargs):
    """收齐整轮输出后再执行工具，只记录模型输出的时间。"""
    if not status.LOG_ENABLED or status.barge_turn.get()[0] is None:
        return local_models.chat(**kwargs).message
    status.barge_event("gemma_start", stage=stage)
    parts, calls = [], []
    first_text = False
    for chunk in local_models.chat(stream=True, **kwargs):
        content = chunk.message.content or ""
        if content.strip() and not first_text:
            status.barge_event("gemma_first_text", stage=stage)
            first_text = True
        parts.append(content)
        calls.extend(chunk.message.tool_calls or [])
        if chunk.done:
            status.barge_event("gemma_end", stage=stage,
                               load_ms=(chunk.load_duration or 0) / 1_000_000,
                               prompt_ms=(chunk.prompt_eval_duration or 0) / 1_000_000,
                               generation_ms=(chunk.eval_duration or 0) / 1_000_000,
                               prompt_tokens=chunk.prompt_eval_count)
    return ollama.Message(role="assistant", content="".join(parts), tool_calls=calls)


def handle(text, mem=None, first=True, *, voice_dispatch=None):
    """
    处理一句话，返回一个字典：
        text     你说的话
        actions  调用了哪些工具，每项包含 tool（工具名）、args（参数）、result（执行结果）
        reply    给你的回复（之后语音回复时就把它读出来）
        ignored  True 表示模型判断这句话不是对它说的（放进暂存区，等复盘）
        seconds  模型一共用了多少秒

        first    是不是唤醒后的第一句。第一句一定是对助手说的；之后的话可能是闲聊，
                 所以会多给模型一个 ignore（忽略）工具。
    对助手说的话：存进短期记忆，并马上写进历史记录。
    不是对助手说的话：只放进暂存区（内存），不写硬盘。
    """
    mem = mem or MEMORY
    now = datetime.now()

    # 有危险操作在等你确认：先看这句话是不是“确定”或“取消”（这一步不经过模型，模型没法替你确认）
    waiting = computer.pending()
    if waiting:
        decision = computer.answer(text)
        if decision:
            return _finish_confirmation(text, waiting, decision, mem, now)
        computer.cancel()  # 说了别的事：等待中的危险操作作废，照常处理这句话

    # Claude 刚才在问你“确定吗”（接管或者自动求助时）：你回“确定”就接着交给它（同一个会话，它记得要做什么）
    if time.time() - _claude_asked["time"] < 120:
        _claude_asked["time"] = 0.0
        decision = computer.answer(text)
        if decision == "yes":
            return _takeover(text, mem, now, prompt=f"用户说：{text}（同意了你刚才问的操作）。请去做，做完简短说结果。")
        if decision == "no":
            return _simple_turn(text, "好的，不做了。", mem, now)

    # 问夜间研究的结果：直接从研究日报里念（本地，免费）
    if RESEARCH_ASK.search(text):
        return _simple_turn(text, research_reply(), mem, now)

    # Claude 全权接管：这句话整个交给 Claude（换大脑、退出接管这类话还是本地处理）
    if brains.current() == brains.TAKEOVER and not takeover.SWITCH_WORDS.search(text) and takeover.available():
        if not first and not addressed(text, mem):
            # 后面几句可能是在和别人说话：本地先过滤（免费），不是对助手说的就不发给 Claude，省额度
            mem.add_pending(text, now)
            return {"text": text, "actions": [], "reply": "", "ignored": True, "uncertain": False, "seconds": 0.0}
        return _takeover(text, mem, now)

    if voice_dispatch is not None:
        dispatched = voice_dispatch(text)
        if dispatched:
            return _simple_turn(text, dispatched, mem, now)

    tools = available_tools()
    if not first:
        tools = tools + [IGNORE_TOOL]
    status.set("思考中", "" if model_loaded() else friendly.MESSAGES["preparing"])

    model = local_models.reasoning_model()
    user_message = {"role": "user", "content": text}
    messages = ([{"role": "system", "content": build_system_prompt(mem, now, model)}]
                + mem.short_term.messages()
                + [user_message])
    turn = [user_message]  # 这一轮新增的消息，结束后存进短期记忆

    actions = []
    reply = None
    natural = False  # 回复是不是模型自己说的话（是的话可以直接念；否则是工具的说明，要先润色）
    nudges = 0       # 因为“只说不做”提醒过它几次
    seconds = 0.0
    for step in range(1, MAX_ROUNDS + 1):
        start = time.time()
        message = _timed_voice_chat(
            step,
            model=model,
            messages=messages,
            tools=tools,
            think=False,  # 关掉“思考”，回答更快
            options={**OPTIONS, "temperature": 0.1},  # 决定调不调工具时尽量稳，不“自由发挥”
            keep_alive=KEEP_ALIVE,
        )
        seconds += time.time() - start
        calls = message.tool_calls or []

        if not calls:
            # 模型直接回答，没有调用工具（这种回答本来就是口语风格，可以直接念）
            reply = (message.content or "").strip()
            if not actions and ACTION_CLAIM.search(reply):
                if nudges < MAX_NUDGES:
                    # 只说不做：提醒它，让它真的调用工具（这些提醒不进短期记忆）
                    nudges += 1
                    messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": NUDGE}]
                    continue
                reply = FAILED_REPLY  # 提醒过了还是只说不做：老实说没做到，不能骗你
            natural = True
            break

        # 模型要调用工具：先把“它要调用什么”记下来
        call_message = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [{"function": {"name": c.function.name,
                                         "arguments": dict(c.function.arguments or {})}} for c in calls],
        }
        messages.append(call_message)
        turn.append(call_message)

        # 逐个执行，并把结果告诉模型
        round_actions = []
        for c in calls:
            name = c.function.name
            args = dict(c.function.arguments or {})
            status.barge_event("tool_start", stage=step)
            try:
                result = run_tool(name, args, mem, text)
            finally:
                status.barge_event("tool_end", stage=step)
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False)})
            # 存进记忆/历史的是简短版本（read_screen 的整张清单不存，省地方，也不把屏幕上的字写进硬盘）
            short = computer.brief(name, result)
            round_actions.append({"tool": name, "args": args, "result": short})
            turn.append({"role": "tool", "tool_name": name, "content": json.dumps(short, ensure_ascii=False)})
        actions += round_actions

        asking = next((a for a in round_actions if a["result"].get("needs_confirm")), None)
        if asking:
            # 危险操作：已经登记好了，原话问你“确定吗”，等你下一句回答（不让模型接着做）
            reply = asking["result"]["message"]
            natural = True
            break

        if all(a["tool"] in FAST_REPLY_TOOLS or a["result"].get("message") == computer.MAC_UNSUPPORTED
               for a in round_actions):
            # 都是“做完就行”的工具：直接用工具返回的中文说明当回复，不再问模型，省时间
            reply = "；".join(a["result"]["message"] for a in round_actions)
            break
        # 否则（比如刚查了历史），进入下一轮，让模型看完结果再回答或接着做

    if reply is None:  # 来回次数用完了还没结束
        reply = "；".join(a["result"].get("message", "") for a in actions) or "抱歉，这个我没能完成。"

    # 自动求助：本地动手了但没做成（找不到窗口、点不到按钮、工具报错），交给 Claude 接手这一件事
    last = actions[-1]["result"] if actions else {}
    failed = (bool(actions) and last.get("ok") is False and not last.get("needs_confirm")) or reply == FAILED_REPLY
    if failed and settings.get("auto_help", True) and takeover.available():
        tried = "；".join(a["result"].get("message", "") for a in actions if a["result"].get("ok") is False)
        return _takeover(text, mem, now, escalated=True, before=[a for a in actions if a["tool"] != "ignore"],
                         note=f"（本地助手刚才试过，没做成：{tried[:300] or '它没能真正动手'}。请你接手把这件事做完。）")

    for action in reversed(actions):
        result = action["result"]
        if not result.get("ok") and result.get("message") in friendly.MESSAGES.values():
            reply = result["message"]
            natural = True
            break

    # 云端只处理问答；操作由本地完成。本地回答留作云端不可用时的备用。
    used_brain = brains.LOCAL
    if not actions and natural and reply != FAILED_REPLY and brains.current() != brains.LOCAL:
        reply, used_brain = ask_brain(text, mem, local_reply=reply)

    # 模型只调用了 ignore：这句话不是对助手说的 → 放进暂存区，不进短期记忆、不写历史
    ignored = bool(actions) and all(a["tool"] == "ignore" for a in actions)
    if ignored:
        mem.add_pending(text, now)
        return {"text": text, "actions": actions, "reply": "", "ignored": True,
                "uncertain": False, "seconds": seconds}

    reply = reply or friendly.MESSAGES["error"]

    # 唤醒后的后续几句里，模型没调用任何工具、只是聊天回答：
    # 这句话可能是在和别人说话（比如“兄弟你那边卡不卡”）。保险起见先放进暂存区，交给复盘决定。
    if not first and not actions:
        mem.add_pending(text, now, reply)
        return {"text": text, "actions": [], "reply": reply, "ignored": False,
                "uncertain": True, "brain": used_brain, "seconds": seconds}

    actions = [a for a in actions if a["tool"] != "ignore"]
    turn.append({"role": "assistant", "content": reply})
    mem.add_turn(text, actions, reply, turn, now)
    return {"text": text, "actions": actions, "reply": reply, "ignored": False,
            "uncertain": False, "natural": natural, "brain": used_brain, "seconds": seconds,
            "end": any(a["tool"] == "end_conversation" for a in actions)}


RESEARCH_ASK = re.compile(r"(昨晚|昨天晚上|夜里).{0,8}(研究|发现|报告|学到|看了)|研究日报|夜间研究")


def research_reply():
    """“昨晚研究了什么”：念最近一次夜间研究的总结和最值得做的三条。"""
    import research
    news = research.latest()
    if not news or not news.get("projects"):
        return "最近还没有夜间研究的结果。研究每天凌晨三点开始，额度够的时候才会做。"
    numbers = "一二三"
    top = "；".join(f"{numbers[i]}，{t.rstrip('。')}" for i, t in enumerate(news.get("top", [])[:3]))
    day = news.get("time", "")[:10]
    return (f"{day} 夜里研究了{'、'.join(news['projects'])}。{news.get('summary', '').rstrip('。')}。"
            + (f"最值得做的：{top}。" if top else "") + "完整报告在 AI 任务文件夹的星期八研究日报里。")


def _simple_turn(text, reply, mem, now):
    """不用模型、直接回一句话的一轮（比如“好的，不做了”）。"""
    mem.add_turn(text, [], reply, [{"role": "user", "content": text}, {"role": "assistant", "content": reply}], now)
    return {"text": text, "actions": [], "reply": reply, "ignored": False, "uncertain": False,
            "natural": True, "brain": brains.current(), "seconds": 0.0}


def _recent_history(mem):
    """最近几轮对话：只带“你说的话”和“助手最后的回答”（交给云端时用，省额度）。"""
    history = []
    for turn in mem.short_term.turns:
        said = turn[0].get("content", "")
        final = turn[-1].get("content", "") if turn[-1].get("role") == "assistant" else ""
        if said:
            history.append({"role": "user", "content": said})
        if final:
            history.append({"role": "assistant", "content": final})
    return history


_claude_asked = {"time": 0.0}   # Claude 最近一次在问你“确定吗”的时间（2 分钟内你回“确定”就接着交给它）


def _takeover(text, mem, now, prompt=None, note="", escalated=False, before=None):
    """交给 Claude Code 去做（Claude 接管，或者本地没做成时的自动求助）。
    用哪个模型：智能选择时按难度挑（简单 Haiku / 一般 Sonnet / 复杂 Opus），没做成就换高一档再试一次。"""
    start = time.time()
    facts = mem.profile.facts()
    try:
        # 本地没做成才交过来的，至少用 Sonnet；“确定”这类回答沿用上一句的难度（这里按一般算）
        key, why = claude_models.choose(text, at_least="sonnet" if escalated or prompt else None)
        tries = []
        while True:
            model = claude_models.MODELS[key]
            status.set("思考中", f"Claude（{model['name']}）正在想")
            r = takeover.run(prompt or text, history=_recent_history(mem), name=ASSISTANT_NAME, note=note,
                             profile="\n".join(f"- {f}" for f in facts) if facts else "（暂无）",
                             model=model["id"], effort=model["takeover"])
            claude_models.record(key, r["cost"])
            tries.append(f"{model['name']}（{why}）")
            upgrade = claude_models.stronger(key)
            if claude_models.current() != claude_models.AUTO or not upgrade or not claude_models.looks_failed(r):
                break
            key, why, prompt, note = upgrade, "上一个没做成，换高一档", "刚才没做成。请换个办法再试一次，把这件事做完。", ""
        if r["error"]:
            friendly.LOG.error("云端接管失败：%s", r["reply"])
            reply = friendly.MESSAGES[friendly.cloud_failure(r["reply"])]
        else:
            reply = ("本地没做成，我请 Claude 接手了。" if escalated else "") + r["reply"]
        if r["needs_confirm"] and not r["error"]:
            _claude_asked["time"] = time.time()  # 它在问你“确定吗”：你下一句的回答交回给它
        result = {"ok": not r["error"],
                  "message": reply if r["error"] else "；".join(r["steps"]) or "（没有动手，直接回答）",
                  "needs_confirm": r["needs_confirm"] and not r["error"],
                  "cost_usd": r["cost"], "model": " → ".join(tries)}
    except (takeover.NotReady, subprocess.TimeoutExpired, ConnectionError, TimeoutError):
        friendly.LOG.exception("云端帮手暂时不可用")
        reply = friendly.MESSAGES["offline"]
        result = {"ok": False, "message": reply}
    except Exception as exc:
        friendly.LOG.exception("云端接管失败")
        reply = friendly.MESSAGES[friendly.cloud_failure(exc)]
        result = {"ok": False, "message": reply}
    actions = (before or []) + [{"tool": "claude_takeover", "args": {"said": text}, "result": result}]
    mem.add_turn(text, actions, reply, [{"role": "user", "content": text}, {"role": "assistant", "content": reply}], now)
    return {"text": text, "actions": actions, "reply": reply, "ignored": False, "uncertain": False,
            "natural": True, "brain": brains.TAKEOVER, "seconds": time.time() - start}


ADDRESSED_PROMPT = """用户叫醒语音助手“{name}”之后，麦克风一直开着。下面是麦克风刚听到的一句话，判断它是不是对助手说的。
- 是：让助手做事、问助手问题、回答助手刚才的提问（比如“好的”“确定”“第二个”“再大一点”）。
- 不是：和别人聊天、打游戏时和队友说话、感叹和自言自语、电脑或手机播放出来的声音（广告、视频台词、别的助手说的话）。
- 拿不准的，算“不是”（真是对助手说的，用户会再说一遍）。

助手刚才说的话：{last}
听到的话：{text}

只输出 JSON。"""
ADDRESSED_FORMAT = {"type": "object", "properties": {"for_assistant": {"type": "boolean"}}, "required": ["for_assistant"]}


DIFFICULTY_PROMPT = """判断用户交给电脑助手的这件事有多难，用来决定请哪个 AI 来做（越难的越贵，所以别高估）。
- 简单：查个信息（时间、电量、磁盘、开着什么程序、天气）、打开或关掉软件、一两步就能做完的操作、简单的问答和闲聊。
- 一般：要好几步的操作、整理一批文件、写个小脚本、操作软件界面、上网查资料再总结。
- 复杂：写或修改程序、排查问题和报错、需要深入思考的分析和规划、很长的任务。

这件事：{text}

只输出 JSON。"""
DIFFICULTY_FORMAT = {"type": "object", "properties": {"level": {"type": "string", "enum": ["简单", "一般", "复杂"]}},
                     "required": ["level"]}


def classify_difficulty(text):
    """这件事有多难：简单 / 一般 / 复杂（本地 gemma 判断，免费；用来挑 Claude 的模型）。"""
    response = local_models.chat(messages=[{"role": "user", "content": DIFFICULTY_PROMPT.format(text=text)}],
                           format=DIFFICULTY_FORMAT, think=False, options={**OPTIONS, "temperature": 0},
                           keep_alive=KEEP_ALIVE)
    return json.loads(response.message.content).get("level")


claude_models.classifier = classify_difficulty


def addressed(text, mem):
    """这句话是不是对助手说的（Claude 接管时，本地先过滤一遍闲聊，免费，不浪费 Claude 的额度）。"""
    turns = mem.short_term.turns
    last = turns[-1][-1].get("content", "") if turns and turns[-1][-1].get("role") == "assistant" else "（无）"
    try:
        response = local_models.chat(messages=[{"role": "user", "content": ADDRESSED_PROMPT.format(
            name=ASSISTANT_NAME, last=last, text=text)}], format=ADDRESSED_FORMAT, think=False,
            options={**OPTIONS, "temperature": 0}, keep_alive=KEEP_ALIVE)
        return bool(json.loads(response.message.content).get("for_assistant"))
    except Exception:
        return True  # 判断不了就当是对助手说的


def _finish_confirmation(text, waiting, decision, mem, now):
    """你回答了“确定吗？”：确定就执行登记好的危险操作，取消就作废。不经过模型。"""
    if decision == "yes":
        status.set("执行", TOOL_NAMES.get(waiting["tool"], waiting["tool"]))
        result = computer.confirm()
        actions = [{"tool": waiting["tool"], "args": waiting["args"], "result": result}]
    else:
        result = computer.cancel()
        actions = []
    reply = result["message"]
    turn = [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
    mem.add_turn(text, actions, reply, turn, now)
    return {"text": text, "actions": actions, "reply": reply, "ignored": False, "uncertain": False,
            "natural": decision != "yes", "brain": brains.LOCAL, "seconds": 0.0}


# ---------------------------------------------------------------------------
# 云端大脑：让 Claude / GPT 来“动脑”回答（见 brains.py）
# ---------------------------------------------------------------------------

BRAIN_PROMPT = """你的名字叫“{name}”，是用户的私人语音助手兼秘书。现在由你来动脑，回答用户的问题、陪用户聊天。
{persona}
你的回答会被念出来：直接回答，不用 Markdown、列表或表情；一般一到三句话。
不要说自己是哪家公司的哪个模型，除非用户专门问你底层用的是什么模型。
（调音量、开软件、点按钮、记东西、交任务这些操作由别的程序负责，你只管回答。）
现在时间：{now}

【关于用户的长期记忆】
{profile}"""


def ask_brain(text, mem, local_reply=""):
    """
    让当前选中的云端大脑回答。返回 (回答, 实际用的大脑名字)。
    大脑用不了（没填密钥、断网、额度用完）时，退回本地模型刚才的回答，并说明原因。
    """
    brain = brains.current()
    status.set("思考中", f"{brain} 正在想（云端）")
    history = []
    for turn in mem.short_term.turns:  # 最近几轮对话：只带“你说的话”和“助手最后的回答”
        said = turn[0].get("content", "")
        final = turn[-1].get("content", "") if turn[-1].get("role") == "assistant" else ""
        if said:
            history.append({"role": "user", "content": said})
        if final:
            history.append({"role": "assistant", "content": final})
    now = datetime.now()
    facts = mem.profile.facts()
    system = BRAIN_PROMPT.format(
        name=ASSISTANT_NAME, persona=persona_text(),
        now=f"{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}",
        profile="\n".join(f"- {f}" for f in facts) if facts else "（暂无）",
    )
    try:
        reply = brains.ask(system, history + [{"role": "user", "content": text}], brain)
        if not reply or not reply.strip():
            raise brains.BrainError("未收到云端回复")
        return reply, brain
    except brains.BrainError:
        friendly.LOG.exception("云端回答失败")
        return friendly.MESSAGES["offline"] + local_reply, brains.LOCAL


SPOKEN_PROMPT = """你叫“{name}”，是用户的语音助手。{persona}
你刚刚替用户做完了一件事。请用一句自然的口语，把结果告诉用户。
只根据下面的执行结果说，不要编造没做的事；数字照实说；不要复述用户的原话。"""


def spoken_reply(result):
    """
    把执行结果变成一句有人情味的话，用来念给你听。
    例如“音量已从 14% 设为 30%”→“好的，音量调到百分之三十了。”
    模型自己说的话本来就是这种风格，直接用。出错时就念原来的结果。
    """
    if result.get("natural") or not result.get("actions") or result.get("end"):
        return result["reply"]  # 告别语本来就是口语，不用再润色
    for action in reversed(result["actions"]):
        message = action["result"].get("message")
        if not action["result"].get("ok") and message in friendly.MESSAGES.values():
            return message
    if all(action["tool"] in VOLUME_FUNCTIONS for action in result["actions"]):
        return result["reply"]
    status.set("组织语言", "把结果说得自然一点")
    try:
        message = _timed_voice_chat(
            "spoken_reply",
            messages=[{"role": "system", "content": SPOKEN_PROMPT.format(name=ASSISTANT_NAME, persona=persona_text())},
                      {"role": "user", "content": f"用户说：{result['text']}\n执行结果：{result['reply']}"}],
            think=False,
            options={**OPTIONS, "num_predict": 80, "temperature": 0.7},
            keep_alive=KEEP_ALIVE,
        )
        return (message.content or "").strip() or result["reply"]
    except Exception:
        friendly.LOG.exception("整理语音回复失败")
        return result["reply"]


# ---------------------------------------------------------------------------
# 复盘：对话窗口关闭后，在后台把暂存区过一遍，决定留下什么、删掉什么、记住什么
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """你在帮一个语音助手做“复盘”。用户唤醒助手后，麦克风一直开着。下面是这段时间里听到的话。

【对助手下的指令】（已经执行，已经保存，这里只给你参考）
{commands}

【其他听到的话】（助手判断可能不是对它说的：可能是用户和别人聊天、打游戏时和队友说话、队友的语音、游戏或视频的声音）
{pending}

【用户已有的长期记忆】
{profile}

请完成两件事：
1. items：把【其他听到的话】逐句分类，每一句都要给出，type 只能是下面之一：
   - "闲聊"：随口聊天、感叹、自言自语、和别人寒暄
   - "游戏或队友"：游戏里的交流（报点、战术、输赢）、队友说的话、游戏或视频里的声音
   - "播放的声音"：电脑或手机播放出来的话，比如用“帮您……”“为您……”这种助手、客服口吻说的话，“你好，我是某某”这种自我介绍，广告、新闻、视频台词（这些不是用户自己的事，不要当成用户的安排或习惯）
   - "安排或待办"：用户提到自己要做的事、时间安排、需要提醒的事（比如“明天下午三点开会”）
   - "习惯或喜好"：用户说到自己的习惯、喜好、要求
   - "其他有用信息"：以后可能需要回头查的信息
2. facts：从这段时间里，总结出关于用户本人的、明确且长期的习惯或喜好（比如“打游戏时习惯把音量开到 40%”）。必须是用户自己清楚说出来的；已有的长期记忆不要重复，一次性的事情不算，别人的事情不算，“播放的声音”里的内容不算，不要靠猜。没有就留空，最多 3 条，每条一句简短完整的话。

只输出 JSON。"""

REVIEW_FORMAT = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "type": {"type": "string",
                             "enum": ["闲聊", "游戏或队友", "播放的声音", "安排或待办", "习惯或喜好", "其他有用信息"]},
                },
                "required": ["index", "type"],
            },
        },
        "facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "facts"],
}

# 复盘时，哪几类会被留在历史记录里（其余直接删除）
KEEP_TYPES = {"安排或待办", "习惯或喜好", "其他有用信息"}


def review(mem=None):
    """
    复盘这个对话窗口。返回 {"kept": 留下几句, "deleted": 删掉几句, "facts": [新记住的], "message": 一句话总结}
    暂存区里没被留下的话，就这样丢掉了，从来没写进过硬盘。
    """
    mem = mem or MEMORY
    records, pending = mem.take_window()
    if not pending:
        return {"kept": 0, "deleted": 0, "facts": [],
                "message": f"这次窗口执行了 {len(records)} 条指令，没有需要复盘的闲聊"}

    def line(r):
        answered = f"（助手当时回答了：{r['reply']}）" if r.get("reply") else ""
        return f"{r['time'][11:16]} {r['you']}{answered}"

    facts_now = mem.profile.facts()
    prompt = REVIEW_PROMPT.format(
        commands="\n".join(f"- {r['time'][11:16]} {r['you']}" for r in records) or "（无）",
        pending="\n".join(f"{i}. {line(p)}" for i, p in enumerate(pending)),
        profile="\n".join(f"- {f}" for f in facts_now) if facts_now else "（暂无）",
    )
    try:
        response = local_models.chat(messages=[{"role": "user", "content": prompt}],
                               format=REVIEW_FORMAT, think=False, options=OPTIONS, keep_alive=KEEP_ALIVE)
        data = json.loads(response.message.content)
    except Exception:
        # 复盘出错时，宁可全部删掉也不乱留（暂存区本来就没写进硬盘）
        friendly.LOG.exception("整理对话记录失败")
        return {"kept": 0, "deleted": len(pending), "facts": [],
                "message": f"刚才的记录没整理好，暂存的 {len(pending)} 句已删除"}

    kinds = {item.get("index"): item.get("type") for item in data.get("items", []) if isinstance(item, dict)}
    keep = [i for i in range(len(pending)) if kinds.get(i) in KEEP_TYPES]  # 没分类到的，当作删除
    for i in keep:
        mem.history.append({"time": pending[i]["time"], "you": pending[i]["you"], "actions": [],
                            "reply": pending[i].get("reply", ""), "note": f"复盘时保留（{kinds[i]}）"})

    new_facts = []
    for fact in data.get("facts", [])[:3]:
        if mem.profile.remember(fact, source="复盘发现")["message"].startswith("好的"):
            new_facts.append(fact)

    deleted = len(pending) - len(keep)
    message = f"保留 {len(keep)} 句，删除 {deleted} 句"
    message += f"，新记住 {len(new_facts)} 条：{'；'.join(new_facts)}" if new_facts else "，没有新的长期记忆"
    return {"kept": len(keep), "deleted": deleted, "facts": new_facts, "message": message}


def print_result(result):
    """把 handle() 的结果整齐地打印出来。"""
    if result.get("ignored"):
        print(f"  （不是对助手说的，先放进暂存区，复盘时再决定留不留）  模型用时 {result['seconds']:.1f} 秒")
        return
    if result.get("uncertain"):
        print(f"  助手：{result['reply']}")
        print("  （拿不准是不是对助手说的：照样回答，但先放进暂存区，复盘时再决定留不留）")
        return
    print(f"  模型用时：{result['seconds']:.1f} 秒")
    if result.get("brain", brains.LOCAL) != brains.LOCAL:
        print(f"  动脑的是：{result['brain']}（云端）")
    if not result["actions"]:
        print("  调用工具：（无）")
    for a in result["actions"]:
        args = json.dumps(a["args"], ensure_ascii=False)
        print(f"  调用工具：{a['tool']}({args})")
    print(f"  助手：{result['reply']}")


if __name__ == "__main__":
    print(f"正在加载模型 {local_models.reasoning_model()} ……")
    print(f"加载完成，用时 {warm_up():.1f} 秒。")
    print(f"记忆：{MEMORY.status()}")
    print("直接回车或输入“退出”结束。\n")
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("", "退出", "exit", "quit"):
            break
        print_result(handle(text))
        print()
    unload()
    print("模型已卸载。再见！")
