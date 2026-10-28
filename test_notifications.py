import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

import dyn
from telegram_notifications import TelegramNotificationReader, parse_toast, windows_filetime


def toast(sender="Анна", body="Привет!", subtitle=""):
    return (f'<toast><visual><binding template="ToastGeneric"><text>{sender}</text>'
            f'<text>{subtitle}</text><text>{body}</text></binding></visual>'
            '<actions><action content="Reply"/></actions></toast>').encode("utf-8")


class NotificationReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "notifications.db"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT);
            INSERT INTO NotificationHandler VALUES (1, 'Telegram.TelegramDesktop');
            INSERT INTO NotificationHandler VALUES (2, 'Other.Application');
            INSERT INTO NotificationHandler VALUES (3, 'TelegramMessengerLLP.TelegramDesktop_test!App');
            CREATE TABLE Notification ([Order] INTEGER PRIMARY KEY, Id INTEGER,
                HandlerId INTEGER, ArrivalTime INTEGER, Payload BLOB, Type TEXT, ExpiryTime INTEGER);
        ''')
        self.started = windows_filetime()
        self.reader = TelegramNotificationReader(self.path, started_at=self.started)

    def insert(self, row_id, handler=1, arrival=None, payload=None, expiry=0, kind="toast"):
        self.db.execute("INSERT INTO Notification VALUES (?, ?, ?, ?, ?, ?, ?)", (
            row_id, row_id, handler, self.started if arrival is None else arrival,
            toast() if payload is None else payload, kind, expiry
        ))
        self.db.commit()

    def test_text_and_group_subtitle_without_action_labels(self):
        parsed = parse_toast(toast("Рабочая группа", "Встреча в 10 &amp; кофе", "Анна"))
        self.assertEqual(parsed["sender"], "Рабочая группа")
        self.assertEqual(parsed["body"], "Анна\nВстреча в 10 & кофе")
        self.assertIsNone(parse_toast(b"broken XML"))
        self.assertIn("скрыт", parse_toast(toast(body=""))["body"])

    def test_filters_old_expired_other_apps_and_non_toasts(self):
        self.insert(1, handler=2)
        self.insert(2, arrival=self.started - 1)
        self.insert(3, expiry=self.started - 1)
        self.insert(4, kind="tile")
        self.insert(5, handler=3)
        self.insert(6)
        self.assertEqual(len(self.reader.poll()), 2)
        self.assertEqual(self.reader.poll(), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM Notification").fetchone()[0], 6)

    def test_changed_toast_and_same_timestamp_are_not_lost(self):
        self.insert(1)
        self.assertEqual(len(self.reader.poll()), 1)
        self.db.execute("UPDATE Notification SET Payload=? WHERE Id=1", (toast(body="Второе"),))
        self.db.commit()
        self.insert(2, payload=toast(body="Третье"))
        self.assertEqual([m["body"] for m in self.reader.poll()], ["Второе", "Третье"])
        self.assertEqual(self.reader.poll(), [])

    def test_malformed_notification_does_not_hide_valid_one(self):
        self.insert(1, payload=b"broken XML")
        self.insert(2)
        self.assertEqual(len(self.reader.poll()), 1)

    def test_missing_database_is_not_created(self):
        path = self.path.with_name("missing.db")
        with self.assertRaises(FileNotFoundError):
            TelegramNotificationReader(path).poll()
        self.assertFalse(path.exists())


class IslandInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = dyn.QApplication.instance() or dyn.QApplication([])

    def setUp(self):
        with patch.object(dyn.MediaWorker, "start"), patch.object(dyn.TelegramNotificationWorker, "start"):
            self.island = dyn.DesktopIslandWidget()
        self.island.pc_timer.stop()
        self.island.outside_click_timer.stop()
        self.island.show()
        self.app.processEvents()
        self.addCleanup(self.island.close)

    def finish_resize(self):
        self.island.resize_anim.setCurrentTime(self.island.resize_anim.duration())
        self.app.processEvents()

    def click(self, button):
        point = button.mapTo(self.island, button.rect().center())
        target = self.island.childAt(point)
        self.assertIs(target, button, "A container intercepted the button click")
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=target.mapFrom(self.island, point))

    def messages(self, count=1):
        return [dict(key=i, sender=f"Отправитель {i}", body=f"Сообщение {i}", time="12:30")
                for i in range(count)]

    def test_all_media_buttons_receive_clicks_and_keep_island_open(self):
        self.island.expand_island()
        self.finish_resize()
        with patch.object(self.island.media_worker, "trigger_action") as action:
            for button, command in [(self.island.btn_prev, "prev"),
                                    (self.island.btn_play, "play_pause"),
                                    (self.island.btn_next, "next")]:
                self.click(button)
                action.assert_called_with(command)
                self.assertTrue(self.island.is_expanded)

    def test_message_survives_media_refresh_and_opens_on_click(self):
        self.island.receive_telegram_messages(self.messages())
        self.assertFalse(self.island.is_expanded)
        self.island.update_player_ui(dict(title="Music", artist="Artist", source="Player",
                                         is_playing=True, pos=1, end=10, thumb=None))
        self.island.update_pc_stats()
        self.assertEqual(self.island.mini_icon.text(), "TG")
        self.assertEqual(self.island.mini_time.text(), "+1")
        QTest.mouseClick(self.island, Qt.MouseButton.LeftButton)
        self.finish_resize()
        self.assertIs(self.island.stack.currentWidget(), self.island.telegram_page)
        self.assertEqual(self.island.telegram_sender.text(), "Отправитель 0")
        self.assertEqual(self.island.telegram_body.toPlainText(), "Сообщение 0")
        self.assertFalse(self.island.telegram_unseen)
        self.island.collapse_island()
        self.finish_resize()
        self.assertEqual(self.island.mini_title.text(), "Music")

    def test_history_navigation_and_unseen_count(self):
        self.island.receive_telegram_messages(self.messages(3))
        self.island.expand_island()
        self.finish_resize()
        self.assertEqual(self.island.telegram_unseen, {0, 1})
        self.click(self.island.telegram_prev)
        self.assertEqual(self.island.telegram_body.toPlainText(), "Сообщение 1")
        self.assertEqual(self.island.telegram_unseen, {0})
        self.assertTrue(self.island.is_expanded)
        self.click(self.island.telegram_next)
        self.assertEqual(self.island.telegram_body.toPlainText(), "Сообщение 2")

    def test_burst_is_bounded_and_open_player_is_not_interrupted(self):
        self.island.expand_island()
        self.finish_resize()
        self.island.receive_telegram_messages(self.messages(30))
        self.assertEqual(len(self.island.telegram_messages), 20)
        self.assertEqual(len(self.island.telegram_unseen), 20)
        self.assertEqual(self.island.stack.currentIndex(), 0)


if __name__ == "__main__":
    unittest.main()
