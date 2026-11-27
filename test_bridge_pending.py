"""调度员挂起任务：时间解析、条件匹配、存取、到点派出。不打开麦克风、不调模型。"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import bridge
import bridge_pending as bp

NOW = datetime(2026, 11, 25, 14, 0, 0)


class ParseWhenTests(unittest.TestCase):
    def check(self, when, expected):
        self.assertEqual(bp.parse_when(when, NOW), expected, when)

    def test_relative(self):
        self.check("四十分钟后", NOW + timedelta(minutes=40))
        self.check("40分钟以后", NOW + timedelta(minutes=40))
        self.check("一个小时后", NOW + timedelta(hours=1))
        self.check("一小时以后", NOW + timedelta(hours=1))
        self.check("半小时后", NOW + timedelta(minutes=30))
        self.check("一个半小时之后", NOW + timedelta(minutes=90))
        self.check("两小时后", NOW + timedelta(hours=2))
        self.check("二十五分钟后", NOW + timedelta(minutes=25))

    def test_absolute(self):
        self.check("明天早上七点", datetime(2026, 11, 26, 7, 0))
        self.check("明天下午三点", datetime(2026, 11, 26, 15, 0))
        self.check("今天晚上8点半", datetime(2026, 11, 25, 20, 30))
        self.check("下午3点", datetime(2026, 11, 25, 15, 0))
        self.check("晚上十点一刻", datetime(2026, 11, 25, 22, 15))
        self.check("中午12点", datetime(2026, 11, 26, 12, 0))   # 今天中午已过：明天
        self.check("七点", datetime(2026, 11, 26, 7, 0))         # 没说哪天、已过：明天
        self.check("今晚9点", datetime(2026, 11, 25, 21, 0))
        self.check("16:30", datetime(2026, 11, 25, 16, 30))

    def test_conditions_are_not_times(self):
        for when in ("洗完澡后", "洗完澡之后", "如果今天下雨的话", "我回来以后", "马上"):
            self.check(when, None)


class ConditionTests(unittest.TestCase):
    def test_met(self):
        self.assertTrue(bp.condition_met("洗完澡后", "我洗完了"))
        self.assertTrue(bp.condition_met("洗完澡之后", "澡洗好了"))
        self.assertTrue(bp.condition_met("我回家以后", "我到家了"))
        self.assertTrue(bp.condition_met("吃完饭后", "饭吃完了"))

    def test_not_met(self):
        self.assertFalse(bp.condition_met("洗完澡后", "我要去洗澡"))      # 还没完
        self.assertFalse(bp.condition_met("洗完澡后", "饭吃完了"))        # 说的是别的事
        self.assertFalse(bp.condition_met("如果今天下雨的话", "好了"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(bp, "PENDING_FILE", str(Path(directory) / "pending.json")))

    def test_add_list_cancel(self):
        timed = bp.add({"to": "提醒", "do": "吃药", "when": "四十分钟后"}, NOW)
        cond = bp.add({"to": "家电", "do": "开电视", "when": "洗完澡后"}, NOW)
        self.assertEqual(timed["due"], "2026-11-25T14:40:00")
        self.assertIsNone(timed["condition"])
        self.assertEqual(cond["condition"], "洗完澡后")
        listing = bp.listing()
        self.assertIn("吃药", listing)
        self.assertIn("等“洗完澡后”", listing)
        self.assertIn("已取消", bp.cancel(2))
        self.assertIn("没有第 5 条", bp.cancel(5))
        self.assertNotIn("开电视", bp.listing())

    def test_due_and_conditions_are_taken_once(self):
        bp.add({"to": "提醒", "do": "吃药", "when": "四十分钟后"}, NOW)
        bp.add({"to": "家电", "do": "开电视", "when": "洗完澡后"}, NOW)
        self.assertEqual(bp.due_now(NOW + timedelta(minutes=39)), [])
        self.assertEqual([i["do"] for i in bp.due_now(NOW + timedelta(minutes=41))], ["吃药"])
        self.assertEqual(bp.due_now(NOW + timedelta(minutes=50)), [])
        self.assertEqual(bp.conditions_met("我要去洗澡了"), [])
        self.assertEqual([i["do"] for i in bp.conditions_met("我洗完了")], ["开电视"])
        self.assertEqual(bp.listing(), "现在没有挂起的任务。")

    def test_spoken_words(self):
        self.assertEqual(bp.spoken({"to": "提醒", "do": "吃药"}), "提醒你：吃药。")
        self.assertEqual(bp.spoken({"to": "提醒", "do": "提醒主人吃药"}), "提醒你：吃药。")
        self.assertEqual(bp.spoken({"to": "提醒", "do": "叫我起床"}), "提醒你：起床。")
        self.assertEqual(bp.spoken({"to": "提醒", "do": "提醒"}), "提醒你：提醒。")
        self.assertIn("家电还没接好", bp.spoken({"to": "家电", "do": "开电视"}, "条件满足了"))


class DispatchTests(unittest.TestCase):
    """调度员把带时间 / 带条件的任务交给 bridge_pending，到时候派出去。"""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(bp, "PENDING_FILE", str(Path(directory) / "pending.json")))
        import groupchat
        self.posts = []
        self.enterContext(patch.object(groupchat, "members", return_value=[]))
        self.enterContext(patch.object(groupchat, "dispatch_generation", return_value=0))
        self.enterContext(patch.object(groupchat, "post_dispatch",
                                       side_effect=lambda msgs, targets, hint, **kw: self.posts.append(msgs) or {"asked": []}))

    def plan(self, *tasks, reply="好的"):
        return {"text": "", "realtime": False, "reply": reply,
                "tasks": [{"to": t[0], "do": t[1], "when": t[2], "ask": ""} for t in tasks]}

    def test_reminder_is_local_and_scheduled(self):
        reply = bridge.dispatch(self.plan(("提醒", "叫主人起床", "四十分钟后"), reply="已经为你设置好了"))
        self.assertTrue(reply.startswith("好的，已定好时间"), reply)
        self.assertNotIn("未接入", "\n".join(self.posts[0]))
        self.assertIn("已定好时间", "\n".join(self.posts[0]))
        self.assertIn("叫主人起床", bp.listing())

    def test_condition_then_fire_by_voice(self):
        bridge.dispatch(self.plan(("家电", "开灯", "马上"), ("家电", "开电视", "洗完澡后")))
        self.assertIn("等“洗完澡后”", bp.listing())
        with patch.object(bridge, "voice_enabled", return_value=True):
            self.assertIsNone(bridge.voice_turn("今天天气不错"))
            said = bridge.voice_turn("我洗完了")
        self.assertIn("开电视", said)
        self.assertIn("家电还没接好", said)
        self.assertIn("条件满足了", "\n".join(self.posts[-1]))
        self.assertEqual(bp.listing(), "现在没有挂起的任务。")

    def test_voice_turn_off_does_nothing(self):
        bp.add({"to": "家电", "do": "开电视", "when": "洗完澡后"})
        with patch.object(bridge, "voice_enabled", return_value=False):
            self.assertIsNone(bridge.voice_turn("我洗完了"))
        self.assertIn("开电视", bp.listing())

    def test_fire_speaks_reminder(self):
        item = bp.add({"to": "提醒", "do": "吃药", "when": "四十分钟后"}, NOW)
        with patch.object(bp, "_say_when_idle") as say, patch.object(bp.threading, "Thread") as thread:
            thread.side_effect = lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})()
            text = bp.fire([item])
        self.assertEqual(text, "提醒你：吃药。")
        say.assert_called_once_with("提醒你：吃药。")


if __name__ == "__main__":
    unittest.main()
