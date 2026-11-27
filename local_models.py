"""星期八的对话模式、本地推理选项及两套 GPU 服务的轮换。"""

import sys
import threading
import time
from contextlib import contextmanager

import httpx
import ollama

import friendly
import settings

WAKE_MODE = "wakeword"
MINICPM_MODE = "minicpm"
MINICPM_MAC_MESSAGE = "Mac 上暂时不可用（需要 CUDA）"
VOICE_MODES = {
    WAKE_MODE: {"label": "唤醒词对话", "detail": "说「星期八」唤醒，支持指令和桌面操作"},
    MINICPM_MODE: {"label": "MiniCPM-o 4.5", "detail": "直接听和说，支持插话，适合闲聊"},
}
# Mac（16GB 统一内存）上 12B 读一遍提示词要十几秒，默认用小一号的 E4B（实测快 4 倍多）
MAC_REASONING = "gemma4:e4b"
DEFAULT_REASONING = MAC_REASONING if sys.platform == "darwin" else "gemma4:12b"
REASONING_MODELS = {
    "gemma4:12b": {"label": "Gemma 4 12B", "detail": "通用问答和已有的语音指令流程"},
    MAC_REASONING: {"label": "Gemma 4 E4B", "detail": "小一号，反应更快，适合内存小的电脑（Mac 默认）"},
    "qwen3.5:9b": {"label": "千问 3.5 9B", "detail": "中文问答、推理和工具调用"},
}

_condition = threading.Condition()
_ollama_lock = threading.RLock()
_waiting = 0
_realtime = False
_managed = set(REASONING_MODELS)
_last_model = None


def voice_mode():
    value = settings.get("voice_mode", WAKE_MODE)
    if value == MINICPM_MODE and sys.platform == "darwin":
        return WAKE_MODE
    return value if isinstance(value, str) and value in VOICE_MODES else WAKE_MODE


def voice_choices():
    modes = choices(VOICE_MODES)
    if sys.platform == "darwin":
        for mode in modes:
            if mode["id"] == MINICPM_MODE:
                mode.update(available=False, unavailable=MINICPM_MAC_MESSAGE)
            else:
                mode["detail"] = "说「星期八」唤醒，支持语音问答和音量控制"
    return modes


def reasoning_model():
    value = settings.get("reasoning_model", DEFAULT_REASONING)
    return value if isinstance(value, str) and value in REASONING_MODELS else DEFAULT_REASONING


def choices(models):
    return [{"id": engine, **info} for engine, info in models.items()]


def reasoning_pending():
    with _condition:
        return _waiting > 0


@contextmanager
def realtime_slot():
    """实时语音退出后才交出显卡，其间新来的推理任务排队。"""
    global _realtime
    with _condition:
        acquired = not _waiting and not _realtime
        if acquired:
            _realtime = True
    try:
        yield acquired
    finally:
        if acquired:
            with _condition:
                _realtime = False
                _condition.notify_all()


@contextmanager
def reasoning_slot():
    """整轮打字对话可以持有该位置，工具往返期间也不用重载实时语音。"""
    global _waiting
    with _condition:
        _waiting += 1
    try:
        with _condition:
            if not _condition.wait_for(lambda: not _realtime, timeout=45):
                raise friendly.ModelNotReady("实时语音尚未释放显存")
        with _ollama_lock:
            yield
    finally:
        with _condition:
            _waiting -= 1
            _condition.notify_all()


def _loaded_to_release(client, keep):
    return {entry.model for entry in client.ps().models
            if entry.model in _managed and entry.model != keep}


def release(keep=None, stopping=lambda: False):
    """只卸载星期八管理的模型，确认显存已释放再启动下一套服务。"""
    global _last_model
    with _ollama_lock:
        client = ollama.Client(timeout=4)
        try:
            models = _loaded_to_release(client, keep)
        except ConnectionError:
            _last_model = None
            return
        for name in models:
            client.chat(model=name, messages=[], keep_alive=0)
        if models:
            deadline = time.monotonic() + 8
            while not stopping() and _loaded_to_release(client, keep):
                if time.monotonic() >= deadline:
                    raise RuntimeError("本地推理模型尚未释放显存")
                time.sleep(0.1)
        _last_model = keep


@contextmanager
def _chat_request(model, kwargs):
    global _last_model
    name = model or reasoning_model()
    with reasoning_slot():
        _managed.add(name)
        try:
            if name != _last_model:
                release(keep=name)
            result = ollama.chat(model=name, **kwargs)
            _last_model = name
            yield result
        except (ConnectionError, TimeoutError, httpx.RequestError, ollama.ResponseError) as exc:
            raise friendly.ModelNotReady("本地对话模型暂时不可用") from exc


def _stream_chat(model, kwargs):
    with _chat_request(model, kwargs) as chunks:
        yield from chunks  # 消费完整个流后才释放显卡调度锁


def chat(*, model=None, **kwargs):
    """将本地对话和群聊的后台推理汇入同一个显卡调度入口。"""
    if kwargs.get("stream"):
        return _stream_chat(model, kwargs)
    with _chat_request(model, kwargs) as response:
        return response
