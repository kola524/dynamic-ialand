"""Read Telegram's Windows toast notifications without changing their state.

The notification database is an internal Windows format. Keep failures visible
in the UI and retry: its location/schema/access may change with Windows updates.
Only Telegram rows are queried; no messages are written to disk or sent online.
"""

import hashlib
import os
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from PyQt6.QtCore import QThread, pyqtSignal


FILETIME_EPOCH = 11644473600
MAX_TOAST_BYTES = 64 * 1024


def windows_filetime():
    return (time.time_ns() // 100) + FILETIME_EPOCH * 10_000_000


def parse_toast(payload):
    """Return only visible text, including a group/channel subtitle if present."""
    if not payload or len(payload) > MAX_TOAST_BYTES:
        return None
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, ValueError, TypeError):
        return None
    # Restrict extraction to the visual binding (exclude reply/action labels).
    for binding in root.findall("./visual/binding"):
        texts = ["".join(node.itertext()).strip() for node in binding.findall("text")]
        texts = [text for text in texts if text]
        if texts:
            return {
                "sender": texts[0][:512],
                "body": "\n".join(texts[1:])[:16384] or "Текст скрыт в уведомлении Telegram.",
            }
    return None


class TelegramNotificationReader:
    def __init__(self, path=None, started_at=None):
        self.path = Path(path) if path is not None else (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft/Windows/Notifications/wpndatabase.db"
        )
        self.started_at = windows_filetime() if started_at is None else started_at
        self.seen = OrderedDict()

    def poll(self):
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        # mode=ro also prevents accidentally creating a new notification DB.
        with closing(sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3
        )) as connection:
            connection.execute("PRAGMA cache_size = -256")
            # Sort only small identifiers, never a batch of full XML payloads.
            rows = connection.execute('''
                SELECT n.[Order], n.Id, n.HandlerId, n.ArrivalTime
                FROM Notification AS n
                JOIN NotificationHandler AS h ON h.RecordId = n.HandlerId
                WHERE (lower(h.PrimaryId) LIKE '%telegram%'
                       OR lower(h.PrimaryId) LIKE '%tdesktop%')
                  AND lower(n.Type) = 'toast'
                  AND n.ArrivalTime >= ?
                  AND (n.ExpiryTime IS NULL OR n.ExpiryTime = 0 OR n.ExpiryTime > ?)
                ORDER BY n.ArrivalTime DESC, n.[Order] DESC
                LIMIT 200
            ''', (self.started_at, windows_filetime())).fetchall()

            messages = deque(maxlen=20)
            for order, notification_id, handler_id, arrival in reversed(rows):
                row = connection.execute(
                    'SELECT Payload FROM Notification WHERE [Order] = ? AND length(Payload) <= ?',
                    (order, MAX_TOAST_BYTES)
                ).fetchone()
                if row is None or not isinstance(row[0], (bytes, str)):
                    continue
                payload = row[0]
                raw = payload.encode("utf-8") if isinstance(payload, str) else payload
                key = (handler_id, notification_id, arrival, hashlib.sha256(raw).digest())
                if key in self.seen:
                    continue
                self.seen[key] = None
                while len(self.seen) > 2048:
                    self.seen.popitem(last=False)
                message = parse_toast(payload)
                if message is None:
                    continue
                try:
                    received = datetime.fromtimestamp(arrival / 10_000_000 - FILETIME_EPOCH)
                except (ValueError, OverflowError, OSError):
                    received = datetime.now()
                message.update(key=key, time=received.strftime("%H:%M"))
                messages.append(message)
        return list(messages)


class TelegramNotificationWorker(QThread):
    messages_received = pyqtSignal(list)
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reader = TelegramNotificationReader()

    def run(self):
        last_status = None
        while not self.isInterruptionRequested():
            try:
                messages = self.reader.poll()
                status = "Ожидание новых уведомлений Telegram"
                if messages and not self.isInterruptionRequested():
                    self.messages_received.emit(messages)
            except FileNotFoundError:
                status = "Центр уведомлений Windows пока недоступен"
            except (sqlite3.Error, OSError):
                status = "Нет доступа к уведомлениям Windows. Повторяем подключение…"
            if status != last_status:
                self.status_changed.emit(status)
                last_status = status
            # Short sleeps make shutdown responsive without blocking the GUI.
            for _ in range(10):
                if self.isInterruptionRequested():
                    return
                self.msleep(100)
