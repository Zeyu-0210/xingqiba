"""中文听写模型。千问转写走 CPU，可在实时语音占用显卡时独立工作。"""

import sys
import threading
from pathlib import Path

import numpy as np

import friendly
import settings

SAMPLE_RATE = 16000
PARAFORMER_SENSEVOICE = "paraformer-sensevoice"
QWEN = "qwen3-asr"
WHISPER = "whisper"
MODELS = {
    PARAFORMER_SENSEVOICE: {"label": "流式 Paraformer + SenseVoice（推荐）",
                           "detail": "群聊边说边出字，录完生成带标点的定稿，本机 CPU"},
    QWEN: {"label": "千问语音识别 1.7B", "detail": "普通话、方言和中英混说，本机 CPU"},
    WHISPER: {"label": "Whisper Large V3 Turbo", "detail": "原有识别模型"},
}
MODELS_DIR = Path(__file__).resolve().parent / "models"
PARAFORMER_DIR = MODELS_DIR / "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
SENSEVOICE_DIR = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
QWEN_DIR = MODELS_DIR / "qwen3-asr-1.7b-int8"
_QWEN_FILES = ("conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx", "tokenizer/vocab.json",
               "tokenizer/merges.txt", "tokenizer/tokenizer_config.json")


def qwen_installed():
    return all((QWEN_DIR / name).is_file() for name in _QWEN_FILES)


def sensevoice_installed():
    return all((SENSEVOICE_DIR / name).is_file() for name in ("model.int8.onnx", "tokens.txt"))


def model_available(model):
    if model == PARAFORMER_SENSEVOICE:
        return (all((PARAFORMER_DIR / name).is_file() for name in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"))
                and sensevoice_installed())
    if model == QWEN:
        return qwen_installed()
    return model == WHISPER


def current():
    selected = settings.get("transcription_model", PARAFORMER_SENSEVOICE)
    if model_available(selected):
        return selected
    return QWEN if selected == PARAFORMER_SENSEVOICE and qwen_installed() else WHISPER


def model_choices():
    return [{"id": model, **info, "available": model_available(model)}
            for model, info in MODELS.items()]


class Paraformer:
    """每段录音用独立的流，互斥访问共用的解码器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._recognizer = None

    def create_stream(self):
        with self._lock:
            if self._recognizer is None:
                import sherpa_onnx

                self._recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                    encoder=str(PARAFORMER_DIR / "encoder.int8.onnx"),
                    decoder=str(PARAFORMER_DIR / "decoder.int8.onnx"),
                    tokens=str(PARAFORMER_DIR / "tokens.txt"),
                    num_threads=6, provider="cpu",
                )
            return self._recognizer.create_stream()

    def accept(self, stream, audio):
        with self._lock:
            recognizer = self._recognizer
            stream.accept_waveform(SAMPLE_RATE, audio)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            return recognizer.get_result(stream).strip()


class SenseVoice:
    """录音结束后识别整段语音，使用模型自带的标点和数字格式。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._recognizer = None

    def transcribe(self, audio):
        with self._lock:
            if self._recognizer is None:
                try:
                    import sherpa_onnx

                    self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=str(SENSEVOICE_DIR / "model.int8.onnx"),
                        tokens=str(SENSEVOICE_DIR / "tokens.txt"),
                        language="auto", use_itn=True, num_threads=6, provider="cpu",
                    )
                except Exception as exc:
                    raise friendly.ModelNotReady("本地转写模型没有准备好") from exc
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()


class Qwen:
    """用一个识别器处理分段语音，互斥访问底层解码器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._recognizer = None

    def _load(self):
        import sherpa_onnx

        if self._recognizer is None:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=str(QWEN_DIR / "conv_frontend.onnx"),
                encoder=str(QWEN_DIR / "encoder.int8.onnx"),
                decoder=str(QWEN_DIR / "decoder.int8.onnx"),
                tokenizer=str(QWEN_DIR / "tokenizer"),
                num_threads=6, provider="cpu", max_total_len=512, max_new_tokens=160,
            )
        return self._recognizer

    def transcribe(self, audio):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        speech = get_speech_timestamps(
            audio, VadOptions(min_speech_duration_ms=100, min_silence_duration_ms=300, speech_pad_ms=160),
            sampling_rate=SAMPLE_RATE,
        )
        if not speech:
            return ""
        start, end = speech[0]["start"], speech[-1]["end"]
        with self._lock:
            recognizer = self._load()
            stream = recognizer.create_stream()
            stream.set_option("language", "Chinese")
            stream.accept_waveform(SAMPLE_RATE, audio[start:end])
            recognizer.decode_stream(stream)
            text = (stream.result.text or "").partition("<asr_text>")
            return (text[2] if text[1] else text[0]).strip().replace("�", "")


paraformer = Paraformer()
sensevoice = SenseVoice()
qwen = Qwen()


class VoiceTranscriber:
    """唤醒后的命令按设置识别，唤醒词复核沿用现有流程。"""

    def __init__(self, whisper):
        self.whisper = whisper

    def transcribe(self, audio):
        engine = current()
        if sys.platform == "darwin" and engine == PARAFORMER_SENSEVOICE:
            try:
                return sensevoice.transcribe(audio)
            except Exception as exc:
                import status
                status.barge_event("transcription_fallback", engine="sensevoice", error=type(exc).__name__)
                return self.whisper.transcribe(audio)
        # 群聊的组合选项沿用原先的唤醒命令转写模型。
        if engine == QWEN or (engine == PARAFORMER_SENSEVOICE and qwen_installed()):
            try:
                return qwen.transcribe(audio)
            except Exception as exc:
                import status
                status.barge_event("transcription_fallback", engine="qwen", error=type(exc).__name__)
                try:
                    return self.whisper.transcribe(audio)
                finally:
                    self.whisper.park()
        return self.whisper.transcribe(audio)
