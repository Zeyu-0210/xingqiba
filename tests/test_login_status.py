import io
import json
import queue
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent_codex
import agent_hands
import brain_claude_code
import group_memory
import group_tasks
import groupchat
import ui_server
from login_status import LoginCache, LoginResult
from run_control import RunControl


def cli_result(event, code=0):
    return SimpleNamespace(stdin=io.BytesIO(), stdout=[json.dumps(event).encode("utf-8")],
                           wait=lambda timeout: code)


class LoginStatusTests(unittest.TestCase):
    def test_group_reply_immediately_after_login(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(groupchat, DATA_DIR=str(directory),
                                        MEMBERS_FILE=str(directory / "members.json"),
                                        HISTORY_FILE=str(directory / "history.jsonl")))
        self.enterContext(patch.object(groupchat, "_key", return_value=""))
        self.enterContext(patch.multiple(group_memory, read=lambda: "", maybe_update=lambda: None))
        self.enterContext(patch.object(group_tasks, "after_turns"))
        self.enterContext(patch.object(agent_hands, "project_snapshot", return_value=str(directory)))
        cases = [
            (agent_codex, "codex", "GPT", {"type": "item.completed",
             "item": {"type": "agent_message", "text": "登录后可以发言"}}),
            (brain_claude_code, "claude", "Claude", {"type": "result", "result": "登录后可以发言"}),
        ]
        for module, provider, name, event in cases:
            with self.subTest(provider=provider):
                signed_out, signed_in = LoginResult(True, False), LoginResult(True, True)
                probe = Mock(side_effect=[signed_out, signed_in])
                cache = LoginCache(provider, probe, signed_out_ttl=30)
                with (patch.object(module, "_login_cache", cache),
                      patch.object(module, "find_exe", return_value=f"{provider}.exe"),
                      patch.object(RunControl, "process", return_value=nullcontext(cli_result(event))) as run):
                    self.assertEqual(cache.get(wait=True), signed_out)
                    started = time.monotonic()
                    result = groupchat.send(f"@{name} 在吗", wait=True)
                    elapsed = time.monotonic() - started
                    self.assertEqual(result.get("asked"), [name], result)
                    reply = groupchat.history()[-1]
                    self.assertEqual((reply["role"], reply["name"], reply["text"]),
                                     ("ai", name, "登录后可以发言"))
                    self.assertLess(elapsed, 30)
                    self.assertEqual(module.cached_login(), signed_in)
                    self.assertEqual(probe.call_count, 2)
                    run.assert_called_once()

    def test_codex_signed_out_cache_expires_after_30_seconds(self):
        clock = Mock()
        clock.monotonic.return_value = 100
        probe = Mock(side_effect=[LoginResult(True, False), LoginResult(True, True)])
        cache = agent_codex._login_cache
        with (patch("login_status.time", clock),
              patch.multiple(cache, _probe=probe, _result=None, _expires=0)):
            self.assertEqual(cache.get(wait=True), LoginResult(True, False))
            clock.monotonic.return_value = 129
            self.assertEqual(cache.get(wait=True), LoginResult(True, False))
            clock.monotonic.return_value = 130
            self.assertEqual(cache.get(wait=True), LoginResult(True, True))

    def test_codex_updates_cache_on_login_failure(self):
        for message, expected in [
            ("Your access token could not be refreshed. Please log out and sign in again.", False),
            ("Model is unavailable. Contact support.", True),
        ]:
            with self.subTest(message=message):
                probe = Mock(return_value=LoginResult(True, True))
                cache = LoginCache("codex", probe)
                result = cli_result({"type": "turn.failed", "error": {"message": message}}, code=1)
                with (tempfile.TemporaryDirectory() as directory,
                      patch.object(agent_codex, "_login_cache", cache),
                      patch.object(agent_codex, "find_exe", return_value="codex.exe"),
                      patch.object(RunControl, "process", return_value=nullcontext(result))):
                    self.assertTrue(agent_codex.logged_in())
                    with self.assertRaises(RuntimeError):
                        agent_codex.run("测试", cwd=directory)
                    self.assertEqual(agent_codex.cached_login(), LoginResult(True, expected))
                    probe.assert_called_once()

    def test_claude_login_window_rechecks_when_opened_and_closed(self):
        closed = threading.Event()
        checked = queue.Queue()
        window = Mock()
        window.wait.side_effect = lambda: closed.wait(3)

        def probe():
            result = LoginResult(True, closed.is_set())
            checked.put(result)
            return result

        cache = LoginCache("claude", probe)
        with (patch.object(brain_claude_code, "_login_cache", cache),
              patch.object(brain_claude_code, "find_exe", return_value="claude.exe"),
              patch.object(ui_server.subprocess, "Popen", return_value=window)):
            try:
                self.assertTrue(ui_server.open_login()["ok"])
                self.assertEqual(checked.get(timeout=2), LoginResult(True, False))
            finally:
                closed.set()
            self.assertEqual(checked.get(timeout=2), LoginResult(True, True))
            self.assertEqual(cache.get(wait=True), LoginResult(True, True))
            window.wait.assert_called_once()

    def test_probe_exception_keeps_installed_status(self):
        cache = LoginCache("codex", Mock(side_effect=OSError("检查失败")))
        self.assertEqual(cache.get(wait=True), LoginResult(True, False))
