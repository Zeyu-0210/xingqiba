"""
speak.py —— 把助手的回复念出来，声音可以随时换（像手机版 ChatGPT / Claude 那样挑声音）

    换声音的三种方法：
      1. 说“换成晓晓的声音”“换个女声”“用离线的声音”……
      2. 右键托盘图标 →“声音”，点一个
      3. 改下面的 DEFAULT_VOICE（只在还没选过声音时生效）
    选过的声音记在 settings.json 里，重启后不变。

    两种来源：
      edge:   微软神经语音：最自然。需要联网，要念的文字会发到微软的服务器合成
      kokoro: 本地离线语音：不联网，文字不出电脑；自然度稍差一点

    试听全部声音：.venv\\Scripts\\python.exe speak.py   （样音存在 voices 文件夹）

    边合成边说：长回答按句子切开，第一句合成好就开始说，后面的在后台接着合成（等的时间更短）。
    可以打断：say(..., listen=True) 时麦克风也开着（见 duplex.py），你一开口它就停下来听你说。
"""

import asyncio
import io
import os
import re
import threading
import time

import numpy as np

import duplex
import friendly
import settings
import status

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
KOKORO_DIR = os.path.join(PROJECT_DIR, "models", "kokoro-multi-lang-v1_0")

EDGE_RATE = "+5%"     # 微软语音的语速（"+10%" 快一点，"-10%" 慢一点）
KOKORO_SPEED = 1.0    # 本地语音的语速
ENABLED = True        # False = 不说话，只显示文字

# 声音菜单：内部编号 → (名字, 特点)。顺序就是菜单里的顺序
VOICES = {
    "edge:zh-CN-YunyangNeural": ("云扬", "男·沉稳专业，像贾维斯"),
    "edge:zh-CN-XiaoxiaoNeural": ("晓晓", "女·温暖，像星期五"),
    "edge:en-US-AndrewMultilingualNeural": ("安德鲁", "男·温暖自信，带点外国口音，像英国管家；反应稍慢"),
    "edge:en-US-AvaMultilingualNeural": ("艾娃", "女·亲切体贴，带点外国口音；反应稍慢"),
    "edge:zh-TW-HsiaoChenNeural": ("晓臻", "女·温柔，台湾腔"),
    "edge:zh-CN-YunxiNeural": ("云希", "男·年轻阳光"),
    "edge:zh-CN-XiaoyiNeural": ("晓伊", "女·活泼"),
    "edge:zh-CN-YunjianNeural": ("云健", "男·有激情"),
    "kokoro:52": ("离线男声", "男·沉稳，不联网"),
    "kokoro:47": ("离线女声", "女·温暖，不联网"),
}
DEFAULT_VOICE = "edge:zh-CN-YunyangNeural"

# 兼容旧代码：编号 → “名字（特点）”
CANDIDATES = {key: f"{name}（{about}）" for key, (name, about) in VOICES.items()}


def current_voice():
    """现在用的是哪个声音（内部编号）。"""
    voice = settings.get("voice", DEFAULT_VOICE)
    return voice if voice in VOICES else DEFAULT_VOICE


def find_voice(name):
    """按名字或内部编号找声音，找不到返回 None。"""
    name = str(name or "").strip()
    for key, (voice_name, _about) in VOICES.items():
        if name in (key, voice_name):
            return key
    return None


def set_voice(voice):
    """换声音（这是给秘书模型用的工具，托盘菜单也用它）。"""
    key = find_voice(voice)
    if key is None:
        return {"message": f"没有叫“{voice}”的声音，可以选：{'、'.join(n for n, _ in VOICES.values())}"}
    settings.set("voice", key)
    name, about = VOICES[key]
    return {"voice": name, "message": f"已经换成{name}的声音（{about}）"}


def clean(text):
    """去掉念出来很奇怪的符号（Markdown 标记、表情等），“30%”改成“百分之30”（离线语音不认识 %）。"""
    text = re.sub(r"(\d+(?:\.\d+)?)\s*[%％]", r"百分之\1", str(text))
    text = re.sub(r"[*#`>_~|]", "", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)  # 表情符号
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text, min_chars=8):
    """按句号、问号、感叹号、分号切开；太短的句子并到下一句（一句一句合成，第一句好了就能开口）。"""
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;\n])", text) if p.strip()]
    merged = []
    for part in parts:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += part
        else:
            merged.append(part)
    return merged or [text]


class Spoken:
    """
    一次说话的结果。可以直接当 True/False 用（有没有说出来）。
        interrupted  被你打断了（停在一半）
        audio        你说的话（打断时说的，或者它刚说完你就接话）；没有就是 None
        resume()     从停下的地方接着说（你只是“嗯”了一声、或者没听清的时候用）
        cancel()     不接着说了（后台还没合成完的句子也不合成了）
    """

    def __init__(self, ok, text="", playback=None, result=None, options=None):
        self.ok, self.text, self.playback, self.result = ok, text, playback, result
        self.options = options or {}

    def __bool__(self):
        return self.ok

    @property
    def interrupted(self):
        return bool(self.result and self.result.interrupted)

    @property
    def audio(self):
        return self.result.audio if self.result else None

    def resume(self):
        status.barge_event("playback_resume", self.playback.id, run=getattr(self.result, "run", None))
        self.playback.rewind()
        status.set("说话中", self.text)
        self.result = self.playback.run(**self.options)
        return self

    def cancel(self):
        if self.playback is not None:
            self.playback.cancel()

    def spoken_text(self):
        """你实际听到的部分（被打断时只到停下的地方）。"""
        return self.playback.spoken_text() if self.playback is not None else ""


class Speaker:
    """
    念文字的“嘴巴”。不指定 voice 时，每次说话都用当前选中的声音（随时换随时生效）。
    """

    def __init__(self, voice=None):
        self.fixed_voice = voice
        self._kokoro = None
        self._lock = threading.Lock()
        self._cache = {}         # (声音, 文字) → 合成好的声音
        self._cache_engine = {}  # (声音, 文字) → 当初合成用的引擎（日志用）
        self._phrases = []       # 需要提前合成好的常用短句
        self._prepared_for = None

    @property
    def voice(self):
        return self.fixed_voice or current_voice()

    # ---------- 合成 ----------

    def _edge(self, name, text):
        import edge_tts
        from faster_whisper.audio import decode_audio  # 用来把 mp3 解码成声音数据

        async def fetch():
            data = bytearray()
            async for chunk in edge_tts.Communicate(text, name, rate=EDGE_RATE).stream():
                if chunk["type"] == "audio":
                    data += chunk["data"]
            return bytes(data)

        mp3 = asyncio.run(fetch())
        return decode_audio(io.BytesIO(mp3), sampling_rate=24000), 24000

    def _kokoro_tts(self, sid, text):
        import sherpa_onnx
        if self._kokoro is None:
            d = KOKORO_DIR
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=os.path.join(d, "model.onnx"),
                        voices=os.path.join(d, "voices.bin"),
                        tokens=os.path.join(d, "tokens.txt"),
                        lexicon=",".join(os.path.join(d, f) for f in ("lexicon-us-en.txt", "lexicon-zh.txt")),
                        data_dir=os.path.join(d, "espeak-ng-data"),
                        dict_dir=os.path.join(d, "dict"),
                    ),
                    num_threads=4,
                ),
                # 让“2026年”“30%”这类数字、日期念对
                rule_fsts=",".join(os.path.join(d, f) for f in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")
                                   if os.path.exists(os.path.join(d, f))),
                max_num_sentences=1,
            )
            self._kokoro = sherpa_onnx.OfflineTts(config)
        audio = self._kokoro.generate(text, sid=int(sid), speed=KOKORO_SPEED)
        return np.array(audio.samples, dtype=np.float32), audio.sample_rate

    def synthesize(self, text, voice=None):
        """把文字变成声音，返回 (声音数据, 采样率)。"""
        samples, rate, _engine, _hit = self._synthesize_ex(text, voice)
        return samples, rate

    def _synthesize_ex(self, text, voice=None):
        """
        同 synthesize，多返回两项给日志用：
            实际用的引擎（edge:名字 / kokoro:编号 / kokoro_fallback:编号；缓存命中时是当初合成用的引擎，
                          旧缓存没记引擎的写 unknown）
            是否命中缓存
        """
        voice = voice or self.voice
        text = clean(text)
        if (voice, text) in self._cache:
            return (*self._cache[(voice, text)], self._cache_engine.get((voice, text), "unknown"), True)
        engine, _, name = voice.partition(":")
        if engine == "kokoro":
            with self._lock:
                return (*self._kokoro_tts(name, text), voice, False)
        # 在线声音失败时重试一次，再换离线声音。联网期间不占用离线合成的锁。
        for _ in range(2):
            try:
                return (*self._edge(name, text), voice, False)
            except Exception:
                friendly.LOG.exception("在线语音合成失败")
        fallback = self._notice_voice(voice).partition(":")[2]
        with self._lock:
            return (*self._kokoro_tts(fallback, text), f"kokoro_fallback:{fallback}", False)

    def _notice_voice(self, voice=None):
        voice = voice or self.voice
        if voice.startswith("kokoro:"):
            return voice
        return "kokoro:47" if VOICES.get(voice, ("", "男"))[1].startswith("女") else "kokoro:52"

    def synthesize_notice(self, text):
        return self.synthesize(text, voice=self._notice_voice())

    def prepare(self, phrases=None, *, offline=False):
        """提前合成几句常用的话（比如唤醒时的“我在”），说的时候 0 等待。合成失败就跳过。"""
        if offline:
            voice = self._notice_voice()
            if phrases is None:
                phrases = friendly.MESSAGES.values()
        else:
            voice = self.voice
            if phrases is not None:
                self._phrases = list(phrases)
            self._prepared_for = voice
            phrases = self._phrases
        for phrase in phrases:
            try:
                samples, rate, engine, _hit = self._synthesize_ex(phrase, voice)
                self._cache[(voice, clean(phrase))] = (samples, rate)
                self._cache_engine[(voice, clean(phrase))] = engine  # 记下预热时用的引擎（能看出是否回退到 Kokoro）
            except Exception:
                friendly.LOG.exception("预先合成语音失败")

    # ---------- 播放 ----------

    def say(self, text, listen=False, should_stop=None, on_level=None, on_interrupt=None, *,
            notice=False, microphone=None):
        """
        念出来（念完、或者被打断才返回），返回 Spoken（可以当 True/False 用）。
            listen        边说边听：你一开口就停下来听你说（需要回声消除，见 duplex.py）
            should_stop   返回 True 时马上停（退出、暂停监听）
            on_level      报告麦克风音量；on_interrupt  被打断的那一刻调用
        出错（比如断网）时不让程序崩溃，返回的 Spoken 是 False。
        """
        words = clean(text)
        if not ENABLED or not words:
            return Spoken(False)
        notice = notice or words in friendly.MESSAGES.values() or words.startswith(friendly.MESSAGES["offline"])
        voice = self._notice_voice() if notice else self.voice
        if not notice and self._phrases and self._prepared_for != voice:
            # 刚换了声音：在后台把常用短句用新声音重新合成好
            self._prepared_for = voice
            threading.Thread(target=self.prepare, daemon=True).start()
        playback = None
        try:
            sentences = [words] if (voice, words) in self._cache else split_sentences(words)
            if (voice, sentences[0]) not in self._cache:
                status.set("合成语音", VOICES.get(voice, ("", ""))[0])
            started_at = time.monotonic() if status.LOG_ENABLED else None
            samples, rate, used, hit = self._synthesize_ex(sentences[0], voice)
            if not len(samples):
                raise RuntimeError("合成的声音为空")
            synthesized_at = time.monotonic() if started_at is not None else None
            playback = duplex.Playback()
            playback.add(samples, rate, sentences[0])
            if started_at is not None:
                # say_mono_ms：送合成的时刻，和 first_output_buffer 相减就是「送合成到首声」
                status.barge_event("tts_completed", playback.id, at=synthesized_at, chunk=1,
                                   synth_ms=round((synthesized_at - started_at) * 1000, 3),
                                   say_mono_ms=round(started_at * 1000, 3), voice=voice, engine=used,
                                   cache_hit=hit, chars=len(sentences[0]))
            if len(sentences) > 1:  # 后面的句子在后台接着合成，第一句先说
                threading.Thread(target=self._synthesize_rest, args=(playback, sentences[1:], voice),
                                 daemon=True).start()
            else:
                playback.finish()
            status.set("说话中", words)  # 状态窗里显示它说的话（像字幕）
            options = dict(listen=listen, should_stop=should_stop, on_level=on_level, on_interrupt=on_interrupt)
            if microphone is not None:
                options["microphone"] = microphone
            result = playback.run(**options)
            return Spoken(True, words, playback, result, options)
        except Exception as e:
            status.barge_event("speech_failed", getattr(playback, "id", None), error_type=type(e).__name__)
            friendly.LOG.exception("语音播放失败")
            if playback is not None:
                playback.cancel()
            if notice:
                status.set("出错", words)
                _fallback_tone(should_stop)
            else:
                play_notice("error", self, should_stop, microphone=microphone)
            return Spoken(False)

    def _synthesize_rest(self, playback, sentences, voice):
        try:
            for chunk, sentence in enumerate(sentences, start=2):
                if playback.cancelled:
                    status.barge_event("tts_skipped", playback.id, chunk=chunk, reason="playback_cancelled")
                    break
                started_at = time.monotonic() if status.LOG_ENABLED else None
                samples, rate, used, hit = self._synthesize_ex(sentence, voice)
                synthesized_at = time.monotonic() if started_at is not None else None
                playback.add(samples, rate, sentence)
                if started_at is not None:
                    status.barge_event("tts_completed", playback.id, at=synthesized_at, chunk=chunk,
                                       synth_ms=round((synthesized_at - started_at) * 1000, 3),
                                       engine=used, cache_hit=hit)
        except Exception as e:
            status.barge_event("tts_failed", playback.id, error_type=type(e).__name__)
            friendly.LOG.exception("后续语音合成失败")
            try:
                if not playback.cancelled:
                    words = friendly.MESSAGES["error"]
                    samples, rate = self.synthesize_notice(words)
                    playback.add(samples, rate, words)
            except Exception:
                friendly.LOG.exception("合成离线提示失败")
        finally:
            playback.finish()


def play_notice(kind, speaker=None, should_stop=None, *, microphone=None):
    """优先说出离线提示，两种播放方式都失败时保留界面提示。"""
    if should_stop is not None and should_stop():
        return
    words = friendly.MESSAGES[kind]
    status.set("没听清" if kind == "unclear" else "思考中" if kind == "waiting" else "出错", words)
    try:
        if ENABLED:
            options = {"microphone": microphone} if microphone is not None else {}
            (speaker or Speaker()).say(words, should_stop=should_stop, notice=True, **options)
            return
    except Exception:
        friendly.LOG.exception("准备语音提示失败")
    _fallback_tone(should_stop)


def _fallback_tone(should_stop):
    try:
        if should_stop is None or not should_stop():
            from wakeword import beep_close

            beep_close()
    except Exception:
        friendly.LOG.exception("播放提示失败")


def save_wav(path, samples, rate):
    import wave
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


if __name__ == "__main__":
    out_dir = os.path.join(PROJECT_DIR, "voices")
    os.makedirs(out_dir, exist_ok=True)
    speaker = Speaker()
    for i, (key, (name, about)) in enumerate(VOICES.items(), 1):
        line = f"你好，我是{name}。已经帮您把音量调到百分之三十了，明天下午三点您还有个会议，别忘了。"
        t = time.time()
        try:
            samples, rate = speaker.synthesize(line, key)
        except Exception as e:
            print(f"[失败] {name}：{e}")
            continue
        filename = f"{i:02d} {name}（{about.split('，')[0]}）.wav"
        save_wav(os.path.join(out_dir, filename), samples, rate)
        print(f"{filename}  合成用时 {time.time() - t:.1f} 秒，时长 {len(samples) / rate:.1f} 秒")
