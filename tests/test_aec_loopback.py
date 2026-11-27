"""离线比较两种回声参考，使用项目里的录音和 LiveKit WebRTC AEC。"""

import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from livekit import rtc

import duplex
import ui_server


ROOT = Path(__file__).resolve().parents[1]
RATE = duplex.SAMPLE_RATE
FRAME = duplex.FRAME
LENGTH = 28 * RATE
CHANGE_TIMES = (8, 17)
SPEECH_TIMES = (8.05, 17.05, 23)
LOOPBACK_DELAY = round(0.060 * RATE)


def _read_audio(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"测试录音必须是单声道 16 位 PCM：{path.name}")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), "<i2") / 32768.0
        rate = wav.getframerate()
    if rate == RATE:
        return samples
    # 先低通再降采样，避免把喇叭录音的高频折回语音频带。
    taps = np.arange(-32, 33)
    cutoff = 0.9 * RATE / rate
    kernel = cutoff * np.sinc(cutoff * taps) * np.hamming(taps.size)
    filtered = np.convolve(samples, kernel / kernel.sum(), mode="same")
    return np.interp(np.arange(len(samples) * RATE // rate) * rate / RATE,
                     np.arange(len(samples)), filtered)


def _shift(samples, offset):
    if offset >= 0:
        return np.pad(samples, (offset, 0))[:len(samples)]
    return np.pad(samples[-offset:], (0, -offset))


def _estimate_delay(program, loopback):
    """在前四秒估计回环偏移，音量差不改变互相关峰值所在的位置。"""
    size = 4 * RATE
    limit = RATE // 5
    fft_size = 2 * size
    product = np.fft.rfft(loopback[:size], fft_size) * np.fft.rfft(program[:size], fft_size).conj()
    corr = np.fft.irfft(product, fft_size)
    return int(np.argmax(np.abs(np.r_[corr[-limit:], corr[:limit + 1]]))) - limit


def _process(reference, microphone):
    apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                    high_pass_filter=True, auto_gain_control=False)
    apm.set_stream_delay_ms(0)
    output = np.empty(len(microphone), np.float32)
    for i in range(0, len(microphone), FRAME):
        block = slice(i, i + FRAME)
        apm.process_reverse_stream(duplex._make_frame(reference[block]))
        frame = duplex._make_frame(microphone[block])
        apm.process_stream(frame)
        output[block] = np.frombuffer(frame.data, "<i2") / 32768.0
    return output


def _process_system(program, loopback, microphone, fallback):
    reference = duplex._SystemReference()
    output = np.empty(len(microphone), np.float32)
    for i in range(0, len(microphone), FRAME):
        block = slice(i, i + FRAME)
        reference.program.write(i, program[block])
        reference.loopback.write(i, loopback[block])
        reference.alignment.update(reference.program, reference.loopback)
        # 离线已知回放晚 60 毫秒。模拟麦克风缓存让对应回环包先就绪。
        start = i - LOOPBACK_DELAY
        if start >= 0:
            earlier = slice(start, start + FRAME)
            samples = microphone[earlier]
            output[earlier] = reference.process(start, samples, duplex._rms(samples), fallback[earlier])
    output[-LOOPBACK_DELAY:] = fallback[-LOOPBACK_DELAY:]
    return output, reference.alignment.offset


def _db(samples):
    return 10 * np.log10(max(np.mean(np.square(samples, dtype=np.float64)), 1e-12))


def _speech_metrics(output, clean, segment):
    # 投影到只含近端人声的 AEC 输出，避免把残余回声的能量算成人声保留。
    heard = output[segment].astype(np.float64)
    target = clean[segment].astype(np.float64)
    gain = (target @ heard) / (target @ target)
    speech = gain * target
    return 100 * gain, _db(speech) - _db(heard - speech)


class LoopbackAecExperiment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        voices = sorted((ROOT / "voices").glob("*.wav"))[:5]
        cls.program = np.concatenate([_read_audio(path) for path in voices])[:LENGTH]
        cls.near = np.zeros(LENGTH)
        cls.speech_segments = []
        for index, start in enumerate(SPEECH_TIMES, 1):
            voice = _read_audio(ROOT / "tests" / "audio" / f"{index:02}.wav") * 0.28
            first = round(start * RATE)
            segment = slice(first, first + len(voice))
            cls.near[segment] = voice
            cls.speech_segments.append(segment)
        cls.clean = _process(np.zeros(LENGTH), cls.near)

    def test_fixed_volume_delay_alignment(self):
        reference = self.program * 0.25
        echo = _shift(reference, 157) * 0.55 - _shift(reference, 203) * 0.22
        mic = echo + np.random.default_rng(11).normal(0, 0.0008, LENGTH)
        late = _shift(reference, LOOPBACK_DELAY)
        offset = _estimate_delay(self.program, late)
        self.assertEqual(offset, LOOPBACK_DELAY)
        stable = slice(5 * RATE, 8 * RATE)
        measured = [_db(_process(ref, mic)[stable])
                    for ref in (self.program, late, _shift(late, -offset))]
        streaming, streaming_offset = _process_system(self.program, late, mic, _process(self.program, mic))
        self.assertAlmostEqual(streaming_offset, LOOPBACK_DELAY, delta=3)
        measured.append(_db(streaming[stable]))
        print("\n固定音量，程序／回环晚 60 毫秒／回环对齐／实时路径，残留 dBFS",
              np.round(measured, 1).tolist())
        self.assertGreater(measured[1] - measured[2], 10)
        self.assertLess(abs(measured[0] - measured[2]), 3)
        self.assertLess(abs(measured[2] - measured[3]), 1)

    def test_volume_changes_and_overlapping_speech(self):
        cases = (
            ((0.25, 0.80, 0.25), ((157, .55), (203, -.22), (399, .15), (636, .07), (1060, -.045))),
            ((0.50, 0.80, 0.50), ((92, .52), (196, .15), (521, -.10), (1001, .07))),
            ((0.25, 0.65, 0.18), ((243, .62), (307, -.18), (712, .19), (1225, .05))),
        )
        results = []
        for index, (gains, room) in enumerate(cases):
            with self.subTest(gains=gains):
                boundaries = (0, *(time * RATE for time in CHANGE_TIMES), LENGTH)
                volume = np.repeat(gains, np.diff(boundaries))
                render = self.program * volume
                mic = sum(_shift(render, delay) * gain for delay, gain in room)
                mic += np.random.default_rng(index).normal(0, 0.0008, LENGTH)
                late = _shift(render, LOOPBACK_DELAY)
                offset = _estimate_delay(self.program, late)
                self.assertEqual(offset, LOOPBACK_DELAY)
                transitions = [slice(t * RATE, t * RATE + int(0.8 * RATE)) for t in CHANGE_TIMES]
                comparisons = []
                base_echo = _process(self.program, mic)
                base_speech = _process(self.program, mic + self.near)
                new_echo, estimate = _process_system(self.program, late, mic, base_echo)
                new_speech, _ = _process_system(self.program, late, mic + self.near, base_speech)
                self.assertAlmostEqual(estimate, LOOPBACK_DELAY, delta=3)
                old_loopback = _shift(late, -offset)
                for echo_out, speech_out in (
                    (base_echo, base_speech),
                    (_process(old_loopback, mic), _process(old_loopback, mic + self.near)),
                    (new_echo, new_speech),
                ):
                    residual = [_db(echo_out[window]) for window in transitions]
                    speech = [_speech_metrics(speech_out, self.clean, segment)
                              for segment in self.speech_segments]
                    comparisons.append((*residual, *np.asarray(speech).ravel()))
                self.assertTrue(np.isfinite(comparisons).all())
                for window, residual in zip(transitions, comparisons[2][:2]):
                    self.assertLess(residual, _db(mic[window]) - 10)
                for old, new in zip(comparisons[1][:2], comparisons[2][:2]):
                    self.assertLessEqual(new, old + 1)
                results.append(comparisons)
                print(f"\n音量增益 {gains}，程序／回环对齐／实时路径", np.round(comparisons, 1).tolist())
        median = np.median(results, axis=0)
        print("\n三组中位数（调大、调小残留 dBFS，三次插话的保留率 % 和 SI-SDR dB）",
              np.round(median, 1).tolist())
        self.assertGreaterEqual(median[2, 2], max(84, median[0, 2]))
        self.assertLess(median[2, 0], median[0, 0] - 10)
        self.assertGreaterEqual(median[2, 4], median[0, 4])

    def test_delay_rechecks_and_smooths(self):
        program, loopback = duplex._AudioTimeline(), duplex._AudioTimeline()
        alignment = duplex._ReferenceAlignment()
        samples = np.random.default_rng(8).normal(0, .1, 9 * RATE).astype(np.float32)
        changes = []
        for i in range(0, len(samples), FRAME):
            offset = LOOPBACK_DELAY if i < 2 * RATE else LOOPBACK_DELAY + 2 * FRAME
            program.write(i, samples[i:i + FRAME])
            if i >= offset:
                loopback.write(i, samples[i - offset:i - offset + FRAME])
            alignment.update(program, loopback)
            if alignment.offset is not None:
                changes.append(alignment.offset)
        self.assertEqual(changes[0], LOOPBACK_DELAY)
        self.assertLess(max(np.diff(changes)), FRAME)
        self.assertAlmostEqual(changes[-1], LOOPBACK_DELAY + 2 * FRAME, delta=40)

    def test_open_failure_falls_back(self):
        reference = duplex._SystemReference()
        reference.alignment.offset = 0
        stored = np.full(FRAME, .02, np.float32)
        reference.loopback.write(0, stored)
        reference.program.write(0, stored)
        with patch.object(duplex, "_capture_loopback", side_effect=OSError("设备被占用")):
            reference.start("测试", 1)
            self.assertTrue(reference.failed.wait(1))
            reference.close()
        fallback = np.ones(FRAME, np.float32)
        self.assertIs(reference.process(0, np.zeros(FRAME), 0, fallback), fallback)

    def test_reference_setting(self):
        name = duplex.ECHO_REFERENCE_SETTING
        with patch.object(duplex.settings, "load", return_value={}):
            self.assertEqual(duplex.echo_reference(), duplex.SYSTEM_REFERENCE)
        with patch.object(duplex.settings, "load", return_value={name: duplex.PROGRAM_REFERENCE}):
            self.assertEqual(duplex.echo_reference(), duplex.PROGRAM_REFERENCE)
        with patch.object(duplex.settings, "set") as save:
            self.assertFalse(ui_server.action(name, {"value": True})["ok"])
            save.assert_not_called()
            for value in (duplex.PROGRAM_REFERENCE, duplex.SYSTEM_REFERENCE):
                self.assertTrue(ui_server.action(name, {"value": value})["ok"])
                save.assert_called_with(name, value)
