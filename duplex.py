"""
duplex.py —— 边说边听：星期八说话的时候麦克风也开着，你一开口它就停下来听你说（打断 / barge-in）

    借鉴 LiveKit Agents 的做法：
      1. 回声消除：用 LiveKit 自带的 WebRTC 音频处理模块（AudioProcessingModule），
         把喇叭里星期八自己的声音从麦克风里减掉，否则它会被自己的声音打断
      2. 人声检测：Silero VAD（faster-whisper 自带的小模型，只用 CPU、1 个线程），
         判断“是不是有人在说话”。敲桌子、鼠标声这类不是人声的不算
      3. 连续说话超过 0.5 秒才算打断（LiveKit 的 min_interruption_duration），咳嗽一声不会打断
      4. 打断后先“暂停”而不是扔掉：等你说完转成文字，如果只是“嗯”“对”这种附和，
         或者什么都没听清，就从停下的地方接着说（LiveKit 的 false interruption → resume）
      5. 边合成边说：第一句合成好就开始播，后面的句子在后台继续合成（add() 随时往里加）

    用法（speak.py 里用）：
        playback = Playback()
        playback.add(声音, 采样率)      # 可以边播边加
        playback.finish()              # 全部加完了
        result = playback.run(listen=True)
        result.interrupted  → 被你打断了；result.audio 是你说的话（16kHz，交给 Whisper）
        playback.rewind(); playback.run(...)   → 从停下的地方接着说

单独运行：实测回声消除效果（会用喇叭放一段声音）
    .venv\\Scripts\\python.exe duplex.py
"""

import collections
import contextlib
import math
import queue
import struct
import sys
import threading
import time
import uuid

import numpy as np
import sounddevice as sd

import settings
import speech_endpoint
import status as app_status

SAMPLE_RATE = 16000          # 麦克风、回声消除、人声检测都用 16kHz（Whisper 也要 16kHz）
FRAME = SAMPLE_RATE // 100   # 回声消除每次处理 10 毫秒
OUT_RATE = 24000             # 喇叭用 24kHz（微软语音和离线语音都是 24kHz，音质不打折）
OUT_FRAME = OUT_RATE // 100
ECHO_REFERENCE_SETTING = "echo_reference"
PROGRAM_REFERENCE = "program"
SYSTEM_REFERENCE = "system"
DEFAULT_ECHO_REFERENCE = SYSTEM_REFERENCE
VAD_CHUNK = 512              # 人声检测每次看 32 毫秒
VAD_SECONDS = VAD_CHUNK / SAMPLE_RATE

# ---- 打断的灵敏度（实测后可以微调）----
MIN_INTERRUPT = 0.5      # 最近 1 秒里你累计说话满多久才算打断（秒）。越小越灵敏，但咳嗽、清嗓子也可能打断
                         # （实测：喇叭回声漏进来最多 0.35 秒，真人插嘴至少 0.67 秒）
WINDOW = 1.0             # 看最近多长时间（两边同时说话时人声检测会断断续续，所以按“累计”算，不按“连续”算）
SPEECH_PROB = 0.5        # 人声检测“有把握是人声”的门槛（0~1，Silero 常用 0.5）
QUIET_PROB = 0.35        # 低于这个数算“没在说话”
END_SILENCE = 0.9        # 流式识别不可用时，打断后按原来的 0.9 秒静音收尾
MAX_SECONDS = 30.0       # 打断后最多录多久
PRE_ROLL = 0.5           # 打断时往前多留一点，开头的字不会丢
TAIL = 0.4               # 说完后再多听一会儿：你如果刚好接话，开头不会被吞掉
TAIL_SPEECH = 0.2        # 这段时间里连续说话超过 0.2 秒，就算你接话了，继续录下去
REWIND = 0.4             # 接着说的时候往回倒一点，听起来不突兀
WARMUP = 0.6             # 第一次说话的前 0.6 秒不判断打断（回声消除刚启动，还没学会这台电脑的回声）
# 声卡缓冲（秒）。实测：缓冲太小（麦克风只有 20 毫秒）时，星期八同时在忙别的（合成下一句、识别、刷新界面），
# 音频就会断断续续，喇叭和麦克风的时间对不上，回声消除失效，它自己的声音会被当成你在插嘴。
# 留大一点的缓冲，回声消除自己会算出这段延迟，不影响效果。
OUT_LATENCY = 0.2
IN_LATENCY = 0.2
GLITCH_GUARD = 1.0       # 音频一旦断续（缓冲不够），接下来 1 秒不判断打断（回声消除要重新对齐）
MIN_RATIO = 0.3          # 播放时：消回声后的音量至少是消之前的 30%，才可能是你在说话（回声会被消掉大半）
# 上面这个固定门槛的毛病：喇叭越响，麦克风里回声占比越大，你本人说话时这个比例也会被压到 30% 以下 →
# 换个音量、说话轻一点就打断不了。所以改成“跟着回声走”：播放中没人说话时，持续量一下“只有回声时还剩多少”
# （回声底），门槛 = 回声底 × ECHO_MARGIN，夹在 RATIO_FLOOR 和 MIN_RATIO 之间。调音量后回声底会自己跟上。
ECHO_MARGIN = 2.0
RATIO_FLOOR = 0.12
ECHO_BASE_SECONDS = 1.5  # 回声底看最近多长时间的“没人说话”片段
ECHO_MIN_RAW = 0.004     # 麦克风里得有点声音（喇叭在响）才拿来算回声底
ECHO_MAX_AGE = 3.0       # 回声底只用最近 3 秒内量到的（时间久了喇叭音量、房间可能都变了）
# 播放中调了系统音量：旧的回声底作废、最近 1 秒的人声计票也作废（都是按旧音量判的），从头重新量。
# 重新量够之前门槛退回固定的 MIN_RATIO。

_apm = None
_apm_error = None
_apm_used = False
_apm_lock = threading.Lock()
MIC_HOOK = None   # 测试用：mic_hook(麦克风这 10 毫秒的声音) → 改过的声音（比如混进一段“有人在说话”）


def echo_reference():
    if sys.platform == "darwin":
        return PROGRAM_REFERENCE
    value = settings.get(ECHO_REFERENCE_SETTING, DEFAULT_ECHO_REFERENCE)
    return value if value in (PROGRAM_REFERENCE, SYSTEM_REFERENCE) else DEFAULT_ECHO_REFERENCE


def _new_apm():
    from livekit import rtc
    return rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                     high_pass_filter=True, auto_gain_control=False)


def _get_apm():
    """回声消除模块（整个程序只建一个：它会“学习”这台电脑喇叭到麦克风的回声，越用越准）。"""
    global _apm, _apm_error
    with _apm_lock:
        if _apm is None and _apm_error is None:
            try:
                _apm = _new_apm()
            except Exception as e:  # 没装 livekit、或者出错：不能打断，但照样能说话
                _apm_error = str(e)
    return _apm


def available():
    """能不能边说边听（回声消除模块能不能用）。"""
    return _get_apm() is not None


def unavailable_reason():
    _get_apm()
    return _apm_error or ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def resample(samples, rate, to_rate):
    """改采样率（用 PyAV，faster-whisper 已经带了它）。"""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if rate == to_rate or len(samples) == 0:
        return samples
    import av
    frame = av.AudioFrame.from_ndarray(samples[None, :], format="flt", layout="mono")
    frame.sample_rate = int(rate)
    resampler = av.AudioResampler(format="flt", layout="mono", rate=int(to_rate))
    parts = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    parts += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


def _fit(samples, length):
    """截断或补零到指定长度。"""
    if len(samples) >= length:
        return samples[:length]
    return np.concatenate([samples, np.zeros(length - len(samples), np.float32)])


def _to_int16(samples):
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)


def _rms(samples):
    return float(np.sqrt(np.mean(samples ** 2)))


def _capture_frame(apm, samples):
    frame = _make_frame(samples)
    apm.process_stream(frame)
    return np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0


class _CallbackClock:
    """把 PortAudio 时间换算到 QPC 采样坐标，短期以声卡帧数维持连续。"""

    def __init__(self):
        self.next_sample = None

    def position(self, stream_time, current_time):
        measured = round((time.perf_counter() + stream_time - current_time) * SAMPLE_RATE)
        if self.next_sample is None or abs(measured - self.next_sample) > FRAME * 4:
            position = measured
        else:
            position = self.next_sample + max(-2, min(2, round((measured - self.next_sample) * .02)))
        self.next_sample = position + FRAME
        return position


class _AudioTimeline:
    """按 QPC 采样坐标保存最近两秒，时间上的空洞填零并标记。"""

    def __init__(self):
        self.samples = np.zeros(2 * SAMPLE_RATE, np.float32)
        self.positions = np.full(len(self.samples), -1, np.int64)
        self.end = None
        self.ready = threading.Condition()

    def write(self, start, samples):
        if len(samples) > len(self.samples):
            start += len(samples) - len(self.samples)
            samples = samples[-len(self.samples):]
        positions = np.arange(start, start + len(samples))
        indices = positions % len(self.samples)
        with self.ready:
            self.samples[indices] = samples
            self.positions[indices] = positions
            end = start + len(samples)
            self.end = end if self.end is None else max(self.end, end)
            self.ready.notify_all()

    def read(self, start, count, *, complete=False, deadline=None):
        positions = np.arange(start, start + count)
        indices = positions % len(self.samples)
        with self.ready:
            while deadline is not None and (self.end is None or self.end < start + count):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                self.ready.wait(remaining)
            valid = self.positions[indices] == positions
            if complete and np.count_nonzero(valid) < count - 3:
                return None
            return np.where(valid, self.samples[indices], 0)


class _ReferenceAlignment:
    WINDOW = SAMPLE_RATE * 2 // 5
    SEARCH = SAMPLE_RATE * 3 // 10

    def __init__(self):
        self.offset = None
        self.next_at = None
        self._pending = collections.deque(maxlen=3)

    def update(self, program, loopback):
        if program.end is None or loopback.end is None:
            return
        end = min(program.end, loopback.end) - self.SEARCH
        if self.next_at is not None and end < self.next_at:
            return
        interval = .10 if self.offset is None else .80
        self.next_at = end + round(SAMPLE_RATE * interval)
        start = end - self.WINDOW
        source = program.read(start, self.WINDOW)
        captured = loopback.read(start - self.SEARCH, self.WINDOW + 2 * self.SEARCH)
        if _rms(source) < .004:
            return
        # 对每个候选位置做能量归一化，系统音量变化不会把峰推向更响的片段。
        size = 1 << (len(source) + len(captured) - 1).bit_length()
        x = source - source.mean()
        y = captured - captured.mean()
        corr = np.fft.irfft(np.fft.rfft(y, size) * np.fft.rfft(x, size).conj(), size)
        count = 2 * self.SEARCH + 1
        energy = np.r_[0, np.cumsum(y * y, dtype=np.float64)]
        scores = corr[:count] / np.sqrt(np.maximum(
            (energy[self.WINDOW:] - energy[:-self.WINDOW]) * (x @ x), 1e-12))
        candidate = int(np.argmax(scores))
        peak = scores[candidate]
        other = max(0, np.max(scores[:max(0, candidate - FRAME)], initial=0),
                    np.max(scores[candidate + FRAME:], initial=0))
        if peak < .35 or other > peak * .94:
            self._pending.clear()
            return
        candidate -= self.SEARCH
        if self.offset is None:
            # 声卡音效可能减弱相关峰。较弱的峰要在相邻两个窗口重现才采用。
            if peak >= .65 or (self._pending and abs(candidate - self._pending[-1]) < FRAME // 2):
                self.offset = float(candidate)
                self._pending.clear()
            else:
                self._pending.clear()
                self._pending.append(candidate)
        elif abs(candidate - self.offset) <= FRAME:
            self.offset += .30 * (candidate - self.offset)
            self._pending.clear()
        else:
            # 大幅变动需要连续三次吻合，再逐步收敛，避免一次误匹配跳到错误位置。
            self._pending.append(candidate)
            if len(self._pending) == 3 and max(self._pending) - min(self._pending) < FRAME:
                self.offset += .40 * (_median(self._pending) - self.offset)


class _SpeechRecovery:
    """回放增益突升后，只对消回声后仍明显可闻的近端声音补偿。"""

    def __init__(self):
        self.level = self.baseline = None
        self.remaining = 0
        self.gain = 1.0
        self.fade = np.linspace(0.0, 1.0, FRAME, dtype=np.float32)

    def process(self, clean, raw, reference, program):
        program_rms = _rms(program)
        if program_rms > .008:
            level = min(4.0, _rms(reference) / program_rms)
            if self.level is None:
                self.level = self.baseline = level
            else:
                self.level += .30 * (level - self.level)
                if self.level > max(.03, 1.26 * self.baseline):
                    self.remaining = 120
                self.baseline += .025 * (self.level - self.baseline)
        target = 1.0
        if self.remaining:
            self.remaining -= 1
            residual = _rms(clean)
            # 绝对电平和残留比例须同时超过回声底，静音和纯回声保持原增益。
            evidence = min(1.0, (residual - .0015) / .0015,
                           (residual / max(raw, 1e-6) - .08) / .12)
            target += .47 * max(0.0, evidence)
        previous = self.gain
        self.gain += (.60 if target > previous else .30) * (target - previous)
        if abs(self.gain - 1.0) < .001:
            self.gain = 1.0
        if previous == self.gain == 1.0:
            return clean
        return np.clip(clean * (previous + self.fade * (self.gain - previous)), -1.0, 1.0)


class _SystemReference:
    def __init__(self):
        self.program = _AudioTimeline()
        self.loopback = _AudioTimeline()
        self.alignment = _ReferenceAlignment()
        self.apm = _new_apm()
        self.apm.set_stream_delay_ms(0)
        self.recovery = _SpeechRecovery()
        self.last_frame = None
        self.warm_frames = 0
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.thread = None

    def start(self, playback_id, run):
        def capture():
            try:
                _capture_loopback(self, playback_id, run)
            except Exception as e:
                self.failed.set()
                app_status.barge_event("loopback_unavailable", playback_id, run=run, reason=str(e))

        self.thread = threading.Thread(target=capture, name="WASAPI 回环", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=.25)

    def process(self, start, samples, raw, fallback, captured_at=None):
        offset = self.alignment.offset
        if offset is None or self.failed.is_set():
            return fallback
        program = self.program.read(start, FRAME)
        deadline = None
        playing = _rms(program) > .002
        if captured_at is not None and playing:
            deadline = captured_at + .035
        reference = self.loopback.read(start + round(offset), FRAME, complete=True, deadline=deadline)
        if reference is None:
            if playing:
                return fallback
            # Windows 在没有音频会话时可能不送静音包，继续推进 AEC 的回声尾。
            reference = np.zeros(FRAME, np.float32)
        if self.last_frame is None or abs(start - self.last_frame - FRAME) > FRAME * 2:
            # 回环刚接通或掉包后，先让滤波器用对齐后的音频收敛。
            self.warm_frames = 12
        self.last_frame = start
        self.apm.process_reverse_stream(_make_frame(reference))
        clean = _capture_frame(self.apm, samples)
        clean = self.recovery.process(clean, raw, reference, program)
        if self.warm_frames:
            self.warm_frames -= 1
            return fallback
        return clean


def _capture_loopback(reference, playback_id, run):
    import comtypes

    comtypes.CoInitialize()
    try:
        _read_loopback(reference, playback_id, run)
    finally:
        comtypes.CoUninitialize()


def _read_loopback(reference, playback_id, run):
    import ctypes as c

    import av
    from comtypes import CLSCTX_ALL, GUID
    from pycaw.utils import AudioUtilities

    ptr, uint, uint64 = c.c_void_p, c.c_uint32, c.c_uint64

    def method(obj, slot, *types, result=c.c_long):
        table = c.cast(obj, c.POINTER(c.POINTER(ptr)))[0]
        call = c.WINFUNCTYPE(result, ptr, *types)(table[slot])

        def invoke(*args):
            hr = call(obj, *args)
            if hr < 0:
                raise OSError(f"WASAPI 调用失败（接口方法 {slot}，0x{hr & 0xffffffff:08X}）")
            return hr

        return invoke

    # IAudioClient2 支持在系统音量和静音处理之后取样。
    endpoint = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(0, 1)
    client = endpoint.Activate(GUID("{726778CD-F60A-4EDA-82DE-E47610CD78AA}"), CLSCTX_ALL, None)
    properties = (uint * 4)(16, 0, 0, 8)  # 大小、硬件卸载、音频类别、音量处理后的回环选项
    method(client, 16, ptr)(c.byref(properties))  # IAudioClient2::SetClientProperties
    mix = ptr()
    method(client, 8, ptr)(c.byref(mix))  # IAudioClient::GetMixFormat
    try:
        tag, channels, rate, _, align, bits, extra = struct.unpack("<HHIIHHH", c.string_at(mix, 18))
        if tag == 0xfffe and extra >= 22:
            tag = int.from_bytes(c.string_at(mix.value + 24, 4), "little")
        dtype, scale = {(3, 32): ("<f4", 1), (1, 16): ("<i2", 32768),
                        (1, 32): ("<i4", 2147483648)}.get((tag, bits), (None, 1))
        if dtype is None or not channels or not rate or align != channels * (bits // 8):
            raise ValueError("回环设备的混音格式不受支持")
        # 共享模式、回环标志、200 毫秒缓冲。采集线程每 5 毫秒取走已就绪的包。
        method(client, 3, uint, uint, c.c_int64, c.c_int64, ptr, ptr)(0, 0x20000, 2000000, 0, mix, None)
    finally:
        c.windll.ole32.CoTaskMemFree(mix)

    capture = ptr()
    iid = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    method(client, 14, ptr, ptr)(c.byref(iid), c.byref(capture))  # IAudioClient::GetService
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(method(capture, 2, result=uint))  # IUnknown::Release
        next_packet = method(capture, 5, ptr)
        get_buffer = method(capture, 3, ptr, ptr, ptr, ptr, ptr)
        release_buffer = method(capture, 4, uint)
        method(client, 10)()  # IAudioClient::Start
        cleanup.callback(method(client, 11))  # IAudioClient::Stop
        converter = None
        expected = next_sample = None
        inputs = outputs = 0
        size, data, count, flags, position, qpc = uint(), ptr(), uint(), uint(), uint64(), uint64()
        while not reference.stop.is_set():
            next_packet(c.byref(size))
            while size.value and not reference.stop.is_set():
                get_buffer(c.byref(data), c.byref(count), c.byref(flags), c.byref(position), c.byref(qpc))
                if not count.value:
                    break
                try:
                    packet = (None if flags.value & 2 else c.string_at(data, count.value * align))
                finally:
                    release_buffer(count.value)
                stamp = (qpc.value / 1e7 if qpc.value and not flags.value & 4 else expected)
                if stamp is not None:
                    if converter is None or flags.value & 1 or abs(stamp - expected) > .035:
                        converter = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
                        inputs = outputs = 0
                        next_sample = round(stamp * SAMPLE_RATE)
                    expected = stamp + count.value / rate
                    mono = (np.zeros(count.value, np.float32) if packet is None else
                            np.frombuffer(packet, dtype).reshape(-1, channels).mean(
                                axis=1, dtype=np.float32) / scale)
                    frame = av.AudioFrame.from_ndarray(mono[None, :], format="flt", layout="mono")
                    frame.sample_rate = rate
                    measured = round(stamp * SAMPLE_RATE) - (round(inputs * SAMPLE_RATE / rate) - outputs)
                    correction = max(-2, min(2, round((measured - next_sample) * .02)))
                    next_sample += correction
                    inputs += count.value
                    for converted in converter.resample(frame):
                        samples = converted.to_ndarray().reshape(-1)
                        reference.loopback.write(next_sample, samples)
                        next_sample += len(samples)
                        outputs += len(samples)
                next_packet(c.byref(size))
            before = reference.alignment.offset
            reference.alignment.update(reference.program, reference.loopback)
            if before is None and reference.alignment.offset is not None:
                app_status.barge_event("loopback_aligned", playback_id, run=run,
                                       delay_ms=round(reference.alignment.offset * 1000 / SAMPLE_RATE, 1))
            reference.stop.wait(.005)


def _median(values):
    vs = sorted(values)
    return vs[len(vs) // 2] if vs else None


def ratio_gate(echo_ratios):
    """播放中判断“是你在说话”的门槛：回声底 × ECHO_MARGIN，夹在 [RATIO_FLOOR, MIN_RATIO]。还没量到回声底就用 MIN_RATIO。"""
    base = _median(echo_ratios) if len(echo_ratios) >= 5 else None
    if base is None:
        return MIN_RATIO
    return min(MIN_RATIO, max(RATIO_FLOOR, base * ECHO_MARGIN))


class _VolumeProbe:
    """诊断用：播放中每 0.5 秒看一次 Windows 系统音量，变了就报告 (之前, 之后)。读不到就安静地什么都不做。"""

    INTERVAL = 0.5

    def __init__(self):
        self.ep = self._endpoint()
        self.last = self._level(self.ep)
        self.next_at = time.monotonic() + self.INTERVAL

    @staticmethod
    def _endpoint():
        if sys.platform == "darwin":
            return None
        try:
            import comtypes
            try:
                comtypes.CoInitialize()
            except OSError:
                pass
            from pycaw.pycaw import AudioUtilities
            return AudioUtilities.GetSpeakers().EndpointVolume
        except Exception:
            return None

    @staticmethod
    def _level(ep):
        try:
            return int(round(ep.GetMasterVolumeLevelScalar() * 100)) if ep is not None else None
        except Exception:
            return None

    @classmethod
    def read(cls):
        return cls._level(cls._endpoint())

    def poll(self):
        now = time.monotonic()
        if self.last is None or now < self.next_at:
            return None
        self.next_at = now + self.INTERVAL
        v = self._level(self.ep)
        if v is None or v == self.last:
            return None
        before, self.last = self.last, v
        return before, v


class Vad:
    """一边听一边判断“是不是有人在说话”（Silero VAD 流式用法：每 32 毫秒给一个 0~1 的把握）。"""

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self.session = get_vad_model().session
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.context = np.zeros(64, np.float32)
        self.buffer = np.zeros(0, np.float32)

    def feed(self, samples):
        """喂进 16kHz 的声音，返回每 32 毫秒一个的“是人声的把握”。"""
        self.buffer = np.concatenate([self.buffer, np.asarray(samples, np.float32).reshape(-1)])
        probs = []
        while len(self.buffer) >= VAD_CHUNK:
            chunk, self.buffer = self.buffer[:VAD_CHUNK], self.buffer[VAD_CHUNK:]
            x = np.concatenate([self.context, chunk])[None, :]
            out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
            self.context = chunk[-64:]
            probs.append(float(np.asarray(out).reshape(-1)[0]))
        return probs


class Result:
    """一次播放的结果。"""

    def __init__(self, playback_id=None, run=None):
        self.playback_id = playback_id
        self.run = run
        self.interrupted = False   # 被你打断了（播放停在一半）
        self.audio = None          # 你说的话（16kHz）：打断时说的，或者刚说完你就接话
        self.stopped = False       # 程序要退出 / 暂停监听，播放被叫停
        self.listened = False      # 这次有没有边说边听
        self.at = 0.0              # 在第几秒被打断
        self.score = 0.0           # 打断那一刻：最近 1 秒里累计“人声”多少秒
        self.ratio = 0.0           # 打断那一刻：消回声后还剩多少（0~1，回声漏进来时偏小，真人说话时偏大）
        self.glitches = 0          # 这次播放里音频断续了几次（缓冲不够）
        self.recording_id = None   # 录下你说的话时：这段录音的 ID（日志配对用）


class _OutputStamp:
    def __init__(self):
        self.at = self.current_time = self.dac_time = None

    def report(self, event, playback_id, run, **extra):
        if self.at is None:
            return
        device_at = None
        if self.current_time is not None and self.dac_time is not None:
            delay = self.dac_time - self.current_time
            if math.isfinite(delay) and self.dac_time > 0 and delay >= 0:
                device_at = round((self.at + delay) * 1000, 3)
        app_status.barge_event(event, playback_id, at=self.at, run=run,
                               device_estimate_mono_ms=device_at, **extra)


# ---------------------------------------------------------------------------
# 播放（可以边播边加、被打断后接着播）
# ---------------------------------------------------------------------------


class Microphone:
    """同一个输入流供唤醒、录音与播放时的回声消除轮流使用。"""

    def __init__(self, device=None):
        self._ready = threading.Condition()
        self._blocks = collections.deque(maxlen=400)
        self._playback = None
        self.last_read_at = None
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=FRAME, device=device, latency=IN_LATENCY,
                                     callback=self._callback)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self.stream.__exit__(*exc)

    def _callback(self, data, frames, time_info, flags):
        with self._ready:
            if self._playback is not None:
                self._playback(data, frames, time_info, flags)
            else:
                self._blocks.append((data[:, 0].copy(), time.monotonic()))
                self._ready.notify()

    def read(self, size, stopping):
        count = size // FRAME
        with self._ready:
            while len(self._blocks) < count and not stopping():
                self._ready.wait(0.05)
            if stopping():
                return None, None
            blocks = [self._blocks.popleft() for _ in range(count)]
        self.last_read_at = blocks[-1][1]
        return np.concatenate([block for block, _ in blocks]), self.last_read_at

    @contextlib.contextmanager
    def during_playback(self, callback=None):
        with self._ready:
            self._playback = callback or (lambda *_: None)
        try:
            yield
        finally:
            with self._ready:
                self._playback = None


class Playback:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self._run = 0
        self._first_output = self._paused_output = None
        self.audio = np.zeros(0, np.float32)   # 要播的声音（24kHz）
        self.ref = np.zeros(0, np.float32)     # 同一段声音的 16kHz 版本，给回声消除当“参考”
        self.pos = 0                           # 播到哪了（24kHz 的第几个采样）
        self.complete = False                  # 后面还有没有要加的
        self.cancelled = False                 # 不要了（后台别再合成后面的句子）
        self.segments = []                     # 每句话在哪一段：(开始, 结束, 文字)，算“说到哪被打断”用
        self.glitches = 0                      # 音频断续了几次
        self._glitch_at = 0.0                  # 最近一次断续的时间
        self._lock = threading.Lock()
        self._pause = False
        self._paused_at = None
        self._apm = None
        self._system_reference = None
        self._render_clock = None
        # 在哪一回合里创建的：(录音 ID, 是不是回答)。首包打点带上它，续播/提示音不会被错配
        self.turn = app_status.barge_turn.get()
        app_status.barge_event("playback_created", self.id)

    # ---------- 往里加声音 ----------

    def add(self, samples, rate, text=""):
        out = resample(samples, rate, OUT_RATE)
        ref = resample(samples, rate, SAMPLE_RATE)
        added_at = None
        with self._lock:
            self.segments.append((len(self.audio), len(self.audio) + len(out), text))
            self.audio = np.concatenate([self.audio, out])
            target = len(self.audio) * SAMPLE_RATE // OUT_RATE  # 两条保持对齐
            self.ref = np.concatenate([self.ref, _fit(ref, target - len(self.ref))])
            if app_status.LOG_ENABLED:
                added_at, cancelled_at_add = time.monotonic(), self.cancelled
        if added_at is not None:
            app_status.barge_event("playback_add", self.id, at=added_at, samples=len(out),
                                   cancelled_at_add=cancelled_at_add)

    def finish(self):
        self.complete = True

    def cancel(self):
        at = time.monotonic() if app_status.LOG_ENABLED else None
        self.cancelled = True
        self.complete = True
        app_status.barge_event("playback_cancel", self.id, at=at, run=self._run, reason="caller_discarded")

    @property
    def done(self):
        return self.complete and self.pos >= len(self.audio)

    @property
    def seconds(self):
        return len(self.audio) / OUT_RATE

    def spoken_text(self):
        """已经说出口的文字（被打断时：按播到的位置，估算说到了哪个字）。"""
        said = ""
        for start, end, text in self.segments:
            if self.pos >= end:
                said += text
            elif self.pos > start:
                said += text[:int(len(text) * (self.pos - start) / max(1, end - start))]
                break
            else:
                break
        return said

    def rewind(self, seconds=REWIND):
        """往回倒一点（接着说之前用）。"""
        with self._lock:
            self.pos = max(0, self.pos - int(seconds * OUT_RATE))

    # ---------- 喇叭的回调（声卡要数据时调用，必须很快）----------

    def _glitch(self):
        self.glitches += 1
        self._glitch_at = time.monotonic()

    def _out_callback(self, outdata, frames, time_info, status):
        if status:  # 声卡等不及数据了（音频断续）：回声消除会暂时对不齐
            self._glitch()
        stamp = None
        with self._lock:
            start = self.pos
            ref = np.zeros(FRAME, np.float32)
            if self._paused_at is not None:
                block = np.zeros(frames, np.float32)
                stamp = self._paused_output
            else:
                block = self.audio[start:start + frames].copy()
                if self._first_output is not None and len(block):
                    stamp = self._first_output
                r0 = start * SAMPLE_RATE // OUT_RATE
                ref = _fit(self.ref[r0:r0 + FRAME].copy(), FRAME)
                if self._pause:  # 要停下：这 10 毫秒淡出，然后停住（不会“咔”一声）
                    block *= np.linspace(1.0, 0.0, len(block), dtype=np.float32)
                    ref *= np.linspace(1.0, 0.0, FRAME, dtype=np.float32)
                    self._paused_at = start + len(block)
                self.pos = start + len(block)
                if len(block) < frames:
                    ref[len(block) * SAMPLE_RATE // OUT_RATE:] = 0
                    block = _fit(block, frames)
        outdata[:, 0] = block
        # 只保存数值。声卡时钟与单调时钟的换算等流关闭后再做。
        if stamp is not None and stamp.at is None:
            stamp.at = time.monotonic()
            if time_info is not None:
                stamp.current_time = time_info.currentTime
                stamp.dac_time = time_info.outputBufferDacTime
        system = self._system_reference
        if system is not None and time_info is not None and not system.failed.is_set():
            position = self._render_clock.position(time_info.outputBufferDacTime, time_info.currentTime)
            system.program.write(position, ref)
        apm = self._apm
        if apm is not None:  # 告诉回声消除：喇叭现在放的是什么
            try:
                apm.process_reverse_stream(_make_frame(ref))
            except Exception:
                pass

    def pause(self, reason="caller_requested"):
        at = time.monotonic() if app_status.LOG_ENABLED else None
        self._pause = True
        app_status.barge_event("pause_requested", self.id, at=at, run=self._run, reason=reason)

    # ---------- 播放 ----------

    def run(self, listen=False, should_stop=None, on_level=None, on_interrupt=None, *, microphone=None):
        """
        从当前位置播到结束（或者被打断）。
            listen        边说边听：你开口超过 MIN_INTERRUPT 秒就停下，录下你说的话
            should_stop   返回 True 时马上停（退出、暂停监听）
            on_level      报告麦克风音量（状态窗的音量条）
            on_interrupt  被打断的那一刻调用（比如让状态窗显示“在听”）
        返回 Result。
        """
        global _apm_used
        should_stop = should_stop or (lambda: False)
        self._run += 1
        result = Result(self.id, self._run)
        trace = app_status.LOG_ENABLED
        self._first_output = _OutputStamp() if trace else None
        self._paused_output = _OutputStamp() if trace else None
        apm = _get_apm() if listen else None
        listen = apm is not None
        result.listened = listen
        self._pause, self._paused_at = False, None
        self._apm = apm
        first_time = listen and not _apm_used
        mic = queue.Queue()
        system = None
        mic_clock = _CallbackClock()

        def in_callback(indata, frames, time_info, status_):
            if status_:  # 麦克风的数据来不及取（丢了一小段）
                self._glitch()
            x = indata[:, 0].copy()
            if MIC_HOOK is not None:
                x = MIC_HOOK(x)
            raw = _rms(x)
            clean = x
            try:
                apm.set_stream_delay_ms(delay_ms[0])
                clean = _capture_frame(apm, x)
            except Exception:
                pass
            if system is not None and time_info is not None and not system.failed.is_set():
                captured_at = time.perf_counter()
                position = mic_clock.position(time_info.inputBufferAdcTime, time_info.currentTime)
                mic.put((clean, raw, position, x, captured_at))
            else:
                mic.put((clean, raw))

        delay_ms = [100]
        out = sd.OutputStream(samplerate=OUT_RATE, channels=1, dtype="float32", blocksize=OUT_FRAME,
                              latency=OUT_LATENCY, callback=self._out_callback)
        inp = None
        shared = contextlib.ExitStack()
        try:
            if listen:
                inp = microphone.stream if microphone is not None else sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME,
                    latency=IN_LATENCY, callback=in_callback)
                if echo_reference() == SYSTEM_REFERENCE:
                    try:
                        system = _SystemReference()
                        system.start(self.id, result.run)
                    except Exception as e:
                        system = None
                        app_status.barge_event("loopback_unavailable", self.id, run=result.run, reason=str(e))
                    if system is not None:
                        self._render_clock = _CallbackClock()
                        self._system_reference = system
            if microphone is not None:
                if listen:
                    delay_ms[0] = int((out.latency + inp.latency) * 1000)
                    _apm_used = True
                shared.enter_context(microphone.during_playback(in_callback if listen else None))
            started_at = time.monotonic() if trace else None
            out.start()
            if inp is not None and microphone is None:
                delay_ms[0] = int((out.latency + inp.latency) * 1000)
                inp.start()
                _apm_used = True
            if trace:
                app_status.barge_event("playback_started", self.id, at=started_at, run=result.run,
                                       input_latency_ms=round(inp.latency * 1000, 3) if inp is not None else None,
                                       output_latency_ms=round(out.latency * 1000, 3))
            if not listen:
                while not self.done:
                    if should_stop():
                        self.pause(reason="should_stop")
                        result.stopped = True
                        break
                    time.sleep(0.02)
                time.sleep(out.latency + 0.05)  # 等喇叭里最后一点声音放完
                return result
            self._listen(mic, result, should_stop, on_level, on_interrupt, out.latency, first_time, system)
            result.glitches = self.glitches
            return result
        finally:
            try:
                with contextlib.ExitStack() as streams:
                    streams.callback(out.close)
                    streams.callback(out.stop)
                    if inp is not None and microphone is None:
                        streams.callback(inp.close)
                        streams.callback(inp.stop)
            finally:
                shared.close()
                self._apm = None
                self._system_reference = None
                if system is not None:
                    system.close()
            st = getattr(self, "_lstats", None)
            if trace and listen and st:
                rs = sorted(st["voiced_ratios"])
                try:
                    app_status.barge_event(
                        "listen_summary", self.id, run=result.run, interrupted=result.interrupted,
                        chunks=st["chunks"], voiced=st["voiced"], blocked_by_ratio=st["blocked_by_ratio"],
                        blocked_by_warmup=st["blocked_by_warmup"], blocked_by_glitch=st["blocked_by_glitch"],
                        glitches=self.glitches,
                        max_prob=round(st["max_prob"], 3), max_speech_ms=round(st["max_speech_ms"], 1),
                        raw_peak=round(st["raw_peak"], 4),
                        voiced_ratio_median=round(rs[len(rs) // 2], 3) if rs else None,
                        voiced_ratio_max=round(rs[-1], 3) if rs else None,
                        gate_median=round(_median(st["gates"]), 3) if st["gates"] else None,
                        system_volume=_VolumeProbe.read(),
                        volume_changes=st["volume_changes"] or None)
                except Exception:
                    pass
                self._lstats = None
            if trace:
                self._first_output.report("first_output_buffer", self.id, result.run,
                                          recording_id=self.turn[0], is_reply=self.turn[1])
                self._paused_output.report("first_paused_silence_buffer", self.id, result.run)
                app_status.barge_event("playback_ended", self.id, run=result.run,
                                       interrupted=result.interrupted, stopped=result.stopped)

    def _listen(self, mic, result, should_stop, on_level, on_interrupt, out_latency, first_time, system=None):
        """一边放一边听。发现你在说话就停下来，把你说的话录完。"""
        vad = Vad()
        scale = speech_endpoint.wait_scale()
        endpoint = speech_endpoint.SpeechEndpoint(FRAME / SAMPLE_RATE, scale, END_SILENCE)
        max_seconds = MAX_SECONDS * scale
        keep = int((WINDOW + PRE_ROLL) * 100)         # 每 10 毫秒一块
        recent = collections.deque(maxlen=keep)       # 最近一小段（打断时连同开头一起交给 Whisper）
        window = collections.deque(maxlen=int(WINDOW / VAD_SECONDS))  # 最近 1 秒每 32 毫秒是不是人声
        speech_times = collections.deque(maxlen=window.maxlen) if app_status.LOG_ENABLED else None
        last_voice_at = None
        recording = None        # 打断后录下来的话
        silence = 0.0
        recorded = 0.0
        tail_until = None       # 播完后再多听到什么时候
        levels = collections.deque(maxlen=5)  # 最近 50 毫秒：(消回声后的音量, 消之前的音量)
        # 诊断用：播放期间的人声检测统计（没打断成功时也能看出差在哪：人声没检出 / 被回声比例拦下 / 时长不够）
        self._lstats = {"chunks": 0, "voiced": 0, "blocked_by_ratio": 0, "blocked_by_warmup": 0,
                        "blocked_by_glitch": 0, "max_prob": 0.0,
                        "max_speech_ms": 0.0, "raw_peak": 0.0, "voiced_ratios": [], "gates": [],
                        "volume_changes": []}
        echo_ratios = collections.deque(maxlen=int(ECHO_BASE_SECONDS / VAD_SECONDS))  # 没人说话时：(时间, 比例)
        vol_probe = _VolumeProbe()  # 不只为日志：播放中调了音量要重新量回声底（读不到音量就什么都不做）
        started = time.monotonic()
        while True:
            if should_stop():
                self.pause(reason="should_stop")
                result.stopped = True
                return
            try:
                block = mic.get(timeout=0.05)
            except queue.Empty:
                x = None
            else:
                x, raw = block[:2]
                if len(block) > 2:
                    position, original, captured_at = block[2:]
                    try:
                        x = system.process(position, original, raw, x, captured_at)
                    except Exception as e:
                        system.failed.set()
                        system.stop.set()
                        app_status.barge_event("loopback_unavailable", self.id, run=result.run, reason=str(e))
            if vol_probe is not None:
                change = vol_probe.poll()
                if change is not None:  # 播放中调了系统音量：旧回声底和按旧门槛判的人声计票都作废，重新量
                    vals = [r for _, r in echo_ratios]
                    base = _median(vals)
                    self._lstats["volume_changes"].append(change[1])
                    app_status.barge_event("system_volume_changed", self.id, run=result.run,
                                           before=change[0], after=change[1],
                                           echo_base=round(base, 3) if base is not None else None,
                                           gate=round(ratio_gate(vals), 3))
                    echo_ratios.clear()
                    if recording is None:
                        window.clear()
                        if speech_times is not None:
                            speech_times.clear()
            if x is not None:
                level = float(np.sqrt(np.mean(x ** 2)))
                levels.append((level, raw))
                if on_level:
                    on_level(level)
                if recording is None:
                    recent.append(x)
                else:
                    recording.append(x)
                    recorded += len(x) / SAMPLE_RATE
                for p in vad.feed(x):
                    if recording is None:
                        playing = tail_until is None
                        out_rms = float(np.sqrt(np.mean([a * a for a, _ in levels])))
                        raw_rms = float(np.sqrt(np.mean([b * b for _, b in levels])))
                        ratio = out_rms / raw_rms if raw_rms > 1e-6 else 0.0
                        now = time.monotonic()
                        if p < QUIET_PROB and raw_rms >= ECHO_MIN_RAW:
                            echo_ratios.append((now, ratio))
                        gate = ratio_gate([r for t, r in echo_ratios if now - t <= ECHO_MAX_AGE])
                        # 算作“你在说话”：人声检测有把握，而且（播放时）消回声后剩下的明显多于“只有回声时”剩下的
                        window.append(p >= SPEECH_PROB and (not playing or ratio >= gate))
                        in_warmup = first_time and now - started < WARMUP
                        in_glitch = now - self._glitch_at < GLITCH_GUARD
                        if in_warmup:
                            window.clear()  # 回声消除刚启动，先不判断
                        if in_glitch:
                            window.clear()  # 音频刚断续过：回声消除要重新对齐，先不判断
                        if playing:  # 诊断统计放在保护处理之后，记的是真正参与判断的人声
                            st = self._lstats
                            st["chunks"] += 1
                            st["raw_peak"] = max(st["raw_peak"], raw_rms)
                            st["max_prob"] = max(st["max_prob"], p)
                            if p >= SPEECH_PROB:
                                st["voiced"] += 1
                                st["voiced_ratios"].append(ratio)
                                st["gates"].append(gate)
                                if ratio < gate:
                                    st["blocked_by_ratio"] += 1
                                if in_warmup:
                                    st["blocked_by_warmup"] += 1
                                if in_glitch:
                                    st["blocked_by_glitch"] += 1
                            st["max_speech_ms"] = max(st["max_speech_ms"], sum(window) * VAD_SECONDS * 1000)
                        if speech_times is not None:
                            if window:
                                speech_times.append(time.monotonic() if window[-1] else None)
                            else:
                                speech_times.clear()
                        speech = sum(window) * VAD_SECONDS  # 最近 1 秒里累计说了多久
                        if speech >= (MIN_INTERRUPT if playing else TAIL_SPEECH):
                            result.score, result.ratio = speech, ratio
                            if speech_times is not None:
                                last_voice_at = time.monotonic()
                            if playing:
                                self.pause(reason="vad")
                                result.interrupted = True
                                result.at = self.pos / OUT_RATE
                                if on_interrupt:
                                    try:
                                        on_interrupt()
                                    except Exception:
                                        pass
                            if speech_times is not None:
                                first_voice_at = next((at for at in speech_times if at is not None), None)
                                phase = "playing" if playing else "tail"
                                app_status.barge_event("vad_first_speech", self.id, at=first_voice_at,
                                                       run=result.run, phase=phase)
                                app_status.barge_event("vad_confirmed", self.id, at=last_voice_at,
                                                       run=result.run, phase=phase,
                                                       speech_ms=round(speech * 1000, 3), aec_ratio=round(ratio, 3))
                            recording = list(recent)
                            recorded = len(recording) * FRAME / SAMPLE_RATE
                            silence = 0.0
                    else:
                        silence = silence + VAD_SECONDS if p < QUIET_PROB else 0.0
                        if speech_times is not None and p >= QUIET_PROB:
                            last_voice_at = time.monotonic()
                finished = recording is not None and endpoint.finished(recording, silence)
                if recording is not None and (finished or recorded >= max_seconds):
                    ended_at = time.monotonic() if last_voice_at is not None else None
                    result.audio = np.concatenate(recording)
                    if ended_at is not None:
                        result.recording_id = app_status.new_recording_id()
                        reason = "silence" if finished else "max_duration"
                        app_status.barge_event("recording_end", self.id, at=ended_at, run=result.run,
                                               reason=reason, last_voice_mono_ms=round(last_voice_at * 1000, 3),
                                               recording_id=result.recording_id)
                    return
            if recording is None:
                if tail_until is None and self.done:
                    tail_until = time.monotonic() + out_latency + TAIL * scale
                    window.clear()  # 播完以后重新算：只看你播完后有没有接话
                    if speech_times is not None:
                        speech_times.clear()
                elif tail_until is not None and time.monotonic() >= tail_until:
                    return  # 播完了，你也没接话


def _make_frame(samples):
    from livekit import rtc
    return rtc.AudioFrame(_to_int16(samples).tobytes(), SAMPLE_RATE, 1, FRAME)


# ---------------------------------------------------------------------------
# 单独运行：实测回声消除效果
# ---------------------------------------------------------------------------


def max_run(probs):
    """按打断的规则算：任意 1 秒里最多累计说了多少秒（≥ MIN_INTERRUPT 就会打断）。"""
    window = collections.deque(maxlen=int(WINDOW / VAD_SECONDS))
    best = 0.0
    for p in probs:
        window.append(p >= SPEECH_PROB)
        best = max(best, sum(window) * VAD_SECONDS)
    return best


def measure_echo(samples, rate, use_aec=True):
    """用喇叭放一段声音，同时录麦克风；返回 (每 32 毫秒的人声把握列表, 录到的声音)。"""
    global _apm
    playback = Playback()
    playback.add(samples, rate)
    playback.finish()
    saved = _apm
    probs, frames = [], []
    vad = Vad()
    mic = queue.Queue()
    apm = _get_apm() if use_aec else None

    def in_callback(indata, frames_, time_info, status_):
        x = indata[:, 0].copy()
        if apm is not None:
            frame = _make_frame(x)
            apm.set_stream_delay_ms(delay[0])
            apm.process_stream(frame)
            x = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        mic.put(x)

    delay = [100]
    playback._apm = apm
    with sd.OutputStream(samplerate=OUT_RATE, channels=1, dtype="float32", blocksize=OUT_FRAME,
                         latency="low", callback=playback._out_callback) as out, \
            sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME,
                           latency="low", callback=in_callback) as inp:
        delay[0] = int((out.latency + inp.latency) * 1000)
        end = None
        while True:
            try:
                x = mic.get(timeout=0.1)
            except queue.Empty:
                continue
            frames.append(x)
            probs += vad.feed(x)
            if playback.done and end is None:
                end = time.monotonic() + 0.5
            if end is not None and time.monotonic() > end:
                break
    _apm = saved
    return probs, np.concatenate(frames)


if __name__ == "__main__":
    import glob
    import os
    import wave

    here = os.path.dirname(os.path.abspath(__file__))
    path = sorted(glob.glob(os.path.join(here, "voices", "*.wav")))[0]
    with wave.open(path) as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    print(f"用喇叭放：{os.path.basename(path)}（{len(data) / rate:.1f} 秒），同时录麦克风")
    print(f"回声消除：{'可以用' if available() else '不能用：' + unavailable_reason()}")
    for use in (False, True, True):
        probs, rec = measure_echo(data, rate, use_aec=use)
        loud = sum(p >= SPEECH_PROB for p in probs) * VAD_SECONDS
        rms = float(np.sqrt(np.mean(rec ** 2)))
        print(f"  {'开' if use else '关'}回声消除：麦克风音量 {rms:.4f}，被当成人声 {loud:.1f} 秒"
              f"（最长连续 {max_run(probs):.1f} 秒）")
