#!/usr/bin/env python3
"""Dictate GUI — PyQt6 tray app for voice dictation via Groq Whisper."""
import fcntl
import sys
import threading
import time

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSpinBox, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget,
)
from pynput import keyboard

import core


def _make_listener(on_press, on_release):
    """Prefer evdev (works on Wayland+Xorg); fall back to pynput on failure."""
    try:
        import evdev_listener
        l = evdev_listener.Listener(on_press=on_press, on_release=on_release)
        l.start()
        print("keyboard listener: evdev", file=sys.stderr)
        return l
    except Exception as e:
        print(f"evdev unavailable ({e}); falling back to pynput", file=sys.stderr)
        l = keyboard.Listener(on_press=on_press, on_release=on_release)
        l.daemon = True
        l.start()
        return l


class Bridge(QObject):
    hotkey_press = pyqtSignal()
    hotkey_release = pyqtSignal()
    other_press = pyqtSignal()
    transcribed = pyqtSignal(str)
    error = pyqtSignal(str)


class SettingsDialog(QDialog):
    # Bridge signals for the background model-download thread.
    _dl_status = pyqtSignal(str)
    _dl_done = pyqtSignal(bool, str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dictate — Settings")
        self.setMinimumWidth(460)
        self.cfg = dict(cfg)

        self.form = QFormLayout()

        # --- Provider selector ---
        self.provider_box = QComboBox()
        for pid, pdef in core.PROVIDERS.items():
            self.provider_box.addItem(pdef["label"], pid)
        i = self.provider_box.findData(self.cfg.get("provider", core.DEFAULT_PROVIDER))
        self.provider_box.setCurrentIndex(max(i, 0))
        self.provider_box.currentIndexChanged.connect(self.on_provider_changed)
        self.form.addRow("Provider:", self.provider_box)

        # --- API key row (hidden when a local provider is selected) ---
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
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
        self.api_key_wrap = QWidget()
        self.api_key_wrap.setLayout(row)
        self.api_key_label = QLabel()
        self.form.addRow(self.api_key_label, self.api_key_wrap)

        # Per-provider key cache — only providers that actually take a key.
        self._keys = {
            pid: self.cfg.get(pdef["key_field"], "")
            for pid, pdef in core.PROVIDERS.items()
            if pdef.get("key_field")
        }

        self.mode_box = QComboBox()
        self.mode_box.addItem("Push-to-talk (hold key)", "ptt")
        self.mode_box.addItem("Toggle (tap to start, tap to stop)", "toggle")
        i = self.mode_box.findData(self.cfg.get("mode", "ptt"))
        self.mode_box.setCurrentIndex(max(i, 0))
        self.form.addRow("Mode:", self.mode_box)

        self.key_box = QComboBox()
        for k in core.KEY_MAP:
            self.key_box.addItem(k.upper().replace("_", " "), k)
        i = self.key_box.findData(self.cfg.get("key", "f9"))
        self.key_box.setCurrentIndex(max(i, 0))
        self.form.addRow("Hotkey:", self.key_box)

        self.model_box = QComboBox()
        self.form.addRow("Model:", self.model_box)

        # --- Local-STT panel (shown only when a local provider is selected) ---
        import os as _os
        cpu_max = max(1, _os.cpu_count() or 1)
        self.local_status_label = QLabel()
        self.form.addRow("Local model:", self.local_status_label)
        self.local_download_btn = QPushButton()
        self.local_download_btn.clicked.connect(self.on_download_model)
        self.form.addRow("", self.local_download_btn)
        self.local_threads_box = QSpinBox()
        self.local_threads_box.setRange(1, cpu_max)
        self.local_threads_box.setValue(int(self.cfg.get("local_stt_num_threads", min(4, cpu_max))))
        self.local_threads_box.setToolTip(
            "sherpa-onnx CPU thread count. 4 = best latency/resource balance on\n"
            "this system; more brings little. Changing this rebuilds the recognizer."
        )
        self.form.addRow("CPU threads:", self.local_threads_box)

        self.threshold_box = QDoubleSpinBox()
        self.threshold_box.setRange(0.0, 5.0)
        self.threshold_box.setSingleStep(0.1)
        self.threshold_box.setDecimals(1)
        self.threshold_box.setSuffix(" s")
        self.threshold_box.setValue(float(self.cfg.get("threshold", 0.0)))
        self.threshold_box.setToolTip(
            "Recording starts immediately on press; release sends for transcription.\n"
            "If you release before this duration, the recording is discarded. 0 =\n"
            "always send. Recommended ~2.0s when bound to Ctrl/Alt/Shift so brief\n"
            "taps and shortcuts don't transcribe."
        )
        self.form.addRow("Min hold to send:", self.threshold_box)

        self.test_btn = QPushButton("Test API key")
        self.test_btn.clicked.connect(self.on_test)
        self.form.addRow("", self.test_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(self.form)

        self.hint = QLabel()
        self.hint.setOpenExternalLinks(True)
        self.hint.setStyleSheet("color: #777; padding: 6px 0;")
        layout.addWidget(self.hint)

        layout.addWidget(buttons)

        # Wire the download-worker bridge, then sync UI to current provider.
        self._dl_status.connect(self.local_status_label.setText)
        self._dl_done.connect(self._on_download_done)
        self.refresh_model_box(self.cfg.get("model", core.DEFAULT_MODEL))
        self.on_provider_changed()

    def on_provider_changed(self):
        # Persist edits to the previously-shown provider's key (if any) before
        # swapping the display to the newly selected provider.
        prev = getattr(self, "_shown_provider", None)
        if prev is not None and prev in self._keys:
            self._keys[prev] = self.api_key_edit.text().strip()

        pid = self.provider_box.currentData()
        pdef = core.PROVIDERS[pid]
        self._shown_provider = pid
        is_local = pdef.get("is_local", False)

        # HTTP-provider widgets
        if not is_local:
            self.api_key_label.setText(f"{pdef['label']} API key:")
            self.api_key_edit.setText(self._keys.get(pid, ""))
            self.api_key_edit.setPlaceholderText(f"{pdef['key_prefix']}...")

        # Toggle row visibility. QFormLayout.setRowVisible(widget, bool) hides
        # both the field and its label.
        self.form.setRowVisible(self.api_key_wrap, not is_local)
        self.form.setRowVisible(self.model_box, not is_local)
        self.form.setRowVisible(self.test_btn, not is_local)
        self.form.setRowVisible(self.local_status_label, is_local)
        self.form.setRowVisible(self.local_download_btn, is_local)
        self.form.setRowVisible(self.local_threads_box, is_local)

        # Hint text at the bottom of the dialog.
        if is_local:
            self.hint.setText(
                "Runs entirely offline on your CPU. Model: parakeet-primeline "
                "(German, ~640 MB, CC-BY-4.0). Attribution: primeline · NVIDIA · "
                "<a href='https://github.com/k2-fsa/sherpa-onnx'>k2-fsa/sherpa-onnx</a>."
            )
        elif pid == "openai":
            self.hint.setText(
                "OpenAI key: <a href='https://platform.openai.com/api-keys'>platform.openai.com/api-keys</a>."
            )
        else:
            self.hint.setText(
                "Get a free Groq API key at <a href='https://console.groq.com'>console.groq.com</a>."
            )

        self.refresh_model_box(pdef["default_model"])
        if is_local:
            self._refresh_local_status()

    def _refresh_local_status(self):
        try:
            import local_stt
            installed = local_stt.model_installed()
        except ImportError:
            self.local_status_label.setText(
                "sherpa-onnx not installed — run: pip install --user sherpa-onnx soundfile"
            )
            self.local_download_btn.setEnabled(False)
            self.local_download_btn.setText("Download model (~640 MB)")
            return
        if installed:
            self.local_status_label.setText("Installed and ready.")
            self.local_download_btn.setEnabled(True)
            self.local_download_btn.setText("Re-download model")
        else:
            self.local_status_label.setText("Not installed.")
            self.local_download_btn.setEnabled(True)
            self.local_download_btn.setText("Download model (~640 MB)")

    def on_download_model(self):
        self.local_download_btn.setEnabled(False)
        self.local_download_btn.setText("Downloading...")
        self.local_status_label.setText("Starting download...")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            import local_stt
            local_stt.download_model(progress_cb=lambda m: self._dl_status.emit(m))
            self._dl_done.emit(True, "Installed.")
        except Exception as e:
            self._dl_done.emit(False, f"Download failed: {e}")

    def _on_download_done(self, ok, msg):
        self.local_status_label.setText(msg)
        self.local_download_btn.setEnabled(True)
        self.local_download_btn.setText("Re-download model" if ok else "Retry download")

    def refresh_model_box(self, preferred_model):
        pid = self.provider_box.currentData()
        pdef = core.PROVIDERS[pid]
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for label, mid in pdef["models"]:
            self.model_box.addItem(label, mid)
        i = self.model_box.findData(preferred_model)
        if i < 0:
            i = self.model_box.findData(pdef["default_model"])
        self.model_box.setCurrentIndex(max(i, 0))
        self.model_box.blockSignals(False)

    def on_test(self):
        key = self.api_key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "Dictate", "Enter an API key first.")
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")
        QApplication.processEvents()
        ok = core.test_api_key(key, self.provider_box.currentData())
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test API key")
        if ok:
            QMessageBox.information(self, "Dictate", "API key works.")
        else:
            QMessageBox.warning(self, "Dictate", "API key rejected.")

    def values(self):
        # Sync current field back into the per-provider cache first, but only
        # if the shown provider actually has a key_field (local providers don't).
        pid = self.provider_box.currentData()
        if pid in self._keys:
            self._keys[pid] = self.api_key_edit.text().strip()
        return {
            "provider":              pid,
            "api_key":               self._keys.get("groq", ""),
            "openai_api_key":        self._keys.get("openai", ""),
            "mode":                  self.mode_box.currentData(),
            "key":                   self.key_box.currentData(),
            "model":                 self.model_box.currentData(),
            "threshold":             float(self.threshold_box.value()),
            "local_stt_num_threads": int(self.local_threads_box.value()),
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
        self._preload_local_if_needed()

        if not core.provider_key(self.cfg):
            QTimer.singleShot(250, self.first_run)

    def _preload_local_if_needed(self):
        """Kick off the parakeet recognizer load in the background so the first
        real dictation isn't slowed by the ~0.8s model load + warmup."""
        provider = self.cfg.get("provider")
        pdef = core.PROVIDERS.get(provider, {})
        if not pdef.get("is_local"):
            return
        try:
            import local_stt
        except ImportError:
            return
        if not local_stt.model_installed():
            return
        local_stt.preload(int(self.cfg.get("local_stt_num_threads", local_stt.DEFAULT_THREADS)))

    def first_run(self):
        QMessageBox.information(
            self,
            "Welcome to Dictate",
            "Open Settings to pick a transcription provider:\n\n"
            "  * Groq (cloud, free tier — console.groq.com)\n"
            "  * OpenAI (cloud — platform.openai.com)\n"
            "  * Local (offline, German, CPU — one-time 640 MB download)",
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
            self._preload_local_if_needed()

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

        self.listener = _make_listener(on_press, on_release)

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
            # Offload PipeWire close to keep the GUI thread responsive.
            threading.Thread(target=self.recorder.stop, daemon=True).start()
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
            # Discard branch also blocks on recorder.stop() → freeze. Offload.
            threading.Thread(target=self.recorder.stop, daemon=True).start()
            self.recording_active = False
            self.set_status("ready", f"Discarded (held {duration:.1f}s)")
            return
        self.stop_and_transcribe()
        self.recording_active = False

    def start_recording(self) -> bool:
        if not core.provider_key(self.cfg):
            self.set_status("error", "No API key — open Settings")
            return False
        self.recorder.start()
        self.set_status("recording", "Recording...")
        return True

    def stop_and_transcribe(self):
        # recorder.stop() can block for hundreds of ms on PipeWire close,
        # which freezes the GUI ("main on_release doesn't return"). Move the
        # whole pipeline (stop + transcribe) onto a worker thread and update
        # status immediately from the main thread instead.
        self.set_status("transcribing", "Transcribing...")
        threading.Thread(target=self._stop_and_transcribe_worker, daemon=True).start()

    def _stop_and_transcribe_worker(self):
        # Short trailing buffer: PipeWire has ~50-100ms input latency and
        # people typically finish the last syllable *after* they release the
        # hotkey. Without this the last word tends to get clipped.
        time.sleep(0.25)
        try:
            audio = self.recorder.stop()
        except Exception as e:
            self.bridge.error.emit(str(e))
            return
        if audio is None:
            self.bridge.transcribed.emit("")
            return
        try:
            text = core.transcribe(audio, self.cfg)
            self.bridge.transcribed.emit(text or "")
        except Exception as e:
            self.bridge.error.emit(str(e))

    def on_transcribed(self, text):
        if text:
            threading.Thread(target=core.type_text, args=(text,), daemon=True).start()
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
        QApplication.quit()


_LOCK_FD = None


def acquire_single_instance_lock():
    global _LOCK_FD
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = core.CONFIG_DIR / "dictate.lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        core.notify("Dictate is already running", urgency="low")
        return False
    _LOCK_FD = fd
    return True


def main():
    if not acquire_single_instance_lock():
        sys.exit(0)
    # Let Ctrl+C in the terminal actually kill us — Qt's C++ event loop
    # otherwise never yields to Python's SIGINT handler.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("Dictate")
    has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not has_tray)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
