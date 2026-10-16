#!/usr/bin/env python3
"""Dictate GUI — PyQt6 tray app for voice dictation (Linux, Windows, macOS)."""
import sys
import threading
import time

import numpy as np
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)
from pynput import keyboard, mouse

import core
import polish


class PynputListener:
    """Global hotkey listener on pynput: a keyboard hook plus a mouse hook, so
    the thumb buttons work as hotkeys and any click while the hotkey is held
    cancels the recording, the same as with evdev. Windows uses low-level
    hooks here, macOS the event tap, X11 XRecord."""

    _BUTTONS = {
        getattr(mouse.Button, name): sentinel
        for name, sentinel in (
            ("x1", ("mouse", "x1")),
            ("x2", ("mouse", "x2")),
            ("button8", ("mouse", "x1")),   # X11 names for the same buttons
            ("button9", ("mouse", "x2")),
            ("left", ("mouse", "left")),
            ("right", ("mouse", "right")),
            ("middle", ("mouse", "middle")),
        )
        if getattr(mouse.Button, name, None) is not None
    }

    def __init__(self, on_press, on_release):
        self._kb = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._kb.daemon = True

        def on_click(x, y, button, pressed):
            key = self._BUTTONS.get(button)
            if key is None:
                return
            cb = on_press if pressed else on_release
            cb(key)

        self._mouse = mouse.Listener(on_click=on_click)
        self._mouse.daemon = True

    def start(self):
        self._kb.start()
        self._mouse.start()

    def stop(self):
        for l in (self._kb, self._mouse):
            try:
                l.stop()
            except Exception:
                pass


def _make_listener(on_press, on_release):
    """Linux: prefer evdev (works on Wayland+Xorg), fall back to pynput.
    Windows/macOS: pynput straight away, there is no /dev/input."""
    if core.IS_LINUX:
        try:
            import evdev_listener
            l = evdev_listener.Listener(on_press=on_press, on_release=on_release)
            l.start()
            core.log.info("keyboard listener: evdev")
            return l
        except Exception as e:
            core.log.warning("evdev unavailable (%s); falling back to pynput", e)
    l = PynputListener(on_press, on_release)
    l.start()
    core.log.info("keyboard listener: pynput")
    return l


class Bridge(QObject):
    hotkey_press = pyqtSignal()
    hotkey_release = pyqtSignal()
    other_press = pyqtSignal()
    transcribed = pyqtSignal(str, str)   # final text, raw transcript
    polishing = pyqtSignal()
    error = pyqtSignal(str)
    notify = pyqtSignal(str, str, int)   # msg, urgency, timeout_ms


class SettingsDialog(QDialog):
    # Bridge signals for the background download threads.
    _dl_status = pyqtSignal(str)
    _dl_done = pyqtSignal(bool, str)
    _pl_status = pyqtSignal(str)
    _pl_done = pyqtSignal(bool, str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dictate — Settings")
        self.setMinimumWidth(600)
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
            if not core.IS_LINUX and k in core.EVDEV_ONLY_KEYS:
                continue
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

        self.hint = QLabel()
        self.hint.setOpenExternalLinks(True)
        self.hint.setStyleSheet("color: #777; padding: 6px 0;")

        page1 = QWidget()
        v1 = QVBoxLayout(page1)
        v1.addLayout(self.form)
        v1.addWidget(self.hint)
        v1.addStretch()

        tabs = QTabWidget()
        tabs.addTab(page1, "Transcription")
        tabs.addTab(self._build_polish_page(), "Written text")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        # Wire the download-worker bridges, then sync UI to current provider.
        self._dl_status.connect(self.local_status_label.setText)
        self._dl_done.connect(self._on_download_done)
        self._pl_status.connect(self.pl_status_label.setText)
        self._pl_done.connect(self._on_polish_download_done)
        self.refresh_model_box(self.cfg.get("model", core.DEFAULT_MODEL))
        self.on_provider_changed()
        self._refresh_polish_status()

    # ------------------------------------------------------------------ polishing
    def _build_polish_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        f = QFormLayout()
        cfg = self.cfg

        self.pl_enabled = QCheckBox("Turn the dictation into written text before typing it")
        self.pl_enabled.setChecked(bool(cfg.get("polish_enabled", False)))
        self.pl_enabled.setToolTip(
            "Optional. A local Gemma 4 model (llama.cpp) removes filler words and\n"
            "false starts, fixes punctuation and spelling, and keeps your wording.\n"
            "If the model is slow or missing, the raw transcript is typed instead."
        )
        f.addRow(self.pl_enabled)
        self.pl_form = f

        self.pl_backend_box = QComboBox()
        self.pl_backend_box.addItem("Built-in — Dictate downloads llama.cpp and a Gemma 4 model", "builtin")
        self.pl_backend_box.addItem("A server that is already running (Ollama, LM Studio, llama-server, vLLM …)", "external")
        i = self.pl_backend_box.findData(cfg.get("polish_backend", "builtin"))
        self.pl_backend_box.setCurrentIndex(max(i, 0))
        self.pl_backend_box.currentIndexChanged.connect(self._refresh_polish_status)
        f.addRow("Model source:", self.pl_backend_box)

        self.pl_ext_url = QLineEdit(cfg.get("polish_external_url") or core.DEFAULT_CONFIG["polish_external_url"])
        self.pl_ext_url.setPlaceholderText("http://127.0.0.1:11434/v1")
        self.pl_ext_url.setToolTip(
            "Base URL of an OpenAI-compatible server, usually ending in /v1.\n"
            "Ollama: http://127.0.0.1:11434/v1   LM Studio: http://127.0.0.1:1234/v1\n"
            "llama-server: http://127.0.0.1:8080/v1   vLLM: http://host:8000/v1\n"
            "Text leaves this machine only if the URL points to another one."
        )
        f.addRow("Server URL:", self.pl_ext_url)
        self.pl_ext_model = QLineEdit(cfg.get("polish_external_model") or "")
        self.pl_ext_model.setPlaceholderText("e.g. gemma4:e2b — leave empty if the server serves one model")
        f.addRow("Model name:", self.pl_ext_model)
        self.pl_ext_key = QLineEdit(cfg.get("polish_external_key") or "")
        self.pl_ext_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.pl_ext_key.setPlaceholderText("optional")
        f.addRow("API key:", self.pl_ext_key)
        self.pl_ext_test_btn = QPushButton("Test connection")
        self.pl_ext_test_btn.clicked.connect(self.on_test_external)
        f.addRow("", self.pl_ext_test_btn)

        self.pl_model_box = QComboBox()
        for mid, m in polish.MODELS.items():
            self.pl_model_box.addItem(m["label"], mid)
        i = self.pl_model_box.findData(cfg.get("polish_model", polish.DEFAULT_MODEL))
        self.pl_model_box.setCurrentIndex(max(i, 0))
        self.pl_model_box.currentIndexChanged.connect(self._refresh_polish_status)
        f.addRow("Model:", self.pl_model_box)

        self.pl_device_box = QComboBox()
        for did, label in polish.DEVICES:
            self.pl_device_box.addItem(label, did)
        i = self.pl_device_box.findData(cfg.get("polish_device", "auto"))
        self.pl_device_box.setCurrentIndex(max(i, 0))
        self.pl_device_box.currentIndexChanged.connect(self._refresh_polish_status)
        self.pl_device_box.setToolTip(
            "Where llama.cpp runs the model.\n"
            "GPU: much faster, usually well under a second per dictation; the model\n"
            "    stays in VRAM (about 2.5 GB for E2B) while Dictate runs.\n"
            "CPU: works on any machine, but slower — expect a second or more per\n"
            "    dictation and several seconds for a long one, depending on the CPU.\n"
            "    Raise \"Give up after\" accordingly.\n"
            "Auto picks the NVIDIA build when an NVIDIA driver is present, else CPU.\n"
            "Changing this downloads the matching llama.cpp build (CUDA ~1.3 GB,\n"
            "Vulkan ~150 MB, CPU ~30 MB) and restarts the model."
        )
        f.addRow("Run on:", self.pl_device_box)

        import os as _os
        self.pl_threads_box = QSpinBox()
        self.pl_threads_box.setRange(1, max(1, _os.cpu_count() or 1))
        self.pl_threads_box.setValue(int(cfg.get("polish_threads", polish.DEFAULT_THREADS)))
        self.pl_threads_box.setToolTip("CPU threads for llama-server. Only matters when running on the CPU.")
        f.addRow("CPU threads:", self.pl_threads_box)

        self.pl_timeout_box = QDoubleSpinBox()
        self.pl_timeout_box.setRange(1.0, 60.0)
        self.pl_timeout_box.setSingleStep(1.0)
        self.pl_timeout_box.setDecimals(0)
        self.pl_timeout_box.setSuffix(" s")
        self.pl_timeout_box.setValue(float(cfg.get("polish_timeout", polish.DEFAULT_TIMEOUT)))
        self.pl_timeout_box.setToolTip("If the model takes longer than this, the raw transcript is typed.")
        f.addRow("Give up after:", self.pl_timeout_box)

        self.pl_status_label = QLabel()
        self.pl_status_label.setWordWrap(True)
        f.addRow("Runtime + model:", self.pl_status_label)
        self.pl_download_btn = QPushButton("Download")
        self.pl_download_btn.clicked.connect(self.on_download_polish)
        f.addRow("", self.pl_download_btn)
        v.addLayout(f)

        v.addWidget(QLabel("Names and terms — one per line (optional). Fixes words the recognizer spells by sound:"))
        self.pl_vocab_edit = QPlainTextEdit(cfg.get("polish_vocabulary", "") or "")
        self.pl_vocab_edit.setPlaceholderText("Dirk Winiecki\nParakeet\nllama.cpp")
        self.pl_vocab_edit.setMaximumHeight(70)
        v.addWidget(self.pl_vocab_edit)

        v.addWidget(QLabel("How you write — paste a few of your own texts (optional):"))
        self.pl_style_edit = QPlainTextEdit(cfg.get("polish_style", "") or "")
        self.pl_style_edit.setPlaceholderText("Your own messages, posts or mails. The model matches this voice.")
        self.pl_style_edit.setMaximumHeight(110)
        v.addWidget(self.pl_style_edit)

        lbl = QLabel("Per window — one line each, \"word in window title = instructions\"; \"* = ...\" is the default:")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        self.pl_profiles_edit = QPlainTextEdit(self._profiles_to_text(cfg))
        self.pl_profiles_edit.setMaximumHeight(110)
        self.pl_profiles_edit.setToolTip(
            "The word on the left is looked for in the title and program name of the\n"
            "window that will receive the text (case does not matter). The first\n"
            "match wins. On GNOME Wayland the window title is not available, so\n"
            "the default line is used there."
        )
        v.addWidget(self.pl_profiles_edit)
        return page

    @staticmethod
    def _profiles_to_text(cfg):
        profiles = cfg.get("polish_profiles")
        if profiles is None:
            profiles = polish.DEFAULT_PROFILES
        lines = [f"{p.get('match', '')} = {p.get('instructions', '')}" for p in profiles]
        lines.append("* = " + (cfg.get("polish_default_instructions") or polish.DEFAULT_INSTRUCTIONS))
        return "\n".join(lines)

    def _polish_cfg(self):
        """The polishing part of the config as currently shown in the dialog."""
        profiles, default = [], ""
        for line in self.pl_profiles_edit.toPlainText().splitlines():
            if "=" not in line:
                continue
            m, instr = line.split("=", 1)
            m, instr = m.strip(), instr.strip()
            if m == "*":
                default = instr
            elif m:
                profiles.append({"match": m, "instructions": instr})
        return {
            "polish_enabled":  self.pl_enabled.isChecked(),
            "polish_backend":  self.pl_backend_box.currentData(),
            "polish_external_url":   self.pl_ext_url.text().strip(),
            "polish_external_model": self.pl_ext_model.text().strip(),
            "polish_external_key":   self.pl_ext_key.text().strip(),
            "polish_model":    self.pl_model_box.currentData(),
            "polish_device":   self.pl_device_box.currentData(),
            "polish_threads":  int(self.pl_threads_box.value()),
            "polish_timeout":  float(self.pl_timeout_box.value()),
            "polish_style":    self.pl_style_edit.toPlainText().strip(),
            "polish_vocabulary": self.pl_vocab_edit.toPlainText().strip(),
            "polish_default_instructions": default,
            "polish_profiles": profiles,
        }

    def on_test_external(self):
        self.pl_ext_test_btn.setEnabled(False)
        self.pl_ext_test_btn.setText("Testing...")
        QApplication.processEvents()
        ok, msg = polish.test_external(self.pl_ext_url.text(), self.pl_ext_key.text(), self.pl_ext_model.text())
        self.pl_ext_test_btn.setEnabled(True)
        self.pl_ext_test_btn.setText("Test connection")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Dictate", msg)

    def _refresh_polish_status(self):
        ext = self.pl_backend_box.currentData() == "external"
        for w in (self.pl_ext_url, self.pl_ext_model, self.pl_ext_key, self.pl_ext_test_btn):
            self.pl_form.setRowVisible(w, ext)
        for w in (self.pl_model_box, self.pl_device_box, self.pl_threads_box,
                  self.pl_status_label, self.pl_download_btn):
            self.pl_form.setRowVisible(w, not ext)
        if ext:
            return
        if not polish.supported():
            self.pl_status_label.setText("Not available on this platform.")
            self.pl_download_btn.setEnabled(False)
            return
        pc = self._polish_cfg()
        if polish.installed(pc):
            self.pl_status_label.setText(f"Installed and ready (llama.cpp {polish.LLAMA_BUILD}, {polish.resolve_device(pc['polish_device'])}).")
            self.pl_download_btn.setText("Re-download")
        else:
            need = polish.download_size_mb(pc)
            free = polish.free_space_mb()
            self.pl_status_label.setText(
                f"Not installed. Download is about {need / 1000:.1f} GB; {free / 1000:.1f} GB free on disk."
            )
            self.pl_download_btn.setText(f"Download (~{need / 1000:.1f} GB)")
        self.pl_download_btn.setEnabled(True)

    def on_download_polish(self):
        self.pl_download_btn.setEnabled(False)
        self.pl_download_btn.setText("Downloading...")
        self.pl_status_label.setText("Starting download...")
        pc = self._polish_cfg()
        threading.Thread(target=self._polish_download_worker, args=(pc,), daemon=True).start()

    def _polish_download_worker(self, pc):
        try:
            polish.download_all(pc, progress_cb=lambda m: self._pl_status.emit(m))
            self._pl_done.emit(True, "Installed.")
        except Exception as e:
            self._pl_done.emit(False, f"Download failed: {e}")

    def _on_polish_download_done(self, ok, msg):
        self.pl_status_label.setText(msg)
        self.pl_download_btn.setEnabled(True)
        self.pl_download_btn.setText("Re-download" if ok else "Retry download")

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
                "sherpa-onnx not installed — run: pip install sherpa-onnx huggingface_hub"
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
            "model":                 self.model_box.currentData(),
            "threshold":             float(self.threshold_box.value()),
            "local_stt_num_threads": int(self.local_threads_box.value()),
            "yorik_url":             self.yorik_url_edit.text().strip() or core.DEFAULT_CONFIG["yorik_url"],
            "yorik_token":           self._keys.get("yorik", ""),
            **self._polish_cfg(),
        }


def _windows_taskbar_is_dark():
    """Windows draws the tray on the taskbar, which follows the *system*
    theme (dark by default). Read it so the icon stays visible."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 0
    except Exception:
        return True


def make_tray_icon():
    if core.IS_WINDOWS and _windows_taskbar_is_dark():
        return make_mic_icon("#f0f0f0")
    return make_mic_icon()


def make_mic_icon(color="#2b2b2b", size=64):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 64, size / 64)
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
        self.bridge.polishing.connect(lambda: self.set_status("transcribing", "Polishing..."))
        self.bridge.error.connect(self.on_error)
        self.bridge.notify.connect(self.on_notify)
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
        self.last.setMaximumHeight(110)
        self.last.setStyleSheet(
            "QTextEdit { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 6px; }"
        )
        v.addWidget(self.last)

        v.addStretch()

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_tray_icon(), self)
            self.tray.setToolTip("Dictate")
            menu = QMenu()
            a_show = QAction("Show window", self)
            a_show.triggered.connect(self.show_window)
            self.a_polish = QAction("Written text (polish)", self)
            self.a_polish.setCheckable(True)
            self.a_polish.setChecked(bool(self.cfg.get("polish_enabled")))
            self.a_polish.toggled.connect(self.on_toggle_polish)
            a_quit = QAction("Quit", self)
            a_quit.triggered.connect(QApplication.quit)
            menu.addAction(a_show)
            menu.addAction(self.a_polish)
            menu.addSeparator()
            menu.addAction(a_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray)
            self.tray.show()
            if not core.IS_LINUX:
                # No notify-send here: desktop notifications come from the
                # tray icon. Worker threads call core.notify, the signal
                # hops onto the GUI thread.
                core.set_notifier(lambda m, u, t: self.bridge.notify.emit(m, u, t))

        self.set_status("ready", "Ready")
        self.update_info()
        self.start_listener()
        self._preload_local_if_needed()
        self._start_polish_if_needed()

        if not core.provider_key(self.cfg):
            QTimer.singleShot(250, self.first_run)

    def _start_polish_if_needed(self):
        """Bring llama-server up in the background so the first polish does
        not wait for the model load. No-op when the feature is off."""
        if not self.cfg.get("polish_enabled"):
            polish.stop_server()
            return
        try:
            if polish.get_server(self.cfg) is None:
                self.set_status("error", "Polishing is on but not installed — open Settings")
        except Exception as e:
            core.log.exception("could not start llama-server")
            self.on_error(f"Polishing: {e}")

    def on_toggle_polish(self, on):
        self.cfg["polish_enabled"] = bool(on)
        core.save_config(self.cfg)
        self.update_info()
        self._start_polish_if_needed()
        if on and not polish.installed(self.cfg):
            core.notify("Polishing is not installed yet — open Settings → Written text", urgency="normal", timeout_ms=4000)

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
        pol = "     Written text: on" if self.cfg.get("polish_enabled") else ""
        self.info_label.setText(
            f"Mode: {mode_name}     Hotkey: {self.cfg['key'].upper()}     "
            f"Model: {self.cfg['model']}{thr_str}{pol}"
        )

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.update(dlg.values())
            core.save_config(self.cfg)
            self.restart_listener()
            self.update_info()
            self._preload_local_if_needed()
            if hasattr(self, "a_polish"):
                self.a_polish.blockSignals(True)
                self.a_polish.setChecked(bool(self.cfg.get("polish_enabled")))
                self.a_polish.blockSignals(False)
            self._start_polish_if_needed()

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
        # The window that has the focus now is the one that gets the text.
        ctx = polish.active_window() if self.cfg.get("polish_enabled") else ("", "")
        threading.Thread(target=self._stop_and_transcribe_worker, args=(take, ctx), daemon=True).start()

    def _discard_take(self):
        take = self.recorder.detach()
        threading.Thread(target=core.Recorder.finish, args=(take,), daemon=True).start()

    def _stop_and_transcribe_worker(self, take, ctx=("", "")):
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
        raw = text
        if self.cfg.get("polish_enabled"):
            self.bridge.polishing.emit()
            t1 = time.monotonic()
            try:
                text = polish.polish(self.cfg, raw, *ctx)
                core.log.info("polished %d -> %d chars in %.1fs (%s)", len(raw), len(text),
                              time.monotonic() - t1, ctx[0] or "no window")
            except Exception as e:
                # Never lose a dictation over the polish: type the raw text.
                core.log.warning("polish skipped after %.1fs: %s", time.monotonic() - t1, e)
                core.notify(f"Typed as spoken — polishing skipped: {str(e)[:120]}", urgency="normal", timeout_ms=4000)
                text = raw
        self.bridge.transcribed.emit(text, raw)

    def on_transcribed(self, text, raw=""):
        if text:
            threading.Thread(target=core.type_text, args=(text,), daemon=True).start()
            if raw and raw != text:
                self.last.setPlainText(f"{text}\n\n— as spoken: {raw}")
            else:
                self.last.setPlainText(text)
        self.set_status("ready", "Ready")

    def on_notify(self, msg, urgency, timeout_ms):
        if not self.tray:
            return
        icon = (QSystemTrayIcon.MessageIcon.Critical if urgency == "critical"
                else QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage("Dictate", msg, icon, timeout_ms)

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
    """Hold an OS file lock for the lifetime of the process. The lock goes
    away with the process, so a crash never leaves a stale lock behind.
    Returns False if another Dictate holds it."""
    global _LOCK_FD
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = core.CONFIG_DIR / "dictate.lock"
    fd = open(lock_path, "w")
    try:
        if core.IS_WINDOWS:
            import msvcrt
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        # Another instance holds the lock: BlockingIOError (flock on Linux/macOS),
        # PermissionError (msvcrt.locking on Windows). Other OSErrors propagate.
        fd.close()
        return False
    _LOCK_FD = fd
    return True


def main():
    # Let Ctrl+C in the terminal actually kill us — Qt's C++ event loop
    # otherwise never yields to Python's SIGINT handler.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("Dictate")
    app.aboutToQuit.connect(polish.stop_server)
    if not acquire_single_instance_lock():
        if core.IS_LINUX:
            core.notify("Dictate is already running", urgency="low")
        else:
            QMessageBox.information(None, "Dictate", "Dictate is already running.")
        sys.exit(0)
    core.setup_logging()
    core.log.info("started")
    has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not has_tray)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
