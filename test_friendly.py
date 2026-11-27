"""模拟语音故障，确认播放文字和诊断日志各走自己的路径。"""

import json
import unittest
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import assistant
import bridge
import friendly
import groupchat
import listen
import local_models
import minicpm_voice
import speak


class FriendlyVoiceTests(unittest.TestCase):
    def setUp(self):
        self.speaker = speak.Speaker(voice="kokoro:52")
        self.synthesize = self.enterContext(patch.object(
            self.speaker, "_synthesize_ex", return_value=([0.1], 24000, "kokoro:52", False)))
        self.enterContext(patch.object(speak, "ENABLED", True))
        playback = self.enterContext(patch.object(speak.duplex, "Playback"))
        playback.return_value.cancelled = False
        self.enterContext(patch.object(listen.status, "set"))
        self.enterContext(patch.object(listen.speech_endpoint, "wait_scale", return_value=1.0))

    def heard(self):
        words = [call.args[0] for call in self.synthesize.call_args_list]
        for text in words:
            self.assertNotRegex(text, "[A-Za-z]")
        return words

    def run_window(self, transcripts, on_text):
        now = 0
        utterances = iter(transcripts)
        stt = Mock()

        def record(start_timeout, prompt):
            nonlocal now
            try:
                result = next(utterances)
            except StopIteration:
                now += start_timeout
                return None
            stt.transcribe.side_effect = result if isinstance(result, Exception) else None
            stt.transcribe.return_value = result
            return [0.1]

        listen.conversation_window(
            stt, on_text, record=record, clock=lambda: now, stopping=lambda: False,
            on_feedback=lambda kind: speak.play_notice(kind, self.speaker),
        )

    def test_empty_transcript_on_first_and_later_turns(self):
        self.run_window([" \n", "你好", None], Mock(return_value=True))
        self.assertEqual(self.heard(), [friendly.MESSAGES["unclear"]] * 2)

    def test_failed_local_model_plays_preparing(self):
        with (patch.object(local_models, "release"),
              patch.object(local_models.ollama, "chat", side_effect=ConnectionError("Ollama ECONNREFUSED")),
              self.assertLogs(friendly.LOG, level="ERROR") as log):
            self.run_window(["你好"], lambda *_: local_models.chat(messages=[]))
        self.assertIn("ECONNREFUSED", "\n".join(log.output))
        self.assertEqual(self.heard(), [friendly.MESSAGES["preparing"]])

    def test_cloud_failure_plays_offline_message_and_keeps_local_reply(self):
        mem = Mock()
        mem.short_term.turns = []
        mem.profile.facts.return_value = []
        with (patch.object(assistant.brains, "current", return_value="GPT"),
              patch.object(assistant.brains, "ask", side_effect=assistant.brains.BrainError("Network timeout")),
              self.assertLogs(friendly.LOG, level="ERROR") as log):
            reply, brain = assistant.ask_brain("现在几点？", mem, "现在八点。")
        self.assertEqual(brain, assistant.brains.LOCAL)
        self.speaker.fixed_voice = "edge:zh-CN-YunyangNeural"
        thread = Mock(side_effect=lambda target, args, **_: SimpleNamespace(start=lambda: target(*args)))
        with patch.object(speak, "threading", SimpleNamespace(Thread=thread)):
            self.speaker.say(reply)
        self.assertEqual(self.heard(), [friendly.MESSAGES["offline"], "现在八点。"])
        self.assertEqual(self.synthesize.call_args_list[0].args[1], "kokoro:52")
        self.assertIn("Network timeout", "\n".join(log.output))

    def test_unavailable_dispatch_is_recorded_and_spoken(self):
        result = json.dumps({"tasks": [{"to": "家电", "do": "开灯", "when": "马上", "ask": ""}],
                             "reply": "Backend supplied a message."})
        with (patch.object(local_models, "voice_mode", return_value=local_models.WAKE_MODE),
              patch.object(groupchat, "members", return_value=[]),
              patch.object(groupchat, "post_dispatch", return_value={"asked": []}) as post,
              patch.object(bridge, "_ask_gemma", return_value=result)):
            self.speaker.say(bridge.route("开灯"))
        self.assertIn("开灯", post.call_args.args[0][0])
        self.assertEqual(self.heard(), [friendly.MESSAGES["unavailable"]])

    def test_cloud_takeover_does_not_speak_backend_error(self):
        mem = Mock()
        mem.profile.facts.return_value = []
        mem.short_term.turns = []
        with (patch.object(assistant.claude_models, "choose", return_value=("sonnet", "测试")),
              patch.object(assistant.takeover, "run", side_effect=RuntimeError("Unable to connect to API")),
              self.assertLogs(friendly.LOG, level="ERROR") as log):
            result = assistant._takeover("帮我整理", mem, datetime.now())
        self.speaker.say(assistant.spoken_reply(result))
        self.assertEqual(result["actions"][0]["result"]["message"], friendly.MESSAGES["offline"])
        self.assertEqual(self.heard(), [friendly.MESSAGES["offline"]])
        self.assertIn("Unable to connect to API", "\n".join(log.output))

    def test_tool_error_gets_the_same_spoken_message(self):
        with (patch.dict(assistant.VOLUME_FUNCTIONS, {"get_volume": Mock(side_effect=ValueError("device failed"))}),
              self.assertLogs(friendly.LOG, level="ERROR") as log):
            result = assistant.run_tool("get_volume", {}, Mock())
        reply = assistant.spoken_reply({"reply": result["message"],
                                        "actions": [{"tool": "get_volume", "result": result}]})
        self.speaker.say(reply)
        self.assertEqual(self.heard(), [friendly.MESSAGES["error"]])
        self.assertIn("device failed", "\n".join(log.output))

    def test_unknown_exception_stays_in_log_and_voice_can_continue(self):
        on_text = Mock(return_value=True)
        with self.assertLogs(friendly.LOG, level="ERROR") as log:
            self.run_window([ValueError("unexpected internal error"), "你好"], on_text)
        self.assertIn("unexpected internal error", "\n".join(log.output))
        self.assertEqual(self.heard(), [friendly.MESSAGES["error"]])
        on_text.assert_called_once_with("你好", True)

    def test_mini_empty_transcript_uses_offline_prompt(self):
        turn = minicpm_voice.TurnRecord(1, [0.1])
        with patch.object(minicpm_voice.transcription.sensevoice, "transcribe", return_value=""):
            self.assertEqual(turn.prepare_dispatch(Mock(), SimpleNamespace(current=(1, [])),
                                                   self.speaker, Event()), ([0.1], 24000))
        self.assertEqual(self.heard(), [friendly.MESSAGES["unclear"]])

    def test_three_second_prompt_plays_once_and_fast_reply_cancels_it(self):
        notice = friendly.WaitNotice(lambda kind: speak.play_notice(kind, self.speaker), lambda: False)
        with patch.object(friendly.threading, "Timer") as timer:
            with notice:
                timer.assert_called_once_with(3, notice._say)
                timer.call_args.args[1]()
            self.assertEqual(self.heard(), [friendly.MESSAGES["waiting"]])
            timer.return_value.join.assert_called_once()
            with notice:
                pass
            self.assertEqual(self.heard(), [friendly.MESSAGES["waiting"]])
            self.assertEqual(timer.return_value.cancel.call_count, 2)

    def test_failed_synthesis_uses_friendly_offline_prompt(self):
        self.synthesize.side_effect = [RuntimeError("backend failed"), ([0.1], 24000, "kokoro:52", False)]
        with self.assertLogs(friendly.LOG, level="ERROR") as log:
            self.speaker.say("现在八点。")
        self.assertIn("backend failed", "\n".join(log.output))
        self.assertEqual(self.heard(), ["现在八点。", friendly.MESSAGES["error"]])


if __name__ == "__main__":
    unittest.main()
