"""
test_record.py —— 不用麦克风、不用你说话，用“假声音”测试录音的判断逻辑

原理：把 sounddevice 的麦克风换成一个“假麦克风”，按剧本喂给程序一段段声音
（安静、说话、咔哒声……），看程序判断得对不对。

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_record.py
"""

import numpy as np

import listen

BLOCK = int(listen.SAMPLE_RATE * listen.BLOCK_SECONDS)  # 每块 480 个采样 = 30 毫秒
rng = np.random.default_rng(0)


def quiet(seconds):
    """安静的环境声（很小的随机噪音）。"""
    n = round(seconds / listen.BLOCK_SECONDS)
    return [rng.normal(0, 0.001, BLOCK).astype(np.float32) for _ in range(n)]


def speech(seconds):
    """模拟说话（音量约 0.05 的声音，和你实测说话的音量差不多）。"""
    n = round(seconds / listen.BLOCK_SECONDS)
    t = np.arange(BLOCK) / listen.SAMPLE_RATE
    return [(0.07 * np.sin(2 * np.pi * 220 * t)).astype(np.float32) for _ in range(n)]


class FakeStream:
    """假麦克风：一打开就把剧本里的声音一块块交给程序。"""
    script = []

    def __init__(self, callback=None, **kwargs):
        self.callback = callback

    def __enter__(self):
        for block in FakeStream.script:
            self.callback(block.reshape(-1, 1), len(block), None, None)
        return self

    def __exit__(self, *args):
        return False


listen.sd.InputStream = FakeStream  # 把真麦克风换成假麦克风

# 开头固定的部分：丢掉的 0.2 秒 + 校准的 0.3 秒
START = quiet(listen.SKIP_START + 0.3)

CASES = [
    # (说明, 剧本, 应该录到吗)
    ("“请说话”一出现就马上开口，说 0.45 秒（像“静音”）",
     START + speech(0.45) + quiet(1.5), True),
    ("等 1 秒再开口，说 0.45 秒",
     START + quiet(1.0) + speech(0.45) + quiet(1.5), True),
    ("马上开口说 2 秒，中间停顿 0.4 秒",
     START + speech(0.8) + quiet(0.4) + speech(0.8) + quiet(1.5), True),
    ("只有一下“咔哒”声（0.03 秒），应该当成杂音",
     START + quiet(0.3) + speech(0.03) + quiet(1.5), False),
    ("一直没人说话",
     START + quiet(listen.START_TIMEOUT + 0.5), False),
]

passed = 0
for name, script, should_record in CASES:
    FakeStream.script = script
    audio, _levels = listen._record(None, verbose=False)
    got = audio is not None
    ok = got == should_record
    passed += ok
    detail = f"录到 {len(audio) / listen.SAMPLE_RATE:.2f} 秒" if got else "没录到"
    print(f"{'[通过]' if ok else '[失败]'} {name}  ->  {detail}")

print(f"\n结果：{passed}/{len(CASES)} 项通过")
