import sys
import ctypes
import calendar
import asyncio
import time
import psutil
from contextlib import suppress
from datetime import datetime

from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QRect, QRectF, QSize, QBuffer, QIODevice, QThread, pyqtSignal, QEvent,
    QVariantAnimation, QPropertyAnimation, QEasingCurve, QParallelAnimationGroup
)
from PyQt6.QtGui import (QColor, QPainter, QPainterPath, QPixmap, QImage,
                        QImageReader, QImageIOHandler, QLinearGradient, QPen)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QStackedWidget, QProgressBar, QPlainTextEdit, QFrame
)
from telegram_notifications import TelegramNotificationWorker

# Поддержка как нового пакета winrt, так и старого winsdk
MEDIA_BACKEND = None
try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
    from winrt.windows.storage.streams import DataReader
    MEDIA_BACKEND = "winrt"
except ImportError:
    try:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
        from winsdk.windows.storage.streams import DataReader
        MEDIA_BACKEND = "winsdk"
    except ImportError:
        MEDIA_BACKEND = None


# Decode a small image before sending it to the GUI; never queue full-size covers.
MAX_COVER_BYTES = 4 * 1024 * 1024


def decode_cover(raw_bytes):
    if not raw_bytes or len(raw_bytes) > MAX_COVER_BYTES:
        return QImage()
    buffer = QBuffer()
    buffer.setData(raw_bytes)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    dimensions = reader.size()
    if not dimensions.isValid():
        return QImage()
    pixels = dimensions.width() * dimensions.height()
    native_scaling = reader.supportsOption(QImageIOHandler.ImageOption.ScaledSize)
    if pixels > (64_000_000 if native_scaling else 8_000_000):
        return QImage()
    reader.setScaledSize(dimensions.scaled(QSize(128, 128), Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


# --- Поток для асинхронного чтения медиа из Windows ---
class MediaWorker(QThread):
    media_updated = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.running = True
        self._action = None
        self._loop = None
        self._task = None
        self._manager = None
        self._track_key = None
        self._thumbnail_loaded = False
        self._thumbnail_retry_after = 0
        self.load_artwork = False

    def stop(self):
        self.running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_pending)
            except RuntimeError:
                pass  # The worker has already closed its event loop.

    def _cancel_pending(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def trigger_action(self, action_name):
        self._action = action_name

    async def _read_thumbnail_bytes(self, thumb_ref):
        stream = reader = None
        try:
            stream = await thumb_ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > MAX_COVER_BYTES:
                return None

            reader = DataReader(stream)
            await reader.load_async(size)

            if MEDIA_BACKEND == "winrt":
                try:
                    buf = bytearray(size)
                    reader.read_bytes(buf)
                    return bytes(buf)
                except TypeError:
                    return bytes(reader.read_byte() for _ in range(size))
            else:
                buf = bytearray(size)
                reader.read_bytes(buf)
                return bytes(buf)
        except Exception:
            return None
        finally:
            if reader is not None:
                with suppress(Exception):
                    reader.detach_stream()
                with suppress(Exception):
                    reader.close()
            if stream is not None:
                with suppress(Exception):
                    stream.close()

    async def _fetch_media(self):
        if MEDIA_BACKEND is None:
            return {
                "title": "Нет модуля winrt",
                "artist": "pip install winrt-Windows.Media.Control",
                "source": "Система",
                "is_playing": False,
                "pos": 0, "end": 100, "thumb": None
            }

        try:
            if self._manager is None:
                self._manager = await MediaManager.request_async()
            session = self._manager.get_current_session()
            if not session:
                self._track_key = None
                self._thumbnail_loaded = False
                return {
                    "title": "Ничего не играет",
                    "artist": "Включите музыку в браузере",
                    "source": "Ожидание медиасессии",
                    "is_playing": False,
                    "pos": 0, "end": 100, "thumb": None
                }

            if self._action == "play_pause":
                await session.try_toggle_play_pause_async()
            elif self._action == "next":
                await session.try_skip_next_async()
            elif self._action == "prev":
                await session.try_skip_previous_async()
            self._action = None

            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            timeline = session.get_timeline_properties()

            title = props.title if props and props.title else "Без названия"
            artist = props.artist if props and props.artist else "Неизвестный исполнитель"
            source_app = str(session.source_app_user_model_id or "Медиаплеер")
            if "chrome" in source_app.lower():
                source_app = "Chrome"
            elif "spotify" in source_app.lower():
                source_app = "Spotify"
            elif "yandex" in source_app.lower():
                source_app = "Яндекс Музыка"

            status_val = int(info.playback_status) if info else 0
            is_playing = (status_val == 4)

            pos = timeline.position.total_seconds() if timeline and timeline.position else 0
            end = timeline.end_time.total_seconds() if timeline and timeline.end_time else 0

            track_key = (str(session.source_app_user_model_id), title, artist,
                         getattr(props, "album_title", ""))
            artwork = {}
            if track_key != self._track_key:
                self._track_key = track_key
                self._thumbnail_loaded = False
                self._thumbnail_retry_after = 0
                artwork["thumb"] = None  # Clear the previous track's cover.
            if (self.load_artwork and not self._thumbnail_loaded and props
                    and props.thumbnail and time.monotonic() >= self._thumbnail_retry_after):
                self._thumbnail_retry_after = time.monotonic() + 10
                raw = await self._read_thumbnail_bytes(props.thumbnail)
                cover = decode_cover(raw)
                if not cover.isNull():
                    artwork["thumb"] = cover
                    self._thumbnail_loaded = True

            return {
                "title": title,
                "artist": artist,
                "source": f"{'Играет' if is_playing else 'Приостановлено'} в {source_app}",
                "is_playing": is_playing,
                "pos": pos,
                "end": end,
                **artwork
            }
        except Exception as e:
            self._manager = None
            self._track_key = None
            return {
                "title": "Ожидание плеера...",
                "artist": str(e)[:32],
                "source": "Windows Media",
                "is_playing": False,
                "pos": 0, "end": 100, "thumb": None
            }

    def run(self):
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        while self.running:
            try:
                self._task = loop.create_task(asyncio.wait_for(self._fetch_media(), timeout=5))
                data = loop.run_until_complete(self._task)
                self.media_updated.emit(data)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            finally:
                self._task = None
            for _ in range(8):
                if not self.running:
                    break
                self.msleep(100)
        self._loop = None
        loop.close()


class MediaButton(QPushButton):
    """Vector controls: consistent shapes at any Windows display scale."""

    def __init__(self, symbol, label, primary=False):
        super().__init__()
        self.symbol = symbol
        self.primary = primary
        self.setFixedSize(42 if primary else 34, 42 if primary else 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.set_symbol(symbol, label)

    def set_symbol(self, symbol, label):
        if self.symbol == symbol and self.toolTip() == label:
            return
        self.symbol = symbol
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.primary:
            fill = QLinearGradient(0, 0, 0, self.height())
            fill.setColorAt(0, QColor("#ff80b5" if self.underMouse() else "#ff5a9f"))
            fill.setColorAt(1, QColor("#d91b69" if self.isDown() else "#ed287c"))
            painter.setBrush(fill)
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setBrush(QColor("#45273b" if self.isDown() else
                                    "#34303d" if self.underMouse() else "#25252e"))
            painter.setPen(QPen(QColor("#45404e"), 1))
        painter.drawEllipse(bounds)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ffc0db"), 1.5))
            painter.drawEllipse(bounds.adjusted(2, 2, -2, -2))

        painter.translate(self.width() / 2, self.height() / 2 + (1 if self.isDown() else 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff" if self.primary else "#eeeaf2"))
        path = QPainterPath()
        if self.symbol == "pause":
            path.addRoundedRect(QRectF(-6, -7, 4, 14), 1.2, 1.2)
            path.addRoundedRect(QRectF(2, -7, 4, 14), 1.2, 1.2)
        elif self.symbol == "play":
            path.moveTo(-4, -8)
            path.lineTo(8, 0)
            path.lineTo(-4, 8)
            path.closeSubpath()
        else:
            if self.symbol == "prev":
                painter.scale(-1, 1)
            path.moveTo(-6, -6)
            path.lineTo(3, 0)
            path.lineTo(-6, 6)
            path.closeSubpath()
            path.addRoundedRect(QRectF(4, -6, 2.5, 12), 1, 1)
        painter.drawPath(path)


# --- Плавный минималистичный календарь в стиле iOS ---
class SmoothCalendarGrid(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(6, 2, 6, 2)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(2)
        self.build_calendar()

    def build_calendar(self):
        now = datetime.now()
        days_header = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

        for col, name in enumerate(days_header):
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            color = "#ff2a85" if col >= 5 else "#78788a"
            lbl.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {color};")
            lbl.setFixedSize(24, 18)
            self.grid.addWidget(lbl, 0, col)

        cal = calendar.Calendar(firstweekday=0)
        month_weeks = cal.monthdayscalendar(now.year, now.month)

        for row_idx, week in enumerate(month_weeks):
            for col_idx, day in enumerate(week):
                lbl = QLabel("" if day == 0 else str(day))
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setFixedSize(24, 21)

                if day == now.day:
                    # Активный сегодняшний день — розовая капсула
                    lbl.setStyleSheet(
                        "background-color: #ff2a85; color: #ffffff; "
                        "border-radius: 10px; font-size: 11px; font-weight: 800;"
                    )
                elif day != 0:
                    text_color = "#ff66a8" if col_idx >= 5 else "#dcdce6"
                    lbl.setStyleSheet(
                        f"color: {text_color}; font-size: 11px; font-weight: 500; background: transparent;"
                    )

                self.grid.addWidget(lbl, row_idx + 1, col_idx)


# --- Стек с плавной вертикальной анимацией сдвига ---
class SlidingStackedWidget(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._animating = False
        self.anim_group = None

    def slide_to_index(self, next_idx, direction="down"):
        if self._animating or next_idx == self.currentIndex():
            return

        self._animating = True
        curr_widget = self.currentWidget()
        next_widget = self.widget(next_idx)

        w, h = self.width(), self.height()
        offset_y = h if direction == "down" else -h

        next_widget.setGeometry(0, offset_y, w, h)
        next_widget.show()
        next_widget.raise_()

        self.anim_group = QParallelAnimationGroup(self)

        anim_curr = QPropertyAnimation(curr_widget, b"pos", self)
        anim_curr.setDuration(320)
        anim_curr.setStartValue(QPoint(0, 0))
        anim_curr.setEndValue(QPoint(0, -offset_y))
        anim_curr.setEasingCurve(QEasingCurve.Type.OutQuart)

        anim_next = QPropertyAnimation(next_widget, b"pos", self)
        anim_next.setDuration(320)
        anim_next.setStartValue(QPoint(0, offset_y))
        anim_next.setEndValue(QPoint(0, 0))
        anim_next.setEasingCurve(QEasingCurve.Type.OutQuart)

        self.anim_group.addAnimation(anim_curr)
        self.anim_group.addAnimation(anim_next)

        def on_finished():
            self.setCurrentIndex(next_idx)
            curr_widget.hide()
            curr_widget.move(0, 0)
            self._animating = False
            group = self.anim_group
            self.anim_group = None
            group.deleteLater()

        self.anim_group.finished.connect(on_finished)
        self.anim_group.start()


# --- Главный виджет Dynamic Island ---
class DesktopIslandWidget(QWidget):
    COLLAPSED_W, COLLAPSED_H = 240, 36
    EXPANDED_W, EXPANDED_H = 370, 175
    TOP_MARGIN = 8

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Desktop Island")
        self.is_expanded = False
        self._was_outside_pressed = False
        self._closing = False
        self._media_title = "Ничего не играет"
        self._media_playing = False
        self._mini_telegram_mode = None
        self.telegram_messages = []
        self.telegram_unseen = set()
        self.telegram_index = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.init_collapsed_view()
        self.init_expanded_view()
        self.apply_styles()

        self._make_children_click_through(self.collapsed_widget)
        self._make_children_click_through(self.expanded_widget)

        # Аниматор геометрии
        self.resize_anim = QVariantAnimation(self)
        self.resize_anim.valueChanged.connect(self._on_anim_step)
        self.resize_anim.finished.connect(self._on_anim_finished)

        # Стартуем в режиме полоски
        start_rect = self.calc_target_rect(expanded=False)
        self.expanded_widget.hide()
        self.collapsed_widget.setGeometry(0, 0, self.COLLAPSED_W, self.COLLAPSED_H)
        self.collapsed_widget.show()
        self.setGeometry(start_rect)

        self.media_worker = MediaWorker()
        self.media_worker.media_updated.connect(self.update_player_ui)
        self.media_worker.start()

        self.telegram_worker = TelegramNotificationWorker(self)
        self.telegram_worker.messages_received.connect(self.receive_telegram_messages)
        self.telegram_worker.status_changed.connect(self.telegram_status.setText)
        self.telegram_worker.start()

        self.pc_timer = QTimer(self)
        self.pc_timer.timeout.connect(self.update_pc_stats)
        self.pc_timer.start(5000)
        self.update_pc_stats()

        # Глобальный детектор клика мимо окна (например, по рабочему столу)
        self.outside_click_timer = QTimer(self)
        self.outside_click_timer.timeout.connect(self._check_global_outside_click)

    def _make_children_click_through(self, parent_widget):
        for child in parent_widget.findChildren(QWidget):
            # Прозрачный контейнер отключает мышь и у вложенных кнопок.
            # Пропускаем клики только через декоративные элементы.
            if isinstance(child, (QLabel, QProgressBar)):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _check_global_outside_click(self):
        """Отслеживает клик мыши в любой точке рабочего стола вне виджета"""
        if not self.is_expanded or sys.platform != "win32":
            self._was_outside_pressed = False
            return

        # Проверяем состояние левой (0x01) и правой (0x02) кнопок мыши через WinAPI
        lmb_down = (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000) != 0
        rmb_down = (ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000) != 0
        any_down = lmb_down or rmb_down

        if any_down and not self._was_outside_pressed:
            self._was_outside_pressed = True
            cursor_pos = self.cursor().pos()
            # Если кликнули за пределами геометрии острова — мгновенно сворачиваем!
            if not self.geometry().contains(cursor_pos):
                self.collapse_island()
        elif not any_down:
            self._was_outside_pressed = False

    def changeEvent(self, event):
        # Если окно потеряло фокус (переключились на другое окно) — тоже сворачиваем
        if event.type() == QEvent.Type.WindowDeactivate and self.is_expanded:
            self.collapse_island()
        super().changeEvent(event)

    def calc_target_rect(self, expanded: bool) -> QRect:
        screen = QApplication.primaryScreen()
        screen_w = screen.geometry().width() if screen else 1920
        w = self.EXPANDED_W if expanded else self.COLLAPSED_W
        h = self.EXPANDED_H if expanded else self.COLLAPSED_H
        x = (screen_w - w) // 2
        y = self.TOP_MARGIN
        return QRect(x, y, w, h)

    def init_collapsed_view(self):
        self.collapsed_widget = QWidget(self)
        self.collapsed_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(self.collapsed_widget)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(8)

        self.mini_icon = QLabel("●")
        self.mini_icon.setStyleSheet("font-size: 12px; color: #ff2a85;")

        self.mini_title = QLabel("Ничего не играет")
        self.mini_title.setTextFormat(Qt.TextFormat.PlainText)
        self.mini_title.setStyleSheet("font-size: 11px; font-weight: 600; color: #ffffff;")

        self.mini_time = QLabel(datetime.now().strftime("%H:%M"))
        self.mini_time.setStyleSheet("font-size: 11px; font-weight: 700; color: #8a8a9e;")
        self.mini_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(self.mini_icon)
        layout.addWidget(self.mini_title, 1)
        layout.addWidget(self.mini_time)

    def init_expanded_view(self):
        self.expanded_widget = QWidget(self)
        main_layout = QHBoxLayout(self.expanded_widget)
        main_layout.setContentsMargins(18, 12, 14, 12)
        main_layout.setSpacing(8)

        self.stack = SlidingStackedWidget()
        main_layout.addWidget(self.stack, 1)

        dots_layout = QVBoxLayout()
        dots_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        dots_layout.setSpacing(6)
        self.dots = []
        for _ in range(4):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            self.dots.append(dot)
            dots_layout.addWidget(dot)
        main_layout.addLayout(dots_layout)

        self.init_player_page()
        self.init_calendar_page()
        self.init_pc_stats_page()
        self.init_telegram_page()
        self.stack.currentChanged.connect(self.on_page_changed)
        self.update_dots(0)

    def init_player_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)

        top_box = QHBoxLayout()
        top_box.setSpacing(12)

        self.cover_label = QLabel("♪")
        self.cover_label.setFixedSize(64, 64)
        self.cover_label.setStyleSheet("background-color: #262630; border-radius: 14px; font-size: 24px; color: #ff2a85;")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_box.addWidget(self.cover_label)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.title_lbl = QLabel("Загрузка...")
        self.title_lbl.setObjectName("TitleLbl")
        self.artist_lbl = QLabel("Исполнитель")
        self.artist_lbl.setObjectName("ArtistLbl")
        self.source_lbl = QLabel("Приостановлено")
        self.source_lbl.setObjectName("SourceLbl")

        text_box.addWidget(self.title_lbl)
        text_box.addWidget(self.artist_lbl)
        text_box.addWidget(self.source_lbl)
        top_box.addLayout(text_box, 1)
        layout.addLayout(top_box)

        prog_box = QHBoxLayout()
        prog_box.setSpacing(8)
        self.time_cur_lbl = QLabel("0:00")
        self.time_cur_lbl.setObjectName("TimeLbl")
        self.prog_bar = QProgressBar()
        self.prog_bar.setFixedHeight(5)
        self.prog_bar.setTextVisible(False)
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self.time_rem_lbl = QLabel("-0:00")
        self.time_rem_lbl.setObjectName("TimeLbl")

        prog_box.addWidget(self.time_cur_lbl)
        prog_box.addWidget(self.prog_bar, 1)
        prog_box.addWidget(self.time_rem_lbl)
        layout.addLayout(prog_box)

        ctrl_box = QHBoxLayout()
        ctrl_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctrl_box.setSpacing(18)

        self.btn_prev = MediaButton("prev", "Предыдущий трек")
        self.btn_play = MediaButton("play", "Воспроизвести", primary=True)
        self.btn_next = MediaButton("next", "Следующий трек")

        self.btn_prev.clicked.connect(lambda: self.media_worker.trigger_action("prev"))
        self.btn_play.clicked.connect(lambda: self.media_worker.trigger_action("play_pause"))
        self.btn_next.clicked.connect(lambda: self.media_worker.trigger_action("next"))

        ctrl_box.addWidget(self.btn_prev)
        ctrl_box.addWidget(self.btn_play)
        ctrl_box.addWidget(self.btn_next)
        layout.addLayout(ctrl_box)

        self.stack.addWidget(page)

    def init_calendar_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(14)

        today_box = QVBoxLayout()
        today_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        today_box.setSpacing(2)
        now = datetime.now()

        day_num = QLabel(now.strftime("%d"))
        day_num.setStyleSheet("font-size: 40px; font-weight: 800; color: #ff2a85;")
        day_num.setAlignment(Qt.AlignmentFlag.AlignCenter)

        months_ru = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                     "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        month_lbl = QLabel(months_ru[now.month - 1])
        month_lbl.setStyleSheet("font-size: 13px; font-weight: 700; color: #ffffff;")
        month_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        weekday_lbl = QLabel(days_ru[now.weekday()])
        weekday_lbl.setStyleSheet("font-size: 11px; color: #8a8a9e;")
        weekday_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        today_box.addWidget(day_num)
        today_box.addWidget(month_lbl)
        today_box.addWidget(weekday_lbl)
        layout.addLayout(today_box)

        # Наш новый плавный и стильный календарь
        self.calendar_grid = SmoothCalendarGrid()
        layout.addWidget(self.calendar_grid, 1)

        self.stack.addWidget(page)

    def init_pc_stats_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 8, 4)
        layout.setSpacing(8)

        header = QLabel("МОНИТОРИНГ СИСТЕМЫ")
        header.setStyleSheet("font-size: 11px; font-weight: 700; color: #8a8a9e; letter-spacing: 1px;")
        layout.addWidget(header)

        self.cpu_lbl = QLabel("CPU: 0%")
        self.cpu_bar = QProgressBar()
        self.cpu_bar.setFixedHeight(6)
        self.cpu_bar.setTextVisible(False)
        layout.addWidget(self.cpu_lbl)
        layout.addWidget(self.cpu_bar)

        self.ram_lbl = QLabel("RAM: 0%")
        self.ram_bar = QProgressBar()
        self.ram_bar.setFixedHeight(6)
        self.ram_bar.setTextVisible(False)
        layout.addWidget(self.ram_lbl)
        layout.addWidget(self.ram_bar)

        self.disk_lbl = QLabel("SSD (C:): 0%")
        self.disk_bar = QProgressBar()
        self.disk_bar.setFixedHeight(6)
        self.disk_bar.setTextVisible(False)
        layout.addWidget(self.disk_lbl)
        layout.addWidget(self.disk_bar)

        self.stack.addWidget(page)

    def init_telegram_page(self):
        self.telegram_page = QWidget()
        layout = QVBoxLayout(self.telegram_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QHBoxLayout()
        badge = QLabel("TG")
        badge.setFixedSize(27, 20)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet("background: #269fda; border-radius: 6px; font-weight: 800;")
        title = QLabel("TELEGRAM")
        title.setStyleSheet("color: #6cc8f4; font-size: 10px; font-weight: 700; letter-spacing: 1px;")
        self.telegram_time = QLabel("")
        self.telegram_time.setStyleSheet("color: #8a8a9e; font-size: 10px;")
        heading.addWidget(badge)
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(self.telegram_time)
        layout.addLayout(heading)

        self.telegram_sender = QLabel("Пока нет новых сообщений")
        self.telegram_sender.setTextFormat(Qt.TextFormat.PlainText)
        self.telegram_sender.setMinimumWidth(0)
        self.telegram_sender.setStyleSheet("font-size: 14px; font-weight: 700;")
        layout.addWidget(self.telegram_sender)

        self.telegram_body = QPlainTextEdit()
        self.telegram_body.setReadOnly(True)
        self.telegram_body.setFrameShape(QFrame.Shape.NoFrame)
        self.telegram_body.setMinimumSize(0, 30)
        self.telegram_body.setStyleSheet(
            "QPlainTextEdit { background: transparent; color: #d4d4df; "
            "font-family: 'Segoe UI'; font-size: 12px; border: none; }"
            "QScrollBar:vertical { background: #20212a; width: 4px; }"
            "QScrollBar::handle:vertical { background: #369bcc; min-height: 12px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.telegram_body.setPlainText(
            "В Telegram включите уведомления рабочего стола, системные уведомления Windows "
            "и показ текста сообщений. Здесь появятся новые уведомления."
        )
        layout.addWidget(self.telegram_body, 1)

        self.telegram_status = QLabel("Подключаем уведомления Windows…")
        self.telegram_status.setWordWrap(True)
        self.telegram_status.setStyleSheet("color: #8a8a9e; font-size: 9px;")
        layout.addWidget(self.telegram_status)

        footer = QHBoxLayout()
        footer.setSpacing(5)
        self.telegram_prev = QPushButton("‹")
        self.telegram_prev.setToolTip("Более старое уведомление")
        self.telegram_next = QPushButton("›")
        self.telegram_next.setToolTip("Более новое уведомление")
        self.telegram_counter = QLabel("0 / 0")
        self.telegram_counter.setStyleSheet("color: #9696aa; font-size: 10px;")
        self.telegram_prev.clicked.connect(lambda: self.browse_telegram(1))
        self.telegram_next.clicked.connect(lambda: self.browse_telegram(-1))
        for button in (self.telegram_prev, self.telegram_next):
            button.setFixedSize(25, 22)
            button.setObjectName("TelegramBtn")
            button.setEnabled(False)
        footer.addWidget(self.telegram_prev)
        footer.addWidget(self.telegram_counter)
        footer.addWidget(self.telegram_next)
        footer.addStretch()
        player_button = QPushButton("К плееру")
        player_button.setObjectName("TelegramBtn")
        player_button.setFixedHeight(22)
        player_button.clicked.connect(lambda: self.stack.slide_to_index(0, "up"))
        footer.addWidget(player_button)
        layout.addLayout(footer)
        self.telegram_page_index = self.stack.addWidget(self.telegram_page)

    def receive_telegram_messages(self, messages):
        if self._closing:
            return
        for message in messages:
            self.telegram_messages.insert(0, message)
            self.telegram_unseen.add(message["key"])
        self.telegram_messages = self.telegram_messages[:20]
        self.telegram_unseen.intersection_update(m["key"] for m in self.telegram_messages)
        self.telegram_index = 0
        self.render_telegram_message()
        self.mark_telegram_viewed()
        self.refresh_collapsed_view()
        self.update_dots(self.stack.currentIndex())

    def render_telegram_message(self):
        if not self.telegram_messages:
            return
        message = self.telegram_messages[self.telegram_index]
        self.telegram_sender.setText(self.telegram_sender.fontMetrics().elidedText(
            message["sender"], Qt.TextElideMode.ElideRight,
            self.EXPANDED_W - 58
        ))
        self.telegram_sender.setToolTip(message["sender"])
        self.telegram_body.setPlainText(message["body"])
        self.telegram_time.setText(message["time"])
        self.telegram_counter.setText(f"{self.telegram_index + 1} / {len(self.telegram_messages)}")
        self.telegram_prev.setEnabled(self.telegram_index + 1 < len(self.telegram_messages))
        self.telegram_next.setEnabled(self.telegram_index > 0)

    def browse_telegram(self, offset):
        index = self.telegram_index + offset
        if 0 <= index < len(self.telegram_messages):
            self.telegram_index = index
            self.render_telegram_message()
            self.mark_telegram_viewed()

    def mark_telegram_viewed(self):
        if (self.is_expanded and self.expanded_widget.isVisible()
                and self.stack.currentWidget() is self.telegram_page
                and self.telegram_messages):
            self.telegram_unseen.discard(self.telegram_messages[self.telegram_index]["key"])
            self.refresh_collapsed_view()
            self.update_dots(self.stack.currentIndex())

    def on_page_changed(self, index):
        self.update_dots(index)
        self.mark_telegram_viewed()
        if hasattr(self, "media_worker"):
            self.media_worker.load_artwork = self.is_expanded and index == 0
            self.update_pc_stats()

    def refresh_collapsed_view(self):
        telegram_mode = bool(self.telegram_unseen)
        if telegram_mode != self._mini_telegram_mode:
            self._mini_telegram_mode = telegram_mode
            self.mini_icon.setStyleSheet(
                "font-size: 10px; font-weight: 800; color: #61c7fa;" if telegram_mode
                else "font-size: 12px; color: #ff2a85;"
            )
        if self.telegram_unseen:
            message = next(m for m in self.telegram_messages if m["key"] in self.telegram_unseen)
            self.mini_icon.setText("TG")
            self.mini_title.setText(self.mini_title.fontMetrics().elidedText(
                message["sender"], Qt.TextElideMode.ElideRight, 140
            ))
            self.mini_time.setText(f"+{len(self.telegram_unseen)}")
        else:
            self.mini_icon.setText("♫" if self._media_playing else "●")
            self.mini_title.setText(self._media_title[:18] + "…" if len(self._media_title) > 18 else self._media_title)
            self.mini_time.setText(datetime.now().strftime("%H:%M"))

    @staticmethod
    def make_rounded_pixmap(raw_bytes: bytes, size: int = 64, radius: int = 14) -> QPixmap:
        img = raw_bytes if isinstance(raw_bytes, QImage) else decode_cover(raw_bytes)
        if img.isNull():
            return QPixmap()

        scaled = QPixmap.fromImage(img).scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        x_off = max(0, (scaled.width() - size) // 2)
        y_off = max(0, (scaled.height() - size) // 2)
        cropped = scaled.copy(x_off, y_off, size, size)

        rounded = QPixmap(size, size)
        rounded.fill(Qt.GlobalColor.transparent)

        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(size), float(size), float(radius), float(radius))
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, cropped)
        painter.end()

        return rounded

    def update_player_ui(self, data):
        title = data["title"]
        self.title_lbl.setText(title[:25] + "..." if len(title) > 25 else title)
        self._media_title = title
        self._media_playing = data["is_playing"]
        self.refresh_collapsed_view()

        artist = data["artist"]
        self.artist_lbl.setText(artist[:30] + "..." if len(artist) > 30 else artist)
        self.source_lbl.setText(data["source"])

        self.btn_play.set_symbol(
            "pause" if data["is_playing"] else "play",
            "Пауза" if data["is_playing"] else "Воспроизвести"
        )

        pos = int(data["pos"])
        end = int(data["end"])
        if end > 0 and pos <= end:
            self.prog_bar.setRange(0, end)
            self.prog_bar.setValue(pos)
            rem = max(0, end - pos)
            self.time_cur_lbl.setText(f"{pos // 60}:{pos % 60:02d}")
            self.time_rem_lbl.setText(f"-{rem // 60}:{rem % 60:02d}")
        else:
            self.prog_bar.setRange(0, 100)
            self.prog_bar.setValue(0)
            self.time_cur_lbl.setText("0:00")
            self.time_rem_lbl.setText("-0:00")

        if "thumb" not in data:
            return  # The track's cover is unchanged; keep the small pixmap.
        if data["thumb"] is not None:
            rounded_pix = self.make_rounded_pixmap(data["thumb"], size=64, radius=14)
            if not rounded_pix.isNull():
                self.cover_label.setStyleSheet("background-color: transparent;")
                self.cover_label.setPixmap(rounded_pix)
                return

        if self.cover_label.text() == "♪":
            return
        self.cover_label.clear()
        self.cover_label.setStyleSheet("background-color: #262630; border-radius: 14px; font-size: 24px; color: #ff2a85;")
        self.cover_label.setText("♪")

    def update_pc_stats(self):
        self.refresh_collapsed_view()
        if not self.is_expanded or self.stack.currentIndex() != 2:
            return

        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage('C:\\')
            disk_percent = disk.percent
            disk_free = disk.free // (1024**3)
        except Exception:
            disk_percent, disk_free = 0, 0

        self.cpu_lbl.setText(f"Процессор (CPU): {cpu:.0f}%")
        self.cpu_bar.setValue(int(cpu))

        used_gb = ram.used / (1024 ** 3)
        total_gb = ram.total / (1024 ** 3)
        self.ram_lbl.setText(f"Память (RAM): {ram.percent:.0f}%  ({used_gb:.1f} / {total_gb:.1f} ГБ)")
        self.ram_bar.setValue(int(ram.percent))

        self.disk_lbl.setText(f"Накопитель: {disk_percent:.0f}% свободно {disk_free} ГБ")
        self.disk_bar.setValue(int(disk_percent))

    # --- УПРАВЛЕНИЕ СВОРАЧИВАНИЕМ И РАЗВОРАЧИВАНИЕМ ---
    def expand_island(self):
        if not self.is_expanded:
            self.toggle_expand()

    def collapse_island(self):
        if self.is_expanded:
            self.toggle_expand()

    def toggle_expand(self):
        if self.resize_anim.state() == QVariantAnimation.State.Running:
            return

        self.is_expanded = not self.is_expanded
        start_rect = self.geometry()
        target_rect = self.calc_target_rect(self.is_expanded)

        if self.is_expanded:
            if self.telegram_unseen:
                if self.stack._animating:
                    self.stack.anim_group.setCurrentTime(self.stack.anim_group.duration())
                self.telegram_index = next(
                    i for i, message in enumerate(self.telegram_messages)
                    if message["key"] in self.telegram_unseen
                )
                self.render_telegram_message()
                self.stack.setCurrentIndex(self.telegram_page_index)
            self.activateWindow()

        self.media_worker.load_artwork = self.is_expanded and self.stack.currentIndex() == 0
        self.pc_timer.setInterval(1500 if self.is_expanded else 5000)
        if self.is_expanded:
            self.outside_click_timer.start(40)
            self.update_pc_stats()
        else:
            self.outside_click_timer.stop()
            self._was_outside_pressed = False

        self.collapsed_widget.hide()
        self.expanded_widget.hide()

        self.setMinimumSize(10, 10)
        self.setMaximumSize(2000, 2000)

        self.resize_anim.stop()
        self.resize_anim.setDuration(300)
        self.resize_anim.setStartValue(start_rect)
        self.resize_anim.setEndValue(target_rect)
        self.resize_anim.setEasingCurve(
            QEasingCurve.Type.OutBack if self.is_expanded else QEasingCurve.Type.InOutCubic
        )
        self.resize_anim.start()

    def _on_anim_step(self, rect: QRect):
        self.setGeometry(rect)
        self.update()

    def _on_anim_finished(self):
        target_rect = self.calc_target_rect(self.is_expanded)
        self.setGeometry(target_rect)

        if self.is_expanded:
            self.expanded_widget.setGeometry(0, 0, self.EXPANDED_W, self.EXPANDED_H)
            self.expanded_widget.show()
            self.mark_telegram_viewed()
        else:
            self.collapsed_widget.setGeometry(0, 0, self.COLLAPSED_W, self.COLLAPSED_H)
            self.collapsed_widget.show()

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(15, 15, 19, 254))
        painter.setPen(QColor(255, 255, 255, 30))

        radius = float(min(26, self.height() // 2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)

    # --- СОБЫТИЯ МЫШИ И КЛАВИАТУРЫ ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_expand()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.close()

    def closeEvent(self, event):
        self._closing = True
        self.pc_timer.stop()
        self.outside_click_timer.stop()
        self.media_worker.stop()
        self.telegram_worker.requestInterruption()
        if self.media_worker.isRunning() or self.telegram_worker.isRunning():
            self.hide()
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.is_expanded:
            self.collapse_island()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        if not self.is_expanded:
            self.expand_island()
            return

        delta = event.angleDelta().y()
        if delta == 0 or self.stack._animating:
            return
        count = self.stack.count()
        current = self.stack.currentIndex()

        if delta < 0:
            next_idx = (current + 1) % count
            direction = "down"
        else:
            next_idx = (current - 1 + count) % count
            direction = "up"

        self.stack.slide_to_index(next_idx, direction=direction)

    def update_dots(self, active_idx):
        for i, dot in enumerate(self.dots):
            if i == active_idx:
                dot.setStyleSheet("background-color: #ff2a85; border-radius: 3px;")
                dot.setFixedSize(6, 14)
            else:
                color = "#39b6f4" if i == self.telegram_page_index and self.telegram_unseen else "#444455"
                dot.setStyleSheet(f"background-color: {color}; border-radius: 3px;")
                dot.setFixedSize(6, 6)

    def apply_styles(self):
        self.setStyleSheet("""
            QLabel {
                color: #ffffff;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }
            QLabel#TitleLbl {
                font-size: 14px;
                font-weight: 700;
                color: #ffffff;
            }
            QLabel#ArtistLbl {
                font-size: 12px;
                color: #b8b8c8;
            }
            QLabel#SourceLbl {
                font-size: 10px;
                color: #787888;
            }
            QLabel#TimeLbl {
                font-size: 10px;
                color: #888898;
            }
            QProgressBar {
                background-color: #2e2e3a;
                border-radius: 2px;
                border: none;
            }
            QProgressBar::chunk {
                background-color: #ff2a85;
                border-radius: 2px;
            }
            QPushButton#TelegramBtn {
                background: #203443;
                color: #87d6ff;
                border: none;
                border-radius: 7px;
                padding: 2px 7px;
                font-size: 11px;
            }
            QPushButton#TelegramBtn:hover { background: #2b4c62; }
            QPushButton#TelegramBtn:pressed { background: #34647e; }
            QPushButton#TelegramBtn:disabled { color: #56616b; background: #20252c; }
            QPushButton#TelegramBtn:focus {
                border: 1px solid #87d6ff;
            }
        """)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    widget = DesktopIslandWidget()
    widget.show()
    sys.exit(app.exec())
