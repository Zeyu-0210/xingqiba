import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_codex
import agent_hands
import groupchat
import takeover
from run_control import RunControl, RunStopped


class GroupProgressTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(groupchat, DATA_DIR=str(directory),
                                        MEMBERS_FILE=str(directory / "members.json"),
                                        HISTORY_FILE=str(directory / "history.jsonl")))
        self.enterContext(patch.object(groupchat, "_publish"))
        self.enterContext(patch.object(agent_hands, "project_snapshot"))

    def member(self, kind):
        member = next(member for member in groupchat.members() if member["kind"] == kind)
        groupchat.set_hands(member["id"])
        return member

    def interruption(self):
        return next(msg["text"] for msg in groupchat.history()
                    if msg["role"] == "system" and "执行已中断" in msg["text"])

    def test_codex_command_progress_skips_encoding_setup(self):
        setup = "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false);\r\n"
        setup += "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        completed = f'{setup}Get-Content "当前文件.py"'
        unfinished = f"{setup}python -m unittest"
        events = [
            {"type": event, "item": {"type": "command_execution", "id": index,
                                     "command": f"\"powershell.exe\" -NoProfile -Command '{command}'",
                                     "exit_code": 0, "aggregated_output": "输出"}}
            for index, (event, command) in enumerate(
                [("item.completed", completed), ("item.started", unfinished)])
        ]

        @contextmanager
        def stream(control, args, **kwargs):
            yield SimpleNamespace(stdin=BytesIO(), stdout=[json.dumps(event).encode() for event in events],
                                  wait=lambda timeout: 0)

        self.enterContext(patch.object(RunControl, "process", stream))
        with patch.multiple(agent_codex, find_exe=lambda: "codex.exe", logged_in=lambda: True):
            steps = []
            with self.assertRaisesRegex(RuntimeError, "没有返回最终结果"):
                agent_codex.run("测试", cwd=groupchat.DATA_DIR, on_step=lambda *args: steps.append(args))

        self.assertEqual(steps, [
            ('运行命令：Get-Content "当前文件.py"', takeover.command_detail(completed, "输出"), "cmd"),
            ("运行命令：python -m unittest（结果未返回）", f"$ {unfinished}", "interrupted"),
        ])

    def test_command_preview_keeps_other_commands(self):
        for command, expected in (
            (r'"cmd.exe" /c "dir C:\code"', r"dir C:\code"),
            ('Get-Content "/code/current.py"', 'Get-Content "/code/current.py"'),
            ('Write-Output "$OutputEncoding"; Get-Date', 'Write-Output "$OutputEncoding"; Get-Date'),
            ("$OutputEncoding=[Text.Encoding]::UTF8", "$OutputEncoding=[Text.Encoding]::UTF8"),
        ):
            with self.subTest(command=command):
                self.assertEqual(agent_codex._short(command), expected)

    def test_execution_and_review_prompts_include_work_rules(self):
        executor = self.member("codex")
        reviewer = next(member for member in groupchat.members() if member["kind"] == "claude")
        with patch.multiple(groupchat, _memory_text=lambda: "", _project_line=lambda: ""):
            execution = groupchat._agent_prompt(executor, "do")
            review = groupchat._agent_prompt(reviewer, "review")
        for prompt in (execution, review):
            for phrase in ("15 分钟", "10 分钟", "Select-String", "失败两次", "不建备份副本", "需要重启"):
                self.assertIn(phrase, prompt)
        self.assertIn("汇报改了哪些文件、测试结果、下一步和卡点", execution)
        self.assertIn("只看本次实际改动的代码片段和 GPT 已有的测试结果", review)
        self.assertIn("复查结论最多 200 字。通过就只写“通过”", review)
        self.assertIn("只核对这次改动的代码片段和已有测试结果",
                      groupchat.REVIEW_HINT.format(executor="GPT", files=""))

    def test_stop_reports_repeated_test_failures_and_running_test(self):
        member = self.member("codex")
        started, finished = threading.Event(), threading.Event()
        command = r'$env:PYTHONIOENCODING="utf-8"; & ".\.venv\Scripts\python.exe" "D:\AI任务\测试_调音量后插话.py"'
        detail = takeover.command_detail(command, "失败  调大音量后插话：没停下\n" + "长日志\n" * 1000 + "3/6 通过")

        def run(*args, on_step, control, **kwargs):
            for _ in range(2):
                on_step("运行命令：调音量后插话", detail, "cmd")
            on_step("运行命令：git diff", takeover.command_detail("git diff", ""), "cmd")
            with control.process([sys.executable, "-c", "import time; time.sleep(20)"],
                                 creationflags=takeover.NO_WINDOW) as proc:
                started.set()
                proc.wait(timeout=3)
                on_step("运行命令：调音量后插话（结果未返回）", f"$ {command}", "interrupted")

        def execute(control):
            try:
                groupchat._exec(member, control=control)
            finally:
                finished.set()

        with patch.object(agent_codex, "run", run):
            groupchat._background(execute, False, groupchat._run_generation)
            try:
                self.assertTrue(started.wait(3))
                self.assertTrue(groupchat.send("停")["ok"])
            finally:
                self.assertTrue(finished.wait(5))

        summary = self.interruption()
        self.assertIn("用户叫停", summary)
        self.assertIn("「python 测试_调音量后插话.py」失败（3/6 项通过）", summary)
        self.assertIn("同一命令运行 2 次均失败", summary)
        self.assertIn("调大音量后插话：没停下", summary)
        self.assertIn("中断时「python 测试_调音量后插话.py」结果未返回", summary)
        self.assertNotIn("长日志", summary)
        self.assertIsNone(groupchat.hands_member())

    def test_timeout_reports_claude_command_error(self):
        member = self.member("claude")
        command = "$OutputEncoding=[Text.Encoding]::UTF8; pytest test_audio.py"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "one", "name": "Bash", "input": {"command": command}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "one", "is_error": True,
                 "content": "ModuleNotFoundError: No module named 'fixture_audio'"}]}},
        ]

        @contextmanager
        def stream(control, args, **kwargs):
            with open(os.devnull, "wb") as stdin:
                class Process:
                    stdout = [json.dumps(event).encode() for event in events]

                    def wait(self, timeout=None):
                        return 0

                process = Process()
                process.stdin = stdin
                yield process
                with real_process(control, [sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.2,
                                  creationflags=takeover.NO_WINDOW) as child:
                    child.wait(timeout=3)

        real_process = RunControl.process
        with patch.object(RunControl, "process", stream), patch.multiple(
            takeover.brain_claude_code, find_exe=lambda: "claude.exe", logged_in=lambda: True,
        ):
            groupchat._exec(member, control=RunControl())

        summary = self.interruption()
        step = next(msg for msg in groupchat.history() if msg["role"] == "step" and msg["kind"] == "cmd")
        self.assertTrue(step["text"].startswith("运行命令：pytest test_audio.py"))
        self.assertTrue(step["detail"].startswith(f"$ {command}"))
        self.assertIn("超过 0.2 秒", summary)
        self.assertIn("「pytest test_audio.py」失败", summary)
        self.assertIn("ModuleNotFoundError", summary)

    def test_latest_pass_does_not_count_an_earlier_failure(self):
        step = groupchat._stepper(self.member("codex"), progress := [])
        detail = takeover.command_detail
        step("运行命令（出错，退出码 1）", detail("python -m pytest test_audio.py", "1 failed"), "cmd")
        step("运行命令", detail("python -m pytest test_audio.py", "= 1 passed in 0.10s ="), "cmd")
        step("运行命令", detail("Get-Content test_other.py", "FAILED a::b"), "cmd")
        summary = groupchat._progress_text(progress)
        self.assertIn("「python -m pytest test_audio.py」通过（1 项通过）", summary)
        self.assertNotIn("失败", summary)
        self.assertNotIn("test_other.py", summary)

    def test_failure_summary_omits_credentials_and_retains_exit_code(self):
        step = groupchat._stepper(self.member("codex"), progress := [])
        command = "python test_api.py --token=demo-value"
        step("运行命令（出错，退出码 1）", takeover.command_detail(command, "AssertionError: token=demo-value"), "cmd")
        summary = groupchat._progress_text(progress)
        self.assertIn("「python test_api.py」失败", summary)
        self.assertIn("退出码 1", summary)
        self.assertNotIn("demo-value", summary)


if __name__ == "__main__":
    unittest.main()
