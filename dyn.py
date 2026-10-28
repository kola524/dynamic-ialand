import sys
import ctypes
import calendar
import asyncio
import psutil
from datetime import datetime

from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QRect, QThread, pyqtSignal, QEvent,
    QVariantAnimation, QPropertyAnimation, QEasingCurve, QParallelAnimationGroup
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QImage
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QStackedWidget, QProgressBar
)

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


# --- Поток для асинхронного чтения медиа из Windows ---
class MediaWorker(QThread):
    media_updated = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.running = True
        self._action = None

    def trigger_action(self, action_name):
        self._action = action_name

    async def _read_thumbnail_bytes(self, thumb_ref):
        try:
            stream = await thumb_ref.open_read_async()
            size = int(stream.size)
            if size <= 0:
                return None

            reader = DataReader(stream)
            await reader.load_async(size)

            if MEDIA_BACKEND == "winrt":
                try:
                    buf = bytearray(size)
                    reader.read_bytes(buf)
                    return bytes(buf)
                except TypeError:
                    return bytes([reader.read_byte() for _ in range(size)])
            else:
                buf = bytearray(size)
                reader.read_bytes(buf)
                return bytes(buf)
        except Exception:
            return None

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
            manager = await MediaManager.request_async()
            session = manager.get_current_session()
            if not session:
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

            thumb_bytes = None
            if props and props.thumbnail:
                thumb_bytes = await self._read_thumbnail_bytes(props.thumbnail)

            return {
                "title": title,
                "artist": artist,
                "source": f"{'Играет' if is_playing else 'Приостановлено'} в {source_app}",
                "is_playing": is_playing,
                "pos": pos,
                "end": end,
                "thumb": thumb_bytes
            }
        except Exception as e:
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

        while self.running:
            try:
                data = loop.run_until_complete(self._fetch_media())
                self.media_updated.emit(data)
            except Exception:
                pass
            self.msleep(800)
        loop.close()


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

        self.pc_timer = QTimer(self)
        self.pc_timer.timeout.connect(self.update_pc_stats)
        self.pc_timer.start(1500)
        self.update_pc_stats()

        # Глобальный детектор клика мимо окна (например, по рабочему столу)
        self.outside_click_timer = QTimer(self)
        self.outside_click_timer.timeout.connect(self._check_global_outside_click)
        self.outside_click_timer.start(40)

    def _make_children_click_through(self, parent_widget):
        for child in parent_widget.findChildren(QWidget):
            if not isinstance(child, QPushButton):
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
        for _ in range(3):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            self.dots.append(dot)
            dots_layout.addWidget(dot)
        main_layout.addLayout(dots_layout)

        self.init_player_page()
        self.init_calendar_page()
        self.init_pc_stats_page()
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
        ctrl_box.setSpacing(24)

        self.btn_prev = QPushButton("|◀")
        self.btn_play = QPushButton("▶")
        self.btn_next = QPushButton("▶|")

        self.btn_prev.setObjectName("MediaBtn")
        self.btn_play.setObjectName("PlayBtn")
        self.btn_next.setObjectName("MediaBtn")

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

    @staticmethod
    def make_rounded_pixmap(raw_bytes: bytes, size: int = 64, radius: int = 14) -> QPixmap:
        img = QImage.fromData(raw_bytes)
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
        self.mini_title.setText(title[:18] + "..." if len(title) > 18 else title)
        self.mini_icon.setText("♫" if data["is_playing"] else "●")

        artist = data["artist"]
        self.artist_lbl.setText(artist[:30] + "..." if len(artist) > 30 else artist)
        self.source_lbl.setText(data["source"])

        self.btn_play.setText("||" if data["is_playing"] else "▶")

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

        if data["thumb"]:
            rounded_pix = self.make_rounded_pixmap(data["thumb"], size=64, radius=14)
            if not rounded_pix.isNull():
                self.cover_label.setStyleSheet("background-color: transparent;")
                self.cover_label.setPixmap(rounded_pix)
                return

        self.cover_label.clear()
        self.cover_label.setStyleSheet("background-color: #262630; border-radius: 14px; font-size: 24px; color: #ff2a85;")
        self.cover_label.setText("♪")

    def update_pc_stats(self):
        self.mini_time.setText(datetime.now().strftime("%H:%M"))

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
            self.activateWindow()

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
            self.media_worker.running = False
            self.media_worker.wait(1500)
            QApplication.quit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.is_expanded:
            self.collapse_island()

    def wheelEvent(self, event):
        if not self.is_expanded:
            self.expand_island()
            return

        delta = event.angleDelta().y()
        count = self.stack.count()
        current = self.stack.currentIndex()

        if delta < 0:
            next_idx = (current + 1) % count
            direction = "down"
        else:
            next_idx = (current - 1 + count) % count
            direction = "up"

        self.stack.slide_to_index(next_idx, direction=direction)
        self.update_dots(next_idx)

    def update_dots(self, active_idx):
        for i, dot in enumerate(self.dots):
            if i == active_idx:
                dot.setStyleSheet("background-color: #ff2a85; border-radius: 3px;")
                dot.setFixedSize(6, 14)
            else:
                dot.setStyleSheet("background-color: #444455; border-radius: 3px;")
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
            QPushButton#MediaBtn {
                background: transparent;
                color: #ff2a85;
                border: none;
                font-size: 13px;
                font-weight: 800;
            }
            QPushButton#MediaBtn:hover {
                color: #ff66a8;
            }
            QPushButton#PlayBtn {
                background-color: rgba(255, 42, 133, 0.18);
                color: #ff2a85;
                border: none;
                border-radius: 16px;
                font-size: 13px;
                font-weight: 800;
                min-width: 32px;
                max-width: 32px;
                min-height: 32px;
                max-height: 32px;
            }
            QPushButton#PlayBtn:hover {
                background-color: rgba(255, 42, 133, 0.35);
            }
        """)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    widget = DesktopIslandWidget()
    widget.show()
    sys.exit(app.exec())