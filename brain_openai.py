"""
brain_openai.py —— 用 GPT（或任何兼容 OpenAI 接口的模型）当大脑（OpenAI 官方 Python SDK）

    只负责“问一句、答一句”。
    api = "responses"：OpenAI 官方推荐的 Responses 接口（GPT 用这个）
    api = "chat"     ：老的 Chat Completions 接口（很多兼容 OpenAI 的服务只支持这个）
    base_url：用别家兼容 OpenAI 的服务时填（比如 DeepSeek、豆包、本地 Ollama）
"""

from openai import OpenAI


def answer(system, messages, model, api_key, base_url=None, api="responses"):
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1)
    if api == "chat":
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}] + messages,
        )
        return (response.choices[0].message.content or "").strip()
    response = client.responses.create(model=model, instructions=system, input=messages)
    return (response.output_text or "").strip()
