#!/usr/bin/env python3
"""Dictate GUI — PyQt6 tray app for voice dictation via Groq Whisper."""
import sys
import threading
import time

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget,
)
from pynput import keyboard

import core


class Bridge(QObject):
    hotkey_press = pyqtSignal()
    hotkey_release = pyqtSignal()
    other_press = pyqtSignal()
    transcribed = pyqtSignal(str)
    error = pyqtSignal(str)


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dictate — Settings")
        self.setMinimumWidth(440)
        self.cfg = dict(cfg)

        form = QFormLayout()

        self.api_key_edit = QLineEdit(self.cfg.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("gsk_...")
        show_btn = QPushButton("show")
        show_btn.setCheckable(True)
        show_btn.setMaximumWidth(60)
        show_btn.toggled.connect(
            lambda on: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.api_key_edit)
        row.addWidget(show_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        form.addRow("Groq API key:", wrap)

        self.mode_box = QComboBox()
        self.mode_box.addItem("Push-to-talk (hold key)", "ptt")
        self.mode_box.addItem("Toggle (tap to start, tap to stop)", "toggle")
        i = self.mode_box.findData(self.cfg.get("mode", "ptt"))
        self.mode_box.setCurrentIndex(max(i, 0))
        form.addRow("Mode:", self.mode_box)

        self.key_box = QComboBox()
        for k in core.KEY_MAP:
            self.key_box.addItem(k.upper().replace("_", " "), k)
        i = self.key_box.findData(self.cfg.get("key", "f9"))
        self.key_box.setCurrentIndex(max(i, 0))
        form.addRow("Hotkey:", self.key_box)

        self.model_box = QComboBox()
        self.model_box.addItem("whisper-large-v3-turbo (fastest)", "whisper-large-v3-turbo")
        self.model_box.addItem("whisper-large-v3 (most accurate)", "whisper-large-v3")
        i = self.model_box.findData(self.cfg.get("model", core.DEFAULT_MODEL))
        self.model_box.setCurrentIndex(max(i, 0))
        form.addRow("Model:", self.model_box)

        self.threshold_box = QDoubleSpinBox()
        self.threshold_box.setRange(0.0, 5.0)
        self.threshold_box.setSingleStep(0.1)
        self.threshold_box.setDecimals(1)
        self.threshold_box.setSuffix(" s")
        self.threshold_box.setValue(float(self.cfg.get("threshold", 0.0)))
        self.threshold_box.setToolTip(
            "Recording starts immediately on press; release sends to Groq.\n"
            "If you release before this duration, the recording is discarded\n"
            "with no API call. 0 = always send. Recommended ~2.0s when bound\n"
            "to Ctrl/Alt/Shift so brief taps and shortcuts don't transcribe."
        )
        form.addRow("Min hold to send:", self.threshold_box)

        self.test_btn = QPushButton("Test API key")
        self.test_btn.clicked.connect(self.on_test)
        form.addRow("", self.test_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        hint = QLabel(
            "Get a free Groq API key at <a href='https://console.groq.com'>console.groq.com</a>."
        )
        hint.setOpenExternalLinks(True)
        hint.setStyleSheet("color: #777; padding: 6px 0;")
        layout.addWidget(hint)

        layout.addWidget(buttons)

    def on_test(self):
        key = self.api_key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "Dictate", "Enter an API key first.")
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")
        QApplication.processEvents()
        ok = core.test_api_key(key)
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test API key")
        if ok:
            QMessageBox.information(self, "Dictate", "API key works.")
        else:
            QMessageBox.warning(self, "Dictate", "API key rejected.")

    def values(self):
        return {
            "api_key": self.api_key_edit.text().strip(),
            "mode": self.mode_box.currentData(),
            "key": self.key_box.currentData(),
            "model": self.model_box.currentData(),
            "threshold": float(self.threshold_box.value()),
        }


def make_mic_icon(color="#2b2b2b"):
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(22, 8, 20, 32, 10, 10)
    p.setPen(QColor(color))
    p.setBrush(Qt.GlobalColor.transparent)
    pen = p.pen()
    pen.setWidth(4)
    p.setPen(pen)
    p.drawArc(16, 28, 32, 24, 0, -180 * 16)
    p.drawLine(32, 50, 32, 58)
    p.drawLine(22, 58, 42, 58)
    p.end()
    return QIcon(pix)


STATUS_STYLES = {
    "ready":        ("#f3f3f3", "#222"),
    "recording":    ("#fde2e2", "#a00000"),
    "transcribing": ("#fff3cc", "#8a6d00"),
    "error":        ("#ffd6d6", "#9a0000"),
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dictate")
        self.resize(500, 380)
        self.setWindowIcon(make_mic_icon())

        self.cfg = core.load_config()
        self.recorder = core.Recorder()
        self.bridge = Bridge()
        self.bridge.hotkey_press.connect(self.on_press)
        self.bridge.hotkey_release.connect(self.on_release)
        self.bridge.other_press.connect(self.on_other_press)
        self.bridge.transcribed.connect(self.on_transcribed)
        self.bridge.error.connect(self.on_error)
        self.key_held = False
        self.combo = False
        self.recording_active = False
        self.listener = None
        self._target_keys = set()

        self.press_time = 0.0

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Dictate")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch()
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        header.addWidget(self.settings_btn)
        v.addLayout(header)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.status_label)

        self.info_label = QLabel()
        self.info_label.setStyleSheet("color: #666; padding: 2px 0;")
        v.addWidget(self.info_label)

        v.addWidget(QLabel("Last transcription:"))
        self.last = QTextEdit()
        self.last.setReadOnly(True)
        self.last.setMaximumHeight(110)
        self.last.setStyleSheet(
            "QTextEdit { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 6px; }"
        )
        v.addWidget(self.last)

        v.addStretch()

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_mic_icon(), self)
            self.tray.setToolTip("Dictate")
            menu = QMenu()
            a_show = QAction("Show window", self)
            a_show.triggered.connect(self.show_window)
            a_quit = QAction("Quit", self)
            a_quit.triggered.connect(QApplication.quit)
            menu.addAction(a_show)
            menu.addSeparator()
            menu.addAction(a_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray)
            self.tray.show()

        self.set_status("ready", "Ready")
        self.update_info()
        self.start_listener()

        if not self.cfg.get("api_key"):
            QTimer.singleShot(250, self.first_run)

    def first_run(self):
        QMessageBox.information(
            self,
            "Welcome to Dictate",
            "Open Settings and paste your Groq API key to start dictating.\n\n"
            "Get a free key at console.groq.com",
        )
        self.open_settings()

    def update_info(self):
        mode_name = "Push-to-talk" if self.cfg["mode"] == "ptt" else "Toggle"
        thr = float(self.cfg.get("threshold", 0.0))
        thr_str = f"     Min hold: {thr:.1f}s" if thr > 0 else ""
        self.info_label.setText(
            f"Mode: {mode_name}     Hotkey: {self.cfg['key'].upper()}     "
            f"Model: {self.cfg['model']}{thr_str}"
        )

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.update(dlg.values())
            core.save_config(self.cfg)
            self.restart_listener()
            self.update_info()

    def start_listener(self):
        keys = core.KEY_MAP.get(self.cfg["key"])
        if not keys:
            return
        self._target_keys = set(keys)

        def on_press(k):
            try:
                if k in self._target_keys:
                    self.bridge.hotkey_press.emit()
                else:
                    self.bridge.other_press.emit()
            except Exception:
                pass

        def on_release(k):
            try:
                if k in self._target_keys:
                    self.bridge.hotkey_release.emit()
            except Exception:
                pass

        self.listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.listener.daemon = True
        self.listener.start()

    def restart_listener(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.key_held = False
        self.combo = False
        self.recording_active = False
        self.start_listener()

    def on_press(self):
        if self.key_held:
            return
        self.key_held = True
        self.combo = False
        self.press_time = time.monotonic()
        if self.cfg["mode"] == "ptt":
            if self.start_recording():
                self.recording_active = True
        else:
            if self.recording_active:
                self.stop_and_transcribe()
                self.recording_active = False
            elif self.start_recording():
                self.recording_active = True

    def on_other_press(self):
        if not self.key_held:
            return
        self.combo = True
        # Only abort if this hold started the recording (PTT). In toggle mode
        # a combo while pressing the hotkey shouldn't kill an ongoing session
        # that was started by a previous tap.
        if self.cfg["mode"] == "ptt" and self.recording_active:
            self.recorder.stop()
            self.recording_active = False
            self.set_status("ready", "Cancelled (combo)")

    def on_release(self):
        self.key_held = False
        if self.cfg["mode"] != "ptt":
            return
        if not self.recording_active:
            return
        duration = time.monotonic() - self.press_time
        min_hold = float(self.cfg.get("threshold", 0.0))
        if min_hold > 0 and duration < min_hold:
            self.recorder.stop()
            self.recording_active = False
            self.set_status("ready", f"Discarded (held {duration:.1f}s)")
            return
        self.stop_and_transcribe()
        self.recording_active = False

    def start_recording(self) -> bool:
        if not self.cfg.get("api_key"):
            self.set_status("error", "No API key — open Settings")
            return False
        self.recorder.start()
        self.set_status("recording", "Recording...")
        return True

    def stop_and_transcribe(self):
        audio = self.recorder.stop()
        if audio is None:
            self.set_status("ready", "Ready")
            return
        self.set_status("transcribing", "Transcribing...")
        threading.Thread(target=self._do_transcribe, args=(audio,), daemon=True).start()

    def _do_transcribe(self, audio):
        try:
            text = core.transcribe(audio, self.cfg["api_key"], self.cfg["model"])
            self.bridge.transcribed.emit(text or "")
        except Exception as e:
            self.bridge.error.emit(str(e))

    def on_transcribed(self, text):
        if text:
            core.type_text(text)
            self.last.setPlainText(text)
        self.set_status("ready", "Ready")

    def on_error(self, msg):
        self.set_status("error", f"Error: {msg[:80]}")

    def set_status(self, state, msg):
        bg, fg = STATUS_STYLES.get(state, STATUS_STYLES["ready"])
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(
            f"font-size: 24px; padding: 28px; border-radius: 10px;"
            f"background: {bg}; color: {fg};"
        )
        if self.tray:
            self.tray.setToolTip(f"Dictate — {msg}")

    def on_tray(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.tray and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Dictate")
    has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not has_tray)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
