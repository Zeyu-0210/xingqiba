"""
brains.py —— “大脑”：谁来动脑（回答问题、聊天、出主意）

    本地 gemma4 一直在：负责听懂你的话、执行操作（调音量、记住、交任务……）—— 这部分永远免费。
    “大脑”只负责动脑：回答问题、聊天、出主意、解释东西。
        默认是“本地”（就是 gemma4 自己：免费、不联网）
        说“让 Claude 当我的脑子”“换成 GPT 回答我”→ 切到云端大模型（按用量付费，更聪明）
        说“换回本地” → 切回来
    选择记在 settings.json 里，重启后不变；右键托盘图标 →“大脑”也能换。

想加别的大脑（比如以后更强的 Gemini、DeepSeek）：在下面 BRAINS 里照着加一段：
    provider = "claude"   ：Claude。api_keys.json 里填了密钥就用密钥（按用量付费）；
                            没填就用你登录的 Claude 账号（借电脑上的 Claude Code，走会员额度，见 brain_claude_code.py）
    provider = "anthropic"：只用 API 密钥的 Claude
    provider = "openai"   ：GPT，或任何兼容 OpenAI 接口的服务（再填 base_url，api 写 "chat"）
密钥写在项目里的 api_keys.json（只存在你电脑上，不要发给别人）。
"""

import json
import os
import sys

import settings

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(PROJECT_DIR, "api_keys.json")
LOCAL = "本地"
TAKEOVER = "Claude接管"  # 不只是动脑：听懂、动手、回答全部交给 Claude Code（见 takeover.py）

BRAINS = {
    LOCAL: {
        "provider": "local",
        "aliases": ["本地", "Gemma", "gemma", "杰玛", "G4", "离线"],
        "about": "使用你选的本地推理模型，无需联网",
    },
    "Claude": {
        "provider": "claude",         # 填了密钥用密钥；没填就用你登录的 Claude 账号（Claude Code）
        "model": "claude-opus-5-5",   # Anthropic 现在最强的 Opus
        "effort": "low",              # 思考深度：念出来的回答要快，所以用 low（想更深就改 medium/high）
        "key": "anthropic",           # 用 api_keys.json 里的哪一个密钥
        "aliases": ["Claude", "Cloud", "克劳德", "克劳"],
        "about": "Claude Opus 5.5：很聪明，用你的 Claude 账号（或 API 密钥）",
    },
    "GPT": {
        "provider": "openai",
        "model": "gpt-6.2-sol",       # OpenAI 的日常主力款（最强是 gpt-6.2-astra，最省是 gpt-6-luna）
        "api": "responses",           # OpenAI 官方推荐的 Responses 接口
        "key": "openai",
        "aliases": ["GPT", "ChatGPT", "gpt", "鸡皮提"],
        "about": "GPT-6.2 Sol：很聪明，按用量付费",
    },
    TAKEOVER: {
        "provider": "claude_takeover",  # 用你登录的 Claude 账号（Claude Code），模型和设置在 takeover.py
        "model": "claude-opus-5-5",
        "effort": "low",
        "aliases": ["接管", "Claude接管", "Claude 接管", "全权接管", "Claude全权", "让Claude接管", "Cloud接管"],
        "about": "Claude 全权接管：听懂、动手、回答都交给 Claude，危险的先问你",
    },
}

KEYS_TEMPLATE = {
    "说明": "把 API 密钥粘贴到对应的引号里，保存即可（不用重启助手）。这个文件只存在你电脑上，不要发给任何人。",
    "anthropic": "",
    "openai": "",
}


class BrainError(Exception):
    """云端大脑用不了（没填密钥、断网、额度用完……）。"""


def current():
    """现在用的是哪个大脑（名字）。"""
    name = settings.get("brain", LOCAL)
    if sys.platform == "darwin" and name == TAKEOVER:
        return LOCAL
    return name if name in BRAINS else LOCAL


def find(name):
    """按名字或别名找大脑，找不到返回 None。"""
    key = str(name or "").strip().lower()
    for brain, cfg in BRAINS.items():
        if key == brain.lower() or key in (a.lower() for a in cfg["aliases"]):
            return brain
    return None


def load_keys():
    """读密钥文件；没有就先建一个空的模板，方便你填。"""
    if not os.path.exists(KEYS_FILE):
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(KEYS_TEMPLATE, f, ensure_ascii=False, indent=2)
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _key_of(brain):
    return str(load_keys().get(BRAINS[brain].get("key", ""), "")).strip()


def has_key(brain, *, claude_login=None):
    """这个大脑现在能不能用：本地永远能用；云端的要填了密钥（Claude 也可以是登录了账号）。"""
    if sys.platform == "darwin" and brain == TAKEOVER:
        return False
    cfg = BRAINS[brain]
    if cfg["provider"] == "local" or _key_of(brain):
        return True
    if cfg["provider"] in ("claude", "claude_takeover"):
        if claude_login is not None:
            return claude_login
        import brain_claude_code
        return brain_claude_code.logged_in()
    return False


def note(brain, *, claude_login=None, login_pending=False):
    """托盘菜单里跟在名字后面的提示：能用就是空的，不能用就说缺什么。"""
    if sys.platform == "darwin" and brain == TAKEOVER:
        from computer import MAC_UNSUPPORTED
        return MAC_UNSUPPORTED
    if has_key(brain, claude_login=claude_login):
        return ""
    provider = BRAINS[brain]["provider"]
    if login_pending and provider in ("claude", "claude_takeover"):
        return "  ← 正在检查 Claude 登录状态"
    if provider == "claude_takeover":
        return "  ← 还没登录 Claude Code"
    return "  ← 还没登录 Claude Code（或填密钥）" if provider == "claude" else "  ← 还没填密钥"


def set_brain(brain):
    """换大脑（这是给秘书模型用的工具，托盘菜单也用它）。"""
    name = find(brain)
    if sys.platform == "darwin" and name == TAKEOVER:
        from computer import MAC_UNSUPPORTED
        return {"ok": False, "message": MAC_UNSUPPORTED}
    if name is None:
        return {"message": f"没有叫“{brain}”的大脑，可以选：{'、'.join(BRAINS)}"}
    previous = current()
    settings.set("brain", name)
    if previous == TAKEOVER and name != TAKEOVER:
        import takeover
        takeover.end_session()  # 退出接管：下次接管重新开始
    if name == LOCAL:
        return {"brain": name, "message": "好的，换回本地大脑了，免费也不联网"}
    if name == TAKEOVER:
        if has_key(name):
            return {"brain": name, "message": "好的，现在由 Claude 全权接管：你说的事都交给 Claude 来想和动手，"
                                              "危险的操作会先问你。想恢复就说“换回本地”"}
        return {"brain": name, "message": "好的，换成 Claude 接管了，不过还没登录你的 Claude 账号：右键点托盘里的“八”字图标，"
                                          "选 大脑 → 登录 Claude 账号；登录之前我先用本地处理"}
    if has_key(name):
        extra = ""
    elif BRAINS[name]["provider"] == "claude":
        extra = ("（不过还没登录你的 Claude 账号：右键点托盘里的“八”字图标，选 大脑 → 登录 Claude 账号，"
                 "按提示登录一次就行；登录之前我先用本地回答）")
    else:
        extra = f"（不过还没在 api_keys.json 里填 {name} 的密钥，填好之前我先用本地回答）"
    return {"brain": name, "message": f"好的，现在由 {name} 来动脑{extra}"}


def ask(system, messages, brain=None):
    """
    让云端大脑回答。messages 是 [{"role": "user"/"assistant", "content": 文字}, ...]，最后一条是你的问题。
    用不了时抛出 BrainError（调用的地方会退回本地回答）。
    """
    name = brain or current()
    cfg = BRAINS[name]
    key = _key_of(name)
    try:
        if cfg["provider"] in ("claude", "claude_takeover"):
            import claude_models  # 用哪个 Claude 模型：智能选择（按难度）或者你在托盘里选的
            model_key, _ = claude_models.choose(messages[-1]["content"])
            model = claude_models.MODELS[model_key]
            if not key:  # 没填密钥：用你登录的 Claude 账号
                import brain_claude_code
                info = {}
                try:
                    text = brain_claude_code.answer(system, messages, model["id"], model["chat"], info=info)
                except brain_claude_code.NotReady as e:
                    raise BrainError(str(e)) from e
                claude_models.record(model_key, info.get("cost"))
                return text
            import brain_claude
            return brain_claude.answer(system, messages, model["id"], model["chat"], key)
        if cfg["provider"] == "anthropic":
            import brain_claude
            return brain_claude.answer(system, messages, cfg["model"], cfg.get("effort", "low"), key or None)
        if cfg["provider"] == "openai":
            if not key and not cfg.get("base_url"):
                raise BrainError(f"还没填 {name} 的密钥")
            import brain_openai
            return brain_openai.answer(system, messages, cfg["model"], key or "none",
                                       cfg.get("base_url"), cfg.get("api", "responses"))
    except BrainError:
        raise
    except Exception as e:  # 密钥不对、断网、额度用完……统一变成 BrainError，并给出能看懂的原因
        raise BrainError(_explain(e)) from e
    raise BrainError(f"{name} 是本地大脑，不用调用云端")


def _explain(error):
    """把各种错误翻译成一句人话。"""
    text = f"{type(error).__name__}: {error}"
    if "auth" in text.lower() or "api key" in text.lower() or "401" in text:
        return "密钥不对或者没填"
    if "connect" in text.lower() or "timeout" in text.lower():
        return "连不上（可能断网了，或者需要特殊网络）"
    if "429" in text or "rate" in text.lower() or "quota" in text.lower() or "credit" in text.lower():
        return "额度用完了或者请求太频繁"
    return text[:80]
