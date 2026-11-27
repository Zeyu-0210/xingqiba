"""语音提示统一放在这里，异常细节交给日志。"""

import logging
import re
import threading


MESSAGES = {
    "unclear": "我没听清，你再说一遍好吗？",
    "preparing": "我正在准备，稍等几秒再叫我。",
    "offline": "现在联网的帮手连不上，简单的事我先自己来。",
    "unavailable": "这个我还不会，已经记下来了。",
    "waiting": "好的，我想一下",
    "error": "出了点小问题，你再说一遍试试。",
}
WAIT_SECONDS = 3
LOG = logging.getLogger(__name__)


class ModelNotReady(RuntimeError):
    """本地模型加载失败或暂时不可用。"""


def failure(exc):
    if isinstance(exc, ModelNotReady):
        return "preparing"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "offline"
    return "error"


def cloud_failure(detail):
    if re.search(r"\b(?:connection|connect|network|timeout|timed out|econnrefused|"
                 r"service unavailable|rate limit|overloaded|unauthorized|authentication)\b", str(detail), re.I):
        return "offline"
    return failure(detail)


class WaitNotice:
    """一轮只提醒一次，正式答复之前让提醒先说完。"""

    def __init__(self, say, stopping):
        self.say = say
        self.stopping = stopping
        self.timer = None

    def __enter__(self):
        self.timer = threading.Timer(WAIT_SECONDS, self._say)
        self.timer.daemon = True
        self.timer.start()

    def _say(self):
        if not self.stopping():
            self.say("waiting")

    def finish(self):
        if self.timer is not None:
            timer, self.timer = self.timer, None
            timer.cancel()
            timer.join()

    def __exit__(self, *_):
        self.finish()
