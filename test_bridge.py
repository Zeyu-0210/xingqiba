import json
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import bridge
import groupchat
import local_models
import minicpm_voice
import settings
import ui_server


def task(name, work="整理资料", when="马上", ask=""):
    return {"to": name, "do": work, "when": when, "ask": ask}


def response(*tasks, reply="我来安排。", **extra):
    return json.dumps({"tasks": tasks, "reply": reply, **extra}, ensure_ascii=False)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # 这些测试按 Windows 的规则写（群聊执行权、MiniCPM-o 通话）；Mac 上的降级行为由 test_mac 覆盖
        self.enterContext(patch.object(sys, "platform", "win32"))
        self.enterContext(patch.object(settings, "PATH", str(folder / "settings.json")))
        self.enterContext(patch.multiple(groupchat, DATA_DIR=str(folder),
                                        MEMBERS_FILE=str(folder / "members.json"),
                                        HISTORY_FILE=str(folder / "history.jsonl")))
        self.enterContext(patch.object(groupchat, "history_file", return_value=groupchat.HISTORY_FILE))
        self.enterContext(patch.object(groupchat, "_publish"))
        self.enterContext(patch.object(groupchat, "_awaiting", {"member": None}))
        self.enterContext(patch.object(groupchat, "_run_generation", 0))
        import bridge_pending  # 挂起任务写进临时文件，别碰真的 groupchat\pending.json
        self.enterContext(patch.object(bridge_pending, "PENDING_FILE", str(folder / "pending.json")))
        self.claude ={"id": "1", "name": "Claude", "kind": "claude", "enabled": True, "hands": True}
        self.gpt = {"id": "2", "name": "GPT", "kind": "codex", "enabled": True}
        self.helper = {"id": "3", "name": "旅程助手", "kind": "openai", "enabled": True, "role": "比较景点"}
        groupchat._save_members([self.claude, self.gpt, self.helper])

    def test_split_and_roster_ignore_confident(self):
        tasks = [task("家电", "开灯"), task("千问", "点外卖", ask="想吃什么？"),
                 task("家电", "开电视", "洗完澡之后")]
        with patch.object(bridge, "_ask_gemma", return_value=response(*tasks, confident=False)) as gemma:
            result = bridge.plan("帮我开灯，点个外卖，洗完澡之后再开电视。")
        self.assertEqual(result["tasks"], tasks)
        self.assertEqual(result["reply"], "我来安排。")
        roster = gemma.call_args.args[1]
        self.assertIn("旅程助手：比较景点", roster)
        self.assertIn("家电：未接入", roster)
        self.assertIn("星期八：自己回答", roster)

    def test_fixed_escalation_rules(self):
        cases = [
            ("修一下项目", response(task("Claude")), "主脑"),
            ("修一下项目", response(task("GPT")), "主脑"),
            ("家里的五件事", response(*[task("家电")] * 5), "超过 4 件"),
            ("开灯", "不是 JSON", "任务清单"),
            ("开灯", '{"tasks":[{"to":"家电"}],"reply":"好"}', "任务清单"),
            ("开灯", response(task("不存在")), "不在调度名单"),
            ("查一下景点，根据查到的资料安排活动", response(task("旅程助手"), task("星期八")), "前后依赖"),
        ]
        for text, raw, reason in cases:
            with self.subTest(text=text, reason=reason), patch.object(bridge, "_ask_gemma", return_value=raw):
                result = bridge.plan(text)
                self.assertIn(reason, result["escalation"])
                self.assertEqual(result["text"], text)
                self.assertEqual(result["tasks"], [])

    def test_escalation_keeps_original_and_uses_current_executor(self):
        text = "问问 GPT 怎么安排。\n我有三个要求。"
        with (patch.object(bridge, "_ask_gemma") as gemma,
              patch.object(groupchat, "_ask_all", return_value={"asked": ["Claude"]}) as ask):
            self.assertEqual(bridge.route(text), "这件事我交给Claude来安排。")
        gemma.assert_not_called()
        self.assertEqual(ask.call_args.args[0], [self.claude])
        self.assertIn(text, ask.call_args.args[1])
        saved = groupchat.history()[-1]
        self.assertEqual(saved["name"], "调度员")
        self.assertIn("@Claude", saved["text"])
        self.assertTrue(saved["text"].endswith(text))

    def test_explicit_both_and_no_executor(self):
        with patch.object(groupchat, "_ask_all", return_value={"asked": ["Claude", "GPT"]}) as ask:
            bridge.route("问问 Claude 和 GPT 怎么安排")
            self.assertEqual(ask.call_args.args[0], [self.claude, self.gpt])
            groupchat.set_hands(None)
            bridge.route("问问 GPT 怎么安排")
            self.assertEqual([m["name"] for m in ask.call_args.args[0]], ["Claude", "GPT"])

    def test_message_format_and_condition_is_only_recorded(self):
        raw = response(task("家电", "开电视", "洗完澡之后"), task("千问", "帮忙点外卖", ask="想吃什么？"),
                       task("旅程助手", "明天比较景点", "一小时后"), task("旅程助手", "找景点"))
        with (patch.object(bridge, "_ask_gemma", return_value=raw),
              patch.object(groupchat, "_ask_all", return_value={"asked": ["旅程助手"]}) as ask):
            # 有未接入的成员：先说“还不会”；定好时间的任务（一小时后）跟着报一下
            self.assertRegex(bridge.route("安排这四件事"),
                             r"^" + bridge.UNAVAILABLE_REPLY + r"已定好时间：\d\d:\d\d（旅程助手）：明天比较景点。$")
        saved = groupchat.history()
        self.assertEqual(len(saved), 4)
        self.assertTrue(all(m["role"] == "dispatcher" and m["name"] == "调度员" for m in saved))
        self.assertEqual(saved[0]["text"], "@家电\n任务：开电视\n时间：洗完澡之后\n追问：无"
                         "\n未接入，等主人接好设备\n已挂起，等主人说条件满足了（比如“洗完了”“回来了”）")
        self.assertIn("追问：想吃什么？", saved[1]["text"])
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[0], [self.helper])
        self.assertIn("找景点", ask.call_args.args[1])
        self.assertNotIn("明天比较景点", ask.call_args.args[1])

    def test_manual_entry_keeps_group_commands_and_deduplicates(self):
        with (patch.object(bridge, "_ask_gemma", return_value=response(task("家电", "关灯"))) as gemma,
              patch.object(groupchat, "_handle_user_message", return_value={"ok": True}) as normal):
            for text in ("记住：我爱喝茶", "让 GPT 执行"):
                groupchat.send(text, wait=True)
                self.assertEqual(normal.call_args.args[0], text)
            for _ in range(2):
                result = groupchat.send("调度：让 GPT 执行关灯", wait=True, request_id="a" * 32)
                self.assertTrue(result["saved"])
            gemma.assert_called_once()
            self.assertEqual(gemma.call_args.args[0], "让 GPT 执行关灯")
            self.assertEqual(normal.call_count, 2)
            self.assertFalse(groupchat.send("调度：", wait=True)["ok"])
        self.assertEqual(groupchat.hands_member()["name"], "Claude")
        self.assertTrue(groupchat.send("停", wait=True)["ok"])
        self.assertIsNone(groupchat.hands_member())

    def test_stop_during_gemma_does_not_post_pending_tasks(self):
        def stop_then_reply(*_):
            groupchat.force_stop()
            return response(task("家电", "开灯"))

        with patch.object(bridge, "_ask_gemma", side_effect=stop_then_reply):
            self.assertEqual(bridge.route("开灯"), bridge.CANCELLED)
        self.assertFalse(any(m["role"] == "dispatcher" for m in groupchat.history()))

    def test_report_corrects_completion_claims_for_unavailable_or_deferred_work(self):
        with patch.object(bridge, "_ask_gemma", return_value=response(
                task("家电", "拉上窗帘"), reply="已经为您拉上了窗帘。")):
            self.assertEqual(bridge.route("拉上窗帘"), bridge.UNAVAILABLE_REPLY)
        with patch.object(bridge, "_ask_gemma", return_value=response(
                task("旅程助手", "查景点", "一小时后"), reply="已经为您安排好了。")):
            # 定时任务不再只是“记一下”：报出具体时间，到点会派出去
            self.assertRegex(bridge.route("一小时后查景点"), r"^好的，已定好时间：\d\d:\d\d（旅程助手）：查景点。$")

    def test_voice_switch_defaults_off_and_minicpm_skips_local_inference(self):
        text = "帮我开灯，然后点个外卖。"
        with (patch.object(bridge, "_ask_gemma") as gemma,
              patch.object(groupchat, "_ask_all", return_value={"asked": ["Claude"]}) as ask):
            self.assertFalse(bridge.voice_enabled())
            self.assertIsNone(minicpm_voice.on_user_turn(text))
            gemma.assert_not_called()
            ask.assert_not_called()
            self.assertTrue(ui_server.action(bridge.VOICE_SETTING, {"value": True})["ok"])
            self.assertIsNone(minicpm_voice.on_user_turn("今天天气怎么样？"))
            self.assertEqual(minicpm_voice.on_user_turn(text), "这件事我交给Claude来安排。")
            self.assertFalse(ask.call_args.kwargs["summarize"])
            gemma.assert_not_called()
            settings.set("voice_mode", local_models.MINICPM_MODE)
            self.assertEqual(bridge.plan(text)["escalation"], "正在通话")
            gemma.assert_not_called()

    def test_voice_only_routes_clear_requests(self):
        for text in ("帮我把灯关了再点个外卖", "交给旅程助手找景点", "问问 GPT 怎么做"):
            with self.subTest(text=text):
                self.assertTrue(bridge.matches_voice(text))
        for text in ("你觉得这个周末怎么样？", "能帮我把灯打开吗？", "我先去买菜，帮我把灯打开。", "停"):
            with self.subTest(text=text):
                self.assertFalse(bridge.matches_voice(text))

    def test_voice_routes_single_timed_reminder(self):
        for text in ("四十分钟后提醒我吃药", "明早七点叫我起床", "下午三点半提醒一下开会"):
            with self.subTest(text=text):
                self.assertTrue(bridge.matches_voice(text))
        for text in ("提醒我一下刚才说到哪了", "三点了吗"):
            with self.subTest(text=text):
                self.assertFalse(bridge.matches_voice(text))

    def test_wakeword_switch_preserves_existing_assistant_path(self):
        import assistant
        import listen
        import speak

        text = "帮我开灯，然后点个外卖。"
        replies = []

        def handle(words, *, first, voice_dispatch=None):
            reply = voice_dispatch(words) if voice_dispatch else None
            replies.append(reply or "原来的语音回复")
            return {"ignored": False, "actions": [], "reply": replies[-1]}

        def conversation_window(_, on_text, **kwargs):
            on_text(text, True)
            return 1

        with (patch.object(speak, "ENABLED", False), patch.object(assistant, "handle", side_effect=handle) as call,
              patch.object(assistant, "load_in_background"), patch.object(assistant, "unload"),
              patch.object(assistant, "review", return_value={"message": "完成"}),
              patch.object(assistant, "print_result"), patch.object(listen, "put_whisper_to_standby"),
              patch.object(listen, "conversation_window", side_effect=conversation_window),
              patch.object(listen.status, "set"), patch.object(listen.status, "publish"),
              patch.object(bridge, "route", return_value="已交给调度员。") as route):
            with patch("builtins.input", side_effect=["", "q"]):
                listen._wakeword_loop(Mock(), True, False, Event(), None, lambda: False)
            call.assert_called_once_with(text, first=True)
            route.assert_not_called()
            settings.set(bridge.VOICE_SETTING, True)
            with patch("builtins.input", side_effect=["", "q"]):
                listen._wakeword_loop(Mock(), True, False, Event(), None, lambda: False)
            route.assert_called_once_with(text)
        self.assertEqual(replies, ["原来的语音回复", "已交给调度员。"])


if __name__ == "__main__":
    unittest.main()
