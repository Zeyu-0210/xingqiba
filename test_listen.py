"""
test_listen.py —— 测试“显卡语音识别”是否正常（不需要你说话，也不会调音量）

用的是 tests\\audio 里事先生成的录音（Windows 自带的中文语音“慧慧”读的）。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_listen.py
"""

import os
import re
import time

import numpy as np

from listen import PROJECT_DIR, SpeechToText

AUDIO_DIR = os.path.join(PROJECT_DIR, "tests", "audio")

# 文件名 → 可以接受的识别结果（数字写成汉字或阿拉伯数字都算对）
EXPECTED = {
    "01.wav": ["把音量调到三十", "把音量调到30"],
    "02.wav": ["声音大一点"],
    "03.wav": ["太吵了小声点"],
    "04.wav": ["静音"],
    "05.wav": ["取消静音"],
    "06.wav": ["现在音量是多少"],
    "07.wav": ["音量调到五十", "音量调到50"],
    "08.wav": ["调小一点"],
}


def normalize(text):
    """去掉标点和空格，方便比较。"""
    return re.sub(r"[\s，。！？、：；,.!?:;]", "", text)


t = time.time()
stt = SpeechToText()
print(f"模型加载完成（显卡），用时 {time.time() - t:.1f} 秒\n")

passed = 0
for name, answers in EXPECTED.items():
    t = time.time()
    text = stt.transcribe(os.path.join(AUDIO_DIR, name))
    seconds = time.time() - t
    ok = normalize(text) in answers
    passed += ok
    print(f"{'[通过]' if ok else '[失败]'} {name}  识别：{text:<12}  期望：{answers[0]:<10} 用时 {seconds:.2f} 秒")

# 加了提示词之后，要确认 Whisper 不会在“只有杂音”时凭空编出“音量调到三十”之类的话
noise = np.random.default_rng(0).normal(0, 0.003, 16000 * 2).astype(np.float32)
text = stt.transcribe(noise)
ok = text == ""
passed += ok
print(f"{'[通过]' if ok else '[失败]'} 2 秒杂音  识别：{text or '（空）'}  期望：（空）")

print(f"\n结果：{passed}/{len(EXPECTED) + 1} 项通过")
