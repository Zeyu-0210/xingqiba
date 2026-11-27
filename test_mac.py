"""模拟 Mac 平台，覆盖降级路径，不动系统音量或桌面。"""

import queue
import runpy
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import agent_codex
import app
import brain_claude_code
import computer
import groupchat
import ui_server

PROJECT = Path(__file__).resolve().parent


class MacTests(unittest.TestCase):
    def test_volume_uses_osascript_and_keeps_mute_semantics(self):
        level, muted = 28, True

        def run(args, **kwargs):
            nonlocal level, muted
            self.assertEqual(args[0], "osascript")
            self.assertTrue(kwargs["check"])
            for script in args[2::2]:
                if script.startswith("set volume output volume "):
                    level = int(script.split()[-1])
                elif script == "set volume with output muted":
                    muted = True
                elif script == "set volume without output muted":
                    muted = False
            return SimpleNamespace(stdout=f"{level}, {str(muted).lower()}\n")

        with (patch.object(sys, "platform", "darwin"), patch("subprocess.run", side_effect=run)):
            volume = runpy.run_path(str(PROJECT / "volume.py"))
            self.assertNotIn("AudioUtilities", volume)
            self.assertEqual(volume["get_volume"]()["volume"], 28)
            self.assertTrue(volume["get_volume"]()["muted"])
            self.assertEqual(volume["set_volume"]("140")["volume"], 100)
            self.assertFalse(muted)
            self.assertEqual(volume["change_volume"](-70)["volume"], 30)
            self.assertTrue(volume["mute"]()["muted"])
            self.assertFalse(volume["unmute"]()["muted"])

    def test_computer_import_and_direct_calls_degrade(self):
        with (patch.object(sys, "platform", "darwin"),
              patch("ctypes.WinDLL", side_effect=AssertionError("Mac 上不应加载 Windows DLL"), create=True)):
            mac_computer = runpy.run_path(str(PROJECT / "computer.py"))
            self.assertEqual(mac_computer["TOOLS"], [])
            for func in mac_computer["FUNCTIONS"].values():
                result = func()
                self.assertFalse(result["ok"])
                self.assertIn("Mac 上暂时不支持", result["message"])
            self.assertIn("Mac 上暂时不支持", computer.open_app("记事本")["message"])
            self.assertFalse(groupchat.set_hands("test-member")["ok"])

    def test_assistant_hides_desktop_tools_and_rejects_stale_calls(self):
        # 这里只测模型收到的工具清单，唤醒词引擎留给语音测试。
        with patch.dict(sys.modules, {"sherpa_onnx": ModuleType("sherpa_onnx")}):
            import assistant

        with patch.object(sys, "platform", "darwin"):
            tools = {tool["function"]["name"]: tool["function"] for tool in assistant.available_tools()}
            self.assertIn("set_volume", tools)
            self.assertNotIn(assistant.brains.TAKEOVER, tools["set_brain"]["parameters"]["properties"]["brain"]["enum"])
            for name in (*computer.FUNCTIONS, "delegate_task", "claude_takeover"):
                self.assertNotIn(name, tools)
                self.assertEqual(assistant.run_tool(name, {}, None), {"ok": False, "message": computer.MAC_UNSUPPORTED})

    def test_cli_uses_path_and_skips_windows_discovery(self):
        commands = {"codex": "/opt/homebrew/bin/codex", "claude": "/opt/homebrew/bin/claude"}
        with (patch.object(sys, "platform", "darwin"),
              patch("shutil.which", side_effect=commands.get) as which,
              patch.object(agent_codex, "_exe_from_appx") as appx,
              patch.dict(agent_codex._exe_cache, path="C:/codex.exe")):
            self.assertEqual(agent_codex.find_exe(), commands["codex"])
            self.assertEqual(brain_claude_code.find_exe(), commands["claude"])
            self.assertEqual([call.args[0] for call in which.call_args_list], ["codex", "claude"])
            appx.assert_not_called()

    def test_cli_prefers_home_bin_when_path_is_missing(self):
        commands = {"/Users/test/.local/bin/codex", "/Users/test/.local/bin/claude"}
        with (patch.object(sys, "platform", "darwin"),
              patch("os.path.expanduser", side_effect=lambda path: path.replace("~", "/Users/test", 1)),
              patch("shutil.which", side_effect=lambda path: path if path in commands else None)):
            self.assertEqual(agent_codex.find_exe(), "/Users/test/.local/bin/codex")
            self.assertEqual(brain_claude_code.find_exe(), "/Users/test/.local/bin/claude")
            workers = runpy.run_path(str(PROJECT / "workers.py"))
            self.assertEqual(workers["TASKS_DIR"], str(PROJECT / "AI任务"))

    def test_minicpm_saved_setting_falls_back_and_api_refuses_selection(self):
        import local_models

        with (patch.object(sys, "platform", "darwin"),
              patch.object(local_models.settings, "get", return_value=local_models.MINICPM_MODE),
              patch.object(local_models.settings, "set") as save):
            self.assertEqual(local_models.voice_mode(), local_models.WAKE_MODE)
            modes = {mode["id"]: mode for mode in local_models.voice_choices()}
            self.assertFalse(modes[local_models.MINICPM_MODE]["available"])
            self.assertIn("CUDA", modes[local_models.MINICPM_MODE]["unavailable"])
            result = ui_server.action("voice_mode", {"value": local_models.MINICPM_MODE})
            self.assertFalse(result["ok"])
            self.assertIn("Mac 上暂时不可用", result["message"])
            save.assert_not_called()

    def test_mac_uses_browser_and_disables_windows_settings(self):
        address = "http://127.0.0.1:8090/"
        with (patch.object(sys, "platform", "darwin"),
              patch.object(ui_server.webbrowser, "open", return_value=True) as open_browser,
              patch.object(ui_server, "_existing_window") as windows,
              patch.object(ui_server.settings, "set") as save):
            self.assertTrue(ui_server.open_window(address))
            open_browser.assert_called_once_with(address)
            windows.assert_not_called()
            self.assertFalse(ui_server.action("overlay", {"value": "always"})["ok"])
            self.assertFalse(ui_server.action("autostart", {"value": True})["ok"])
            save.assert_not_called()

    def test_sensevoice_transcribes_commands_with_whisper_as_fallback(self):
        import transcription

        audio = object()
        whisper = Mock()
        transcriber = transcription.VoiceTranscriber(whisper)
        with (patch.object(sys, "platform", "darwin"),
              patch.object(transcription, "current", return_value=transcription.PARAFORMER_SENSEVOICE),
              patch.object(transcription.sensevoice, "transcribe", return_value="明天提醒我") as sensevoice,
              patch("status.barge_event")):
            self.assertEqual(transcriber.transcribe(audio), "明天提醒我")
            sensevoice.assert_called_once_with(audio)
            whisper.transcribe.assert_not_called()
            sensevoice.side_effect = RuntimeError("模型加载失败")
            self.assertIs(transcriber.transcribe(audio), whisper.transcribe.return_value)
            whisper.transcribe.assert_called_once_with(audio)

    def test_mac_ignores_saved_system_loopback_setting(self):
        import duplex

        with (patch.object(sys, "platform", "darwin"),
              patch.object(duplex.settings, "get", return_value=duplex.SYSTEM_REFERENCE)):
            self.assertEqual(duplex.echo_reference(), duplex.PROGRAM_REFERENCE)
            self.assertFalse(ui_server.action("echo_reference", {"value": duplex.SYSTEM_REFERENCE})["ok"])

    def test_tray_runs_on_main_thread_and_audio_on_worker(self):
        pending = queue.Queue()
        tray_finished = threading.Event()
        audio_threads = []

        class Icon:
            def __init__(self, *args):
                self.visible = False

            def run(icon, setup):
                self.assertIs(threading.current_thread(), threading.main_thread())
                setup(icon)
                while not tray_finished.is_set():
                    pending.get(timeout=3)()

            def stop(icon):
                self.assertIs(threading.current_thread(), threading.main_thread())
                tray_finished.set()

        def call_after(func, *args):
            pending.put(lambda: func(*args))

        ui = SimpleNamespace(start=Mock(return_value="http://127.0.0.1:8090/"), stop=Mock(),
                             open_window=Mock(), broadcast=Mock())
        listen = SimpleNamespace(PAUSED=threading.Event(), STOP=threading.Event(),
                                 main=lambda **kwargs: audio_threads.append(threading.current_thread()))
        app_helper = ModuleType("PyObjCTools.AppHelper")
        app_helper.callAfter = call_after
        modules = {"pystray": SimpleNamespace(Icon=Icon, Menu=Mock(SEPARATOR=None), MenuItem=Mock()),
                   "PyObjCTools": ModuleType("PyObjCTools"), "PyObjCTools.AppHelper": app_helper,
                   "ui_server": ui, "listen": listen, "overlay": None,
                   "bridge_pending": SimpleNamespace(start=Mock()),
                   "housekeeping": SimpleNamespace(find_instances=Mock(return_value=[]), close_old_instances=Mock()),
                   "status": SimpleNamespace(subscribe=Mock())}
        with (patch.object(sys, "platform", "darwin"), patch.dict(sys.modules, modules),
              patch.object(sys, "argv", ["app.py", "--console"]),
              patch.object(app, "STOP_FILE", str(PROJECT / "tests" / ".mac_test_no_stop_request"))):
            app.main()
        self.assertEqual(len(audio_threads), 1)
        self.assertIsNot(audio_threads[0], threading.main_thread())
        ui.open_window.assert_called_once_with(ui.start.return_value)
        ui.stop.assert_called_once_with()
        self.assertTrue(tray_finished.is_set())


if __name__ == "__main__":
    unittest.main()
