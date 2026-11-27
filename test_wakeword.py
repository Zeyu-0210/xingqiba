"""用录音回放测试两道唤醒检查，不打开麦克风。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

import listen
import transcription
import wakeword

AUDIO = Path(wakeword.PROJECT_DIR) / "tests" / "audio"
SR = wakeword.SAMPLE_RATE


def load(path):
    with wave.open(str(path), "rb") as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def add_white_noise(audio, *, seed=8, reference=None):
    # 白噪声的有效值是语音录音的十分之一（20 dB 信噪比）。
    if reference is None:
        reference = audio
    noise = np.random.default_rng(seed).standard_normal(len(audio), dtype=np.float32)
    noise *= np.sqrt(np.mean(reference**2) / np.mean(noise**2)) / 10
    return np.clip(audio + noise, -1, 1)


class WakewordRulesTests(unittest.TestCase):
    def test_confirm(self):
        for text in ("星期八", "嗯，星期八。", "星期8", "嘿,星期八。",
                     "xīng qī bā", "XING1 QI1 BA1", "你好，xi\u0304ng qi\u0304 ba\u0304！"):
            with self.subTest(text=text):
                self.assertTrue(wakeword.confirm(text))
        for text in ("这星期吧。", "下个星期吧", "那就星期八点见。", "星期8点", "",
                     "xīng qī ba", "xing qi ba", "xīng qī bā diǎn", "xing1 qi1 ba1 dian3",
                     "xīng qī bā点", "axing1 qi1 ba1", "xing1 qi1 ba12"):
            with self.subTest(text=text):
                self.assertFalse(wakeword.confirm(text))

    def test_strip_wake_word(self):
        for text, expected in (
            ("星期八，把音量调大", (True, "把音量调大")),
            ("星期8", (True, "")),
            ("嘿，星期八。", (True, "")),
            ("把音量调大", (False, "把音量调大")),
            ("你的名字叫星期八", (False, "你的名字叫星期八")),
            ("记住一点，你叫星期八，不叫Gemma", (False, "记住一点，你叫星期八，不叫Gemma")),
        ):
            with self.subTest(text=text):
                self.assertEqual(wakeword.strip_wake_word(text), expected)


class WakewordTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.audio = np.zeros(SR, dtype=np.float32)
        self.whisper = Mock()
        self.whisper.transcribe.return_value = "星期八"

    def test_sensevoice_is_preferred(self):
        with patch.object(transcription, "sensevoice_installed", return_value=True), \
                patch.object(transcription.sensevoice, "transcribe", return_value="星期八。") as sense:
            self.assertEqual(listen._transcribe_wake(self.audio, self.whisper), ("星期八。", "sensevoice"))
        sense.assert_called_once_with(self.audio)
        self.whisper.transcribe.assert_not_called()

    def test_missing_sensevoice_uses_whisper(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transcription, "SENSEVOICE_DIR", Path(directory)), \
                patch.object(transcription.sensevoice, "transcribe") as sense:
            self.assertEqual(listen._transcribe_wake(self.audio, self.whisper), ("星期八", "whisper"))
        sense.assert_not_called()
        self.whisper.transcribe.assert_called_once_with(
            self.audio, prompt=wakeword.VERIFY_PROMPT, vad_filter=False
        )

    def test_sensevoice_error_uses_whisper(self):
        with patch.object(transcription, "sensevoice_installed", return_value=True), \
                patch.object(transcription.sensevoice, "transcribe", side_effect=RuntimeError), \
                patch.object(listen.status, "barge_event") as event:
            self.assertEqual(listen._transcribe_wake(self.audio, self.whisper), ("星期八", "whisper"))
        event.assert_called_once_with("transcription_fallback", engine="sensevoice", error="RuntimeError")
        self.whisper.transcribe.assert_called_once()

    def test_time_reference_is_not_retried(self):
        noisy = np.full(SR, 0.01, dtype=np.float32)
        with patch.object(transcription, "sensevoice_installed", return_value=True), \
                patch.object(transcription.sensevoice, "transcribe", return_value="那就星期八点见。") as sense:
            text, _engine = listen._transcribe_wake(noisy, self.whisper)
        self.assertFalse(wakeword.confirm(text))
        sense.assert_called_once_with(noisy)
        self.whisper.transcribe.assert_not_called()


class WakewordAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not transcription.sensevoice_installed():
            raise unittest.SkipTest("缺少 SenseVoice 模型")
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        with patch.object(wakeword, "KEYWORDS_FILE", str(Path(temp.name) / "keywords.txt")):
            cls.detector = wakeword.WakeWordDetector()
        cls.positive = sorted((AUDIO / "wake").glob("pos_*.wav")) + sorted((AUDIO / "wake_candidates").glob("p_*.wav"))
        cls.negative = sorted((AUDIO / "wake").glob("neg_*.wav")) + sorted((AUDIO / "wake_candidates").glob("c_*.wav"))

    def verify_hits(self, audio, hits):
        confirmed = 0
        for _word, end in hits:
            recent = audio[max(0, end - int(wakeword.BUFFER_SECONDS * SR)):end]
            text, engine = listen._transcribe_wake(recent, None)
            self.assertEqual(engine, "sensevoice")
            confirmed += wakeword.confirm(text)
        return confirmed

    def test_recordings_at_lower_volume_and_with_noise(self):
        self.assertEqual((len(self.positive), len(self.negative)), (13, 12))
        for paths, should_wake in ((self.positive, True), (self.negative, False)):
            for path in paths:
                original = load(path)
                for name, audio in (("原音量", original), ("50%", original * 0.5),
                                    ("25%", original * 0.25), ("白噪声", add_white_noise(original))):
                    with self.subTest(recording=path.name, condition=name):
                        hits = self.detector.detect(audio)
                        if should_wake:
                            self.assertTrue(hits, "第一关没有检出唤醒词")
                        self.assertEqual(bool(self.verify_hits(audio, hits)), should_wake, "第二关结果不符")

    def test_chatter(self):
        gap = np.zeros(SR // 2, dtype=np.float32)
        chatter = np.concatenate([part for path in self.negative for part in (load(path), gap)])
        self.assertEqual(self.verify_hits(chatter, self.detector.detect(chatter)), 0)
        middle = len(chatter) // 2
        mixed = np.concatenate([chatter[:middle], gap, load(self.positive[0]), gap, chatter[middle:]])
        self.assertEqual(self.verify_hits(mixed, self.detector.detect(mixed)), 1)

    def test_wait_with_quiet_fast_speech_in_continuous_noise(self):
        speech = load(AUDIO / "wake_candidates" / "p_hei_f.wav") * 0.25
        block = int(SR * wakeword.READ_SECONDS)
        padded = np.pad(speech, (SR, 2 * SR + (-len(speech) % block)))
        replay = add_white_noise(padded, seed=11, reference=speech).reshape(-1, block)
        with patch.object(wakeword.sd, "InputStream") as input_stream:
            mic = input_stream.return_value.__enter__.return_value
            mic.read.side_effect = ((samples[:, None], False) for samples in replay)
            word, audio = self.detector.wait(stop=lambda: mic.read.call_count >= len(replay))
        self.assertEqual(word, "星期八")
        text, engine = listen._transcribe_wake(audio, None)
        self.assertEqual(engine, "sensevoice")
        self.assertTrue(wakeword.confirm(text), text)


if __name__ == "__main__":
    unittest.main()
