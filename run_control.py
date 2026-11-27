"""中断群聊 AI 的一次任务，并停止它启动的本地进程。"""

import ctypes
import os
import subprocess
import threading
from contextlib import contextmanager
from ctypes import wintypes


class RunStopped(RuntimeError):
    """任务被用户叫停或达到时限。"""


if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel.CloseHandle.restype = wintypes.BOOL

_CREATE_SUSPENDED = 0x00000004


class RunControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._reason = None
        self._terminate = None

    def check(self):
        if self._reason is not None:
            raise RunStopped(self._reason)

    def stop(self, reason):
        with self._lock:
            if self._reason is None:
                self._reason = reason
            if self._terminate is not None:
                self._terminate()

    @contextmanager
    def process(self, args, *, timeout=None, creationflags=0, **kwargs):
        """Windows 上在程序开始运行前就把它加入作业，停止时连同派生进程一起结束。"""
        job = None
        with self._lock:
            self.check()
            if os.name == "nt":
                import psutil

                job = _kernel.CreateJobObjectW(None, None)
                if not job:
                    raise ctypes.WinError(ctypes.get_last_error())
            try:
                proc = subprocess.Popen(args, creationflags=creationflags | (_CREATE_SUSPENDED if job else 0),
                                        **kwargs)
                if job:
                    try:
                        if not _kernel.AssignProcessToJobObject(job, int(proc._handle)):
                            raise ctypes.WinError(ctypes.get_last_error())
                        psutil.Process(proc.pid).resume()
                    except BaseException:
                        proc.kill()
                        proc.wait()
                        for pipe in (proc.stdin, proc.stdout, proc.stderr):
                            if pipe is not None:
                                pipe.close()
                        raise
            except BaseException:
                if job:
                    _kernel.CloseHandle(job)
                raise

            def terminate():
                if job:
                    if not _kernel.TerminateJobObject(job, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                elif proc.poll() is None:
                    proc.kill()

            self._terminate = terminate
        timer = None
        if timeout is not None:
            timer = threading.Timer(timeout, self._expire, (terminate, timeout))
            timer.daemon = True
            timer.start()
        try:
            with proc:
                try:
                    self.check()
                    yield proc
                except BaseException:
                    with self._lock:
                        terminate()
                    self.check()
                    raise
        finally:
            with self._lock:
                if timer:
                    timer.cancel()
                self._terminate = None
                if job:
                    _kernel.CloseHandle(job)
        self.check()

    def _expire(self, terminate, timeout):
        with self._lock:
            # 已经进入下一阶段时，上一阶段的计时器不能把新进程停掉。
            if self._terminate is terminate and self._reason is None:
                elapsed = f"{timeout / 60:g} 分钟" if timeout >= 60 else f"{timeout:g} 秒"
                self._reason = f"超过 {elapsed} 的时限"
                terminate()
