"""
test_duplex.py —— 测试“说话时可以打断”（边说边听）

    1. 不出声的部分：切句子、判断插嘴的话算什么（附和 / 叫停 / 新指令 / 回声）、对话窗口怎么处理
    2. 真的用喇叭和麦克风测（会放出声音，大约 30 秒）：
         - 只放星期八的声音、没人说话：不能被自己的声音打断（回声消除要管用）
         - 放星期八的声音，中途用喇叭放一段你以前的录音（假装你插嘴）：要停下来，录下你说的话，转成文字
         - 停下后接着说：能把剩下的说完
       加 --quiet 只跑第 1 部分（不出声）

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_duplex.py
"""

import contextlib
import io
import os
import sys
import threading
import time
import wave

import numpy as np

import duplex
import listen
import speak

HERE = os.path.dirname(os.path.abspath(__file__))
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def read_wav(path):
    with wave.open(path) as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return data, rate


print("===== 1. 切句子（边合成边说：第一句好了就开口）=====")
parts = speak.split_sentences("好的。音量已经调到百分之三十了！明天下午三点你还有个会议，别忘了。")
check("太短的“好的。”并到下一句，其余按句号切开", parts == ["好的。音量已经调到百分之三十了！", "明天下午三点你还有个会议，别忘了。"],
      f"→ {parts}")
check("没有标点的一句话不切", speak.split_sentences("我在") == ["我在"])

print("\n===== 2. 它说话时你插了一句，这句话算什么 =====")
SAID = "已经帮你打开记事本了，需要我在里面写点什么吗"
for text, expect in [
    ("", "resume"), ("嗯。", "resume"), ("对对对", "resume"), ("啊", "resume"),
    ("停一下", "stop"), ("别说了！", "stop"), ("等等", "stop"), ("星期八。", "stop"), ("星期八，停", "stop"),
    ("已经帮你打开记事本了", "resume"),          # 它自己的回声
    ("帮你打开记事本了需要我", "resume"),        # 回声（识别得不完整）
    ("把记事本关掉", "new"), ("音量调到三十", "new"), ("星期八，把音量调大", "new"),
]:
    got = listen.interruption_kind(text, SAID)
    check(f"“{text}” → {expect}", got == expect, f"→ {got}")
SAID2 = "好的，已经帮你把音量调到百分之30了。"
for text, expect in [
    ("已经帮你把音量调到百分之三十了", "resume"),   # 数字写法不同：还是它自己的回声
    ("音量调到30%", "resume"),                     # 只录到半句、写法不同
    ("帮你把", "resume"),                          # 只录到很短的一截
    ("把音量调到五十", "new"),                     # 你真的要改成 50
    ("再大一点", "new"),
]:
    got = listen.interruption_kind(text, SAID2)
    check(f"它说“{SAID2}”时录到“{text}” → {expect}", got == expect,
          f"→ {got}（相似度 {listen.echo_similarity(text, SAID2):.0%}）")

print("\n===== 3. 对话窗口怎么处理插嘴（假录音、假识别）=====")


class FakeBarge:
    def __init__(self, text, can_resume=True):
        self.text, self.audio, self.said = text, np.zeros(1600, np.float32), SAID
        self.resumed = self.dropped = 0
        self.resume = self._resume if can_resume else None

    def _resume(self):
        self.resumed += 1

    def drop(self):
        self.dropped += 1


def run_window(items, chatter=()):
    """按顺序把插嘴的话交给对话窗口；之后一直没人说话，窗口按时关闭。"""
    queue_ = list(items)
    passed = []
    now = [0.0]

    class Stt:
        def transcribe(self, audio):
            return current[0].text

    current = [None]

    def pending():
        if queue_:
            current[0] = queue_.pop(0)
            return current[0]
        return None

    def record(start_timeout, prompt):
        now[0] += start_timeout
        return None

    def on_text(text, first):
        passed.append((text, first))
        return text not in chatter

    with contextlib.redirect_stdout(io.StringIO()):
        listen.conversation_window(Stt(), on_text, record=record, clock=lambda: now[0], pending=pending)
    return passed, now[0]


b = FakeBarge("嗯")
passed, _ = run_window([b])
check("只是“嗯”一声：接着说，不交给助手", b.resumed == 1 and not passed and not b.dropped)
b = FakeBarge("")
passed, _ = run_window([b])
check("什么都没听清：接着说", b.resumed == 1 and not passed)
b = FakeBarge("停一下")
passed, closed = run_window([b])
check("“停一下”：不说了，也不交给助手，窗口继续开着",
      b.dropped == 1 and not b.resumed and not passed and closed >= listen.INACTIVE_MINUTES * 60)
b = FakeBarge("把音量调到三十")
passed, _ = run_window([b])
check("新指令：不再接着说，交给助手（当作第一句）", passed == [("把音量调到三十", True)] and b.dropped == 1 and not b.resumed)
b = FakeBarge("已经帮你打开记事本了")
passed, _ = run_window([b])
check("录到的是它自己的回声：接着说，不当成指令", b.resumed == 1 and not passed)
b = FakeBarge("快去B点")
passed, _ = run_window([b], chatter={"快去B点"})
check("插的那句是跟队友说的（助手判断不是对它说的）：接着把话说完", b.resumed == 1 and passed == [("快去B点", True)])
b = FakeBarge("好了")
passed, closed = run_window([b])
check("“好了”：不说了，窗口马上关闭", b.dropped == 1 and not passed and closed == 0)
b = FakeBarge("已经帮你打开记事本了", can_resume=False)
passed, _ = run_window([b])
check("它说完后录到的余音（回声）：不当成指令", not passed)
b = FakeBarge("再帮我打开计算器", can_resume=False)
passed, _ = run_window([b])
check("它刚说完你就接话：这句话照样交给助手", passed == [("再帮我打开计算器", True)])

print("\n===== 3b. 记住说到哪被打断（像 ChatGPT 通话模式）=====")
p = duplex.Playback()
p.add(np.zeros(24000, np.float32), 24000, "第一句话说完了。")          # 1 秒
p.add(np.zeros(48000, np.float32), 24000, "第二句话一共有十二个字。")    # 2 秒
p.pos = int(24000 * 2.0)  # 播到第 2 秒（第二句的一半）停下
check("按停下的位置算出说到哪个字", p.spoken_text() == "第一句话说完了。第二句话一共",
      f"→ {p.spoken_text()}")
import memory  # noqa: E402
import tempfile  # noqa: E402
m = memory.Memory(tempfile.mkdtemp(), resume=False)
m.short_term.add([{"role": "user", "content": "讲讲明天的安排"},
                  {"role": "assistant", "content": "明天上午九点开会，下午三点去医院，晚上七点吃饭。"}])
m.mark_interrupted("明天上午九点开会，下午三")
last = m.short_term.messages()[-1]["content"]
check("短期记忆里注明了只念到哪就被打断", "只念到“明天上午九点开会，下午三”就被用户打断了" in last, f"→ {last[-40:]}")

print("\n===== 3c. 诊断记录：预热 / 断续保护拦下的人声不算有效时长 =====")
import queue  # noqa: E402


class _FakeVad:
    def feed(self, x):
        return [0.9]  # 每块都当成人声


def _diag_run(first_time, glitch):
    orig = duplex.Vad
    duplex.Vad = _FakeVad
    try:
        p = duplex.Playback()
        p.add(np.zeros(24000 * 5, np.float32), 24000)
        p.finish()
        q = queue.Queue()
        frame = np.full(duplex.FRAME, 0.05, np.float32)
        for _ in range(40):
            q.put((frame, 0.05))
        if glitch:
            p._glitch_at = time.monotonic() + 100  # 整段都在断续保护期内
        r = duplex.Result(p.id, 1)
        p._listen(q, r, lambda: q.empty(), None, None, 0.1, first_time)
        return r, p._lstats
    finally:
        duplex.Vad = orig


r, st = _diag_run(False, False)
check("没保护：人声够长就打断，有效时长 > 0", r.interrupted and st["max_speech_ms"] > 0,
      f"→ {st['max_speech_ms']} ms")
r, st = _diag_run(True, False)
check("预热期：不打断，有效时长 0，记为预热拦下",
      not r.interrupted and st["max_speech_ms"] == 0 and st["blocked_by_warmup"] == st["voiced"] > 0
      and st["blocked_by_glitch"] == 0, f"→ {st}")
r, st = _diag_run(False, True)
check("断续保护期：不打断，有效时长 0，记为断续拦下",
      not r.interrupted and st["max_speech_ms"] == 0 and st["blocked_by_glitch"] == st["voiced"] > 0
      and st["blocked_by_warmup"] == 0, f"→ {st}")



class _ScriptVad:
    """按顺序返回预设的“人声把握”。"""
    probs = []

    def __init__(self):
        self.i = 0

    def feed(self, x):
        p = self.probs[min(self.i, len(self.probs) - 1)]
        self.i += 1
        return [p]


def _median_or(vs):
    return round(sorted(vs)[len(vs) // 2], 3) if vs else None


def _echo_run(frames):
    """frames: [(人声把握, 消回声后音量, 消之前音量)]，每项一块。返回 (结果, 诊断统计)。"""
    orig = duplex.Vad
    duplex.Vad = _ScriptVad
    _ScriptVad.probs = [f[0] for f in frames]
    try:
        p = duplex.Playback()
        p.add(np.zeros(24000 * 10, np.float32), 24000)
        p.finish()
        q = queue.Queue()
        for _p, out, raw in frames:
            q.put((np.full(duplex.FRAME, out, np.float32), raw))
        r = duplex.Result(p.id, 1)
        p._listen(q, r, lambda: q.empty(), None, None, 0.1, False)
        return r, p._lstats
    finally:
        duplex.Vad = orig


check("门槛：还没量到回声底 → 用固定的 0.3", duplex.ratio_gate([]) == duplex.MIN_RATIO)
check("门槛：回声底 0.1 → 0.2；回声底很小 → 不低于 0.12；回声底大 → 不高于 0.3",
      abs(duplex.ratio_gate([0.1] * 10) - 0.2) < 1e-9 and duplex.ratio_gate([0.02] * 10) == duplex.RATIO_FLOOR
      and duplex.ratio_gate([0.5] * 10) == duplex.MIN_RATIO)
# 喇叭很响：只有回声时剩 10%；你轻声插话时只剩 25%（旧的固定门槛 30% 会把你拦下）
echo = [(0.1, 0.01, 0.1)] * 60
r, st = _echo_run(echo + [(0.9, 0.025, 0.1)] * 60)
check("喇叭很响时轻声插话（比例 0.25，回声底 0.1）：能打断", r.interrupted, f"→ 门槛中位 {_median_or(st['gates'])}")
r, st = _echo_run(echo + [(0.9, 0.013, 0.1)] * 60)
check("喇叭很响时回声漏成“像人声”（比例 0.13，回声底 0.1）：不打断", not r.interrupted,
      f"→ 拦下 {st['blocked_by_ratio']} 次")


print("\n===== 4. 人声检测（Silero VAD）=====")
voice, rate = read_wav(os.path.join(HERE, "tests", "audio", "01.wav"))
voice16 = duplex.resample(voice, rate, 16000)
vad = duplex.Vad()
probs = vad.feed(voice16)
check("你的录音：能连续检测到人声超过 0.5 秒", duplex.max_run(probs) >= duplex.MIN_INTERRUPT,
      f"→ 最长连续 {duplex.max_run(probs):.1f} 秒")
vad.reset()
noise = (np.random.RandomState(0).randn(16000 * 2) * 0.01).astype(np.float32)
check("两秒白噪声：不算人声", duplex.max_run(vad.feed(noise)) < 0.2)
tts, tts_rate = read_wav(os.path.join(HERE, "voices", "01 云扬（男·沉稳专业）.wav"))
check("改采样率：24kHz → 16kHz 长度对", abs(len(duplex.resample(tts, tts_rate, 16000)) - len(tts) * 16000 / tts_rate) < 50)

if "--quiet" not in sys.argv:
    print("\n===== 5. 真的用喇叭和麦克风测（会放出声音；测试时旁边如果有人说话，会被当成插嘴）=====")
    check("回声消除可以用", duplex.available(), duplex.unavailable_reason())
    stt = listen.SpeechToText()

    for i in range(3):
        p = duplex.Playback()
        p.add(tts, tts_rate)
        p.finish()
        r = p.run(listen=True)
        heard = stt.transcribe(r.audio) if r.audio is not None else ""
        check(f"只放星期八的声音、没人说话（第 {i + 1} 次）：没有被自己的声音打断",
              not r.interrupted and r.audio is None and p.done,
              f"→ 打断：{r.interrupted}" + (f"，录到的是：“{heard}”（是房间里有人说话，还是它自己的回声？）"
                                          if r.audio is not None else ""))

    # 模拟“你插嘴”：把你以前的一段录音直接混进麦克风信号（就像有人在麦克风旁边说话），
    # 音量按实测：真人在电脑前说话，麦克风收到的音量是喇叭回声的 3～5 倍（约 0.06）
    talker = voice16 / max(1e-6, float(np.sqrt(np.mean(voice16 ** 2)))) * 0.06
    mix = {"pos": None, "start": time.monotonic() + 1.5}

    def mic_hook(x):
        if mix["pos"] is None and time.monotonic() >= mix["start"]:
            mix["pos"] = 0
        if mix["pos"] is not None and mix["pos"] < len(talker):
            part = talker[mix["pos"]:mix["pos"] + len(x)]
            x = x.copy()
            x[:len(part)] += part
            mix["pos"] += len(x)
        return x

    p = duplex.Playback()
    p.add(tts, tts_rate)
    p.finish()
    marks = []
    t0 = time.monotonic()
    mix["start"] = t0 + 1.5  # 1.5 秒时“你”开口
    duplex.MIC_HOOK = mic_hook
    r = p.run(listen=True, on_interrupt=lambda: marks.append(time.monotonic() - t0))
    duplex.MIC_HOOK = None
    took = marks[0] if marks else None
    check("中途有人插嘴：停下来了", r.interrupted, f"→ 在第 {r.at:.1f} 秒停下（插嘴从第 1.5 秒开始）")
    check("开口后很快就停（录音开头有约 0.5 秒空白，所以在 3.5 秒内算合格）",
          took is not None and 1.5 < took < 3.5, f"→ 第 {took or 0:.1f} 秒")
    seconds = len(r.audio) / 16000 if r.audio is not None else 0
    check("录下了插嘴的话（包括开头）", seconds >= 1.5, f"→ {seconds:.1f} 秒")
    if r.audio is not None:
        heard = stt.transcribe(r.audio)
        check("录下的话能听清：识别出“音量”", "音量" in heard, f"→ 识别结果：{heard}")
    stt.unload()
    left = p.seconds - p.pos / duplex.OUT_RATE
    p.rewind()
    r2 = p.run(listen=False)
    check("接着说：把剩下的说完", p.done and not r2.interrupted, f"→ 剩下约 {left:.1f} 秒")

    print("\n===== 6. 边合成边说（离线声音，不联网）=====")
    speaker = speak.Speaker(voice="kokoro:47")
    t0 = time.monotonic()
    starts = []
    import status
    status.subscribe(lambda state, detail: starts.append(time.monotonic() - t0) if state == "说话中" else None)
    spoken = speaker.say("好的，这是第一句话。这是第二句，在后台合成。这是第三句，说完就结束。")
    total = time.monotonic() - t0
    check("三句话都说完了", bool(spoken) and spoken.playback.done and spoken.playback.seconds > 4,
          f"→ 开口用了 {starts[0] if starts else -1:.1f} 秒，全部说完 {total:.1f} 秒")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
