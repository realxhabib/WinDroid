"""Android's notifications on Windows (notifications.py): which ones show, what the toast says, and
what a click or a reply does. No Android and no real toasts: the watch and the toaster are stand-ins."""
import types
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import features, notifications
from androiddesktop.notifications import (FOREGROUND_SERVICE, GROUP_SUMMARY, ONGOING, ONLY_ALERT_ONCE, Notifications,
                                          tag_for, toast_xml, worth_showing)
from androiddesktop.vm import OFF, RUNNING
from tests.test_features import temp_state


def message(**extra) -> dict:
    n = {"event": "posted", "key": "0|com.example.chat|7|null|10123", "package": "com.example.chat",
         "initial": False, "update": False, "flags": 0, "priority": 0, "importance": 4, "title": "Sam",
         "text": "Dinner at 8?", "category": "msg", "canReply": True, "replyLabel": "Reply"}
    n.update(extra)
    return n


class Showing(unittest.TestCase):
    def test_a_message_shows(self):
        self.assertTrue(worth_showing(message()))

    def test_what_android_wouldnt_interrupt_for(self):
        self.assertFalse(worth_showing(message(initial=True)))  # there before WinDroid started
        self.assertFalse(worth_showing(message(importance=2)))  # a silent channel
        self.assertFalse(worth_showing(message(flags=ONGOING)))
        self.assertFalse(worth_showing(message(flags=FOREGROUND_SERVICE)))
        self.assertFalse(worth_showing(message(flags=GROUP_SUMMARY)))
        self.assertFalse(worth_showing(message(update=True, flags=ONLY_ALERT_ONCE)))
        self.assertFalse(worth_showing(message(category="transport")))
        self.assertFalse(worth_showing(message(package="com.android.systemui")))
        self.assertFalse(worth_showing(message(title=" ", text="")))

    def test_updates_alert_again_unless_told_not_to(self):
        self.assertTrue(worth_showing(message(update=True)))
        self.assertTrue(worth_showing(message(flags=ONLY_ALERT_ONCE)))  # its first post still alerts

    def test_without_importance_the_old_priority_decides(self):
        n = message()
        del n["importance"]
        self.assertTrue(worth_showing(n))
        self.assertFalse(worth_showing({**n, "priority": -1}))


class Toast(unittest.TestCase):
    def test_text_is_escaped_and_reply_offered(self):
        xml = toast_xml(message(title="Sam & <Alex>", text='"hi"', conversation="Weekend"), "Chat", None, None)
        self.assertIn("<text>Sam &amp; &lt;Alex&gt;</text>", xml)
        self.assertIn('<text placement="attribution">Chat · Weekend</text>', xml)
        self.assertIn('<input id="reply" type="text" placeHolderContent="Reply"/>', xml)
        self.assertIn('arguments="reply" hint-inputId="reply"', xml)
        self.assertTrue(xml.startswith('<toast launch="open"'))

    def test_no_reply_box_without_a_reply_action(self):
        xml = toast_xml(message(canReply=False, title="", text="Your order shipped"), "Shop", None, None)
        self.assertNotIn("<actions>", xml)
        self.assertIn("<text>Shop</text><text>Your order shipped</text>", xml)  # the app's name as the title

    def test_pictures(self):
        with mock.patch.object(Path, "exists", lambda self: True):
            photo = toast_xml(message(), "Chat", Path("C:/pics/a.png"), Path("C:/icons/chat.png"))
            icon = toast_xml(message(), "Chat", None, Path("C:/icons/chat.png"))
        self.assertIn('hint-crop="circle"', photo)
        self.assertIn("a.png", photo)
        self.assertNotIn("hint-crop", icon)
        self.assertIn("chat.png", icon)

    def test_tags_fit_windows(self):
        self.assertLessEqual(len(tag_for("0|" + "x" * 300)), 64)
        self.assertNotEqual(tag_for("a"), tag_for("b"))


class Flow(unittest.TestCase):
    def setUp(self):
        temp_state(self)
        self.front: set[str] = set()
        self.opened: list = []
        self.notices: list[str] = []
        self.n = Notifications(label=lambda p: "Chat", icon=lambda p: None, in_front=lambda p: p in self.front,
                               open=lambda p, then: self.opened.append((p, then)), notice=self.notices.append,
                               app_id="AndroidDesktop.Launcher")
        self.n.toaster = mock.Mock()
        self.n.watch = mock.Mock()
        p = mock.patch.object(notifications, "log")
        p.start()
        self.addCleanup(p.stop)

    def post(self, **extra):
        self.n._event(message(**extra))

    def test_shown_then_removed_with_android(self):
        self.post()
        tag = tag_for(message()["key"])
        self.n.toaster.show.assert_called_once()
        self.assertEqual(self.n.toaster.show.call_args.args[0], tag)
        self.n._event({"event": "removed", "key": message()["key"]})
        self.n.toaster.remove.assert_called_once_with(tag)

    def test_not_while_its_window_is_in_front(self):
        self.front.add("com.example.chat")
        self.post()
        self.n.toaster.show.assert_not_called()

    def test_click_opens_the_window_then_the_tap_action(self):
        self.post()
        self.n._activated(tag_for(message()["key"]), "open", "")
        package, then = self.opened[0]
        self.assertEqual(package, "com.example.chat")
        then(7)
        self.n.watch.call.assert_called_once_with("open", key=message()["key"], display=7)

    def test_reply(self):
        self.post()
        tag = tag_for(message()["key"])
        self.n._activated(tag, "reply", "  ")  # nothing typed
        self.n.watch.call.assert_not_called()
        self.n._activated(tag, "reply", "See you there")
        self.n.watch.call.assert_called_once_with("reply", key=message()["key"], text="See you there")
        self.assertEqual(self.opened, [])
        self.post(update=True, text="You: See you there")  # the app shows the reply: no new popup
        self.assertEqual(self.n.toaster.show.call_count, 1)

    def test_a_failed_reply_is_told(self):
        self.post()
        self.n.watch.call.side_effect = RuntimeError("that notification is gone")
        self.n._activated(tag_for(message()["key"]), "reply", "ok")
        self.assertEqual(self.notices, ["Couldn't send that reply to Chat: that notification is gone"])

    def test_off(self):
        self.n.set_enabled(False)
        self.assertFalse(notifications.enabled())
        self.n.android_up()
        self.assertFalse(self.n._wanted)
        self.n.set_enabled(True)
        self.assertTrue(notifications.enabled())

    def test_android_going_away_takes_the_toasts(self):
        self.post()
        self.n.android_down()
        self.n.watch.stop.assert_called_once()
        self.n.toaster.clear.assert_called_once()
        self.n._activated(tag_for(message()["key"]), "open", "")
        self.assertEqual(self.opened, [])


class Card(unittest.TestCase):
    def test_card(self):
        self.assertFalse(features.notifications_card(False, False, RUNNING)["on"])
        self.assertIn("show on Windows", features.notifications_card(True, True, RUNNING)["why"])
        self.assertIn("once Android is running", features.notifications_card(True, False, OFF)["why"])
        self.assertIn("aren't being read", features.notifications_card(True, False, RUNNING)["why"])

    def test_switch(self):
        temp_state(self)
        agent = types.SimpleNamespace(vm=types.SimpleNamespace(state=RUNNING), notifications=mock.Mock())
        f = features.Features(agent)
        self.assertTrue(f.set_notifications(True))
        agent.notifications.set_enabled.assert_called_once_with(True)
        agent.notifications.android_up.assert_called_once()


if __name__ == "__main__":
    unittest.main()
