"""群聊录音只在内存中处理，转写定稿后交给输入框。"""

import threading
import time
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

import listen
import status
import transcription

MAX_SECONDS = 180
MIN_SECONDS = 0.3
PARTIAL_INTERVAL = 2.0
STREAMING_INTERVAL = 0.4
PARTIAL_MIN = 1.2
SEG_MIN = 10.0
SEG_MAX = 20.0
PAUSE_SECONDS = 0.35
OVERLAP = 1.0
_FRAME = 0.05
RATE = listen.SAMPLE_RATE

_lock = threading.Lock()
_active = None
_next_session = 0


class _Transcriber:
    def __init__(self):
        self.engine = transcription.current()
        self.notice = ""
        self.whisper = None

    def transcribe(self, audio):
        if self.engine == transcription.PARAFORMER_SENSEVOICE:
            # SenseVoice 在纯静音上也可能出字，先确认这一段有人声。
            if not _speech_spans(audio, RATE):
                return ""
            return transcription.sensevoice.transcribe(audio)
        if self.engine == transcription.QWEN:
            try:
                return transcription.qwen.transcribe(audio)
            except Exception as exc:
                status.barge_event("dictation_fallback", error=type(exc).__name__)
                self.engine = transcription.WHISPER
                self.notice = "千问转写未能完成，已切换到 Whisper"
        if self.whisper is None:
            self.whisper = listen.SpeechToText(load=False, device="cpu")
        return self.whisper.transcribe(audio, prompt=None, reject_hallucinations=False)

    def close(self):
        if self.whisper is not None:
            self.whisper.close()


@dataclass
class _Recording:
    id: int
    stt: _Transcriber = field(default_factory=_Transcriber)
    chunks: list = field(default_factory=list)
    stopped: threading.Event = field(default_factory=threading.Event)
    seg: dict = field(default_factory=lambda: {"seg": ((), 0, False)})
    started: float = field(default_factory=time.time)
    preview: str = ""
    stream: object = None
    timer: threading.Timer = None
    worker: threading.Thread = None

    def snapshot(self):
        return {
            "session": self.id,
            "engine": self.stt.engine,
            "recording": not self.stopped.is_set(),
            "processing": self.stopped.is_set(),
            "started": self.started,
            "partial": self.preview,
        }


def state():
    with _lock:
        return _active.snapshot() if _active else {"recording": False}


def recording():
    return state()["recording"]


def _publish(session, **extra):
    status.publish({"type": "dictation", **session.snapshot(), **extra})


def _release_stt(stt, borrowed):
    if borrowed:
        listen.put_whisper_to_standby(stt)
    else:
        stt.close()


def _speech_spans(audio, rate):
    """Silero 人声检测：返回 [(开始, 结束)] 样本下标。不加前后余量，停顿多长就是多长。"""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    opts = VadOptions(min_silence_duration_ms=int(PAUSE_SECONDS * 1000), min_speech_duration_ms=100,
                      speech_pad_ms=0)
    return [(s["start"], s["end"]) for s in get_speech_timestamps(audio, opts, sampling_rate=rate)]


def find_cut(audio, rate, spans=None):
    """在 audio 里找切分点，返回 (样本下标, 是不是硬切)；找不到返回 None。
    优先：两段人声之间最后一个够长的停顿的中间（人声检测判定）。一直没停顿、攒到 SEG_MAX 才硬切在最安静处。"""
    lo = int(2.0 * rate)                # 切点前至少留 2 秒
    hi = len(audio) - int(1.0 * rate)   # 切点后至少留 1 秒（最后正在说的不动）
    if hi <= lo:
        return None
    if spans is None:
        spans = _speech_spans(audio, rate)
    best = None
    for (_s0, e0), (s1, _e1) in zip(spans, spans[1:]):
        mid = (e0 + s1) // 2
        if s1 - e0 >= PAUSE_SECONDS * rate and lo <= mid < hi:
            best = mid
    if best is not None:
        return best, False
    if len(audio) / rate >= SEG_MAX:
        n = int(rate * _FRAME)
        start = max(lo, hi - int(5.0 * rate))   # 在最后 5 秒里挑最安静的地方
        frames = (hi - start) // n
        if frames <= 0:
            return None
        rms = np.sqrt(np.mean(audio[start:start + frames * n].reshape(frames, n) ** 2, axis=1))
        return start + int(np.argmin(rms)) * n + n // 2, True
    return None


_PUNCT = set(" \t\r\n，。！？、,.!?；;：:“”\"'…—-")


def merge_overlap(prev, new, max_chars=20):
    """硬切的两段有约 1 秒重叠：去掉 new 开头和 prev 结尾重复的字（忽略标点），返回去重后的 new。
    切口处的字两边都可能听岔（“中调了音量”/“中掉了音量”），所以按“大部分字对得上”算重叠，不要求一字不差。"""
    if not prev or not new:
        return new
    import difflib
    a = "".join(c for c in prev if c not in _PUNCT)[-max_chars:]
    idx = [i for i, c in enumerate(new) if c not in _PUNCT][:max_chars]
    b = "".join(new[i] for i in idx)
    best = None   # (对上的字数, 相似度, new 去掉前几个字)
    for k in range(2, len(a) + 1):
        for j in range(max(2, k - 2), min(len(b), k + 2) + 1):
            sm = difflib.SequenceMatcher(None, a[-k:], b[:j], autojunk=False)
            same = sum(m.size for m in sm.get_matching_blocks())
            ratio = 2 * same / (k + j)
            if same >= 2 and ratio >= 0.75 and a[-1] == b[j - 1] and (best is None or (same, ratio) > best[:2]):
                best = (same, ratio, j)
    if best is None:
        return new
    return new[idx[best[2] - 1] + 1:].lstrip("".join(_PUNCT))


def _join(parts):
    merged = ""
    for part in filter(None, parts):
        if merged and merged[-1].isascii() and merged[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            merged += " "
        merged += part
    return merged


def _append(texts, text, overlapped):
    """把新的一段接到已定稿的后面；这段开头和前面有重叠（硬切）的话先去重。"""
    if overlapped:
        text = merge_overlap(_join(texts), text)
    return texts + (text,)


def _segment(tail):
    if len(tail) < int(SEG_MIN * RATE):
        return None
    return find_cut(tail[:int(SEG_MAX * RATE)], RATE)


def _decode(audio, stt, state, stopped=None):
    texts, pos, overlap = state["seg"]
    while found := _segment(audio[pos:]):
        cut, forced = found
        text = stt.transcribe(audio[pos:pos + cut])
        texts = _append(texts, text, overlap)
        pos += cut - int(OVERLAP * RATE) if forced else cut
        overlap = forced
        state["seg"] = (texts, pos, overlap)
        if stopped is not None and stopped.is_set():
            return ""
    tail = audio[pos:]
    minimum = PARTIAL_MIN if stopped is not None else MIN_SECONDS
    if len(tail) >= int(minimum * RATE):
        texts = _append(texts, stt.transcribe(tail), overlap)
    return _join(texts)


def _preview(session):
    streaming = session.stt.engine == transcription.PARAFORMER_SENSEVOICE
    interval = STREAMING_INTERVAL if streaming else PARTIAL_INTERVAL
    try:
        wait = interval
        if streaming:
            # 识别线程先加载模型，让加载和麦克风采集重叠。
            first_update = time.monotonic() + interval
            stream = transcription.paraformer.create_stream()
            wait = max(0, first_update - time.monotonic())
        seen = 0
        while not session.stopped.wait(wait):
            wait = interval
            end = len(session.chunks)
            if end == seen:
                continue
            if streaming:
                # 麦克风回调只存音频，识别线程每次只送入新增的块。
                text = transcription.paraformer.accept(stream, np.concatenate(session.chunks[seen:end]))
            else:
                text = _decode(np.concatenate(session.chunks[:end]), session.stt, session.seg, session.stopped)
            seen = end
            if text and text != session.preview and not session.stopped.is_set():
                session.preview = text
                _publish(session)
    except Exception as exc:
        status.barge_event("dictation_preview_error", error=type(exc).__name__)


def start():
    global _active, _next_session
    with _lock:
        if _active is not None:
            if _active.stopped.is_set():
                return {"ok": False, "message": "上一段录音还在转写"}
            return {"ok": True, **_active.snapshot()}
        _next_session += 1
        session = _Recording(_next_session)
        listen.DICTATING.set()

        def capture(indata, _frames, _time_info, _flags):
            session.chunks.append(indata[:, 0].copy())

        try:
            session.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                            blocksize=RATE // 10, callback=capture)
            session.stream.start()
        except Exception as exc:
            try:
                if session.stream is not None:
                    session.stream.close()
            finally:
                listen.DICTATING.clear()
            return {"ok": False, "message": f"打不开麦克风：{str(exc)[:100]}"}
        _active = session
        session.worker = threading.Thread(target=_preview, args=(session,), daemon=True)
        session.timer = threading.Timer(MAX_SECONDS, _finish, args=(session.id,), kwargs={"auto": True})
        session.timer.daemon = True
        session.worker.start()
        session.timer.start()
    _publish(session)
    return {"ok": True, **session.snapshot()}


def _transcribe(audio, session_stt):
    stt, borrowed, seg = session_stt
    state = seg or {"seg": ((), 0, False)}
    seconds = round(len(audio) / RATE, 1)
    begin = time.monotonic()
    error = None
    try:
        if len(audio) < int(MIN_SECONDS * RATE):
            return {"ok": False, "text": "", "message": "录音太短了"}
        text = _decode(audio, stt, state)
    except Exception as exc:
        text = _join(state["seg"][0])
        error = f"最后一段没能转写：{str(exc)[:100]}"
        status.barge_event("dictation_tail_error", seconds=seconds, error=type(exc).__name__)
    finally:
        _release_stt(stt, borrowed)
    engine = getattr(stt, "engine", "")
    status.barge_event("dictation", engine=engine, seconds=seconds, chars=len(text),
                       elapsed_ms=round((time.monotonic() - begin) * 1000))
    result = {"ok": bool(text), "text": text, "seconds": seconds, "engine": engine}
    warning = error or getattr(stt, "notice", "") or ("没听清，再说一次试试" if not text else "")
    if warning:
        result["message"] = warning
    return result


def _finish(session_id=None, *, auto=False, cancel=False):
    global _active
    with _lock:
        session = _active
        if session is None or session_id not in (None, session.id):
            return {"ok": False, "message": "没有在录音"}
        if session.stopped.is_set():
            return {"ok": False, "message": "上一段录音还在转写"}
        session.stopped.set()
        session.timer.cancel()
    _publish(session)
    try:
        try:
            session.stream.stop()
        finally:
            try:
                session.stream.close()
            finally:
                listen.DICTATING.clear()
        session.worker.join()
        if cancel:
            session.stt.close()
            result = {"ok": True}
        else:
            audio = np.concatenate(session.chunks) if session.chunks else np.empty(0, dtype=np.float32)
            result = _transcribe(audio, (session.stt, False, session.seg))
    except Exception as exc:
        session.worker.join()
        session.stt.close()
        result = {"ok": False, "message": f"结束录音时出错：{str(exc)[:100]}"}
    finally:
        with _lock:
            _active = None
    _publish(session, processing=False, partial="", auto=auto, **result)
    return {**result, "session": session.id}


def stop():
    return _finish()


def cancel():
    return _finish(cancel=True)
