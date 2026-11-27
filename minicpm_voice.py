"""MiniCPM-o 通话：单一双工音频流、回声消除和打断。"""

import queue
import tempfile
import threading
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import sounddevice as sd

import bridge
import friendly
import memory
import status
import transcription
from duplex import QUIET_PROB, SPEECH_PROB, VAD_CHUNK, Vad, resample
from minicpm_engine import BINARY, INPUT_RATE, MODEL, OUTPUT_RATE, ROOT, VOICE, Engine, pcm16


RATE = 48000
BLOCK = RATE // 100
QUIET = 1.2
READY = "我在听，你直接说就好。"
ROUND_GAP = 4  # 给服务的异步合成线程留出它自行递增的编号。


class Trace:
    def __init__(self):
        self.session = uuid4().hex[:10]

    def write(self, event, turn=0, at=None, **fields):
        status.barge_event(event, at=at, engine="minicpm", session=self.session, turn_id=turn, **fields)


def on_user_turn(text):
    """通话占着显卡，调度请求直接交给群里的主脑；“我洗完了”这类话会触发等条件的挂起任务。
    返回要念给主人的话，和调度无关时返回 None（照常由 MiniCPM-o 回答）。"""
    return bridge.voice_turn(text, realtime=True)


class TurnRecord:
    def __init__(self, turn, samples):
        self.turn = turn
        self.samples = samples
        self.when = datetime.now()
        self.response = None
        self.text = None
        self.transcription_failure = None
        self.dispatch_reply = None

    def transcribe(self, trace):
        if self.text is not None:
            return self.text
        try:
            # 当前解码接口只返回助手文字，用原本送给模型的降噪录音转写主人。
            self.text = (transcription.sensevoice.transcribe(self.samples) or "").strip()
        except Exception as exc:
            self.text = ""
            self.transcription_failure = friendly.failure(exc)
            trace.write("transcription_error", self.turn, error_type=type(exc).__name__)
            friendly.LOG.exception("实时通话的转写失败")
        else:
            if not self.text:
                trace.write("transcription_empty", self.turn)
        return self.text

    def prepare_dispatch(self, trace, audio, speaker, closing):
        text = self.transcribe(trace)
        if audio.current[0] != self.turn or closing.is_set():
            return None
        try:
            routing = bridge.voice_enabled()
            if self.transcription_failure:
                if routing:
                    self.dispatch_reply = friendly.MESSAGES[self.transcription_failure]
            elif not text:
                self.dispatch_reply = friendly.MESSAGES["unclear"]
            elif routing:
                self.dispatch_reply = on_user_turn(text)
        except Exception as exc:
            trace.write("user_turn_hook_error", self.turn, error_type=type(exc).__name__)
            friendly.LOG.exception("实时通话的调度失败")
            self.dispatch_reply = friendly.MESSAGES[friendly.failure(exc)]
        if not self.dispatch_reply:
            return None
        if self.dispatch_reply in friendly.MESSAGES.values():
            return notice_audio(speaker, self.dispatch_reply)
        try:
            return speaker.synthesize(self.dispatch_reply)
        except Exception as exc:
            trace.write("dispatch_speech_error", self.turn, error_type=type(exc).__name__)
            friendly.LOG.exception("合成调度回复失败")
            self.dispatch_reply = friendly.MESSAGES["error"]
            return notice_audio(speaker, self.dispatch_reply)

    def save(self, trace, played, total):
        text = self.transcribe(trace)
        if not text:
            return

        words = self.dispatch_reply or ""
        if self.response is not None and self.dispatch_reply is None:
            try:
                words = self.response.result()
            except Exception as exc:
                trace.write("turn_response_error", self.turn, error_type=type(exc).__name__)
                friendly.LOG.exception("读取实时通话的回复失败")
        if played is not None:
            # 合成完成时按播放比例估算。还有音频未合成时，用约每秒四字估算。
            count = int(len(words) * played / total) if total else int(4 * played / RATE)
            heard = words[:min(count, max(0, len(words) - 1))]
            words = (words + memory.interruption_note(heard)).lstrip("\n")

        try:
            import assistant

            messages = memory.record_to_messages({"you": text, "reply": words})
            with assistant.TURN_LOCK:
                # MiniCPM-o 持续监听，每轮直接归档。对话窗口的复盘由唤醒词模式管理。
                assistant.MEMORY.add_turn(text, [], words, messages, self.when, track_window=False)
            status.publish({"type": "turn", "source": "voice", "you": text, "reply": words, "actions": []})
        except Exception as exc:
            trace.write("turn_record_error", self.turn, error_type=type(exc).__name__)
            friendly.LOG.exception("记录实时通话失败")


def notice_audio(speaker, words):
    try:
        samples, rate = speaker.synthesize_notice(words)
        if samples is not None and len(samples):
            return samples, rate
        friendly.LOG.warning("离线提示的声音为空")
    except Exception:
        friendly.LOG.exception("合成离线提示失败")
    from wakeword import SAMPLE_RATE, _tone

    return _tone([660, 440]), SAMPLE_RATE


class Audio:
    def __init__(self):
        self.current = (0, deque())
        self.received = queue.SimpleQueue()
        self.stamps = queue.SimpleQueue()
        self.driver_turn = 0
        self.heard_turn = 0
        self.last_voice = 0.0
        self.playback = (0, 0, 0.0)

    def advance(self):
        turn, previous = self.current
        self.current = (turn + 1, deque())
        dropped = len(previous)
        previous.clear()
        return turn + 1, dropped

    def enqueue(self, turn, samples, rate=None):
        epoch = self.current
        if turn != epoch[0]:
            return 0
        samples = resample(samples, rate or OUTPUT_RATE, RATE)
        padded = np.pad(samples, (0, -len(samples) % BLOCK))
        for frame in padded.reshape(-1, BLOCK):
            audible = np.flatnonzero(np.abs(frame) >= 0.002)
            first = int(audible[0]) if audible.size else None
            epoch[1].append((frame, first))
        return len(padded)

    def played_samples(self, turn, at):
        # 声卡回调可能提前交付音频，减掉在打断时还没到达扬声器的部分。
        played_turn, scheduled, end = self.playback
        if played_turn != turn:
            return 0
        return max(0, scheduled - max(0, round((end - at) * RATE)))

    def callback(self, microphone, output, frames, clock, flags):
        now = time.monotonic()
        dac_time = now + (clock.outputBufferDacTime - clock.currentTime)
        adc_time = now + (clock.inputBufferAdcTime - clock.currentTime)
        epoch = self.current
        turn, pending = epoch
        output.fill(0)
        if frames != BLOCK:
            self.stamps.put(("device_error", turn, now, {"frames": frames}))
            self.received.put(None)
            return
        try:
            frame, first = pending.popleft()
        except IndexError:
            first = None
        else:
            if self.current is epoch:
                output[:, 0] = frame
                played_turn, scheduled, _ = self.playback
                if played_turn != turn:
                    scheduled = 0
                self.playback = (turn, scheduled + frames, dac_time + frames / RATE)
        if self.current is not epoch:
            output.fill(0)
            first = None
        if turn != self.driver_turn:
            if self.heard_turn == self.driver_turn and self.driver_turn:
                self.stamps.put(("speaker_silent_estimate", turn, dac_time, {}))
            self.driver_turn = turn
        if first is not None:
            self.last_voice = dac_time + frames / RATE
            if self.heard_turn != turn:
                self.heard_turn = turn
                self.stamps.put(("speaker_first", turn, dac_time + first / RATE, {}))
        if flags:
            self.stamps.put(("device_status", turn, now, {"detail": str(flags)}))
        self.received.put((adc_time, dac_time, microphone[:, 0].copy(), output[:, 0].copy()))


class Speech:
    def __init__(self):
        from livekit import rtc

        self.rtc = rtc
        self.apm = rtc.AudioProcessingModule(
            echo_cancellation=True, noise_suppression=True, high_pass_filter=True,
        )
        self.vad = Vad()
        self.preroll = deque(maxlen=50)
        self.frames = []
        self.started = None
        self.last_voice = self.first_voice = 0.0
        self.consecutive = 0

    def clean(self, microphone, reference, adc, dac):
        self.apm.set_stream_delay_ms(max(0, round(1000 * (dac - adc))))
        self.apm.process_reverse_stream(self.rtc.AudioFrame(pcm16(reference), RATE, 1, BLOCK))
        frame = self.rtc.AudioFrame(pcm16(microphone), RATE, 1, BLOCK)
        self.apm.process_stream(frame)
        clean = np.frombuffer(frame.data, dtype="<i2").astype(np.float32) / 32768
        return resample(clean, RATE, INPUT_RATE)

    def feed(self, samples, at):
        self.preroll.append(samples)
        if self.started is not None:
            self.frames.append(samples)
        for probability in self.vad.feed(samples):
            vad_at = at + (len(samples) - len(self.vad.buffer) - VAD_CHUNK) / INPUT_RATE
            if probability >= SPEECH_PROB:
                if self.consecutive == 0:
                    self.first_voice = vad_at
                self.consecutive += 1
            else:
                self.consecutive = 0
            if self.started is None and self.consecutive >= 5:
                self.started = self.first_voice
                self.last_voice = at
                self.frames = list(self.preroll)
                return "start", self.first_voice
            if self.started is not None and probability >= QUIET_PROB:
                self.last_voice = at
        if self.started is not None and (at - self.last_voice >= QUIET or at - self.started >= 20):
            keep = len(self.frames) - max(0, int((at - self.last_voice - 0.25) * 100))
            utterance = np.concatenate(self.frames[:keep])
            self.frames = []
            self.preroll.clear()
            self.started = None
            self.consecutive = 0
            return "end", (utterance, self.last_voice)
        return None, None


def wasapi_devices():
    for api in sd.query_hostapis():
        if api["name"] == "Windows WASAPI":
            microphone, speaker = api["default_input_device"], api["default_output_device"]
            if microphone >= 0 and speaker >= 0:
                return microphone, speaker
    raise RuntimeError("未找到可用的 WASAPI 麦克风和扬声器")


def reply(engine, audio, trace, turn, round_index, samples):
    if audio.current[0] != turn:
        return ""

    def log_zombie(interrupted_round, wav):
        trace.write("zombie_tts", turn, interrupted_round=interrupted_round,
                    next_round=round_index, wav=wav)

    branch, wait_ms, prefill_ms = engine.prefill(samples, next_round=round_index, on_zombie=log_zombie)
    trace.write("engine_prefill", turn, branch=branch, wait_ms=wait_ms, prefill_ms=prefill_ms)
    if audio.current[0] != turn:
        return ""
    trace.write("engine_decode_request", turn)
    words = engine.decode(round_index)
    trace.write("engine_decode_return", turn)
    return words


def break_engine(engine, trace, turn):
    requested = time.monotonic()
    trace.write("engine_break_request", turn, requested)
    try:
        acked = engine.interrupt()
    except (OSError, RuntimeError) as exc:
        trace.write("engine_break_error", turn, error_type=type(exc).__name__)
        friendly.LOG.exception("停止实时通话的上一轮回复失败")
    else:
        trace.write("engine_break_ack", turn, acked, request_ms=round((acked - requested) * 1000, 1))


def serve(engine, trace, workers, records, stopping, speaker=None):
    from listen import DICTATING  # 群聊语音输入时：模型不停，只把麦克风换成静音
    from speak import Speaker

    speaker = speaker or Speaker()
    audio = Audio()
    speech = Speech()
    running = cancelling = pending = None
    output_turn, round_index, offset = 0, -ROUND_GAP, 0
    active = None
    queued_samples = 0
    next_poll = 0.0
    decoded_at = None
    speaking = False
    dispatched = False
    wait_until = None
    waiting_audio = recovery = None
    waiting_samples = 0
    closing = threading.Event()

    def finish_turn(played=None, total=None):
        nonlocal active
        if active is not None:
            turn, active = active, None
            try:
                records.submit(turn.save, trace, played, total)
            except RuntimeError as exc:
                trace.write("turn_record_error", turn.turn, error_type=type(exc).__name__)

    def finish_playback(at, complete):
        if active is not None:
            played = max(0, audio.played_samples(active.turn, at) - waiting_samples)
            total = queued_samples if complete and active.turn == output_turn else None
            if total and played >= total:
                finish_turn()
            else:
                finish_turn(played, total)

    def finish_on_close():
        closing.set()
        if active is not None:
            at = time.monotonic()
            complete = active.turn == output_turn and (dispatched or engine.output_done(round_index, offset))
            finish_playback(at, complete)

    def recover(words):
        nonlocal recovery, dispatched, decoded_at
        dispatched = True
        decoded_at = None
        active.dispatch_reply = words
        recovery = notices.submit(notice_audio, speaker, words)
        status.set("出错", words)

    with ExitStack() as stack:
        stack.callback(finish_on_close)
        notices = ThreadPoolExecutor(max_workers=1, thread_name_prefix="minicpm-notice")
        stack.callback(notices.shutdown, wait=False, cancel_futures=True)
        stream = stack.enter_context(sd.Stream(device=wasapi_devices(), samplerate=RATE, channels=1,
                                               blocksize=BLOCK, latency="low", dtype="float32",
                                               callback=audio.callback))
        trace.write("ready", latency=stream.latency)
        status.set("在听", READY)
        while not stopping():
            if engine.proc.poll() is not None or not stream.active:
                raise RuntimeError("音频流或模型意外退出")
            try:
                block = audio.received.get(timeout=0.1)
            except queue.Empty:
                continue

            current = audio.current[0]
            while not audio.stamps.empty():
                kind, turn, at, fields = audio.stamps.get()
                trace.write(kind, turn, at, **fields)
                if kind == "device_error":
                    raise RuntimeError("声卡返回了未预期的帧长")
                if kind == "speaker_first" and turn == current:
                    speaking = True
                    status.set("说话中", "正在回答，可以插话")
            adc, dac, mic, ref = block
            samples = speech.clean(mic, ref, adc, dac)
            if DICTATING.is_set():
                samples = np.zeros_like(samples)  # 这段话只给群聊转文字，不能让 MiniCPM-o 当成在跟它说
            status.level(float(np.sqrt(np.mean(samples * samples))))
            event, value = speech.feed(samples, adc)
            now = time.monotonic()
            if event == "start":
                was_playing = bool(audio.current[1]) or now < audio.last_voice + 0.12
                was_responding = current > 0 and output_turn == current
                current, dropped = audio.advance()
                trace.write("vad_first_speech", current, value)
                trace.write("vad_confirmed", current, now)
                was_generating = was_responding and not dispatched and not engine.output_done(round_index)
                if was_playing or was_generating:
                    trace.write("queue_cleared", current, dropped_blocks=dropped)
                if was_generating and cancelling is None:
                    cancelling = workers.submit(break_engine, engine, trace, current)
                if active is not None:
                    complete = (was_responding and not was_generating
                                and (dispatched or engine.output_done(round_index, offset)))
                    finish_playback(now, complete)
                pending = None
                wait_until = waiting_audio = recovery = None
                waiting_samples = 0
                speaking = False
                status.set("在听", "听到你了，接着说")
            elif event == "end":
                utterance, last_voice = value
                trace.write("utterance_end", current, silence_ms=round((now - last_voice) * 1000, 1))
                active = TurnRecord(current, utterance)
                routing = records.submit(active.prepare_dispatch, trace, audio, speaker, closing)
                pending = (current, utterance, now, routing)
                queued_samples = 0
                wait_until = now + friendly.WAIT_SECONDS
                status.set("思考中", "")

            if cancelling is not None and cancelling.done():
                cancelling.result()
                cancelling = None
            if running is not None and running[1].done():
                turn, future = running
                try:
                    future.result()
                except Exception as exc:
                    if turn == current:
                        friendly.LOG.exception("实时通话生成回复失败")
                        recover(friendly.MESSAGES[friendly.failure(exc)])
                    else:
                        trace.write("late_generation_discarded", turn, error_type=type(exc).__name__)
                        friendly.LOG.exception("已打断的一轮生成失败")
                else:
                    if turn == current:
                        decoded_at = now
                running = None
            if pending is not None and running is None and cancelling is None and pending[3].done():
                output_turn, utterance, endpoint, routing = pending
                pending = None
                routed_audio = routing.result()
                dispatched = routed_audio is not None
                offset = 0
                queued_samples = 0
                decoded_at = None
                if dispatched:
                    samples, rate = routed_audio
                    if samples is not None:
                        queued_samples = audio.enqueue(current, samples, rate)
                    if not queued_samples:
                        recover(friendly.MESSAGES["error"])
                else:
                    round_index += ROUND_GAP
                    trace.write("inference_start", output_turn, engine_wait_ms=round((now - endpoint) * 1000, 1))
                    future = workers.submit(reply, engine, audio, trace, output_turn, round_index, utterance)
                    active.response = future
                    running = (output_turn, future)
            if current and output_turn == current and now >= next_poll:
                next_poll = now + 0.025
                if not dispatched:
                    for wave_data in engine.output(round_index, offset):
                        queued_samples += audio.enqueue(current, wave_data)
                        if offset == 0:
                            trace.write("first_wav_queued", current)
                        offset += 1
                if (speaking and running is None and recovery is None and now > audio.last_voice + 0.25
                        and not audio.current[1]):
                    speaking = False
                    status.set("在听", READY)
                if recovery is not None and recovery.done():
                    samples, rate = recovery.result()
                    recovery = None
                    queued_samples += audio.enqueue(current, samples, rate)
                if (active is not None and running is None and recovery is None and queued_samples
                        and audio.played_samples(current, now) >= queued_samples + waiting_samples
                        and (dispatched or engine.output_done(round_index, offset))):
                    finish_turn()
                if not queued_samples and decoded_at is not None and now - decoded_at >= 2:
                    friendly.LOG.warning("实时通话没有生成语音")
                    recover(friendly.MESSAGES["error"])
            if active is not None and not queued_samples:
                if wait_until is not None and now >= wait_until:
                    wait_until = None
                    waiting_audio = notices.submit(notice_audio, speaker, friendly.MESSAGES["waiting"])
                if waiting_audio is not None and waiting_audio.done() and recovery is None:
                    samples, rate = waiting_audio.result()
                    waiting_audio = None
                    waiting_samples = audio.enqueue(current, samples, rate)


def run(stopping):
    import local_models

    if sys.platform == "darwin":
        raise friendly.ModelNotReady(f"MiniCPM-o {local_models.MINICPM_MAC_MESSAGE}")
    trace = Trace()
    status.set("启动中", friendly.MESSAGES["preparing"])
    for path in (BINARY, MODEL, VOICE):
        if not (ROOT / path).is_file():
            raise friendly.ModelNotReady("找不到已下载的本地语音模型或运行库")
    from speak import Speaker, play_notice

    released_at = time.monotonic()
    local_models.release(stopping=stopping)
    trace.write("model_release", model="ollama", elapsed_ms=round((time.monotonic() - released_at) * 1000, 1))
    if stopping():
        return
    speaker = Speaker()
    threading.Thread(target=speaker.prepare, args=([friendly.MESSAGES["waiting"]],),
                     kwargs={"offline": True}, daemon=True).start()
    # 便携运行库通过相对路径交换 WAV，会话结束后清理这次生成的文件。
    with tempfile.TemporaryDirectory(prefix="minicpm-session-", dir=ROOT) as directory:
        with (ThreadPoolExecutor(max_workers=1, thread_name_prefix="minicpm-record") as records,
              ThreadPoolExecutor(max_workers=2) as workers):
            engine = Engine(Path(directory))
            startup = workers.submit(engine.start)
            try:
                # 启动阶段只在界面显示“正在准备”，不出声：开机时没人在叫它，突然说话会吓人。
                with nullcontext():
                    while not startup.done():
                        if stopping():
                            return
                        time.sleep(0.05)
                    try:
                        startup.result()
                    except Exception as exc:
                        raise friendly.ModelNotReady("本地语音模型启动失败") from exc
                if not stopping():
                    serve(engine, trace, workers, records, stopping, speaker)
            finally:
                # 若恰好在启动进程时点了暂停，等句柄就绪，再关闭进程。
                while engine.proc is None and not startup.done():
                    time.sleep(0.01)
                release_at = time.monotonic()
                engine.close()
                trace.write("model_release", model="minicpm", pid=getattr(engine.proc, "pid", None),
                            elapsed_ms=round((time.monotonic() - release_at) * 1000, 1))
