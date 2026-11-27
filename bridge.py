"""星期八的调度员：拆分口语，把清单交给群里的成员。"""

import json
import re

import friendly
import settings


VOICE_SETTING = "voice_dispatch"
ACKNOWLEDGEMENT = "好的，我来安排。"
CANCELLED = "这轮调度已停止。"
UNAVAILABLE_REPLY = friendly.MESSAGES["unavailable"]
SELF = "星期八"
PLACEHOLDERS = {
    "家电": "开关灯、空调、电视、窗帘、热水器、扫地机等家里的设备",
    "美团": "点外卖、找餐馆、团购",
    "淘宝": "网上买东西",
    "千问": "打车、订票、订酒店",
    "提醒": "定时提醒、闹钟、叫起床（星期八自己做，到点用语音说）",
}
LOCAL = ("提醒", SELF)  # 由星期八自己办的成员：不算“未接入”
IMMEDIATE = ("马上", "现在", "立即", "立刻", "立马", "马上做", "现在做")
REMIND = re.compile(r"提醒我|提醒一下|叫我|喊我|叫醒我|闹钟")
SELF_ROLE = "自己回答：闲聊、安慰、天气、时间、简单问答"
BRAIN_ROLE = "复杂任务规划、查资料、写文档、写代码、操作电脑"
BRAIN = r"(?:(?:Chat\s*)?GPT|Codex|Claude|克劳德)(?![A-Za-z0-9])"
ASK_BRAIN = re.compile(r"(?:问问|问一下|问下|问一问)\s*[,，:：]?\s*@?\s*" + BRAIN, re.I)
BOTH_BRAINS = re.compile(r"(?:Claude|克劳德)\s*(?:和|跟|与|、|及|/|以及)\s*@?\s*(?:Chat\s*)?GPT"
                         r"|(?:Chat\s*)?GPT\s*(?:和|跟|与|、|及|/|以及)\s*@?\s*(?:Claude|克劳德)", re.I)
DEPENDENCY = re.compile(r"(?:根据|按照)(?:它|他|她|上一步|前一步|查到的|找到的|选好的|.{0,12}的结果)"
                        r"|(?:结果|确认|通过|选好|定好|修好|查好|查完|写完|完成)(?:了|出来)?(?:之后|以后|后)?(?:，|,|就)?再")
ASSIGN = r"(?:交给|让|请|叫|派给|问问)\s*@?\s*"
ACTION = re.compile(r"开|关|调|拉|点|订|买|叫|打|查|写|发|找|做|修|扫|设|提醒|安排|问|整理|预约|取消|记录|看")
MULTI = re.compile(r"然后|接着|再帮我|再把|再给我|再提醒|再(?=开|关|点|查|买|订|叫|设|安排|修|写|问)"
                   r"|顺便|另外|对了|还有|同时|以及|并且|[，,。；;、]|(?<=了)再")
REQUEST = re.compile(r"帮我|麻烦|请|给我|我要|我想|能不能|可以|把|(?:开|关|点|订|买|查|写|修|发|找|设|调|叫|扫)(?:一下|个|一|好)")
BACKGROUND = re.compile(r"^\s*我(?:先|自己|准备)?(?:去|回|出门)")
COMPLETION_CLAIM = re.compile(r"(?:已经|已)(?:为|帮|替|安排好)|(?:把|为你|为您).{0,25}(?:开了|关了|拉上了)"
                              r"|(?:设置|预约|预订|下单|打开|关闭|拉上)(?:成功|完成|好了|了)")

SYSTEM = """你是家庭语音助手“星期八”的调度员。主人说了一段口语，你要把里面的任务拆出来，交给合适的成员。
成员和他们会做的事：
{roster}
规则：
1. 只拆出主人真正要做的事。“好累”“我先去洗澡”这类是背景，不是任务，但可以影响时间。
2. to 必须是成员名字，照抄冒号前的名字。do 用一句话写清要做什么。when 写“马上”、具体时间或完整条件。
3. 责任按要做的事确定，时间只影响 when。“一小时后叫车”交给千问，“一小时后提醒我叫车”才交给提醒。
   信息不够也照样分派，例如点外卖没说吃什么就在 ask 里问。无须追问填空字符串。标注未接入的成员也可以分派。
4. reply 是给主人的一句话。如果分派给了未接入的成员，reply 必须逐字写“{unavailable_reply}”
   其他分派只汇报交给了谁，设备或服务还没有执行。追问只写在 ask。单纯闲聊或简单问答可直接回答。
只输出 JSON：{{"tasks":[{{"to":"成员","do":"要做的事","when":"马上","ask":""}}],"reply":"一句话回复主人"}}"""


def voice_enabled():
    return bool(settings.get(VOICE_SETTING, False))


def matches_voice(text):
    """只把明显的分派和有多项动作的请求送来调度。"""
    if ASK_BRAIN.search(text):
        return True
    if "@" in text or re.search(ASSIGN, text):
        import groupchat

        names = [*PLACEHOLDERS, *(m["name"] for m in groupchat.members())]
        names_pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
        if re.search(r"(?:@|" + ASSIGN + ")(?:" + BRAIN + "|" + names_pattern + ")", text, re.I):
            return True
    if REMIND.search(text):
        import bridge_pending

        if bridge_pending.parse_when(text) is not None:
            return True  # “四十分钟后提醒我吃药”“明早七点叫我”：只有一件事也要记成定时提醒
    return (bool(REQUEST.search(text))
            and sum(bool(ACTION.search(part) and not BACKGROUND.match(part)) for part in MULTI.split(text)) > 1)


def _ask_gemma(text, roster):
    import assistant
    import local_models

    # 用和语音对话同一个模型、同样的上下文长度：不一样的话 Ollama 会把模型重新加载一遍，白等好几秒
    response = local_models.chat(
        model=local_models.reasoning_model(), think=False, format="json", keep_alive=assistant.KEEP_ALIVE,
        options={"num_ctx": assistant.OPTIONS["num_ctx"], "temperature": 0.2},
        messages=[{"role": "system", "content": SYSTEM.format(roster=roster, unavailable_reply=UNAVAILABLE_REPLY)},
                  {"role": "user", "content": text}],
    )
    return response.message.content


def _is_brain(member):
    return (member.get("kind") in ("claude", "codex") or member.get("preset") == "gpt"
            or bool(re.fullmatch(BRAIN, member["name"], re.I)))


def plan(text, *, realtime=False):
    """返回任务清单，或用固定规则保留原话供主脑拆分。"""
    import groupchat
    import local_models
    from httpx import HTTPError
    from ollama import ResponseError

    members = {m["name"]: m for m in groupchat.members()}
    realtime = realtime or local_models.voice_mode() == local_models.MINICPM_MODE
    result = {"text": text, "tasks": [], "realtime": realtime}

    def escalate(reason, failure=None):
        return {**result, "escalation": reason, "failure": failure}

    if realtime:
        return escalate("正在通话")
    if ASK_BRAIN.search(text):
        return escalate("主人点名请主脑回答")

    roles = {name: role if name in LOCAL else f"未接入。{role}" for name, role in PLACEHOLDERS.items()}
    for name, member in members.items():
        role = member.get("role") or (BRAIN_ROLE if _is_brain(member) else groupchat._role_text(member))
        roles[name] = role if member.get("enabled") else f"未在群中。{role}"
    roles[SELF] = SELF_ROLE
    roster = "\n".join(f"- {name}：{role}" for name, role in roles.items())

    try:
        raw = _ask_gemma(text, roster)
    except (OSError, RuntimeError, HTTPError, ResponseError):
        friendly.LOG.exception("本地调度模型暂时不可用")
        return escalate("本地调度模型暂时不可用", "preparing")
    try:
        data = json.loads(raw)
        tasks, reply = data["tasks"], data["reply"]
        if not isinstance(tasks, list) or not isinstance(reply, str) or not reply.strip():
            raise ValueError
        if len(tasks) > 4:
            return escalate("任务超过 4 件")
        tasks = [{key: task[key].strip() for key in ("to", "do", "when", "ask")} for task in tasks]
        if any(not task["to"] or not task["do"] for task in tasks):
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError):
        friendly.LOG.exception("调度模型返回的清单格式有误")
        return escalate("没能整理出任务清单", "error")

    for task in tasks:
        name = task["to"]
        if re.fullmatch(BRAIN, name, re.I) or (name in members and _is_brain(members[name])):
            return escalate("任务需要主脑安排")
        if name not in roles:
            return escalate("收件人不在调度名单里")
    if len(tasks) > 1 and DEPENDENCY.search(text):
        return escalate("多项任务有前后依赖")
    return {**result, "tasks": tasks, "reply": reply.strip()}


def dispatch(plan, *, wait=False, generation=None):
    """把调度消息写进群聊，返回一句可以直接念给主人听的话。"""
    import groupchat

    if generation is None:
        generation = groupchat.dispatch_generation()
    members = groupchat.members()
    if "escalation" in plan:
        text = plan["text"]
        brains = [m for m in members if m.get("enabled") and _is_brain(m)]
        hands = next((m for m in brains if m.get("hands")), None)
        targets = [hands] if hands and not BOTH_BRAINS.search(text) else brains
        mentions = " ".join(f"@{m['name']}" for m in targets) or "等待主脑加入群聊"
        messages = [f"{mentions}\n请安排这件事（{plan['escalation']}）。\n主人原话：{text}"]
        hint = ("请作为主脑拆分并安排下面这条请求，说明每步交给谁、何时做和需要询问什么。"
                f"\n主人原话：{text}")
        result = groupchat.post_dispatch(messages, targets, hint, wait=wait, generation=generation,
                                         summarize=not plan["realtime"])
        if result is None:
            return CANCELLED
        asked = result["asked"]
        if not asked:
            return friendly.MESSAGES[plan.get("failure") or "unavailable"]
        return f"这件事我交给{'和'.join(asked)}来安排。"

    import bridge_pending

    messages = []
    ready_messages = []
    targets = {}
    by_name = {member["name"]: member for member in members}
    unavailable = deferred = False
    scheduled = []
    for task in plan["tasks"]:
        name = task["to"]
        member = by_name.get(name)
        when = task["when"] or "马上"
        line = f"@{name}\n任务：{task['do']}\n时间：{when}\n追问：{task['ask'] or '无'}"
        if name not in LOCAL and (not member or not member.get("enabled")):
            unavailable = True
            line += ("\n未接入，等主人接好设备" if not member and name in PLACEHOLDERS
                     else "\n成员未在群中，等主人邀请")
        if when not in IMMEDIATE:
            deferred = True
            # 到点的记成定时任务，等条件的记成条件任务（bridge_pending 负责到时候去办）
            item = bridge_pending.add({**task, "when": when})
            scheduled.append(item)
            line += (f"\n已定好时间：{bridge_pending.describe(item)}" if item["due"]
                     else f"\n已挂起，等主人说条件满足了（比如“洗完了”“回来了”）")
        elif member and member.get("enabled") and name not in LOCAL:
            targets[name] = member
            ready_messages.append(line)
        messages.append(line)
    hint = "请处理以下清单中 @ 给你的任务。有追问就先问主人。\n\n" + "\n\n".join(ready_messages)
    result = groupchat.post_dispatch(messages, list(targets.values()), hint, wait=wait, generation=generation)
    if result is None:
        return CANCELLED
    reply = plan["reply"]
    timed = [item for item in scheduled if item["due"]]
    timed_text = ("已定好时间：" + "；".join(bridge_pending.describe(item) for item in timed) + "。") if timed else ""
    # Gemma 偶尔会把只记录的任务说成已执行。用实际记录状态汇报这一句。
    if unavailable:
        return UNAVAILABLE_REPLY + timed_text
    if timed and (COMPLETION_CLAIM.search(reply) or not reply):
        return "好的，" + timed_text
    if deferred and COMPLETION_CLAIM.search(reply):
        return "带条件的任务已记下，等条件满足。"
    return reply


def _fire_conditions(text):
    """主人这句话让哪些“等条件”的任务满足了：派出去，返回要念给主人的话（没有就是空字符串）。"""
    import bridge_pending

    fired = bridge_pending.conditions_met(text)
    if not fired:
        return "", []
    said = bridge_pending.fire(fired, "条件满足了", speak_out=False)
    return said or ("好的，接着安排：" + "、".join(item["do"] for item in fired) + "。"), fired


def route(text, *, wait=False, generation=None, realtime=False):
    import groupchat

    try:
        if generation is None:
            generation = groupchat.dispatch_generation()
        said, fired = _fire_conditions(text)
        if fired and len(text) <= 15:
            return said  # “我洗完了”这类短句：只是在说条件满足了，不再拆新任务
        reply = dispatch(plan(text, realtime=realtime), wait=wait, generation=generation)
        return said + reply
    except Exception as exc:
        friendly.LOG.exception("调度请求失败")
        return friendly.MESSAGES[friendly.failure(exc)]


def voice_turn(text, *, realtime=False):
    """语音每一轮都过一下：条件满足的挂起任务先派出去；明显是要分派的话交给调度员。
    返回要念给主人的话；和调度无关就返回 None（照原来的流程走）。开关没开也返回 None。"""
    if not voice_enabled():
        return None
    try:
        if matches_voice(text):
            return route(text, realtime=realtime)
        said, fired = _fire_conditions(text)
        return said if fired else None
    except Exception:
        friendly.LOG.exception("语音调度失败")
        return None
