"""唤醒词对话的断句：结合连续静音和流式识别的文字决定何时收尾。"""

import re

import numpy as np

import settings
import transcription

SLOW_SPEECH_SETTING = "slow_speech"
SLOW_SPEECH_SCALE = 1.5
ASR_BATCH_SECONDS = 0.12

# 从上往下匹配。静音满对应时间就收尾，最长录音时间由录音入口单独限制。
PAUSE_SECONDS = {
    "未说完": 1.8,  # 连词、口头禅、半截短语
    "说完了": 0.84,  # 简短指令、问句、完整祈使句
    "拿不准": 1.2,  # 暂时没有文字，或看不出句子是否完整
}
CONTINUATION_ENDINGS = (
    "然后", "还有", "就是", "那个", "这个", "其实", "嗯", "呃", "额", "的", "和", "跟", "与",
    "但是", "不过", "因为", "所以", "如果", "假如", "虽然", "而且", "或者", "还是", "比如", "为了",
    "以及", "把", "帮我", "给我", "让它", "我觉得", "我的意思是", "请你", "你可以",
    "调到", "调成", "改成", "设为", "设置成", "换成",
)
COMPLETE_ENDINGS = (
    "吧", "了", "吗", "呢", "么", "嘛", "多少", "一下", "一点", "点儿", "一些", "大点", "小点", "快点", "慢点",
    "小声点", "大声点", "轻点",
)
SHORT_COMMANDS = {
    "静音", "取消静音", "静默", "暂停", "继续", "停止", "停下", "退出", "关闭", "待命", "重来",
    "取消", "保存", "确认", "再见", "拜拜", "晚安", "不用", "调大", "调小", "音量调大", "音量调小",
}
INCOMPLETE_PHRASE = re.compile(
    r"请|麻烦你|能不能|我想|我想要|我需要|你这样做"
    r"|把(?:音量|声音|灯|门|电脑|这个|那个|文件|它|音乐|视频)?(?:调|改|设|换)?"
    r"|(?:请|请你|帮我|请帮我|麻烦你|你可以|能不能|你能不能)?(?:打开|关闭|启动|播放|搜索|查看)"
)
COMPLETE_COMMAND = re.compile(
    r"(?:请|帮我|请帮我|麻烦你)?(?:打开|关闭|启动|退出|播放|搜索|保存|查看).+"
    r"|.*(?:调到|设为|调成|设置成)(?:百分之)?[零〇一二两三四五六七八九十百千万0-9.％%]+"
    r"|(?:请|帮我|请帮我)?把.+(?:调大|调小|调高|调低|打开|关闭|关掉|关上|关小|开大)"
)
TRAILING_PUNCTUATION = "，。！？、；：,.!?;:…～~—"


def wait_scale():
    """读出当前的说话等待倍率，设置缺省时按正常速度。"""
    return SLOW_SPEECH_SCALE if settings.get(SLOW_SPEECH_SETTING, False) else 1.0


def pause_seconds(text):
    """按已识别的句尾选出本次停顿要等多久。"""
    compact = "".join(text.split())
    phrase = compact.rstrip(TRAILING_PUNCTUATION)
    if not phrase:
        return PAUSE_SECONDS["拿不准"]
    if phrase in SHORT_COMMANDS:
        return PAUSE_SECONDS["说完了"]
    if phrase.endswith(CONTINUATION_ENDINGS) or INCOMPLETE_PHRASE.fullmatch(phrase):
        return PAUSE_SECONDS["未说完"]
    if (compact.endswith(("?", "？")) or phrase.endswith(COMPLETE_ENDINGS)
            or COMPLETE_COMMAND.fullmatch(phrase)):
        return PAUSE_SECONDS["说完了"]
    return PAUSE_SECONDS["未说完" if len(phrase) <= 3 else "拿不准"]


class SpeechEndpoint:
    """一段录音一个识别流，连同停顿一起送入模型。"""

    def __init__(self, block_seconds, scale, fallback_silence, *, missing_pre_roll=0):
        self._batch_blocks = max(1, round(ASR_BATCH_SECONDS / block_seconds))
        self._scale = scale
        self._fallback_silence = fallback_silence
        self._missing_pre_roll = missing_pre_roll
        self._stream = None
        self._fed = 0
        self._pause = PAUSE_SECONDS["拿不准"]
        self._failed = False

    def finished(self, frames, silence):
        # 快到收尾时也送进还没凑够一批的声音，尽量用最新的识别结果。
        if not self._failed and self._fed < len(frames) and (
            len(frames) - self._fed >= self._batch_blocks or silence >= self._pause * self._scale
        ):
            try:
                audio = np.concatenate(frames[self._fed:])
                if self._stream is None:
                    self._stream = transcription.paraformer.create_stream()
                    if self._missing_pre_roll:
                        audio = np.pad(audio, (self._missing_pre_roll, 0))
                text = transcription.paraformer.accept(self._stream, audio)
            except Exception:
                # 模型不可用时，按原来的静音规则录完这句话。
                self._stream = None
                self._failed = True
                self._pause = self._fallback_silence
            else:
                self._fed = len(frames)
                self._pause = pause_seconds(text)
        return silence >= self._pause * self._scale
