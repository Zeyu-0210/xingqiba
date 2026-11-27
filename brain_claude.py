"""
brain_claude.py —— 用 Claude 当大脑（Anthropic 官方 Python SDK）

    只负责“问一句、答一句”：把系统说明 + 最近的对话 + 你的问题发给 Claude，拿回文字回答。
    密钥：优先用 api_keys.json 里的 "anthropic"；没填的话，SDK 会自己找环境变量 ANTHROPIC_API_KEY
          或者 ant 命令行登录的账号。
    被安全策略拒绝时，会自动换一个模型再试（server-side fallback，默认开启）。
"""

import anthropic


def answer(system, messages, model, effort="low", api_key=None):
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=1) if api_key \
        else anthropic.Anthropic(timeout=60.0, max_retries=1)
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        messages=messages,
        output_config={"effort": effort},          # 思考深度：念出来的回答要快，默认 low
        betas=["server-side-fallback-2026-07-01"],  # 被拒绝时自动换模型重试
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return "抱歉，这个问题我没办法回答。"
    return "".join(block.text for block in response.content if block.type == "text").strip()
