#!/usr/bin/env python3
"""Dictate GUI — PyQt6 tray app for voice dictation via Groq Whisper."""
import fcntl
import sys
import threading
import time

import numpy as np
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSpinBox, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget,
)
from pynput import keyboard

import capslock
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
    capslock_done = pyqtSignal(bool, str)   # managed flag, message: result of the worker thread


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

        # --- Yorik URL row (shown only for the Yorik provider) ---
        self.yorik_url_edit = QLineEdit(self.cfg.get("yorik_url", core.DEFAULT_CONFIG["yorik_url"]))
        self.yorik_url_edit.setPlaceholderText("http://yorik.local:8000")
        self.form.addRow("Yorik URL:", self.yorik_url_edit)

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

        # Only offered with Caps Lock as hotkey: otherwise every press would also
        # toggle Caps Lock and the typed text would come out in capitals.
        self.capslock_box = QCheckBox("Disable the Caps Lock function (use the key only for dictation)")
        self.capslock_box.setChecked(bool(self.cfg.get("disable_capslock", True)))
        if capslock.is_available():
            self.capslock_box.setToolTip(
                "While Dictate is running, switches the Caps Lock function off in the desktop\n"
                "(GNOME keyboard option caps:none), after turning Caps Lock itself off so it\n"
                "cannot stay on. Only with direct access to the input devices (group 'input');\n"
                "until then Caps Lock keeps working. Closing Dictate gives the function back."
            )
        else:
            self.capslock_box.setEnabled(False)
            self.capslock_box.setToolTip("Needs GNOME (the gsettings tool) to change the desktop setting.")
        self.form.addRow("", self.capslock_box)
        self.key_box.currentIndexChanged.connect(self._update_capslock_row)

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
        self._update_capslock_row()

    def _update_capslock_row(self):
        self.form.setRowVisible(self.capslock_box, self.key_box.currentData() == "caps_lock")

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
        self.form.setRowVisible(self.yorik_url_edit, pid == "yorik")
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
        ok = core.test_api_key(key, self.provider_box.currentData(),
                               cfg={"yorik_url": self.yorik_url_edit.text().strip()})
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
            "disable_capslock":      self.capslock_box.isChecked(),
            "model":                 self.model_box.currentData(),
            "threshold":             float(self.threshold_box.value()),
            "local_stt_num_threads": int(self.local_threads_box.value()),
            "yorik_url":             self.yorik_url_edit.text().strip() or core.DEFAULT_CONFIG["yorik_url"],
            "yorik_token":           self._keys.get("yorik", ""),
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


LAST_MIN_WIDTH = 360     # smallest size of the "Last transcription" area; it grows from here
LAST_MIN_HEIGHT = 110    # the height it had before it became resizable


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
        self.bridge.capslock_done.connect(self._on_capslock_done)
        self._capslock_lock = threading.Lock()   # one desktop change at a time
        self.key_held = False
        self.combo = False
        self.recording_active = False
        self.listener = None
        self._target_keys = set()
        self._last_other = None

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
        self.last.setMinimumSize(LAST_MIN_WIDTH, LAST_MIN_HEIGHT)   # grows with the window from here
        self.last.setStyleSheet(
            "QTextEdit { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 6px; }"
        )
        v.addWidget(self.last, 1)

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
        # Caps Lock gets its function back when Dictate closes (a killed process
        # skips this; the next start repairs it).
        QApplication.instance().aboutToQuit.connect(self._release_capslock)
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
                    self._last_other = k
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
        self._apply_capslock_setting()

    def _listener_is_evdev(self):
        try:
            import evdev_listener
        except Exception:
            return False
        return isinstance(self.listener, evdev_listener.Listener)

    def _apply_capslock_setting(self):
        """Caps Lock as hotkey with 'disable' ticked: switch its function off in the
        desktop, but only while the evdev listener reads the raw key; without it
        (pynput fallback) the hotkey would stop working altogether.

        gsettings, xset and xdotool can hang, so the work runs on a worker thread; the
        result comes back through a signal and is handled on the GUI thread."""
        wanted = self.cfg.get("key") == "caps_lock" and bool(self.cfg.get("disable_capslock", True))
        evdev_active = self._listener_is_evdev()
        notice = ""
        if wanted and not evdev_active:
            core.log.info("caps lock function kept: the listener has no direct access to the input devices")
            notice = "Caps Lock keeps its function until Dictate can read the input devices (log out and in again)"
        threading.Thread(target=self._capslock_worker, args=(wanted and evdev_active, notice),
                         daemon=True).start()

    def _capslock_worker(self, wanted, notice):
        with self._capslock_lock:
            try:
                managed, message = capslock.apply(wanted, bool(self.cfg.get("capslock_managed")))
            except Exception as e:      # apply() reports its own errors; this is the last net
                core.log.exception("caps lock handling failed")
                managed, message = bool(self.cfg.get("capslock_managed")), f"Caps Lock handling failed: {e}"
        self.bridge.capslock_done.emit(managed, message or notice)

    def _on_capslock_done(self, managed, message):
        if message:
            core.log.warning(message)
            threading.Thread(target=core.notify, args=(message, "normal", 5000), daemon=True).start()
        if managed != bool(self.cfg.get("capslock_managed")):
            self.cfg["capslock_managed"] = managed
            core.save_config(self.cfg)

    def _release_capslock(self):
        """Give the Caps Lock function back if this app switched it off. Runs while the app
        closes, so it stays synchronous, but with short timeouts, and it does not wait for a
        busy worker for longer than a moment."""
        was_managed = bool(self.cfg.get("capslock_managed"))
        got_lock = self._capslock_lock.acquire(timeout=capslock.QUIT_LOCK_WAIT_S)
        try:
            managed, message = capslock.apply(False, was_managed,
                                              backend=capslock.Gsettings(timeout=capslock.QUIT_TIMEOUT_S))
        finally:
            if got_lock:
                self._capslock_lock.release()
        if message:
            core.log.warning(message)
        if managed != was_managed:
            self.cfg["capslock_managed"] = managed
            core.save_config(self.cfg)

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
            self._discard_take()
            self.recording_active = False
            self.set_status("ready", "Cancelled (combo)")
            held = time.monotonic() - self.press_time
            core.log.info("cancelled after %.1fs by %s", held, self._last_other)
            # A shortcut like Ctrl+C cancels within a moment and needs no
            # message; a long hold was a dictation that just got thrown away.
            min_hold = float(self.cfg.get("threshold", 0.0))
            if held >= max(min_hold, 1.0):
                core.notify(f"Recording cancelled after {held:.0f}s — another key or mouse button was pressed",
                            urgency="critical", timeout_ms=5000)

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
            self._discard_take()
            self.recording_active = False
            self.set_status("ready", f"Discarded (held {duration:.1f}s)")
            return
        self.stop_and_transcribe()
        self.recording_active = False

    def start_recording(self) -> bool:
        if not core.provider_key(self.cfg):
            self.set_status("error", "No API key — open Settings")
            return False
        try:
            self.recorder.start()
        except Exception as e:
            core.log.exception("could not open the microphone")
            self.on_error(f"Microphone: {e}")
            return False
        self.set_status("recording", "Recording...")
        return True

    def stop_and_transcribe(self):
        # recorder.stop() can block for hundreds of ms on PipeWire close,
        # which freezes the GUI ("main on_release doesn't return"). Move the
        # whole pipeline (stop + transcribe) onto a worker thread and update
        # status immediately from the main thread instead.
        self.set_status("transcribing", "Transcribing...")
        # Detach here, on the GUI thread: the take now belongs to the worker,
        # so pressing the hotkey again right away starts a fresh recording
        # instead of being swallowed by the worker's delayed stop.
        take = self.recorder.detach()
        threading.Thread(target=self._stop_and_transcribe_worker, args=(take,), daemon=True).start()

    def _discard_take(self):
        take = self.recorder.detach()
        threading.Thread(target=core.Recorder.finish, args=(take,), daemon=True).start()

    def _stop_and_transcribe_worker(self, take):
        # Short trailing buffer: PipeWire has ~50-100ms input latency and
        # people typically finish the last syllable *after* they release the
        # hotkey. Without this the last word tends to get clipped.
        time.sleep(0.25)
        audio = core.Recorder.finish(take)
        if audio is None:
            core.log.warning("no audio captured (mic delivered no frames)")
            self.bridge.error.emit("No audio captured — check the microphone")
            return
        secs = len(audio) / core.SAMPLE_RATE
        peak = int(np.abs(audio).max())
        t0 = time.monotonic()
        try:
            text = core.transcribe(audio, self.cfg)
        except Exception as e:
            core.log.exception("transcribe failed: %.1fs audio, peak %d", secs, peak)
            self.bridge.error.emit(str(e))
            return
        core.log.info("transcribed %.1fs audio, peak %d, %d chars in %.1fs",
                      secs, peak, len(text or ""), time.monotonic() - t0)
        if not text:
            # Peak is out of 32768; a live mic with speech is in the thousands.
            why = "microphone was silent" if peak < 300 else "no speech recognised"
            if peak >= 300:
                core.keep_failed_audio(audio)
            self.bridge.error.emit(f"Nothing transcribed ({secs:.0f}s) — {why}")
            return
        self.bridge.transcribed.emit(text)

    def on_transcribed(self, text):
        if text:
            threading.Thread(target=core.type_text, args=(text,), daemon=True).start()
            self.last.setPlainText(text)
        self.set_status("ready", "Ready")

    def on_error(self, msg):
        self.set_status("error", f"Error: {msg[:80]}")
        # The window is usually hidden in the tray, so the status label alone
        # means the dictation fails without anyone noticing.
        threading.Thread(target=core.notify, args=(msg[:200], "critical", 5000), daemon=True).start()

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
    core.setup_logging()
    core.log.info("started")
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
