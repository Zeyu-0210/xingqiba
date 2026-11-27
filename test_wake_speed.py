"""检查唤醒连说、麦克风复用、本地模型保温和分段计时。"""

from datetime import datetime, timedelta
from threading import Event
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np
import ollama

import assistant
import duplex
import listen
import local_models
import speak
import wakeword


class WakeSpeedTests(unittest.TestCase):
    def test_wake_audio_keeps_command_after_keyword_detection(self):
        block = round(listen.SAMPLE_RATE * listen.BLOCK_SECONDS)
        head = np.r_[np.zeros(block * 10), np.full(block * 12, 0.04)]
        command = np.full(block, 0.07)
        quiet = np.zeros(block)
        mic = Mock(last_read_at=10.0)
        mic.read.side_effect = [(samples, 10 + i * listen.BLOCK_SECONDS) for i, samples in
                                enumerate([command] * 15 + [quiet] * 40, start=1)]
        endpoint = Mock()
        endpoint.finished.side_effect = lambda _frames, silence: silence >= 0.84
        with patch.object(listen.speech_endpoint, "SpeechEndpoint", return_value=endpoint), \
                patch.object(listen.speech_endpoint, "wait_scale", return_value=1.0), \
                patch.object(listen.status, "LOG_ENABLED", False):
            audio, _, _ = listen._finish_wake_audio(mic, head, lambda: False)
        np.testing.assert_array_equal(audio[:len(head)], head)
        self.assertEqual(np.count_nonzero(audio == 0.07), 15 * block)

    def test_confirmed_suffix_is_dispatched_without_recording_or_retranscribing(self):
        for recognized in ("星期八，把音量调大。", "嗯，星期8：把音量调大！", "嘿，xīng qī bā，把音量调大"):
            with self.subTest(recognized=recognized):
                command = wakeword.wake_command(recognized)
                on_text = Mock(return_value="end")
                record, stt = Mock(), Mock()
                rounds = listen.conversation_window(stt, on_text, record=record,
                                                    initial_command=(command, "样本录音"), stopping=lambda: False)
                self.assertEqual(rounds, 1)
                on_text.assert_called_once_with("把音量调大", True)
                record.assert_not_called()
                stt.transcribe.assert_not_called()
        self.assertEqual(wakeword.wake_command("你的名字叫星期八"), "")
        self.assertEqual(wakeword.wake_command("星期八点见"), "")
        self.assertEqual(wakeword.wake_command("星期八。"), "")

    def test_two_recordings_and_playback_share_one_input_stream(self):
        endpoint = Mock()
        endpoint.finished.side_effect = lambda _frames, silence: silence >= 0.09
        voice = np.full((duplex.FRAME, 1), 0.05, dtype=np.float32)
        quiet = np.zeros_like(voice)
        with (patch.object(duplex.sd, "InputStream") as open_input,
              patch.object(duplex.sd, "OutputStream") as open_output,
              patch.object(duplex, "_get_apm", return_value=Mock()),
              patch.object(duplex, "echo_reference", return_value=duplex.PROGRAM_REFERENCE),
              patch.object(duplex.Playback, "_listen"),
              patch.object(listen.speech_endpoint, "SpeechEndpoint", return_value=endpoint),
              patch.object(listen.status, "LOG_ENABLED", False)):
            open_input.return_value.latency = open_output.return_value.latency = 0.01
            with duplex.Microphone() as mic:
                callback = open_input.call_args.kwargs["callback"]
                for turn in range(2):
                    for samples in [voice] * 36 + [quiet] * 9:
                        callback(samples, len(samples), None, None)
                    audio = listen.record_utterance(verbose=False, should_stop=lambda: False, microphone=mic)
                    self.assertEqual(audio[0], voice[0, 0])
                    if turn == 0:
                        duplex.Playback().run(listen=True, microphone=mic)
                open_input.return_value.stop.assert_not_called()
                open_input.return_value.close.assert_not_called()
            open_input.assert_called_once()
            open_input.return_value.__exit__.assert_called_once()

    def test_wakeword_loop_dispatches_suffix_before_listening_again(self):
        stopped = Event()
        record, on_text = Mock(), Mock(return_value="end")
        run_window = listen.conversation_window

        def conversation(stt, _on_text, **options):
            self.assertEqual(options["initial_command"], ("把音量调大", "样本录音"))
            options.update(record=record, stopping=lambda: False)
            result = run_window(stt, on_text, **options)
            stopped.set()
            return result

        with (patch.object(listen, "STOP", stopped),
              patch.object(listen, "put_whisper_to_standby"),
              patch.object(listen.duplex, "Microphone") as microphone,
              patch.object(listen.wakeword, "WakeWordDetector") as detector,
              patch.object(listen, "_finish_wake_audio", return_value=(np.zeros(1), 0.001, "样本录音")),
              patch.object(listen, "_transcribe_wake", return_value=("星期八，把音量调大。", "sensevoice")),
              patch.object(listen, "conversation_window", side_effect=conversation),
              patch.object(listen.wakeword, "beep_wake") as wake_sound,
              patch.object(listen.wakeword, "beep_close"),
              patch.object(listen.status, "LOG_ENABLED", False)):
            detector.return_value.wait.return_value = ("星期八", np.zeros(1))
            stt = Mock()
            listen._wakeword_loop(stt, False, True, Event(), None, lambda: False)
        microphone.assert_called_once()
        detector.return_value.wait.assert_called_once()
        on_text.assert_called_once_with("把音量调大", True)
        record.assert_not_called()
        stt.transcribe.assert_not_called()
        wake_sound.assert_not_called()

    def test_time_changes_only_the_end_of_the_system_prompt(self):
        mem = Mock()
        mem.profile.facts.return_value = ["合成测试用偏好"]
        now = datetime(2026, 11, 25, 23, 59)
        earlier = assistant.build_system_prompt(mem, now, "gemma4:12b")
        later = assistant.build_system_prompt(mem, now + timedelta(minutes=1), "gemma4:12b")
        self.assertEqual(earlier.rsplit("\n", 1)[0], later.rsplit("\n", 1)[0])
        self.assertTrue(earlier.endswith("2026-11-25 23:59 星期三（今天是 2026-11-25，昨天是 2026-11-24）"))
        self.assertTrue(later.endswith("2026-11-26 00:00 星期四（今天是 2026-11-26，昨天是 2026-11-25）"))

    def test_window_close_keeps_gemma_loaded_until_exit(self):
        with (patch("builtins.input", side_effect=["", "", "q"]),
              patch.object(speak, "ENABLED", False),
              patch.object(listen, "put_whisper_to_standby"),
              patch.object(listen, "conversation_window", return_value=1),
              patch.object(assistant, "load_in_background", return_value=None) as warmup,
              patch.object(assistant, "review", return_value={"message": "完成"}) as review,
              patch.object(assistant, "unload") as unload):
            listen._wakeword_loop(Mock(), True, False, Event(), None, lambda: False)
        self.assertEqual(warmup.call_count, 2)
        self.assertEqual(review.call_count, 2)
        unload.assert_called_once_with()
        with patch.object(assistant.local_models, "chat") as chat:
            assistant.warm_up()
        chat.assert_called_once_with(messages=[], options=assistant.OPTIONS, keep_alive="5m")
        self.assertEqual(assistant.OPTIONS["num_ctx"], 8192 if sys.platform == "darwin" else 12288)

    def test_startup_does_not_speak(self):
        with (patch.object(speak, "ENABLED", True),
              patch.object(speak, "Speaker") as speaker,
              patch.object(speak, "play_notice") as notice,
              patch.object(listen, "put_whisper_to_standby"),
              patch.object(listen.wakeword, "WakeWordDetector"),
              patch.object(listen.wakeword, "beep_wake") as wake_sound):
            listen._wakeword_loop(Mock(), False, False, Event(), None, lambda: True)
        speaker.return_value.say.assert_not_called()
        notice.assert_not_called()
        wake_sound.assert_not_called()

    def test_stream_assembles_text_and_tools_and_logs_only_timing(self):
        chunks = [ollama.ChatResponse(message={"role": "assistant", "content": "好"}),
                  ollama.ChatResponse(message={"role": "assistant", "content": "的"}),
                  ollama.ChatResponse(message={"role": "assistant", "tool_calls": [
                      {"function": {"name": "get_volume", "arguments": {}}}
                  ]}),
                  ollama.ChatResponse(message={"role": "assistant"}, done=True,
                                      load_duration=2_000_000, prompt_eval_count=120)]
        token = listen.status.barge_turn.set(("样本录音", None))
        try:
            with (patch.object(listen.status, "LOG_ENABLED", True),
                  patch.object(listen.status, "barge_event") as log,
                  patch.object(assistant.local_models, "chat", return_value=iter(chunks)) as chat):
                message = assistant._timed_voice_chat(1, messages=[])
        finally:
            listen.status.barge_turn.reset(token)
        chat.assert_called_once_with(stream=True, messages=[])
        self.assertEqual(message.content, "好的")
        self.assertEqual(message.tool_calls[0].function.name, "get_volume")
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["gemma_start", "gemma_first_text", "gemma_end"])
        self.assertEqual(log.call_args.kwargs["load_ms"], 2)
        for call in log.call_args_list:
            self.assertFalse({"text", "content", "transcript"}.intersection(call.kwargs))

    def test_stream_keeps_scheduling_slot_until_closed_or_failed(self):
        start_waiting = local_models._waiting

        def response():
            yield "第一块"
            raise ConnectionError("模拟流中途断开")

        with (patch.object(local_models, "_last_model", "gemma4:12b"),
              patch.object(local_models, "_managed", set()),
              patch.object(local_models.ollama, "chat", side_effect=lambda **_: response())):
            stream = local_models.chat(model="gemma4:12b", stream=True)
            self.assertEqual(next(stream), "第一块")
            self.assertEqual(local_models._waiting, start_waiting + 1)
            stream.close()
            self.assertEqual(local_models._waiting, start_waiting)
            stream = local_models.chat(model="gemma4:12b", stream=True)
            next(stream)
            with self.assertRaises(listen.friendly.ModelNotReady):
                next(stream)
            self.assertEqual(local_models._waiting, start_waiting)

    def test_voice_turn_runs_tool_once_after_the_stream_finishes(self):
        mem = Mock()
        mem.profile.facts.return_value = []
        mem.short_term.messages.return_value = []
        chunks = [ollama.ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "get_volume", "arguments": {}}}
        ]}), ollama.ChatResponse(message={"role": "assistant"}, done=True)]
        events = []

        def record_event(event, *args, **kwargs):
            events.append(event)

        def run_tool(*args):
            self.assertIn("gemma_end", events)
            return {"ok": True, "message": "音量是30%", "volume": 30}

        token = listen.status.barge_turn.set(("样本录音", None))
        try:
            with (patch.object(assistant.computer, "pending", return_value=None),
                  patch.object(assistant, "model_loaded", return_value=True),
                  patch.object(assistant.brains, "current", return_value=assistant.brains.LOCAL),
                  patch.object(assistant.local_models, "chat", return_value=iter(chunks)),
                  patch.object(assistant, "run_tool", side_effect=run_tool) as execute,
                  patch.object(listen.status, "LOG_ENABLED", True),
                  patch.object(listen.status, "set"),
                  patch.object(listen.status, "barge_event", side_effect=record_event)):
                result = assistant.handle("现在音量多少", mem=mem)
        finally:
            listen.status.barge_turn.reset(token)
        self.assertEqual(result["reply"], "音量是30%")
        execute.assert_called_once_with("get_volume", {}, mem, "现在音量多少")
        self.assertEqual(events, ["gemma_start", "gemma_end", "tool_start", "tool_end"])
        mem.add_turn.assert_called_once()
