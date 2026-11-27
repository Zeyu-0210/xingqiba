"""
test_window.py —— 不用麦克风、不用真等 3 分钟，测试“对话窗口”什么时候关闭

原理：用“假录音、假识别、假时钟”按剧本喂给 conversation_window()：
    剧本里每一项是 (这句话的文字, 从上一句结束到这句说完过了几秒)；None 表示一直没人说话。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_window.py
"""

import contextlib
import io

import numpy as np

import listen

INACTIVE = listen.INACTIVE_MINUTES * 60
AFTER = "（窗口关闭后，打游戏时说的话）"
COMMANDS = {"音量调到30", "调大一点", "静音"}  # 假装这些是“对助手说的”，其余是闲聊
PAUSE = "（点了暂停）"


class Script:
    def __init__(self, lines):
        self.lines = list(lines)
        self.now = 0.0       # 假时钟（秒）
        self.heard = []      # 真正被“听写”的话
        self.passed = []     # 交给助手的话：(文字, 是不是第一句)

    def clock(self):
        return self.now

    def record(self, start_timeout, prompt):
        item = self.lines.pop(0) if self.lines else None
        if item is None:              # 一直没人说话：等满 start_timeout
            self.now += start_timeout
            return None
        if item[0] == PAUSE:          # 你在托盘里点了“暂停监听”
            self.now += item[1]
            listen.PAUSED.set()
            return None
        text, seconds = item
        self.now += seconds
        self._current = text
        return np.zeros(1600, dtype=np.float32)  # 假录音

    def transcribe(self, audio):
        self.heard.append(self._current)
        return self._current

    def on_text(self, text, first):
        self.passed.append((text, first))
        return first or text in COMMANDS


CASES = [
    # (说明, 剧本, 应该在第几秒关闭, 应该有几句是对助手说的, 交给助手时哪些句子算“一定是对助手说的”,
    #  应该出声提示什么：唤醒后无声和空转写都提示再说一遍)
    ("唤醒后一直不说话：会提示再说一遍",
     [None, (AFTER, 1)],
     listen.START_TIMEOUT, 0, [], ["not_heard"]),
    ("唤醒后第一句没听清：会说“没听清”，然后接着听",
     [("", 2), ("音量调到30", 3), None, (AFTER, 1)],
     5 + INACTIVE, 1, ["音量调到30"], ["unclear"]),
    ("说一条指令，之后没再说话",
     [("音量调到30", 3), None, (AFTER, 1)],
     3 + INACTIVE, 1, ["音量调到30"]),
    ("指令之后一直在和队友聊天：闲聊不会让窗口一直开着",
     [("音量调到30", 3), ("快去B点", 60), ("这把稳了", 60), ("敌人在中路", 50), None, (AFTER, 1)],
     3 + INACTIVE, 1, ["音量调到30"]),
    ("聊天中途又下了指令：重新计时",
     [("音量调到30", 3), ("快去B点", 60), ("调大一点", 90), ("哈哈哈", 100), None, (AFTER, 1)],
     3 + 60 + 90 + INACTIVE, 2, ["音量调到30"]),
    ("说“好了”立即结束",
     [("音量调到30", 3), ("好了。", 2), (AFTER, 1)],
     5, 1, ["音量调到30"]),
    ("窗口里只喊了一声“星期八”：重新计时",
     [("音量调到30", 3), ("快去B点", 60), ("星期八。", 30), None, (AFTER, 1)],
     3 + 60 + 30 + INACTIVE, 1, ["音量调到30"]),
    ("窗口里说“星期八，把音量调大”：一定当作对助手说的",
     [("音量调到30", 3), ("星期八，把音量调大", 100), None, (AFTER, 1)],
     3 + 100 + INACTIVE, 2, ["音量调到30", "把音量调大"]),
    ("话中间提到“星期八”（比如“你叫星期八”）：名字原样交给助手，不会被删掉",
     [("音量调到30", 3), ("你的名字叫星期八", 10), None, (AFTER, 1)],
     3 + INACTIVE, 1, ["音量调到30"]),  # 这里假装它是闲聊（不重新计时），重点是名字有没有被删
    ("对话进行到一半点了“暂停”：马上停下，不再听写",
     [("音量调到30", 3), (PAUSE, 5), (AFTER, 1)],
     8, 1, ["音量调到30"]),
]

passed = 0
for case in CASES:
    name, lines, close_at, expect_addressed, expect_sure = case[:5]
    expect_feedback = case[5] if len(case) > 5 else []
    s = Script(lines)
    feedback = []
    with contextlib.redirect_stdout(io.StringIO()):  # 不显示过程中的打印
        addressed = listen.conversation_window(s, s.on_text, record=s.record, clock=s.clock,
                                               on_feedback=feedback.append)
    listen.PAUSED.clear()
    sure = [text for text, first in s.passed if first]
    passed_texts = [text for text, _ in s.passed]
    ok = (abs(s.now - close_at) < 0.01 and addressed == expect_addressed
          and AFTER not in s.heard and sure == expect_sure and feedback == expect_feedback
          and all("星期八" in t for t in passed_texts if "名字" in t))
    passed += ok
    print(f"{'[通过]' if ok else '[失败]'} {name}")
    print(f"       第 {s.now:.0f} 秒关闭（应为 {close_at:.0f}），对助手说了 {addressed} 句（应为 {expect_addressed}），"
          f"窗口关闭后的话被听写了吗：{'是' if AFTER in s.heard else '否'}，出声提示：{feedback or '无'}")

print(f"\n结果：{passed}/{len(CASES)} 项通过")
