"""登录状态缓存。查询界面的线程只读取最近结果，真正执行任务可等检查完成。"""

import threading
import time
from typing import NamedTuple

import status


class LoginResult(NamedTuple):
    installed: bool
    logged_in: bool


class LoginCache:
    def __init__(self, provider, probe, signed_out_ttl=300):
        self._provider = provider
        self._probe = probe
        self._signed_out_ttl = signed_out_ttl
        self._condition = threading.Condition()
        self._result = None
        self._expires = 0
        self._checking = False
        self._revision = 0

    def get(self, *, wait=False, refresh=False, recheck_signed_out=False):
        with self._condition:
            signed_out = self._result is not None and not self._result.logged_in
            needs_check = refresh or (recheck_signed_out and signed_out) or time.monotonic() >= self._expires
            if not self._checking and needs_check:
                worker = threading.Thread(target=self._refresh, args=(self._revision,),
                                          name=f"{self._provider}-login", daemon=True)
                self._checking = True
                try:
                    worker.start()
                except RuntimeError:
                    self._checking = False
                    raise
            if wait:
                self._condition.wait_for(lambda: not self._checking)
            return self._result

    def mark_signed_out(self):
        with self._condition:
            self._revision += 1  # 实际调用发现已退出，丢弃同时进行的旧检查。
            changed = self._store(LoginResult(True, False))
        if changed:
            self._publish()

    def _refresh(self, revision):
        try:
            result = self._probe()
        except Exception:
            result = LoginResult(True, False)
        with self._condition:
            changed = revision == self._revision and self._store(result)
            self._checking = False
            self._condition.notify_all()
        if changed:
            self._publish()

    def _store(self, result):
        changed = self._result != result
        self._result = result
        self._expires = time.monotonic() + (300 if result.logged_in else self._signed_out_ttl)
        return changed

    def _publish(self):
        status.publish({"type": "login_status", "provider": self._provider})
