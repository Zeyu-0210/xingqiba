"""
wakeword.py —— 第四步：唤醒词监听（sherpa-onnx 关键词检测）

    离线、免费、不用注册；只用 CPU，模型只有几 MB。待命时整个助手只有它在运行。

    WakeWordDetector.wait()        打开麦克风一直听，听到“像唤醒词”的声音就返回（连同最近 3 秒录音）
    confirm(文字)                   二次确认：把那 3 秒录音转成文字后，看是不是真的说了唤醒词
                                   （“星期八”和“星期吧”只差声调，要靠语音转写确认）
    WakeWordDetector.detect(音频)   检查一段录音里有没有唤醒词（测试用）
    beep_wake() / beep_close()     提示音：唤醒时“叮↑”，窗口关闭时“叮↓”

想换唤醒词：改下面的 WAKE_WORDS。右边是拼音，声母和韵母分开、带声调、用空格隔开，
例如“你好小智” → "n ǐ h ǎo x iǎo zh ì"。不会写的话告诉我新的唤醒词，我来转换。
"""

import collections
import os
import re
import unicodedata

import numpy as np
import sherpa_onnx
import sounddevice as sd

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
KWS_DIR = os.path.join(PROJECT_DIR, "models", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
KEYWORDS_FILE = os.path.join(PROJECT_DIR, "models", "wakeword_keywords.txt")  # 根据 WAKE_WORDS 自动生成

# 唤醒词：可以有好几个，任意一个都能唤醒
WAKE_WORDS = {
    "星期八": "x īng q ī b ā",
}

# 灵敏度（实测后可以微调）：
#   THRESHOLD 越大越难被唤醒（误唤醒少，但可能要喊两遍）；越小越容易被唤醒（但可能被别的话误唤醒）
#   SCORE     给唤醒词的“加分”，越大越容易被唤醒
THRESHOLD = 0.10
SCORE = 2.0

SAMPLE_RATE = 16000
READ_SECONDS = 0.1    # 每 0.1 秒处理一次麦克风的声音
BUFFER_SECONDS = 3.0  # 一直保留最近 3 秒的声音（只在内存里），听到唤醒词时拿去做二次确认
BEEP_VOLUME = 0.2     # 提示音音量（0~1）

# 二次确认退回 Whisper 时用的提示（不要写“星期八”进去，否则它会倾向于听成星期八）
VERIFY_PROMPT = "以下是普通话的句子。"
_PINYIN_WAKE = re.compile(
    r"(?<![a-z0-9])(?:xīng\s*qī\s*bā|xing1\s*qi1\s*ba1)"
    r"(?![a-z0-9]|\s*(?:点|diǎn\b|dian3\b))",
    re.IGNORECASE,
)
_LEADING_FILLER = re.compile(r"^[\s，。！？、,.!?]*(?:嘿|喂|嗨|哎|嗯)?[\s，。！？、,.!?]*")
_WAKE_PUNCTUATION = " \t\r\n，。！？、：；,.!?:;"


def _write_keywords_file():
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        for text, tokens in WAKE_WORDS.items():
            f.write(f"{tokens} @{text}\n")


class WakeWordDetector:
    def __init__(self, threshold=THRESHOLD, score=SCORE):
        _write_keywords_file()
        model = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(KWS_DIR, "tokens.txt"),
            encoder=os.path.join(KWS_DIR, f"encoder-{model}"),
            decoder=os.path.join(KWS_DIR, f"decoder-{model}"),
            joiner=os.path.join(KWS_DIR, f"joiner-{model}"),
            keywords_file=KEYWORDS_FILE,
            num_threads=1,            # 只用 1 个 CPU 核心，尽量不影响游戏
            keywords_score=score,
            keywords_threshold=threshold,
            provider="cpu",
        )

    def _feed(self, stream, samples):
        """喂一小段声音进去；如果听到了唤醒词，返回唤醒词，否则返回 None。"""
        stream.accept_waveform(SAMPLE_RATE, samples)
        while self.spotter.is_ready(stream):
            self.spotter.decode_stream(stream)
            word = self.spotter.get_result(stream)
            if word:
                self.spotter.reset_stream(stream)  # 听到后要重置，才能接着听下一次
                return word
        return None

    def wait(self, device=None, stop=None, *, read=None):
        """
        打开麦克风一直听，直到听到“像唤醒词”的声音，返回 (唤醒词, 最近 3 秒的录音)。
        stop 可以是 threading.Event，也可以是一个返回 True/False 的函数；
        变成 True 时停止监听并返回 (None, None)（用来退出或暂停）。
        声音只在内存里一小段一小段地处理，最多保留最近 3 秒，不转文字、不保存。
        """
        if stop is None:
            should_stop = lambda: False  # noqa: E731
        elif callable(stop):
            should_stop = stop
        else:
            should_stop = stop.is_set
        stream = self.spotter.create_stream()
        block = int(SAMPLE_RATE * READ_SECONDS)
        recent = collections.deque(maxlen=int(BUFFER_SECONDS / READ_SECONDS))
        def collect(read_samples):
            while not should_stop():
                samples = read_samples()
                if samples is None:
                    break
                recent.append(samples)
                word = self._feed(stream, samples)
                if word:
                    return word, np.concatenate(recent)
            return None, None

        if read is not None:
            return collect(lambda: read(block, should_stop)[0])
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=block, device=device) as mic:
            return collect(lambda: mic.read(block)[0][:, 0].copy())

    def detect(self, audio):
        """
        检查一段 16kHz 录音（numpy 数组）里出现了哪些唤醒词（测试用）。
        返回 [(唤醒词, 在录音里第几个采样点听到的), ...]
        """
        stream = self.spotter.create_stream()
        found = []
        block = int(SAMPLE_RATE * READ_SECONDS)
        padded = np.concatenate([audio.astype(np.float32), np.zeros(SAMPLE_RATE, np.float32)])
        for i in range(0, len(padded), block):
            word = self._feed(stream, padded[i:i + block])
            if word:
                found.append((word, min(i + block, len(audio))))
        return found


def confirm(text):
    """
    二次确认：录音转成的文字里，是不是真的有唤醒词。
        “星期八”“嗯，星期八” → True
        “这星期吧”“下个星期吧” → False（小模型会把它们听成星期八）
        “星期八点见”          → False（说的是几点，不是在叫助手）
    """
    for word in WAKE_WORDS:
        pattern = re.escape(word).replace("八", "[八8]") + r"(?!\s*点)"
        if re.search(pattern, text):
            return True
    return bool(_PINYIN_WAKE.search(unicodedata.normalize("NFC", text)))


def verification_audio(audio):
    """先用原音。需要重听时，逐步减轻底噪和录音两端静音的影响。"""
    yield audio
    if not len(audio):
        return
    frame_size = SAMPLE_RATE // 100  # 10 毫秒
    frames = np.pad(audio, (0, -len(audio) % frame_size)).reshape(-1, frame_size)
    levels = np.sqrt(np.mean(frames**2, axis=1))
    floor = np.quantile(levels, 0.2)
    if floor < 1e-6:
        return  # 已经很安静，不再重转
    gains = np.clip((levels / floor - 1) / 2, 0, 1).astype(np.float32)
    cleaned = (frames * gains[:, None]).ravel()
    yield cleaned[:len(audio)]

    active = np.flatnonzero(levels > 3 * floor)
    if len(active):
        start = max(0, active[0] - 10)
        end = min(len(frames), active[-1] + 11)  # 语音前后各留 100 毫秒
        if start or end < len(frames):
            yield cleaned[start * frame_size:min(end * frame_size, len(audio))]


def _tone(freqs, seconds=0.12):
    """生成一段柔和的提示音（几个音连着放，带淡入淡出，不刺耳）。"""
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    fade = np.minimum(1, np.minimum(t, t[::-1]) / 0.02)  # 头尾 20 毫秒淡入淡出
    parts = [BEEP_VOLUME * fade * np.sin(2 * np.pi * f * t) for f in freqs]
    return np.concatenate(parts).astype(np.float32)


def beep_wake():
    """唤醒提示音：叮↑"""
    sd.play(_tone([660, 880]), SAMPLE_RATE)
    sd.wait()


def beep_close():
    """窗口关闭提示音：叮↓"""
    sd.play(_tone([660, 440]), SAMPLE_RATE)
    sd.wait()


def strip_wake_word(text):
    """
    对话窗口里又喊了唤醒词时用：返回 (有没有在叫助手, 去掉唤醒词后剩下的话)。
    只认“开头”的唤醒词（这才是在叫它）：
        “星期八，把音量调大” → (True, "把音量调大")
        “嘿，星期八”         → (True, "")
    话中间提到的不算，原样保留（之前会把“你叫星期八”里的名字删掉，模型就不知道你说的名字了）：
        “你的名字叫星期八”   → (False, "你的名字叫星期八")
    """
    start = _LEADING_FILLER.match(text).end()
    for word in WAKE_WORDS:
        pattern = re.escape(word).replace("八", "[八8]") + r"(?!\s*点)"
        match = re.match(pattern, text[start:])
        if match:
            return True, text[start + match.end():].strip(_WAKE_PUNCTUATION)
    return False, text


def wake_command(text):
    """取已确认的唤醒语句中紧跟在名字后的指令。"""
    text = unicodedata.normalize("NFC", text).strip()
    matched, command = strip_wake_word(text)
    if matched:
        return command
    match = _PINYIN_WAKE.match(text, _LEADING_FILLER.match(text).end())
    if match:
        return text[match.end():].strip(_WAKE_PUNCTUATION)
    return ""


if __name__ == "__main__":
    # 只测第一道检查（不做二次确认），看看灵敏度
    detector = WakeWordDetector()
    print(f"正在听唤醒词：{'、'.join(WAKE_WORDS)}（只做第一道检查；按 Ctrl+C 退出）")
    try:
        while True:
            word, _audio = detector.wait()
            beep_wake()
            print(f"  听到了：{word}")
    except KeyboardInterrupt:
        print("再见！")
