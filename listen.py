"""
listen.py —— 第三步：录下你说的一句话，用显卡（faster-whisper）把它转成中文文字

两个部分：
    record_utterance()   从麦克风录一句话：等你开口 → 根据停顿时长和识别文字自动收尾
    SpeechToText         加载 Whisper 模型，把录音转成文字

    conversation_window  对话窗口：唤醒后一直听；对助手说的话马上执行，闲聊放进暂存区；
                         对话超时后关闭并在后台复盘，本地大模型空闲后自动卸载

单独运行这个文件：说“星期八”唤醒 → 叮 → 说话 → 转成文字 → 交给助手执行并记录（按 Ctrl+C 退出）
    .venv\\Scripts\\python.exe listen.py
加上 --enter 则改成按回车唤醒（不方便说话、或者调试的时候）：
    .venv\\Scripts\\python.exe listen.py --enter
加上 --only-text 则只识别文字、不交给助手（不会调音量，也不记录）：
    .venv\\Scripts\\python.exe listen.py --only-text
"""

import atexit
import collections
import gc
import os
import queue
import random
import re
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext

import numpy as np
import sounddevice as sd

import cuda_libs  # noqa: F401  必须在 faster_whisper 之前导入，让它找到显卡库
import duplex
import friendly
import housekeeping
import local_models
import settings
import speech_endpoint
import status
import transcription
import wakeword
from faster_whisper import WhisperModel

# ---------------------------------------------------------------------------
# 设置（以后觉得不好用，可以改这里的数字）
# ---------------------------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(PROJECT_DIR, "models", "faster-whisper-large-v3-turbo")
WAKE_MODE = local_models.WAKE_MODE
MINICPM_MODE = local_models.MINICPM_MODE
VOICE_MODES = tuple(local_models.VOICE_MODES)
voice_mode = local_models.voice_mode

SAMPLE_RATE = 16000     # Whisper 要求 16kHz 单声道
BLOCK_SECONDS = 0.03    # 每 30 毫秒检查一次声音大小

START_TIMEOUT = 8.0     # 唤醒后最多等 8 秒，你还没开口就放弃（原来 5 秒，实测有时来不及开口）
END_SILENCE = 1.0       # 流式识别不可用时，按原来的 1 秒静音收尾
MAX_SECONDS = 30.0      # 一句话最长录 30 秒（原来 15 秒，交代作业要求这种长句会被截断）
PRE_ROLL = 0.5          # 多保留开口前 0.5 秒，防止第一个字被切掉
MIN_SPEECH = 0.3        # 说话不到 0.3 秒的当成杂音（比如敲桌子）
MIN_THRESHOLD = 0.006   # “算作说话”的最低音量，防止很安静时把轻微杂音当成说话
                        # （原来是 0.01，实测有一次没听到，所以调低一点）
MAX_THRESHOLD = 0.03    # 门槛的上限：不管环境测得多吵，门槛都不会高过这个数，
                        # 防止校准出错后，正常说话也达不到门槛
SKIP_START = 0.2        # 麦克风刚打开时可能有一小段启动杂音，丢掉前 0.2 秒不用
SHOW_LEVELS = True      # 每句录完显示“环境 / 门槛 / 你的声音”三个数字，调好后可改成 False

# 给 Whisper 的提示：让它倾向于输出简体中文，并且知道这是对电脑语音助手说的话
# （之前只写了“调节音量”，现在助手还能交任务、换声音，所以写得更通用）。
# 注意：不要在这里逐字列出很短的指令（比如“静音”），实测会让 Whisper 把短录音
# 听成“请不吝点赞 订阅……”这类幻觉。
INITIAL_PROMPT = "以下是普通话的句子，是用户对电脑语音助手说的话，比如调节音量、交代任务。"

# Whisper 的常见“幻觉”：它从大量视频字幕里学来的套话。识别结果里出现这些词，
# 基本可以肯定不是你说的，直接当成“没听清”
HALLUCINATION_WORDS = ["点赞", "订阅", "打赏", "字幕", "明镜", "栏目", "感谢观看", "谢谢观看"]

# 每说满 5 句，下次唤醒时清空一次终端屏幕（只清屏幕显示，不影响记忆和历史记录）
CLEAR_EVERY = 5

# 对话窗口：唤醒后麦克风一直开着，你说的每句话都会转成文字：
#   - 对助手说的（指令、提问）→ 马上执行，写进历史记录，并且重新开始计时
#   - 不是对助手说的（闲聊、队友语音、游戏声音）→ 放进暂存区（只在内存里）
# 连续 INACTIVE_MINUTES 分钟没有对助手说话，窗口就关闭，然后在后台“复盘”暂存区，
# 复盘完回到待命，本地大模型由 Ollama 按空闲时限卸载。
INACTIVE_MINUTES = 3

# 它回答完以后再听多久（秒），等你接着说（不用再喊名字）。设置里可以改（settings.json 的 follow_up_seconds）。
# 以前固定 3 分钟，实测太长：你转头跟别人说话，它还在听，可能把闲聊当成指令。
# 参考 Home Assistant（回答以问号结尾才继续听）、OpenVoiceOS（说完只开 3～10 秒麦克风）、Alexa 的追问模式：
#   平时回答完再听 FOLLOW_UP 秒；它的回答是在问你问题时，至少等 QUESTION_FOLLOW_UP 秒
FOLLOW_UP = 20
QUESTION_FOLLOW_UP = 60
FOLLOW_UP_CHOICES = {10: "10 秒", 20: "20 秒（推荐）", 60: "1 分钟", 180: "3 分钟（以前的做法）"}


def follow_up_seconds(last_reply=""):
    """它回答完以后再听多少秒：按设置；它刚问了你一个问题时多等一会儿。"""
    import settings
    try:
        seconds = int(settings.get("follow_up_seconds", FOLLOW_UP))
    except (TypeError, ValueError):
        seconds = FOLLOW_UP
    if asks_question(last_reply):
        seconds = max(seconds, QUESTION_FOLLOW_UP)
    return seconds


def asks_question(reply):
    """它的回答是不是在问你问题（以问号结尾，或者“……吗”“……呢”结尾）。"""
    text = re.sub(r"[\s。.!！~～…]+$", "", reply or "")
    return text.endswith(("?", "？")) or bool(re.search(r"(吗|呢|么)$", text))

# 待命时语音识别模型（Whisper）放在哪：
#   "ram" → 停在内存里（约 1.5GB 内存，几乎不占显存）。听到唤醒词时 0.2 秒挪回显卡做二次确认。推荐
#   "gpu" → 一直在显卡上（占 2.3GB 显存），二次确认再快 0.2 秒
#   "off" → 完全卸载（不占内存也不占显存），但每次听到唤醒词要花约 2 秒加载才能确认
# Mac 优先使用 SenseVoice，只有需要备用转写时才加载 Whisper。
WHISPER_IN_STANDBY = "off" if sys.platform == "darwin" else "ram"

# 唤醒后的语音回应（随机挑一句；会提前合成好，0 等待）
WAKE_REPLIES = ["我在。", "请吩咐。", "在的，什么事？", "随时待命。"]

# “退出”信号：托盘点“退出”或运行关闭快捷方式时会设置它，正在录音、正在等唤醒词的地方都会马上停下
STOP = threading.Event()

# “暂停”信号：托盘点“暂停监听”时设置。对话进行到一半也会马上停下（之前只在待命时生效）
PAUSED = threading.Event()

# “群聊语音输入”信号：在群聊里点麦克风录音时设置（dictation.py）。
# 唤醒词模式：像暂停一样不听（这段话只用来转文字，不能被当成指令）；
# MiniCPM-o 模式：不卸载模型，只把这段时间的麦克风输入换成静音。
DICTATING = threading.Event()
ACTIVE_STT = None  # 唤醒词模式正在用的语音识别（群聊语音输入借用它，不另占一份显存）


def mic_blocked():
    """助手现在不该听麦克风：暂停了，或者群聊正在语音输入。"""
    return PAUSED.is_set() or DICTATING.is_set()


class _EitherEvent:
    """把“暂停”和“群聊语音输入”合成一个信号，给唤醒词模式用（只需要 is_set）。"""

    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(e.is_set() for e in self.events)

# 说这些话会直接关闭窗口、回到待命（不发给模型，也不记录）
END_PHRASES = {"好了", "好的", "可以了", "没事了", "没事", "就这样", "就这样吧",
               "退下", "退下吧", "结束", "不用了", "谢谢", "谢谢你", "退出", "关闭", "安静"}
# 换种说法的“让它退下”（整句话就是这个意思才算，避免误伤“把手机调成静默模式”这种）。
# 参考小智（xiaozhi-esp32-server）：明确的退出词本地直接处理；说法不固定的交给模型的 end_conversation 工具
EXIT_PATTERN = re.compile(
    r"^(那)?(你)?(就|先|可以|现在)?(先)?(进入|切换到|切到|回到|回去|去)?(静默|待命|安静)(模式|状态)?(吧|了|去吧|一下)?$"
    r"|^(你)?(先|可以|就)?(去)?(休息|退下)(吧|了|去吧|一下|一会儿?)?$"
    r"|^(好了|好的|行|行了|嗯)?(拜拜|再见|回见|晚安)(了|啦|吧|喽)?$"
    r"|^(好了|行了)?(我)?(先)?不(跟|和)你(说|聊)了(啊|哈)?$"
    r"|^(这儿|这里)?没(有)?你(的)?事(儿)?了$"
    r"|^(你)?(先)?(别|不用)(再)?听了$"
)

# 它说话时你插嘴说了这些：马上闭嘴，但窗口还开着、继续听你说
STOP_TALKING = {"停", "停停", "停停停", "停一下", "停下", "别说了", "不用说了", "先别说了", "闭嘴",
                "安静", "打住", "够了", "行了", "等一下", "等等", "等会", "等会儿"}
# 它说话时你只是附和一声：不算打断，从停下的地方接着说
BACKCHANNEL = {"嗯", "嗯嗯", "嗯嗯嗯", "啊", "哦", "哦哦", "噢", "呃", "额", "对", "对对", "对对对",
               "是", "是的", "好", "嗯好", "嗯对"}


# ---------------------------------------------------------------------------
# 录音
# ---------------------------------------------------------------------------


def _rms(block):
    """一小段声音的平均响度。"""
    return float(np.sqrt(np.mean(block ** 2)))


def record_utterance(device=None, verbose=True, start_timeout=None, prompt="请说话……", should_stop=None,
                     *, microphone=None, noise=0.0):
    """
    录一句话，返回 16kHz 的 numpy 数组；没听到有人说话就返回 None。

    原理：先听 0.3 秒环境噪音，把“比环境声大 3 倍”当作在说话；
    开口后开始录，根据连续静音和流式识别的文字判断何时停止。
        start_timeout  最多等你几秒开口（不填时用随设置调整的 START_TIMEOUT）
        prompt         开始听的时候显示的提示
    """
    audio, levels = _record(device, verbose, start_timeout, prompt, should_stop, microphone=microphone, noise=noise)
    if verbose and SHOW_LEVELS:
        hint = "（你的声音没达到门槛）" if levels["peak"] <= levels["threshold"] else ""
        print(f"  [音量] 环境 {levels['noise']:.4f}  门槛 {levels['threshold']:.4f}  "
              f"你的声音最大 {levels['peak']:.4f}{hint}")
    return audio


@contextmanager
def _record_blocks(device, block_size, stopping, microphone, noise):
    if microphone is not None:
        yield lambda: microphone.read(block_size, stopping), noise
        return
    q = queue.Queue()

    def callback(indata, frames, time_info, status):
        # 麦克风每采到 30 毫秒的声音就会调用一次，这里只把数据放进队列
        q.put((indata[:, 0].copy(), time.monotonic()))

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=block_size, device=device, callback=callback):
        # 0. 麦克风刚打开时可能有启动杂音，先丢掉一小段
        for _ in range(int(SKIP_START / BLOCK_SECONDS)):
            q.get()

        # 1. 校准：听一下环境有多吵。用“中位数”，中间偶尔响一下不会把门槛拉高
        noise = float(np.median([_rms(q.get()[0]) for _ in range(int(0.3 / BLOCK_SECONDS))]))
        yield q.get, noise


def _record(device, verbose, start_timeout=None, prompt="请说话……", should_stop=None,
            *, microphone=None, noise=0.0):
    """真正录音的部分。返回 (录音或 None, 三个音量数字)。"""
    should_stop = should_stop or (lambda: STOP.is_set() or mic_blocked())
    scale = speech_endpoint.wait_scale()
    start_timeout = start_timeout or START_TIMEOUT * scale
    max_seconds = MAX_SECONDS * scale
    block_size = int(SAMPLE_RATE * BLOCK_SECONDS)
    trace = status.LOG_ENABLED
    last_voice_at = ended_at = None

    with _record_blocks(device, block_size, should_stop, microphone, noise) as (read, noise):
        threshold = min(max(noise * 3, MIN_THRESHOLD), MAX_THRESHOLD)
        levels = {"noise": noise, "threshold": threshold, "peak": 0.0}
        if verbose:
            print(f"  {prompt}")

        # 2. 等你开口，然后一直录到你说完
        pre_roll = collections.deque(maxlen=int(PRE_ROLL / BLOCK_SECONDS))
        frames = []
        waited = 0.0      # 等待开口的时间
        recorded = 0.0    # 已经录了多长
        silence = 0.0     # 连续安静了多久
        while True:
            if should_stop():
                return None, levels
            block, captured_at = read()
            if block is None:
                return None, levels
            level = _rms(block)
            status.level(level)  # 状态窗里的音量条
            levels["peak"] = max(levels["peak"], level)
            loud = level > threshold
            if trace and loud:
                last_voice_at = captured_at
            if not frames:                      # 还没开口
                pre_roll.append(block)
                waited += BLOCK_SECONDS
                if loud:
                    frames.extend(pre_roll)
                    speech_start = len(frames) - 1  # 开口那一刻在录音里的位置
                    recorded = len(frames) * BLOCK_SECONDS
                    # 如果开口早，给识别流补齐开头的静音，录音里仍保留实际采到的声音。
                    missing = (pre_roll.maxlen - len(frames)) * block_size
                    endpoint = speech_endpoint.SpeechEndpoint(BLOCK_SECONDS, scale, END_SILENCE,
                                                              missing_pre_roll=missing)
                elif waited >= start_timeout:
                    return None, levels
            else:                               # 正在说话
                frames.append(block)
                recorded += BLOCK_SECONDS
                silence = 0.0 if loud else silence + BLOCK_SECONDS
                finished = endpoint.finished(frames, silence)
                if finished or recorded >= max_seconds:
                    ended_at = time.monotonic() if trace else None
                    break

    # 实际说话时长：从开口那一刻算到最后一次有声音，
    # 不算开口前多保留的那段，也不算结尾的安静。
    # （之前固定减去 PRE_ROLL，但如果你一看到“请说话”就开口，前面其实没保留那么长，
    #   结果减多了，短句子会被误当成杂音丢掉。）
    speech_seconds = (len(frames) - speech_start) * BLOCK_SECONDS - silence
    if speech_seconds < MIN_SPEECH:
        return None, levels
    if ended_at is not None:
        status.barge_event("recording_end", at=ended_at, path="normal", recording_id=status.new_recording_id(),
                           reason="silence" if finished else "max_duration",
                           last_voice_mono_ms=round(last_voice_at * 1000, 3))
    return np.concatenate(frames), levels


def _finish_wake_audio(microphone, audio, stopping):
    """关键词检出后继续录到句尾，把同一口气说完的后半句交给复核。"""
    block_size = int(SAMPLE_RATE * BLOCK_SECONDS)
    offset = len(audio) % block_size
    frames = ([audio[:offset]] if offset else []) + [
        audio[i:i + block_size] for i in range(offset, len(audio), block_size)
    ]
    levels = [_rms(block) for block in frames]
    noise = float(np.quantile(levels, 0.2))
    threshold = max(0.0001, min(max(noise * 3, MIN_THRESHOLD), MAX_THRESHOLD, max(levels) * 0.3))
    quiet_blocks = next((i for i, level in enumerate(reversed(levels)) if level >= threshold), len(frames))
    silence = quiet_blocks * BLOCK_SECONDS
    last_voice_at = microphone.last_read_at - silence
    scale = speech_endpoint.wait_scale()
    endpoint = speech_endpoint.SpeechEndpoint(BLOCK_SECONDS, scale, END_SILENCE)
    heard_more = False
    while True:
        # 检出名字后没人接着说时，及早复核和回应；连着说话时用正常的收尾判断。
        finished = (silence >= 0.45 * scale if not heard_more else endpoint.finished(frames, silence))
        if finished or len(frames) * BLOCK_SECONDS >= MAX_SECONDS * scale:
            recording_id = status.new_recording_id(remember=False) if status.LOG_ENABLED else None
            if recording_id is not None:
                status.barge_event("recording_end", path="wake", recording_id=recording_id,
                                   reason="silence" if finished else "max_duration",
                                   last_voice_mono_ms=round(last_voice_at * 1000, 3))
            return np.concatenate(frames), noise, recording_id
        block, captured_at = microphone.read(block_size, stopping)
        if block is None:
            return None, noise, None
        frames.append(block)
        if _rms(block) >= threshold:
            heard_more = True
            silence = 0.0
            last_voice_at = captured_at
        else:
            silence += BLOCK_SECONDS


# ---------------------------------------------------------------------------
# 语音转文字
# ---------------------------------------------------------------------------


class SpeechToText:
    """
    语音识别。模型有三种状态：
        在显卡上（能马上识别） / 停在内存里（park，不占显存，0.2 秒能挪回显卡） / 完全卸载（unload）
    识别时如果不在显卡上，会自动挪回或加载。

    重要：对模型的所有操作（加载、挪动、识别、卸载）都放在同一个“专用线程”里做。
    之前在不同线程里挪来挪去，程序退出时会崩溃（实测 3 次全崩），所以统一交给一个线程。
    """

    def __init__(self, model_dir=MODEL_DIR, load=True, *, device="cuda"):
        self.model_dir = model_dir
        self.device = "cpu" if sys.platform == "darwin" else device
        self.model = None
        self._tasks = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="whisper", daemon=True)
        self._thread.start()
        atexit.register(self.close)  # 程序退出前，在专用线程里把模型收拾干净
        if load:
            self.load()

    # ---------- 专用线程 ----------

    def _serve(self):
        while True:
            item = self._tasks.get()
            if item is None:
                return
            func, done, box, wait = item
            try:
                box["result"] = func()
            except BaseException as e:  # 出错了也要通知等着的人，不能让它一直等
                box["error"] = e
                if not wait:
                    friendly.LOG.exception("后台准备语音识别失败")
            done.set()

    def _call(self, func, wait=True):
        """把一件事交给专用线程去做；wait=True 时等它做完并拿回结果。"""
        if threading.current_thread() is self._thread:
            return func()
        if self._closed:
            raise RuntimeError("语音识别已经关闭")
        done, box = threading.Event(), {}
        self._tasks.put((func, done, box, wait))
        if not wait:
            return None
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ---------- 对外的功能 ----------

    @property
    def loaded(self):
        """模型是不是在显卡上、能马上识别。"""
        model = self.model
        return model is not None and model.model.model_is_loaded

    def park(self):
        """把模型挪到内存（RAM）里，释放约 2.1GB 显存；下次识别时 0.2 秒就能挪回显卡。"""
        if sys.platform == "darwin":
            return  # 模型已经在 CPU 上。
        def work():
            if self.loaded:
                self.model.model.unload_model(to_cpu=True)
        self._call(work)

    def load(self):
        """让模型回到显卡上：停在内存里的就挪回来，没加载的就加载。返回用时（秒）。"""
        return self._call(self._load)

    def load_in_background(self):
        """在后台加载（唤醒时调用：你说第一句话的同时，模型就在加载了）。"""
        self._call(self._load, wait=False)

    def unload(self):
        """卸载模型，释放约 2.3GB 显存。"""
        def work():
            self.model = None
            gc.collect()
        self._call(work)

    def close(self):
        """卸载模型并结束专用线程（程序退出时会自动调用）。"""
        if self._closed or not self._thread.is_alive():
            return
        self.unload()
        self._closed = True
        self._tasks.put(None)
        self._thread.join(timeout=5)
        atexit.unregister(self.close)

    def _load(self):
        try:
            return self._load_model()
        except Exception as exc:
            raise friendly.ModelNotReady("语音识别模型没有准备好") from exc

    def _load_model(self):
        if self.model is not None:
            if self.model.model.model_is_loaded:
                return 0.0
            start = time.time()
            self.model.model.load_model()  # 从内存挪回显卡
            return time.time() - start
        start = time.time()
        # device="cuda" 用显卡；float16 是半精度，速度快、占显存少
        options = {"compute_type": "float16"} if self.device == "cuda" else {"compute_type": "int8", "cpu_threads": 6}
        model = WhisperModel(self.model_dir, device=self.device, **options)
        # 先空跑一次，让显卡完成初始化，之后第一句话就不用多等。
        # 注意：transcribe() 要“读取结果”时才真正开始计算，所以要用 list() 把结果读出来
        segments, _info = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                           language="zh", beam_size=5)
        list(segments)
        self.model = model
        return time.time() - start

    def transcribe(self, audio, prompt=INITIAL_PROMPT, vad_filter=True, *, reject_hallucinations=True):
        """
        audio 可以是录音数组，也可以是 .wav 文件路径。返回识别出的文字。
        prompt / vad_filter：给唤醒词二次确认用的时候，会换成通用提示、不过滤静音。
        """
        def work():
            self._load()  # 不在显卡上的话，先挪回来或加载
            segments, _info = self.model.transcribe(
                audio,
                language="zh",                 # 固定中文，不用自动判断语言
                beam_size=5,
                vad_filter=vad_filter,         # 自动去掉开头结尾的静音，减少“幻听”
                initial_prompt=prompt,
                condition_on_previous_text=False,
            )
            # 去掉偶尔出现的乱码字符（Whisper 有时会输出半个汉字）
            return "".join(s.text for s in segments).strip().replace("�", "")

        text = self._call(work)
        if reject_hallucinations and any(word in text for word in HALLUCINATION_WORDS):
            return ""  # 是幻觉，当成没听清
        return text


# ---------------------------------------------------------------------------
# 对话窗口：唤醒一次，可以连续说几条；窗口关闭后就不再听写、不再记录
# ---------------------------------------------------------------------------


def _plain(text):
    return re.sub(r"[\s，。！？、,.!?；;：:“”\"'…—-]", "", text or "")


def is_end_phrase(text):
    """是不是“好了”“没事了”“静默”“退下吧”“拜拜”这类让它回去待命的话。"""
    plain = _plain(text)
    plain = re.sub(r"^(星期[八8])", "", plain)  # “星期八，退下吧”也算
    return plain in END_PHRASES or bool(EXIT_PATTERN.match(plain))


class Interruption:
    """
    它说话时录下的你的话（被你打断，或者它刚说完你就接话），等对话窗口来处理。
        audio   你说的话（16kHz）
        resume  接着说（你只是“嗯”一声、没听清时用）；None 表示它已经说完了，没什么可接着说的
        said    它当时在说的话（用来判断录到的是不是它自己的回声）
        drop    不接着说了（停掉后台还没合成完的后半段）
    """

    def __init__(self, audio, resume=None, said="", drop=None, info=None):
        self.audio, self.resume, self.said = audio, resume, said
        self.drop = drop or (lambda: None)
        self.info = info  # 这次播放的数据（duplex.Result：第几秒打断、消回声后剩多少……），写诊断日志用


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(match):
    """“三十”→30、“四十五”→45、“一百”→100（0～999 够用了）。"""
    s, total, current = match.group(0), 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
    return str(total + current)


def _for_compare(text):
    """比较“是不是它自己的话”之前，统一写法：去标点，中文数字变阿拉伯数字，“百分之30”和“30%”一样。"""
    text = _plain(text).replace("%", "").replace("％", "").replace("百分之", "")
    text = re.sub(r"(?<!星期)[零〇一二两三四五六七八九十百]+", _cn_number, text)  # “星期八”不算数字
    return text.lower()


def echo_similarity(heard, said):
    """录到的话和它刚才说的话有多像（0～1）：录到的每个字，有多少能在它说的话里按顺序找到。"""
    import difflib
    a, b = _for_compare(heard), _for_compare(said)
    if not a or not b:
        return 0.0
    if a in b:
        return 1.0
    same = sum(block.size for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return same / len(a)


def sounds_like_echo(heard, said):
    """
    录到的话是不是它自己刚才说的（回声没消干净时，Whisper 会把它自己的话转出来）。
    这是最后一道保险：不管回声是怎么漏进来的，转出来的字和它说的话对得上，就不当成你的指令。
        两个字以上、整段都在它说的话里（比如只录到半句“打开记事本”）→ 是回声
        四个字以上、六成以上的字对得上 → 是回声（识别有点误差也算）
    """
    a = _for_compare(heard)
    if len(a) < 2:
        return False
    if set(re.findall(r"\d+", a)) - set(re.findall(r"\d+", _for_compare(said))):
        return False  # 你说的话里有它没说过的数字（比如它说 30，你说“调到五十”）：一定是你的新指令
    similarity = echo_similarity(heard, said)
    return similarity >= 1.0 or (len(a) >= 4 and similarity >= 0.6)


def _barge_ids(barge):
    info = getattr(barge, "info", None)
    return getattr(info, "playback_id", None), getattr(info, "run", None)


def _drop_barge(barge, reason):
    playback_id, run = _barge_ids(barge)
    status.barge_event("barge_drop", playback_id, run=run, reason=reason)
    barge.drop()


def log_barge(barge, text, kind):
    """记录识别分类和数字摘要。"""
    if not status.LOG_ENABLED or barge is None:
        return
    info = getattr(barge, "info", None)
    playback_id, run = _barge_ids(barge)
    details = {}
    if info is not None:
        details.update(position_ms=round(info.at * 1000, 3), speech_ms=round(info.score * 1000, 3),
                       aec_ratio=round(info.ratio, 3), glitches=info.glitches)
    status.barge_event("interruption_classified", playback_id, run=run, kind=kind,
                       can_resume=barge.resume is not None, transcript_chars=len(_plain(text)),
                       echo_similarity=round(echo_similarity(text, barge.said), 3), **details)


def interruption_kind(text, said=""):
    """
    它说话时你插了一句，这句话算什么：
        "resume"  什么都没听清 / 只是“嗯”“对”这种附和 / 其实是它自己的回声 → 接着说
        "stop"    “停”“别说了”“等一下”，或者只喊了一声“星期八” → 不说了，继续听你说
        "new"     一句新的话 → 不说了，处理这句话
    """
    plain = _plain(text)
    if not plain or len(plain) < 2 and plain not in STOP_TALKING or plain in BACKCHANNEL:
        return "resume"
    had_wake, rest = wakeword.strip_wake_word(text)
    rest = _plain(rest)
    if plain in STOP_TALKING or (had_wake and (not rest or rest in STOP_TALKING)):
        return "stop"
    if sounds_like_echo(text, said):
        return "resume"
    return "new"


def _left(seconds):
    """剩下的时间，写成“20 秒”或“2.5 分钟”。"""
    return f"{seconds:.0f} 秒" if seconds < 60 else f"{seconds / 60:.1f} 分钟"


def conversation_window(stt, on_text, record=None, clock=time.monotonic, on_feedback=None, pending=None,
                        follow_up=None, stopping=None, progress=None, initial_command=None):
    """
    一次唤醒 = 一个对话窗口。返回这个窗口里对助手说了几句话。

        stt          语音识别（SpeechToText）
        on_text      拿到一句话后做什么：on_text(文字, 是不是第一句)，
                     返回 True 表示是对助手说的（会重新计时），False 表示不是（闲聊）
        record       录音函数（测试时可以换成假的，不用真麦克风）
        clock        计时用的时钟（测试时可以换成假的，不用真等 3 分钟）
        on_feedback  出状况时调用：on_feedback("not_heard" 唤醒后没开口 / "unclear" 没听清)
        pending      取“它说话时录下的你的话”（Interruption），没有就返回 None。
                     有的话先处理它，不用再开麦克风录（你打断它时说的那句话不会丢）
        follow_up    它回答完以后，再听多少秒等你接着说（返回秒数的函数）。不填就是 INACTIVE_MINUTES 分钟
        initial_command  复核唤醒词时已经听清的指令和录音 ID
        on_text 返回 "end" 表示你让它退下了（模型调用了 end_conversation）：说完告别就关窗口

    窗口在这些情况下关闭：
        - 唤醒后 START_TIMEOUT 秒内没开口
        - 它回答完以后 follow_up() 秒内没有再对它说话（闲聊不算，不会让窗口一直开着）
        - 你说了结束语（“好了”“静默”“退下吧”……），或者模型判断你想结束对话
    """
    stopping = stopping or (lambda: STOP.is_set() or mic_blocked())
    if record is None:
        def record(**options):
            return record_utterance(**options, should_stop=stopping)
    on_feedback = on_feedback or (lambda kind: None)
    pending = pending or (lambda: None)
    finish_progress = progress.finish if progress else lambda: None
    progress = progress or nullcontext()

    def keep():
        return (follow_up() if follow_up else INACTIVE_MINUTES * 60) * speech_endpoint.wait_scale()

    addressed = 0
    heard_anything = False
    first = True
    deadline = clock() + START_TIMEOUT * speech_endpoint.wait_scale()
    while not stopping():
        remaining = deadline - clock()
        barge = pending() if initial_command is None else None  # 优先处理已经录下的话
        if initial_command is None and barge is None and remaining <= 0:
            break
        if first:
            prompt = "请说话……"
        else:
            prompt = f"还在听……（{_left(remaining)}内没有新指令就回去待命，说“静默”“好了”立即结束）"
        try:
            audio = None
            if initial_command is None:
                status.set("在听", "请说话……" if first else f"{_left(remaining)}内没新指令就回去待命，说“静默”立即结束")
                audio = barge.audio if barge is not None else record(start_timeout=remaining, prompt=prompt)
        except Exception as exc:
            friendly.LOG.exception("录音失败")
            on_feedback(friendly.failure(exc))
            return addressed
        if stopping():
            if barge is not None:
                _drop_barge(barge, "window_closed")
            break
        if audio is None and initial_command is None:
            continue  # 这段时间没人说话（或者只是杂音），回到上面看看时间到了没
        playback_id, run = _barge_ids(barge)
        # 这段录音的 ID（录音结束时生成），回答播放时带上它，日志里好把“说完”和“回答首声”配成一对
        recording_id = status.take_recording_id()  # 无论哪条路径都先取走，免得留到下一回合
        if barge is not None:
            recording_id = getattr(getattr(barge, "info", None), "recording_id", None)

        with progress:
            if initial_command is not None:
                text, recording_id = initial_command
                initial_command = None
            else:
                status.set("识别中", f"录到 {len(audio) / SAMPLE_RATE:.1f} 秒的声音")
                t = time.monotonic()
                status.barge_event("transcribe_start", playback_id, run=run, recording_id=recording_id)
                try:
                    text = (stt.transcribe(audio) or "").strip()
                except Exception as exc:
                    friendly.LOG.exception("没能识别这段录音")
                    if barge is not None:
                        _drop_barge(barge, "transcribe_error")
                    if not stopping():
                        heard_anything = True
                        on_feedback(friendly.failure(exc))
                        deadline = clock() + keep()
                    continue
                status.barge_event("transcribe_end", playback_id, run=run, recording_id=recording_id)
                print(f"  录音 {len(audio) / SAMPLE_RATE:.1f} 秒，识别用时 {time.monotonic() - t:.2f} 秒")
            print(f"  识别结果：{text or '（空）'}")
            audio = None  # 录音用完就丢，不保存
            if stopping():
                if barge is not None:
                    _drop_barge(barge, "window_closed")
                break

            if barge is not None:  # 这句话是它说话时录下的
                kind = interruption_kind(text, barge.said)
                log_barge(barge, text, kind)
                if kind == "resume":
                    if barge.resume is not None:
                        print("  （只是附和一声 / 没听清，接着说）")
                        status.barge_event("barge_resume", playback_id, run=run, reason="echo_or_backchannel")
                        finish_progress()
                        barge.resume()
                        continue
                    if text:
                        continue
                if kind == "stop":
                    _drop_barge(barge, "stop_phrase")
                    print("  好的，不说了。")
                    status.set("在听", "好的，不说了")
                    heard_anything = True
                    deadline = clock() + keep()
                    first = False
                    continue

            if not text:
                heard_anything = True
                status.set("没听清", friendly.MESSAGES["unclear"])
                on_feedback("unclear")
                if first:  # 唤醒后的第一句没听清：告诉你，别让你干等
                    deadline = clock() + START_TIMEOUT * speech_endpoint.wait_scale()
                continue
            heard_anything = True

            if is_end_phrase(text):
                if barge is not None:
                    _drop_barge(barge, "end_phrase")
                print("  好的。")
                break

            # 对话中又喊了唤醒词：重新计时；唤醒词后面跟着的话，一定是对助手说的
            had_wake, text = wakeword.strip_wake_word(text)
            if had_wake:
                deadline = clock() + keep()
                if not text:
                    print("  （听到唤醒词，继续听着）")
                    continue

            dispatch_first = first or had_wake
            token = status.barge_source.set((playback_id, run))
            turn_token = status.barge_turn.set((recording_id, None))
            try:
                status.barge_event("on_text_call", playback_id, run=run, first=dispatch_first)
                answer = on_text(text, dispatch_first)
            except Exception as exc:
                status.barge_event("on_text_error", playback_id, run=run, error_type=type(exc).__name__)
                friendly.LOG.exception("处理语音指令失败")
                on_feedback(friendly.failure(exc))
                answer = True
            finally:
                status.barge_turn.reset(turn_token)
                status.barge_source.reset(token)
            # 返回协议是 True、False 或 "end"。其他类型只记录类型名，避免把任意对象写进日志。
            value = answer if answer is None or isinstance(answer, bool) or (
                isinstance(answer, str) and answer == "end"
            ) else type(answer).__name__
            status.barge_event("on_text_return", playback_id, run=run, result=value)
            if answer:
                addressed += 1
                deadline = clock() + keep()  # 对助手说了话：它回答完以后再听一会儿，等你接着说
                if barge is not None:
                    _drop_barge(barge, "on_text_handled")
                if answer == "end":  # 你让它退下了（模型判断的）：说完告别就回去待命
                    print("  好的，回去待命。")
                    break
            elif barge is not None and barge.resume is not None:
                print("  （刚才那句不是对我说的，接着说）")
                status.barge_event("barge_resume", playback_id, run=run, reason="on_text_ignored")
                finish_progress()
                barge.resume()  # 你插嘴的那句是在跟别人说话：它接着把话说完
            first = False

    if DICTATING.is_set():
        status.set("群聊语音输入", "正在录群聊的语音输入，这段时间助手不听")
    elif PAUSED.is_set():
        status.set("已暂停", "右键托盘图标 → 取消“暂停监听”就能继续")
    elif not heard_anything and not stopping():
        status.set("没听清", friendly.MESSAGES["unclear"])
        on_feedback("not_heard")
    print("  ——对话窗口关闭。之后说的话不会被听写，也不会被记录——")
    return addressed


# ---------------------------------------------------------------------------
# 单独运行：说唤醒词“星期八”唤醒（加 --enter 则改成按回车唤醒）
# ---------------------------------------------------------------------------


def put_whisper_to_standby(stt):
    """按 WHISPER_IN_STANDBY 的设置，把语音识别模型放回待命的位置。"""
    if WHISPER_IN_STANDBY == "ram":
        stt.park()
    elif WHISPER_IN_STANDBY == "off":
        stt.unload()
    # "gpu"：留在显卡上，什么也不做


def main(use_enter=False, only_text=False, paused=None, notify=None):
    """
    语音助手的主流程：待命 → 唤醒 → 对话窗口 → 复盘 → 待命……
        use_enter  按回车唤醒（调试用）；否则听唤醒词
        only_text  只识别文字，不交给助手
        paused     threading.Event：设置后暂停监听（麦克风关闭），清除后继续
        notify     弹出系统通知的函数（后台运行时用），notify(文字)
    每一步都会通过 status.set(...) 广播出去（状态窗、托盘图标会跟着变）。
    退出：设置 STOP（托盘“退出”、关闭快捷方式）或按 Ctrl+C。
    """
    notify = notify or (lambda message: None)
    paused = paused or PAUSED  # 托盘传进来的就是 PAUSED；对话窗口里也看这个信号
    status.LOG_ENABLED = True  # 记录每一步用了几秒（logs\timing-日期.log，不记你说的话）
    if use_enter or only_text:
        _run_wakeword(use_enter, only_text, paused, notify, lambda: False)
        return

    while not STOP.is_set():
        selected = voice_mode()
        next_check = 0.0
        changed = False

        def switched():
            nonlocal changed, next_check
            now = time.monotonic()
            if not changed and now >= next_check:
                next_check = now + 0.1
                changed = voice_mode() != selected
            return changed

        if selected == MINICPM_MODE:
            _run_minicpm(paused, switched)  # 群聊语音输入时不停 MiniCPM-o，在 minicpm_voice 里静音
        else:
            _run_wakeword(False, False, _EitherEvent(paused, DICTATING), notify, switched)
        if not switched():
            return


def _run_minicpm(paused, switched):
    from speak import play_notice

    def stopping():
        return STOP.is_set() or paused.is_set() or switched() or local_models.reasoning_pending()

    last_failure = None
    while not STOP.is_set() and not switched():
        if paused.is_set():
            if status.current()["state"] != "已暂停":
                status.set("已暂停", "取消暂停监听后，MiniCPM-o 会重新加载")
            STOP.wait(0.1)
            continue
        if local_models.reasoning_pending():
            STOP.wait(0.1)
            continue
        try:
            from minicpm_voice import run
            with local_models.realtime_slot() as acquired:
                if acquired:
                    run(stopping)
                    last_failure = None
        except Exception as exc:
            if not stopping():
                friendly.LOG.exception("实时通话失败")
                kind = friendly.failure(exc)
                if kind != last_failure:
                    play_notice(kind, should_stop=stopping)
                    last_failure = kind
                deadline = time.monotonic() + 5
                while not stopping() and time.monotonic() < deadline:
                    STOP.wait(0.1)


def _run_wakeword(use_enter, only_text, paused, notify, switched):
    from speak import play_notice

    global ACTIVE_STT
    last_failure = None
    while not STOP.is_set() and not switched():
        status.set("启动中", friendly.MESSAGES["preparing"])
        try:
            print(f"麦克风：{sd.query_devices(kind='input')['name']}")
            stt = SpeechToText(load=False)
            ACTIVE_STT = stt
            try:
                _wakeword_loop(stt, use_enter, only_text, paused, notify, switched)
            finally:
                ACTIVE_STT = None
                start = time.monotonic()
                stt.close()
                status.barge_event("model_release", engine="wakeword", model="whisper",
                                   elapsed_ms=round((time.monotonic() - start) * 1000, 1))
            return
        except Exception as exc:
            friendly.LOG.exception("唤醒词模式运行失败")
            kind = friendly.failure(exc)
            if kind != last_failure and not STOP.is_set() and not switched():
                if only_text or sys.platform == "darwin":
                    status.set("出错", friendly.MESSAGES[kind])
                else:
                    play_notice(kind, should_stop=lambda: STOP.is_set() or switched() or paused.is_set())
                last_failure = kind
            STOP.wait(5)


def _transcribe_wake(audio, stt):
    """用 SenseVoice 复核唤醒词，模型不可用时按原流程用 Whisper。"""
    if transcription.sensevoice_installed():
        try:
            original = ""
            for samples in wakeword.verification_audio(audio):
                text = transcription.sensevoice.transcribe(samples)
                # 听清了“星期八点”就结束复核，避免把“点”压掉后误唤醒。
                if wakeword.confirm(text) or re.search(r"星期[八8]点", text):
                    return text, "sensevoice"
                if samples is audio:
                    original = text
            return original, "sensevoice"
        except Exception as exc:
            status.barge_event("transcription_fallback", engine="sensevoice", error=type(exc).__name__)
    return stt.transcribe(audio, prompt=wakeword.VERIFY_PROMPT, vad_filter=False), transcription.WHISPER


def _wakeword_loop(stt, use_enter, only_text, paused, notify, switched):
    def stopping():
        return STOP.is_set() or paused.is_set() or switched()

    # 开机准备阶段只在界面显示“正在准备”，不出声：开机时没人在叫它，突然说话会吓人。
    startup_notice = nullcontext()
    if not only_text:
        import speak  # 后面播放提示要用

    t = time.time()
    with startup_notice:
        if WHISPER_IN_STANDBY != "off":
            stt.load()
        put_whisper_to_standby(stt)
        try:
            detector = None if use_enter else wakeword.WakeWordDetector()
        except Exception as exc:
            raise friendly.ModelNotReady("唤醒模型没有准备好") from exc
    print(f"语音识别模型准备好了（待命时放在：{WHISPER_IN_STANDBY}），用时 {time.time() - t:.1f} 秒")
    command_stt = transcription.VoiceTranscriber(stt)
    wake_names = "、".join(wakeword.WAKE_WORDS)

    speaker = None
    if only_text:
        def on_text(text, first):
            return True  # 只看识别结果，不交给助手，也不记录
    else:
        import assistant
        import bridge
        print(f"记忆：{assistant.MEMORY.status()}")
        if speak.ENABLED:
            speaker = speak.Speaker()
            # 在后台提前合成“我在”和出状况时的提示，用的时候 0 等待（不拖慢启动）
            threading.Thread(target=speaker.prepare, args=(WAKE_REPLIES,), daemon=True).start()
            threading.Thread(target=speaker.prepare, args=(list(friendly.MESSAGES.values()),),
                             kwargs={"offline": True}, daemon=True).start()
            print(f"语音回复：{speak.CANDIDATES.get(speaker.voice, speaker.voice)}")

        def voice_dispatch(text):
            if not bridge.matches_voice(text):
                return bridge.voice_turn(text)  # 只看“我洗完了”这类话有没有让挂起任务满足条件，很快
            if not speaker:
                return bridge.route(text)
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as worker:
                report = worker.submit(bridge.route, text)
                speak_out(bridge.ACKNOWLEDGEMENT)
                return report.result()

        def on_text(text, first):
            with assistant.TURN_LOCK:  # 和界面里打字的排队，不同时处理两句
                if bridge.voice_enabled():
                    result = assistant.handle(text, first=first, voice_dispatch=voice_dispatch)
                else:
                    result = assistant.handle(text, first=first)
            assistant.print_result(result)
            if result["ignored"]:
                return False  # 不是对助手说的：不出声、不重新计时
            for a in result["actions"]:
                if a["tool"] == "delegate_task":
                    notify(a["result"]["message"])  # 交给 Claude 等 AI 时弹个通知
            # “拿不准”的也照样回答（之前不出声，你会以为它没反应）；它们只是先放在暂存区，等复盘决定留不留
            words = (assistant.spoken_reply(result) if speaker else result["reply"]) or friendly.MESSAGES["error"]
            if progress is not None:
                progress.finish()
            status.publish({"type": "turn", "source": "voice", "you": text, "reply": words,
                            "actions": [a["tool"] for a in result["actions"]]})  # 界面里显示这一轮对话
            last_reply["text"] = words
            if speaker:
                print(f"  助手说：{words}")
                source_id, source_run = status.barge_source.get()
                status.barge_event("reply_ready", source_id, run=source_run)
                speak_out(words, is_reply=True)
            return "end" if result.get("end") else True  # "end"：你让它退下了，说完告别就回去待命

    last_reply = {"text": ""}  # 它最后说的话（是在问你问题的话，多等你一会儿）

    # ---- 说话（可以被打断）----
    barge = []  # 它说话时录下的你的话，等对话窗口来取
    playback_lock = threading.Lock()
    microphone = None

    def barge_in_on():
        import settings
        return settings.get("barge_in", True)

    def on_interrupt():
        print("  （你打断了我，在听你说）")
        status.set("在听", "你打断了我，请说……")

    def play(start, words, is_reply):
        """播放；被打断（或者刚说完你就接话）时，把录下的话留给对话窗口。"""
        spoken = start()
        if spoken:
            status.barge_event("speech_return", spoken.result.playback_id, run=spoken.result.run,
                               is_reply=is_reply)
        if spoken and spoken.audio is not None:
            resume = None
            if spoken.interrupted:
                resume = lambda: play(spoken.resume, words, is_reply)  # noqa: E731

            def drop():
                """不接着说了。如果打断的是回答，在短期记忆里注明你只听到了哪部分（像 ChatGPT 通话模式那样）。"""
                spoken.cancel()
                if is_reply and spoken.interrupted:
                    assistant.MEMORY.mark_interrupted(spoken.spoken_text())

            barge.append(Interruption(spoken.audio, resume, words, drop=drop, info=spoken.result))
        return bool(spoken)

    def speak_out(words, is_reply=False):
        """说一句话。设置里打开了“可以打断”时，边说边听。is_reply：这是对你那句话的回答（会记进对话）。"""
        if not speaker:
            return False
        token = status.barge_turn.set((status.barge_turn.get()[0], is_reply))
        try:
            with playback_lock:
                return play(lambda: speaker.say(words, listen=barge_in_on(), should_stop=stopping,
                                                on_level=status.level, on_interrupt=on_interrupt,
                                                microphone=microphone), words, is_reply)
        finally:
            status.barge_turn.reset(token)

    def take_barge():
        return barge.pop(0) if barge else None

    def feedback(kind):
        """出状况时出声告诉你（没有声音时“叮”一声）。"""
        if stopping():
            return
        if progress is not None:
            progress.finish()
        if not only_text:
            speak.play_notice("unclear" if kind == "not_heard" else kind, speaker, stopping,
                              microphone=microphone)

    def waiting_feedback(kind):
        with playback_lock:
            speak.play_notice(kind, speaker, stopping, microphone=microphone)

    progress = friendly.WaitNotice(waiting_feedback, stopping) if speaker else None

    window_open = threading.Event()    # 对话窗口是否开着
    models_lock = threading.Lock()     # 加载/卸载模型时上锁，防止“刚唤醒就被复盘线程卸载”
    review_thread = None
    warmup_thread = None

    def wake_up():
        """唤醒：在后台加载模型，你说第一句话的同时就在加载了。"""
        nonlocal warmup_thread
        with models_lock:
            window_open.set()
            if transcription.current() == transcription.WHISPER:
                stt.load_in_background()
            if not only_text and (warmup_thread is None or not warmup_thread.is_alive()):
                warmup_thread = assistant.load_in_background()

    def after_window():
        """窗口关闭后复盘并释放转写模型，Gemma 由 Ollama 按空闲时限卸载。"""
        if not only_text:
            if not window_open.is_set():
                status.set("复盘中", "整理刚才听到的话，决定留下什么")
            result = assistant.review()
            print(f"\n  （后台复盘完成：{result['message']}）")
        with models_lock:
            if window_open.is_set():
                return  # 复盘期间你又唤醒了，模型继续留着用
            put_whisper_to_standby(stt)
            status.set("待命中", f"说“{wake_names}”唤醒")

    def is_paused():
        return paused is not None and paused.is_set()

    def wait_for_wake(audio_io):
        """等你唤醒，返回后续指令、录音 ID 和底噪。暂停时先交还麦克风。"""
        nonlocal microphone
        if use_enter:
            if input("\n【待命】按回车唤醒（输入 q 退出）：").strip().lower() == "q":
                return False
            if stopping():
                return None
            return "", None, 0.0
        print(f"\n【待命】说“{wake_names}”唤醒（按 Ctrl+C 退出）")
        while not STOP.is_set() and not switched():
            if is_paused():
                if microphone is not None:
                    return None
                if DICTATING.is_set() and not PAUSED.is_set():
                    status.set("群聊语音输入", "正在录群聊的语音输入，这段时间助手不听")
                else:
                    status.set("已暂停", "右键托盘图标 → 取消“暂停监听”就能继续")
                while is_paused() and not STOP.is_set() and not switched():
                    time.sleep(0.3)
                continue
            if microphone is None:
                microphone = audio_io.enter_context(duplex.Microphone())
            if status.current()["state"] not in ("待命中", "复盘中", "卸载模型"):
                status.set("待命中", f"说“{wake_names}”唤醒")
            word, audio = detector.wait(stop=stopping, read=microphone.read)
            if STOP.is_set() or switched():
                return False
            if audio is None:
                return None
            audio, noise, recording_id = _finish_wake_audio(microphone, audio, stopping)
            if audio is None or stopping():
                return None
            # 二次确认：转写唤醒词和同一句话里的指令。
            status.set("确认唤醒词", "听起来像唤醒词，正在确认")
            verify_start = time.monotonic()
            status.barge_event("transcribe_start", recording_id=recording_id, path="wake")
            with progress or nullcontext():
                text, engine = _transcribe_wake(audio, stt)
            status.barge_event("transcribe_end", recording_id=recording_id, path="wake")
            transcribe_ms = round((time.monotonic() - verify_start) * 1000)
            audio = None
            confirmed = wakeword.confirm(text)
            status.barge_event("wake_verify", kws_word=word, confirmed=confirmed, engine=engine,
                               transcribe_ms=transcribe_ms, recording_id=recording_id)
            if not stopping() and confirmed:
                if transcription.current() == transcription.QWEN:
                    put_whisper_to_standby(stt)
                return wakeword.wake_command(text), recording_id, noise
            put_whisper_to_standby(stt)
            status.set("待命中", "刚才不是唤醒词，继续待命")
            print("  （听起来像唤醒词，但确认不是，继续待命）")
            if stopping():
                return None
        return False

    def acknowledge():
        """唤醒后的回应：能说话就说一句“我在”，不能就“叮”一声。
        昨晚做了“夜间研究”的话，今天第一次叫醒时顺带提一句（只提一次，细节你问“昨晚研究了什么”再说）。"""
        news = None
        if speaker and not only_text:
            try:
                import research
                news = research.morning_news()
            except Exception:
                friendly.LOG.exception("读取唤醒问候失败")
                news = None
        if news:
            research.mark_spoken()
            summary = news["summary"].rstrip("。.!！ ")
            line = f"我在。另外，昨晚我研究了{'、'.join(news['projects'])}，{summary}。想听细节就问我昨晚研究了什么。"
            if speak_out(line):
                return
        if speaker and speak_out(random.choice(WAKE_REPLIES)):
            return
        with microphone.during_playback():
            wakeword.beep_wake()

    print("待命中：首次唤醒时加载本地大模型，短时间内再次唤醒可以继续使用。")
    rounds = 0  # 上次清屏之后对助手说了几句
    try:
        while not STOP.is_set() and not switched():
            audio_io = ExitStack()
            try:
                wake = wait_for_wake(audio_io)
                if wake is False:
                    break
                if wake is None or stopping():
                    continue
                command, recording_id, noise = wake
                wake_up()
                if not use_enter and not command:
                    acknowledge()

                # 满 CLEAR_EVERY 句，就在这次唤醒时清屏（只清屏幕显示，不影响记忆）
                if rounds >= CLEAR_EVERY:
                    housekeeping.clear_screen()
                    print("（已清屏。对话仍记在 memory\\history 里）" if not only_text else "（已清屏）")
                    rounds = 0

                print("  ——对话窗口打开——")
                last_reply["text"] = ""

                def record(**options):
                    nonlocal microphone
                    if microphone is None:  # 回车唤醒时，开始录音才打开麦克风。
                        microphone = audio_io.enter_context(duplex.Microphone())
                    return record_utterance(**options, should_stop=stopping, microphone=microphone, noise=noise)

                rounds += conversation_window(command_stt, on_text, on_feedback=None if only_text else feedback,
                                              record=record, pending=take_barge,
                                              follow_up=lambda: follow_up_seconds(last_reply["text"]),
                                              stopping=stopping, progress=progress,
                                              initial_command=(command, recording_id) if command else None)
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                audio_io.close()
                microphone = None
                window_open.clear()
                for leftover in barge:  # 窗口关了：没处理的录音丢掉，后台合成也停掉
                    _drop_barge(leftover, "window_closed")
                barge.clear()
            if not use_enter and not stopping():
                wakeword.beep_close()

            if review_thread is not None:
                review_thread.join()  # 上一次复盘还没做完的话，先等它做完
            review_thread = threading.Thread(target=after_window)
            review_thread.start()
    finally:
        # 换模式和退出都先等复盘及模型预热结束。
        if switched() and not STOP.is_set():
            status.set("卸载模型", "正在关闭唤醒词模式")
        else:
            status.set("正在退出", "做完复盘、卸载模型")
        if review_thread is not None:
            review_thread.join()
        if not only_text:
            if warmup_thread is not None:
                warmup_thread.join()
            # 换到 MiniCPM-o 前会单独卸载并确认 Ollama 的模型已释放。
            if not switched():
                assistant.unload()
    print("再见！")


if __name__ == "__main__":
    # 先关掉之前没关的 listen.py，保证同时只有一个在运行，不会几个程序抢显存
    closed = housekeeping.close_old_instances(__file__)
    if closed:
        print(f"已关掉 {closed} 个之前没关的 listen.py，释放了显存。")
    status_window = None
    if sys.platform != "darwin" and "--no-overlay" not in sys.argv:
        import overlay
        import settings
        status_window = overlay.Overlay(mode=lambda: settings.get("overlay_mode", "auto")).start()
    try:
        main(use_enter="--enter" in sys.argv, only_text="--only-text" in sys.argv)
    finally:
        if status_window:
            status_window.stop()
