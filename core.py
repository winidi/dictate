"""Shared dictation core: config, recording, transcription, typing."""
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import sys
from pynput import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1

PROVIDERS = {
    "groq": {
        "label":         "Groq",
        "url":           "https://api.groq.com/openai/v1/audio/transcriptions",
        "models_url":    "https://api.groq.com/openai/v1/models",
        "key_field":     "api_key",
        "key_prefix":    "gsk_",
        "default_model": "whisper-large-v3-turbo",
        "models": [
            ("whisper-large-v3-turbo (fastest)",   "whisper-large-v3-turbo"),
            ("whisper-large-v3 (most accurate)",    "whisper-large-v3"),
        ],
        "is_local":      False,
    },
    "openai": {
        "label":         "OpenAI",
        "url":           "https://api.openai.com/v1/audio/transcriptions",
        "models_url":    "https://api.openai.com/v1/models",
        "key_field":     "openai_api_key",
        "key_prefix":    "sk-",
        "default_model": "gpt-4o-mini-transcribe",
        "models": [
            ("gpt-4o-mini-transcribe (fast · cheap)", "gpt-4o-mini-transcribe"),
            ("gpt-4o-transcribe (best quality)",      "gpt-4o-transcribe"),
            ("whisper-1 (classic)",                   "whisper-1"),
        ],
        "is_local":      False,
    },
    "yorik": {
        "label":         "Yorik (home server)",
        "url":           "http://127.0.0.1:8000/v1/audio/transcriptions",
        "models_url":    None,
        "key_field":     "yorik_token",
        "key_prefix":    "yk_",
        "default_model": "parakeet",
        "models": [("Yorik's configured engine (Parakeet, CPU)", "parakeet")],
        "is_local":      False,
        "url_field":     "yorik_url",   # base URL, editable in Settings
    },
    "parakeet_local": {
        "label":         "Local (Parakeet DE, CPU offline)",
        "url":           None,
        "models_url":    None,
        "key_field":     None,
        "key_prefix":    None,
        "default_model": "parakeet-primeline-int8",
        "models": [("parakeet-primeline (German, int8)", "parakeet-primeline-int8")],
        "is_local":      True,
    },
}

# Backward-compat aliases used elsewhere in the codebase.
DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL    = PROVIDERS[DEFAULT_PROVIDER]["default_model"]
GROQ_URL         = PROVIDERS["groq"]["url"]
GROQ_MODELS_URL  = PROVIDERS["groq"]["models_url"]

CONFIG_DIR = Path.home() / ".config" / "dictate"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = Path.home() / ".local" / "state" / "dictate" / "dictate.log"

log = logging.getLogger("dictate")


def setup_logging():
    """Every recording leaves one line in LOG_PATH, so a dictation that went
    nowhere can be traced afterwards. Never logs transcribed text."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

KEY_MAP = {f"f{i}": {getattr(keyboard.Key, f"f{i}")} for i in range(1, 13)}
KEY_MAP.update({
    "pause":       {keyboard.Key.pause},
    "scroll_lock": {keyboard.Key.scroll_lock},
    "insert":      {keyboard.Key.insert},
    "home":        {keyboard.Key.home},
    "end":         {keyboard.Key.end},
    "page_up":     {keyboard.Key.page_up},
    "page_down":   {keyboard.Key.page_down},
    "caps_lock":   {keyboard.Key.caps_lock},
    "ctrl":        {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r},
    "ctrl_l":      {keyboard.Key.ctrl_l},
    "ctrl_r":      {keyboard.Key.ctrl_r},
    "alt":         {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r},
    "alt_l":       {keyboard.Key.alt_l},
    "alt_r":       {keyboard.Key.alt_r, keyboard.Key.alt_gr},
    "shift":       {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r},
    "super":       {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
    # Mouse-side buttons. Sentinels are tuples emitted by evdev_listener;
    # pynput won't see these (mouse events go through a separate listener) —
    # so these hotkeys only work when the evdev backend is active.
    "mouse_back":    {("mouse", "back")},
    "mouse_forward": {("mouse", "forward")},
    "mouse_side":    {("mouse", "side")},
    "mouse_extra":   {("mouse", "extra")},
    "mouse_task":    {("mouse", "task")},
})

DEFAULT_CONFIG = {
    "provider":              DEFAULT_PROVIDER,
    "api_key":               "",   # Groq key (kept name for backward compat)
    "openai_api_key":        "",   # OpenAI key
    "mode":                  "ptt",
    "key":                   "f9",
    "output_method":         "paste",   # how the text reaches the focused window: "paste" or "type"
    "model":                 DEFAULT_MODEL,
    "threshold":             0.0,
    "local_stt_num_threads": 4,    # sherpa-onnx CPU thread count for parakeet_local
    "yorik_url":             "http://127.0.0.1:8000",  # Yorik base URL (provider "yorik")
    "yorik_token":           "",   # personal API token from Yorik Settings → API tokens
}


def provider_url(cfg, p):
    """Endpoint URL for an HTTP provider; Yorik's comes from the config."""
    if p.get("url_field"):
        base = (cfg.get(p["url_field"]) or "").strip().rstrip("/")
        if base:
            return base + "/v1/audio/transcriptions"
    return p["url"]


def provider_key(cfg):
    """Return a truthy value iff the selected provider is ready to use.

    HTTP providers need an API key. The local provider is 'ready' when the
    ONNX model files have been downloaded — no key, no network.
    """
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if p.get("is_local"):
        try:
            import local_stt
            return "installed" if local_stt.model_installed() else ""
        except ImportError:
            return ""
    return cfg.get(p["key_field"], "")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def notify(msg, urgency="normal", timeout_ms=1500):
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", str(timeout_ms), "Dictate", msg],
            check=False,
        )
    except FileNotFoundError:
        pass


def type_text(text):
    """Type text into the focused window. True when it was typed, False when not."""
    if not text:
        return True
    # xdotool respects the active XKB layout (correct German umlauts, no y/z
    # swap on QWERTZ). Prefer it on X11 where it actually works. On Wayland,
    # native windows ignore XTEST so we must fall back to ydotool, which
    # writes raw US-QWERTY scancodes via /dev/uinput — layout-agnostic and
    # therefore wrong for non-US keyboards, but there is no better option
    # for global typing into native Wayland windows today.
    import os
    if os.name == "nt" or sys.platform == "darwin":
        # Windows / macOS: no xdotool or ydotool; pynput types through the
        # OS input API and respects the active keyboard layout.
        keyboard.Controller().type(text)
        return True
    on_x11 = os.environ.get("XDG_SESSION_TYPE") == "x11"
    tools = ["xdotool", "ydotool"] if on_x11 else ["ydotool", "xdotool"]
    for tool in tools:
        try:
            if tool == "xdotool":
                r = subprocess.run(["xdotool", "type", "--delay", "0", "--", text], check=False)
            else:
                r = subprocess.run(
                    ["ydotool", "type", "--key-delay", "0", "--", text],
                    check=False,
                    stderr=subprocess.DEVNULL,
                )
            if r.returncode != 0:
                log.error("%s exited with %s — text not typed", tool, r.returncode)
                notify(f"Typing failed ({tool} exit {r.returncode})", urgency="critical", timeout_ms=5000)
                return False
            return True
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError) as e:
            log.error("%s failed: %s — text not typed", tool, e)
            notify(f"Typing failed ({tool}: {e})", urgency="critical", timeout_ms=5000)
            return False
    log.error("no typing tool found (xdotool, ydotool) — text not typed")
    notify("Typing failed: neither xdotool nor ydotool is installed", urgency="critical", timeout_ms=5000)
    return False


# --- pasting instead of typing -------------------------------------------------------------
# Typing sends one fake key press per character, and a terminal with its input method
# handles each one separately, so long text can take seconds. Pasting is one key press.

OUTPUT_METHODS = ("paste", "type")
PASTE_SETTLE_S = 0.5   # time the focused program gets to read the clipboard before it is restored
_PASTE_LIKE_XTERM = ("xterm", "uxterm", "urxvt", "st", "st-256color")   # paste with Shift+Insert
_TERMINALS = ("gnome-terminal", "kgx", "konsole", "xfce4-terminal", "lxterminal", "mate-terminal",
              "terminator", "tilix", "alacritty", "kitty", "wezterm", "foot", "guake", "yakuake",
              "terminology", "sakura", "qterminal", "tabby", "cool-retro-term")   # Ctrl+Shift+V
_paste_fallback_told = False


class PasteUnavailable(Exception):
    """Pasting cannot work here (not X11, no clipboard tool); typing is the fallback."""


def paste_shortcut(window_class):
    """Key combination that pastes in the window with this WM_CLASS (names separated by spaces)."""
    names = (window_class or "").lower().split()
    if any(n in _PASTE_LIKE_XTERM for n in names):
        return "shift+Insert"
    if any(t in n for n in names for t in _TERMINALS):
        return "ctrl+shift+v"
    return "ctrl+v"


def _active_window_class():
    """WM_CLASS names of the focused X11 window, or '' when unknown."""
    try:
        win = subprocess.run(["xdotool", "getactivewindow"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if not win:
            return ""
        out = subprocess.run(["xprop", "-id", win, "WM_CLASS"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(re.findall(r'"([^"]*)"', out))


def _clipboard_commands():
    """(read, write) commands for the X11 clipboard, or None when neither xclip nor xsel is installed."""
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"])
    if shutil.which("xsel"):
        return (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"])
    return None


def _write_clipboard(cmd, data):
    # The tool forks to keep owning the clipboard, so its output must not be piped back to us.
    subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=5)


def _read_clipboard(cmd):
    """Current clipboard text as bytes, or None when it is empty or not text."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def paste_text(text):
    """Put text on the clipboard, press the paste shortcut of the focused window, then restore
    the previous clipboard text. True when the shortcut was sent, False when that failed.
    Raises PasteUnavailable when pasting cannot work here (nothing was inserted then)."""
    if not text:
        return True
    if os.environ.get("XDG_SESSION_TYPE") != "x11" or not shutil.which("xdotool"):
        raise PasteUnavailable("pasting needs an X11 session with xdotool; Wayland does not tell "
                               "which window has the focus")
    tools = _clipboard_commands()
    if tools is None:
        raise PasteUnavailable("no clipboard tool, install xclip or xsel")
    read_cmd, write_cmd = tools
    previous = _read_clipboard(read_cmd)
    shortcut = paste_shortcut(_active_window_class())
    try:
        _write_clipboard(write_cmd, text.encode())
    except (OSError, subprocess.SubprocessError) as e:
        raise PasteUnavailable(f"could not write the clipboard: {e}") from e
    try:
        r = subprocess.run(["xdotool", "key", "--clearmodifiers", shortcut], check=False, timeout=5)
        if r.returncode != 0:
            log.error("xdotool exited with %s — paste shortcut %s not sent", r.returncode, shortcut)
            notify(f"Pasting failed (xdotool exit {r.returncode})", urgency="critical", timeout_ms=5000)
            return False
        time.sleep(PASTE_SETTLE_S)   # the program reads the clipboard after the key press
        return True
    except (OSError, subprocess.SubprocessError) as e:    # TimeoutExpired, tool vanished, ...
        log.error("paste key press failed: %s", e)
        notify(f"Pasting failed ({e})", urgency="critical", timeout_ms=5000)
        return False
    finally:
        # Only text can be restored; if the clipboard held something else, the dictated text stays.
        if previous is not None:
            try:
                _write_clipboard(write_cmd, previous)
            except (OSError, subprocess.SubprocessError):
                log.warning("could not restore the previous clipboard text")


class _OrderedLock:
    """A lock that lets callers in strictly in the order they asked (threading.Lock does not
    promise that). Used as a context manager; released on every exit, also on exceptions."""

    def __init__(self):
        self._cond = threading.Condition()
        self._next_ticket = 0
        self._serving = 0

    def __enter__(self):
        with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving:
                self._cond.wait()

    def __exit__(self, *exc_info):
        with self._cond:
            self._serving += 1
            self._cond.notify_all()


# One dictation is inserted at a time. Without this, two results arriving together would
# interleave their typed characters, and one paste could read, replace and restore the
# clipboard in the middle of another one.
_output_lock = _OrderedLock()


def output_text(text, method="paste"):
    """Get text into the focused window by pasting or by typing key presses. True when it worked.
    Pasting that cannot work here falls back to typing, with one notice per run.
    Calls are served one after the other, in the order they arrive."""
    with _output_lock:
        return _output_text_locked(text, method)


def _output_text_locked(text, method):
    global _paste_fallback_told
    t0 = time.monotonic()
    used, ok = "type", None
    if method != "type":
        try:
            ok = paste_text(text)
            used = "paste"
        except PasteUnavailable as e:
            log.warning("paste not possible, typing instead: %s", e)
            if not _paste_fallback_told:
                _paste_fallback_told = True
                notify(f"Pasting is not possible here ({e}); typing the text instead",
                       urgency="normal", timeout_ms=6000)
    if ok is None:
        ok = type_text(text)
    log.info("output via %s: %d chars in %.2fs, %s", used, len(text or ""),
             time.monotonic() - t0, "ok" if ok else "failed")
    return ok


class Recorder:
    """Each recording is a "take" (stream + its frames). detach() hands the
    current take to the caller without touching PipeWire, so a new start()
    can never collide with a take that is still being finished on a worker
    thread (trailing buffer + slow PipeWire close)."""

    def __init__(self):
        self._take = None
        self.lock = threading.Lock()

    @property
    def recording(self):
        return self._take is not None

    def start(self):
        with self.lock:
            if self._take is not None:
                return
            frames = []

            def callback(indata, n, time_info, status):
                frames.append(indata.copy())

            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=callback,
            )
            stream.start()
            self._take = (stream, frames)

    def detach(self):
        """Non-blocking: give up the current take (or None). Safe on the GUI thread."""
        with self.lock:
            take, self._take = self._take, None
            return take

    @staticmethod
    def finish(take):
        """Close a detached take and return its audio, or None if it has none.
        Blocks for hundreds of ms on PipeWire close — call from a worker."""
        if take is None:
            return None
        stream, frames = take
        try:
            stream.stop()
            stream.close()
        except Exception:
            # A vanished mic must not lose what was already captured.
            log.exception("closing input stream failed")
        if not frames:
            return None
        return np.concatenate(frames, axis=0)

    def stop(self):
        return self.finish(self.detach())


def to_wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf


PENDING_DIR = LOG_PATH.parent / "pending"
PENDING_KEEP = 30                    # at most this many saved recordings
PENDING_MAX_AGE_S = 14 * 86400       # and none older than two weeks


def save_pending(audio):
    """Write a recording to disk before it is transcribed, so an error, a crash or
    a power cut cannot lose it. It is deleted only once its text was typed
    (discard_pending). Returns the path, or None when it could not be written."""
    import time
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stem = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int(now * 1000) % 1000:03d}"
        path = PENDING_DIR / f"{stem}.wav"
        for n in range(1, 100):      # same millisecond twice: never overwrite, keep the order
            if not path.exists():
                break
            path = PENDING_DIR / f"{stem}{n:02d}.wav"
        part = path.with_suffix(".part")
        part.write_bytes(to_wav_bytes(audio).getvalue())
        part.replace(path)           # never a half-written .wav
        prune_pending()
        return path
    except Exception:
        log.exception("could not save the recording")
        return None


def list_pending():
    """Saved recordings that still wait for a successful transcription, newest first."""
    if not PENDING_DIR.exists():
        return []
    return sorted(PENDING_DIR.glob("*.wav"), reverse=True)


def load_pending(path):
    """Read a saved recording back as int16 audio of shape (N, 1)."""
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        return np.frombuffer(frames, dtype=np.int16).reshape(-1, wf.getnchannels())


def discard_pending(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("could not delete %s", path)


def prune_pending(now=None):
    """Keep the disk tidy: drop recordings beyond PENDING_KEEP and older than PENDING_MAX_AGE_S."""
    import time
    now = time.time() if now is None else now
    for i, path in enumerate(list_pending()):
        try:
            too_old = now - path.stat().st_mtime > PENDING_MAX_AGE_S
            if i >= PENDING_KEEP or too_old:
                path.unlink()
                log.info("removed old saved recording %s", path.name)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not prune %s", path)


def transcribe(audio, cfg):
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if p.get("is_local"):
        import local_stt
        threads = int(cfg.get("local_stt_num_threads", local_stt.DEFAULT_THREADS))
        return local_stt.get_recognizer(num_threads=threads).transcribe(audio)
    # HTTP providers
    key = cfg.get(p["key_field"], "")
    model = cfg.get("model") or p["default_model"]
    wav = to_wav_bytes(audio)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {"model": model, "response_format": "text"}
    headers = {"Authorization": f"Bearer {key}"}
    r = requests.post(provider_url(cfg, p), files=files, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text.strip()


def test_api_key(api_key, provider=DEFAULT_PROVIDER, cfg=None):
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if provider == "yorik":
        # A token is valid when Yorik lists the caller's tokens with it.
        base = ((cfg or {}).get("yorik_url") or DEFAULT_CONFIG["yorik_url"]).strip().rstrip("/")
        try:
            r = requests.get(base + "/api/tokens", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False
    if p.get("is_local") or not p.get("models_url"):
        return False  # no key to test — caller should not have called this
    try:
        r = requests.get(
            p["models_url"],
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False
