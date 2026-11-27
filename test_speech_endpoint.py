"""用已有的 16kHz 录音和流式 Paraformer 验证断句，无需打开麦克风。"""

from contextlib import contextmanager, redirect_stdout
import io
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

import duplex
import listen
import settings
import speech_endpoint
import transcription
import ui_server


RATE = listen.SAMPLE_RATE
BLOCK = round(RATE * listen.BLOCK_SECONDS)
AUDIO = Path(__file__).with_name("tests") / "audio"
# “把音量调到｜三十”的词间位置。
COMMAND_SPLIT = round(1.32 * RATE)


def quiet(seconds):
    return np.zeros(round(seconds * RATE), dtype=np.float32)


def read_audio(name):
    with wave.open(str(AUDIO / name), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, RATE)
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768


def paused_command(command):
    return np.concatenate((command[:COMMAND_SPLIT], quiet(1), command[COMMAND_SPLIT:]))


def record_audio(audio):
    # 先给足麦克风启动和校准的静音，末尾留足断句等待的静音。
    scripted = np.concatenate((quiet(0.6), audio, quiet(3.6)))
    blocks = np.pad(scripted, (0, -len(scripted) % BLOCK)).reshape(-1, BLOCK)
    calibration = int(listen.SKIP_START / listen.BLOCK_SECONDS) + int(0.3 / listen.BLOCK_SECONDS)
    stopping = iter([False] * (len(blocks) - calibration))

    @contextmanager
    def fake_input(callback, **kwargs):
        for block in blocks:
            callback(block[:, None], len(block), None, None)
        yield

    with patch.object(listen.sd, "InputStream", fake_input):
        return listen.record_utterance(verbose=False, should_stop=lambda: next(stopping, True))


class SpeechEndpointRulesTests(unittest.TestCase):
    def test_pause_rules(self):
        unfinished = ("然后，", "还有", "就是", "那个", "嗯……", "的", "和", "但是", "因为", "所以",
                      "如果", "你这样做", "你这样做其实", "把音量调到", "把音量", "帮我打开")
        completed = ("静音", "帮我打开记事本", "把灯打开", "把声音调小", "把音量调到三十", "现在音量是多少",
                     "这是什么？", "好了", "可以吧", "可以吗", "你呢")
        for texts, seconds in ((unfinished, 1.8), (completed, 0.84), (("", "我明白你的意思"), 1.2)):
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(speech_endpoint.pause_seconds(text), seconds)

    def test_paraformer_error_uses_previous_silence(self):
        audio = read_audio("04.wav")[:BLOCK * 5].reshape(-1, BLOCK)
        endpoint = speech_endpoint.SpeechEndpoint(listen.BLOCK_SECONDS, 1.0, listen.END_SILENCE)
        with patch.object(transcription.paraformer, "create_stream", side_effect=RuntimeError) as create:
            self.assertFalse(endpoint.finished(audio, 0.9))
            self.assertTrue(endpoint.finished(audio, 1.0))
        create.assert_called_once()

    def test_setting_extends_opening_and_follow_up(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            settings, "PATH", str(Path(directory) / "settings.json")
        ), patch.dict(sys.modules, {"groupchat": types.ModuleType("groupchat")}):
            self.assertEqual(speech_endpoint.wait_scale(), 1.0)
            self.assertTrue(ui_server.action("slow_speech", {"value": True})["ok"])
            self.assertTrue(settings.load()["slow_speech"])

            now = 0.0
            waits = []

            def record(start_timeout, prompt):
                nonlocal now
                waits.append(start_timeout)
                if len(waits) == 1:
                    now += 1
                    return quiet(1)
                now += start_timeout
                return None

            with redirect_stdout(io.StringIO()):
                listen.conversation_window(Mock(transcribe=Mock(return_value="静音")), lambda *_: True,
                                           record=record, clock=lambda: now, follow_up=lambda: 20,
                                           stopping=lambda: False)
            self.assertEqual(waits, [12.0, 30.0])
            self.assertTrue(ui_server.action("slow_speech", {"value": False})["ok"])
            self.assertEqual(speech_endpoint.wait_scale(), 1.0)


class RecordingEndpointTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(settings, "get", side_effect=lambda key, default=None: default))

    def test_keeps_both_halves_across_one_second_pause(self):
        command = read_audio("01.wav")
        audio = record_audio(paused_command(command))
        self.assertIsNotNone(audio)
        # 用实际样本确认第二截被保留，单看录音时长会把加长的静音也算进去。
        second_half = command[round(1.45 * RATE):round(1.75 * RATE)]
        offset = int(np.argmax(np.abs(second_half)))
        matches = np.flatnonzero(audio == second_half[offset]) - offset
        self.assertTrue(any(start >= 0 and np.array_equal(audio[start:start + len(second_half)], second_half)
                            for start in matches))

    def test_incomplete_phrase_has_a_bounded_wait(self):
        audio = record_audio(read_audio("01.wav")[:COMMAND_SPLIT])
        pause = self.trailing_silence(audio)
        self.assertGreaterEqual(pause, 1.79)
        self.assertLessEqual(pause, 1.84)

    def test_complete_short_command_ends_quickly(self):
        command = read_audio("04.wav")
        audio = record_audio(command)
        pause = self.trailing_silence(audio)
        self.assertGreaterEqual(pause, 0.80)
        self.assertLessEqual(pause, 0.90)

    def test_slow_setting_extends_silence_after_complete_command(self):
        with patch.object(settings, "get", return_value=True):
            audio = record_audio(read_audio("04.wav"))
        pause = self.trailing_silence(audio)
        self.assertGreaterEqual(pause, 1.25)
        self.assertLessEqual(pause, 1.33)

    def test_recording_has_a_hard_limit(self):
        with patch.object(listen, "MAX_SECONDS", 1.2):
            audio = record_audio(np.tile(read_audio("01.wav"), 3))
        self.assertIsNotNone(audio)
        self.assertAlmostEqual(len(audio) / RATE, 1.2, delta=listen.BLOCK_SECONDS)
        self.assertEqual(self.trailing_silence(audio), 0)

    def test_playback_interruption_uses_text_before_ending(self):
        command = read_audio("01.wav")
        stitched = np.concatenate((paused_command(command), quiet(3.6)))
        width = duplex.FRAME
        mic = queue.Queue()
        for block in np.pad(stitched, (0, -len(stitched) % width)).reshape(-1, width):
            mic.put((block, listen._rms(block)))

        class FakeVad:
            def __init__(self):
                self.pending = np.zeros(0, dtype=np.float32)

            def feed(self, block):
                self.pending = np.concatenate((self.pending, block))
                if len(self.pending) < duplex.VAD_CHUNK:
                    return []
                chunk = self.pending[:duplex.VAD_CHUNK]
                self.pending = self.pending[duplex.VAD_CHUNK:]
                return [float(listen._rms(chunk) > listen.MIN_THRESHOLD)]

        playback = duplex.Playback()
        result = duplex.Result(playback.id)
        with patch.object(duplex, "Vad", FakeVad), patch.object(duplex, "_VolumeProbe", return_value=None):
            playback._listen(mic, result, mic.empty, None, None, 0, False)
        self.assertTrue(result.interrupted)
        self.assertIsNotNone(result.audio)
        second_half = command[round(1.45 * RATE):round(1.75 * RATE)]
        # 开口前的样本都在回溯缓冲里，所以拼接后的第二截位置不变。
        np.testing.assert_array_equal(result.audio[round(2.45 * RATE):round(2.75 * RATE)], second_half)

    def trailing_silence(self, audio):
        self.assertIsNotNone(audio)
        voiced = [index for index, block in enumerate(audio.reshape(-1, BLOCK))
                  if listen._rms(block) > listen.MIN_THRESHOLD]
        return len(audio) / RATE - (voiced[-1] + 1) * listen.BLOCK_SECONDS


if __name__ == "__main__":
    unittest.main()
