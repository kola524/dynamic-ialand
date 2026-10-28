import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from types import SimpleNamespace

from PyQt6.QtCore import Qt, QCoreApplication, QEvent, QBuffer, QIODevice
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtTest import QTest

import dyn
from telegram_notifications import TelegramNotificationReader, parse_toast, windows_filetime, MAX_TOAST_BYTES


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

    def test_large_payload_is_skipped_and_burst_keeps_latest_twenty(self):
        self.insert(1, payload=b'x' * (MAX_TOAST_BYTES + 1))
        for i in range(2, 32):
            self.insert(i, payload=toast(body=str(i)))
        messages = self.reader.poll()
        self.assertEqual([m['body'] for m in messages], [str(i) for i in range(12, 32)])
        self.assertEqual(self.reader.poll(), [])


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

    def test_finished_animations_do_not_accumulate(self):
        self.island.expand_island()
        self.finish_resize()
        for _ in range(200):
            stack = self.island.stack
            stack.slide_to_index((stack.currentIndex() + 1) % stack.count())
            stack.anim_group.setCurrentTime(320)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.island.stack.findChildren(dyn.QParallelAnimationGroup), [])
        self.assertEqual(self.island.stack.findChildren(dyn.QPropertyAnimation), [])

    def test_background_work_follows_visible_page(self):
        self.assertFalse(self.island.media_worker.load_artwork)
        self.island.expand_island()
        self.finish_resize()
        self.assertTrue(self.island.media_worker.load_artwork)
        self.island.stack.setCurrentIndex(2)
        self.assertFalse(self.island.media_worker.load_artwork)
        self.island.collapse_island()
        self.finish_resize()
        self.assertFalse(self.island.outside_click_timer.isActive())


class ArtworkMemoryTests(unittest.TestCase):
    @staticmethod
    def encoded_cover():
        image = QImage(512, 256, QImage.Format.Format_RGB32)
        image.fill(QColor('red'))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'JPEG')
        return bytes(buffer.data())

    def test_cover_is_decoded_to_small_image(self):
        image = dyn.decode_cover(self.encoded_cover())
        self.assertEqual((image.width(), image.height()), (128, 64))
        self.assertLessEqual(image.sizeInBytes(), 128 * 128 * 4)
        self.assertTrue(dyn.decode_cover(b'not an image').isNull())
        self.assertTrue(dyn.decode_cover(b'x' * (dyn.MAX_COVER_BYTES + 1)).isNull())

    def test_cover_is_only_read_once_per_track_when_player_visible(self):
        async def scenario():
            worker = dyn.MediaWorker()
            props = SimpleNamespace(title='Track', artist='Artist', album_title='Album', thumbnail=object())
            session = SimpleNamespace(
                source_app_user_model_id='Player',
                try_get_media_properties_async=AsyncMock(return_value=props),
                get_playback_info=lambda: SimpleNamespace(playback_status=4),
                get_timeline_properties=lambda: None,
            )
            manager = SimpleNamespace(get_current_session=lambda: session)
            manager_type = SimpleNamespace(request_async=AsyncMock(return_value=manager))
            with patch.object(dyn, 'MEDIA_BACKEND', 'winrt'), \
                    patch.object(dyn, 'MediaManager', manager_type, create=True), \
                    patch.object(worker, '_read_thumbnail_bytes', AsyncMock(return_value=self.encoded_cover())) as read:
                await worker._fetch_media()
                read.assert_not_awaited()
                worker.load_artwork = True
                first = await worker._fetch_media()
                self.assertIsInstance(first['thumb'], QImage)
                self.assertNotIn('thumb', await worker._fetch_media())
                read.assert_awaited_once()
                props.title = 'Next track'
                self.assertIn('thumb', await worker._fetch_media())
                self.assertEqual(read.await_count, 2)
                manager_type.request_async.assert_awaited_once()
        asyncio.run(scenario())

    def test_thumbnail_stream_is_closed_on_read_failure_and_size_limit(self):
        async def scenario():
            worker = dyn.MediaWorker()
            stream = SimpleNamespace(size=100, close=Mock())
            reference = SimpleNamespace(open_read_async=AsyncMock(return_value=stream))
            reader = SimpleNamespace(load_async=AsyncMock(side_effect=OSError('read failed')),
                                     detach_stream=Mock(), close=Mock())
            with patch.object(dyn, 'DataReader', return_value=reader, create=True):
                self.assertIsNone(await worker._read_thumbnail_bytes(reference))
                reader.close.assert_called_once()
                stream.close.assert_called_once()
            stream.size = dyn.MAX_COVER_BYTES + 1
            stream.close.reset_mock()
            with patch.object(dyn, 'DataReader', create=True) as factory:
                self.assertIsNone(await worker._read_thumbnail_bytes(reference))
                factory.assert_not_called()
                stream.close.assert_called_once()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
