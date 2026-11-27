import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import dictation
import listen
import settings
import transcription
import ui_server


def read_audio(name):
    path = Path(__file__).with_name("audio") / name
    with wave.open(str(path), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, dictation.RATE)
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768


class DictationTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(settings, "PATH", str(directory / "settings.json")))

    def test_default_and_model_switches(self):
        pair = transcription.PARAFORMER_SENSEVOICE
        self.assertEqual(transcription.current(), pair)
        choices = transcription.model_choices()
        self.assertEqual([choice["id"] for choice in choices], [pair, transcription.QWEN, transcription.WHISPER])
        self.assertEqual(choices[0]["label"], "流式 Paraformer + SenseVoice（推荐）")
        self.assertTrue(all(choice["available"] for choice in choices))
        for choice in (transcription.QWEN, transcription.WHISPER, pair):
            with self.subTest(choice=choice):
                self.assertTrue(ui_server.action("transcription_model", {"value": choice})["ok"])
                self.assertEqual(dictation._Transcriber().engine, choice)

    def test_wake_commands_keep_their_transcription_engine(self):
        audio = read_audio("01.wav")
        whisper = Mock()
        commands = transcription.VoiceTranscriber(whisper)
        with patch.object(transcription.qwen, "transcribe", return_value="把音量调到30") as qwen:
            self.assertEqual(commands.transcribe(audio), qwen.return_value)
            qwen.assert_called_once_with(audio)
        whisper.transcribe.assert_not_called()

    def test_preview_before_recording_ends_and_punctuated_final(self):
        updated = threading.Event()
        events = []

        def publish(event):
            events.append(event)
            if event.get("recording") and event.get("partial"):
                updated.set()

        microphone = self.enterContext(patch.object(dictation.sd, "InputStream"))
        self.enterContext(patch.object(dictation.status, "publish", side_effect=publish))
        self.enterContext(patch.object(dictation.status, "barge_event"))
        self.assertTrue(dictation.start()["ok"])
        self.addCleanup(dictation.cancel)
        self.assertTrue(listen.DICTATING.is_set())
        self.assertTrue(listen.mic_blocked())
        callback = microphone.call_args.kwargs["callback"]
        speech = read_audio("01.wav")
        block = dictation.RATE // 10
        first = 16 * block
        for offset in range(0, first, block):
            callback(speech[offset:offset + block, None], block, None, None)

        self.assertTrue(updated.wait(10), "录音还没结束就应收到流式文字")
        self.assertTrue(dictation.recording())
        self.assertIn("音量", dictation.state()["partial"])

        for offset in range(first, len(speech), block):
            samples = speech[offset:offset + block, None]
            callback(samples, len(samples), None, None)
        result = dictation.stop()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "把音量调到30。")
        self.assertEqual(events[-1]["text"], result["text"])
        self.assertEqual(events[-1]["partial"], "")
        self.assertFalse(listen.DICTATING.is_set())

    def test_long_recording_is_split_and_joined(self):
        first = read_audio("01.wav")
        second = read_audio("02.wav")
        audio = np.concatenate((first, np.zeros(8 * dictation.RATE, dtype=np.float32), second))
        with patch.object(dictation.status, "barge_event"), patch.object(
            transcription.sensevoice, "transcribe", wraps=transcription.sensevoice.transcribe
        ) as decode:
            result = dictation._transcribe(audio, (dictation._Transcriber(), False, None))
        self.assertTrue(result["ok"], result)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(result["text"], "把音量调到30。声音大一点。")

    def test_silence_does_not_produce_final_text(self):
        audio = np.zeros(dictation.RATE, dtype=np.float32)
        with patch.object(dictation.status, "barge_event"):
            result = dictation._transcribe(audio, (dictation._Transcriber(), False, None))
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
