import json
import sys
import tempfile
import unittest
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Event, RLock, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import numpy as np

import friendly
import memory
import minicpm_voice as voice
import ui_server
from minicpm_engine import Engine


def load_recording(name):
    path = Path(__file__).parent / "tests" / "audio" / name
    with wave.open(str(path), "rb") as reader:
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32768


def sse(*fragments):
    chunks = [{"content": text, "stop": False, "end_of_turn": False} for text in fragments]
    chunks.append({"content": "", "stop": True, "end_of_turn": True})
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


class MiniCPMDecodeTests(unittest.TestCase):
    def test_decode_joins_reply_fragments_and_ignores_control_events(self):
        engine = Engine(Path("unused"))
        response = 'data: {\n\n' + sse("明天", "上午九点。", " Next.")
        with patch.object(engine, "request", return_value=response), self.assertLogs("minicpm_engine", "WARNING"):
            self.assertEqual(engine.decode(4), "明天上午九点。 Next.")

    def test_decode_keeps_backend_error(self):
        engine = Engine(Path("unused"))
        with patch.object(engine, "request", return_value='data: {"error": {}}\n\n'):
            with self.assertRaisesRegex(RuntimeError, "本地模型生成语音失败"):
                engine.decode(4)

    def test_completion_waits_for_final_wav_to_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Path(directory))
            folder = engine.wave_folder(4)
            folder.mkdir(parents=True)
            (folder / "generation_done.flag").write_text("4001\n", encoding="ascii")
            self.assertTrue(engine.output_done(4))
            self.assertFalse(engine.output_done(4, 1))
            self.assertTrue(engine.output_done(4, 2))


class MiniCPMHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.mem = memory.Memory(folder, resume=False)
        self.recordings = [load_recording("01.wav"), load_recording("02.wav")]
        self.engine = Engine(Path(folder))
        self.engine.proc = Mock(**{"poll.return_value": None})
        self.engine.prefill = Mock(return_value=("normal", 0, 0))
        self.engine.request = Mock(side_effect=[sse("音量已调", "到三十了"), sse("好的。")])
        self.engine.output_done = Mock(return_value=True)
        self.engine.output = Mock(side_effect=lambda _, offset: () if offset else (np.ones(4 * voice.BLOCK),))
        self.trace = Mock()
        self.publish = self.enterContext(patch.object(voice.status, "publish"))
        self.hook = self.enterContext(patch.object(voice, "on_user_turn"))
        self.routing = self.enterContext(patch.object(voice.bridge, "voice_enabled", return_value=False))
        self.enterContext(patch.object(voice.status, "set"))
        self.enterContext(patch.object(voice.status, "level"))
        self.enterContext(patch.dict(sys.modules, {
            "assistant": SimpleNamespace(MEMORY=self.mem, TURN_LOCK=RLock()),
            "listen": SimpleNamespace(DICTATING=Event()),
        }))

    def run_turns(self, second_start, transcribe, *, time_step=None, routing_wait=0):
        tick = -1
        now = 100.0
        timing = SimpleNamespace(currentTime=0.0, inputBufferAdcTime=0.0, outputBufferDacTime=0.0)
        microphone = np.zeros((voice.BLOCK, 1), dtype=np.float32)
        output = microphone.copy()
        events = {0: ("start", None), 1: ("end", self.recordings[0])}
        if second_start is not None:
            events[second_start] = ("start", None)
            events[second_start + 1] = ("end", self.recordings[1])
        last_tick = (second_start or 0) + 14 + routing_wait
        audible_blocks = 0

        def stopping():
            nonlocal tick, now, audible_blocks
            tick += 1
            if tick >= last_tick:
                return True
            now = 100 + tick * (time_step or voice.BLOCK / voice.RATE)
            callback = stream_factory.call_args.kwargs["callback"]
            callback(microphone, output, voice.BLOCK, timing, None)
            audible_blocks += bool(output.any())
            return False

        def feed(samples, at):
            kind, recording = events.get(tick, (None, None))
            return kind, (recording, at) if kind == "end" else at

        def run_model(function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

        speech = Mock(**{"clean.return_value": microphone[:, 0], "feed.side_effect": feed})
        with (patch.object(voice, "wasapi_devices", return_value=(0, 1)),
              patch.object(voice, "Speech", return_value=speech),
              patch.object(voice, "OUTPUT_RATE", voice.RATE),
              patch.object(voice, "ThreadPoolExecutor", return_value=Mock(submit=Mock(side_effect=run_model))),
              patch.object(voice, "time", SimpleNamespace(monotonic=lambda: now)),
              patch.object(voice.sd, "Stream", return_value=nullcontext(SimpleNamespace(active=True, latency=0)))
              as stream_factory,
              patch.object(voice.transcription.sensevoice, "transcribe", side_effect=transcribe),
              ThreadPoolExecutor(max_workers=1) as records):
            submit = records.submit

            def submit_record(function, *args):
                future = submit(function, *args)
                # 假时钟不自然让出时间片，等后台转写好了再推进。
                if function.__name__ == "prepare_dispatch":
                    future.result(timeout=5)
                    if routing_wait:
                        return Mock(done=lambda: tick >= 1 + routing_wait, result=future.result)
                return future

            records.submit = submit_record
            voice.serve(self.engine, self.trace, Mock(**{"submit.side_effect": run_model}), records, stopping)
        return audible_blocks

    def assert_recorded(self, user, reply, history_index=0):
        row = self.mem.history.recent(60)[history_index]
        self.assertEqual(row, {"time": row["time"], "you": user, "reply": reply, "actions": []})
        self.assertEqual(ui_server.chat_history()[history_index], row)
        self.assertEqual(self.mem.short_term.turns[history_index], [
            {"role": "user", "content": user}, {"role": "assistant", "content": reply},
        ])
        self.assertEqual(self.publish.call_args_list[history_index], call({
            "type": "turn", "source": "voice", "you": user, "reply": reply, "actions": [],
        }))

    def test_completed_turn_records_text_history_and_event(self):
        caller = get_ident()
        calls = []
        sensevoice = voice.transcription.sensevoice.transcribe

        def transcribe(samples):
            calls.append((get_ident(), samples))
            return sensevoice(samples)

        self.assertEqual(self.run_turns(None, transcribe), 4)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], caller)
        self.assertIs(calls[0][1], self.recordings[0])
        self.assert_recorded("把音量调到30。", "音量已调到三十了")
        self.hook.assert_not_called()
        self.assertEqual(self.mem.window_records, [])

    def test_interruption_marks_only_the_played_prefix_and_keeps_next_turn(self):
        self.run_turns(4, ["把音量调到30。", "声音大一点。"])
        self.assert_recorded("把音量调到30。", "音量已调到三十了\n（这段话只念到“音量已调”就被用户打断了，后面的用户没听到）")
        self.assert_recorded("声音大一点。", "好的。", history_index=1)
        self.hook.assert_not_called()

    def test_interruption_before_first_audio_reaches_speaker_marks_unheard(self):
        self.run_turns(2, ["把音量调到30。", "声音大一点。"])
        self.assert_recorded("把音量调到30。", "音量已调到三十了\n（这段话刚开口就被用户打断了，用户没听到）")

    def test_transcription_failure_leaves_audio_and_next_turn_working(self):
        self.assertEqual(self.run_turns(8, [RuntimeError("识别失败"), "继续。"]), 8)
        self.trace.write.assert_any_call("transcription_error", 1, error_type="RuntimeError")
        self.assertEqual(len(self.mem.history.recent(60)), 1)
        self.assert_recorded("继续。", "好的。")
        self.hook.assert_not_called()

    def test_dispatch_plays_report_without_decoding_the_original_request(self):
        text = "帮我开灯，然后点个外卖。"
        report = "这件事我交给GPT来安排。"
        self.routing.return_value = True
        self.hook.return_value = report
        synthesize = Mock(return_value=(np.ones(4 * voice.BLOCK), voice.RATE))
        with patch.dict(sys.modules, {"speak": SimpleNamespace(Speaker=lambda: SimpleNamespace(synthesize=synthesize))}):
            self.assertEqual(self.run_turns(None, [text]), 4)
        self.hook.assert_called_once_with(text)
        synthesize.assert_called_once_with(report)
        self.engine.prefill.assert_not_called()
        self.engine.request.assert_not_called()
        self.assert_recorded(text, report)

    def test_slow_turn_plays_one_waiting_prompt_then_the_answer(self):
        synthesize_notice = Mock(return_value=(np.ones(4 * voice.BLOCK), voice.RATE))
        speaker = SimpleNamespace(synthesize_notice=synthesize_notice)
        with patch("speak.Speaker", return_value=speaker):
            self.assertEqual(self.run_turns(None, ["现在几点？"], time_step=1, routing_wait=6), 8)
        synthesize_notice.assert_called_once_with(friendly.MESSAGES["waiting"])
        self.assert_recorded("现在几点？", "音量已调到三十了")

    def test_no_generated_audio_plays_the_error_prompt(self):
        self.engine.output.side_effect = None
        self.engine.output.return_value = ()
        synthesize_notice = Mock(return_value=(np.ones(4 * voice.BLOCK), voice.RATE))
        speaker = SimpleNamespace(synthesize_notice=synthesize_notice)
        with patch("speak.Speaker", return_value=speaker), self.assertLogs(friendly.LOG, level="WARNING"):
            self.assertEqual(self.run_turns(None, ["现在几点？"], time_step=0.5), 4)
        synthesize_notice.assert_called_once_with(friendly.MESSAGES["error"])
        self.assert_recorded("现在几点？", friendly.MESSAGES["error"])

    def test_history_write_failure_is_logged_without_a_partial_record(self):
        with patch.object(self.mem.history, "append", side_effect=OSError("写入失败")):
            self.assertEqual(self.run_turns(None, ["把音量调到30。"]), 4)
        self.trace.write.assert_any_call("turn_record_error", 1, error_type="OSError")
        self.publish.assert_not_called()
        self.assertEqual(self.mem.short_term.messages(), [])


if __name__ == "__main__":
    unittest.main()
