"""Shared dictation core: config, recording, transcription, typing."""
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from pynput import keyboard

from i18n import tr

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

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

# A fresh install starts on the offline model: no key, no network, nothing
# leaves the machine. DEFAULT_PROVIDER stays Groq for the fallbacks and for
# configs from before providers existed, which were Groq-only (load_config).
FRESH_PROVIDER   = "parakeet_local"
DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL    = PROVIDERS[DEFAULT_PROVIDER]["default_model"]
GROQ_URL         = PROVIDERS["groq"]["url"]
GROQ_MODELS_URL  = PROVIDERS["groq"]["models_url"]

if IS_WINDOWS:
    # Settings roam with the profile; logs, failed clips and the model stay
    # on this machine.
    _roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    _local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    CONFIG_DIR = _roaming / "Dictate"
    STATE_DIR = _local / "Dictate"
    DATA_DIR = _local / "Dictate"
else:
    CONFIG_DIR = Path.home() / ".config" / "dictate"
    STATE_DIR = Path.home() / ".local" / "state" / "dictate"
    DATA_DIR = Path.home() / ".local" / "share" / "dictate"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = STATE_DIR / "dictate.log"

log = logging.getLogger("dictate")


def setup_logging():
    """Every recording leaves one line in LOG_PATH, so a dictation that went
    nowhere can be traced afterwards. Never logs transcribed text."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

# Sentinel for the Copilot key, like the ("mouse", ...) tuples below: it has
# no pynput equivalent and only the evdev listener can deliver it.
COPILOT_KEY = ("key", "copilot")

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
    # Mouse-side buttons. Sentinels are tuples: evdev_listener emits the
    # Linux button names, the pynput mouse listener (Windows, macOS, X11)
    # emits ("mouse", "x1") / ("mouse", "x2") for the two thumb buttons
    # Windows calls XBUTTON1 (back) and XBUTTON2 (forward). Logitech's
    # BTN_SIDE/BTN_EXTRA are the same two physical buttons, so both names
    # accept them. mouse_task has no pynput equivalent.
    "mouse_back":    {("mouse", "back"),    ("mouse", "x1")},
    "mouse_forward": {("mouse", "forward"), ("mouse", "x2")},
    "mouse_side":    {("mouse", "side"),    ("mouse", "x1")},
    "mouse_extra":   {("mouse", "extra"),   ("mouse", "x2")},
    "mouse_task":    {("mouse", "task")},
    # The Microsoft Copilot key. Standards-compliant keyboards send
    # KEY_ASSISTANT; HP laptops send the firmware macro Super+Shift+F23. The
    # evdev listener reports both as this one sentinel (see evdev_listener).
    "copilot":       {COPILOT_KEY},
})

# Hotkeys the pynput backend (everything but Linux-evdev) cannot deliver.
EVDEV_ONLY_KEYS = {"mouse_task", "copilot"}

DEFAULT_CONFIG = {
    "provider":              FRESH_PROVIDER,
    "api_key":               "",   # Groq key (kept name for backward compat)
    "openai_api_key":        "",   # OpenAI key
    "mode":                  "ptt",
    "key":                   "f9",
    "model":                 PROVIDERS[FRESH_PROVIDER]["default_model"],
    "threshold":             2.0,   # seconds; a shorter hold is discarded
    "output_method":         "paste",  # how the text reaches the window: paste | type (key presses)
    "disable_capslock":      True,  # hotkey Caps Lock: switch the Caps Lock function off in the desktop
    "capslock_managed":      False, # True once Dictate itself switched it off (so it may undo that)
    "local_stt_num_threads": 4,    # sherpa-onnx CPU thread count for parakeet_local
    "ui_language":           "auto",   # auto (system) | en | de
    # Names and terms, one per line. Cloud providers get them as the Whisper
    # prompt, the polish model as its word list; "Wrong -> Right" lines are
    # replaced in every transcript, also offline (see parse_vocabulary).
    "vocabulary":            "",
    "yorik_url":             "http://127.0.0.1:8000",  # Yorik base URL (provider "yorik")
    "yorik_token":           "",   # personal API token from Yorik Settings → API tokens
    # Optional polishing of the transcript through a local Gemma 4 model
    # (see polish.py). Off by default; nothing is downloaded until the user
    # asks for it in Settings.
    "polish_enabled":        False,
    "polish_backend":        "builtin", # builtin (Dictate's own llama-server) | external (a server already running)
    "polish_external_url":   "http://127.0.0.1:11434/v1",  # OpenAI-compatible base URL (Ollama default)
    "polish_external_model": "",       # model name the external server expects, e.g. "gemma4:e2b"
    "polish_external_key":   "",       # API key, if the server wants one
    "polish_model":          "gemma4-e2b-q4",
    "polish_device":         "auto",   # auto | cuda | vulkan | cpu
    "polish_threads":        4,        # CPU threads for llama-server
    "polish_timeout":        8.0,      # seconds; after that the raw text is typed
    "polish_style":          "",       # the user's own texts as a style sample
    "polish_signature":      "",       # added word for word to modes with {signature}; "" -> none
    "polish_default_instructions": "", # "" -> polish.DEFAULT_INSTRUCTIONS
    "polish_profiles":       None,     # None -> polish.DEFAULT_PROFILES
    "polish_modes":          None,     # pie-menu modes; None -> pie.DEFAULT_MODES, [] -> no pie
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
            saved = json.loads(CONFIG_PATH.read_text())
            if "provider" not in saved:
                # A config from before providers existed was Groq-only. Keep it
                # there instead of switching to a model that was never downloaded.
                saved["provider"] = DEFAULT_PROVIDER
                saved.setdefault("model", DEFAULT_MODEL)
            # The word list used to belong to the polish only; it now serves
            # every provider, so it moved out of the polish_ namespace.
            if "vocabulary" not in saved and saved.get("polish_vocabulary"):
                saved["vocabulary"] = saved["polish_vocabulary"]
            saved.pop("polish_vocabulary", None)
            cfg.update(saved)
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


_notifier = None


def set_notifier(fn):
    """Route notify() through fn(msg, urgency, timeout_ms) instead of
    notify-send. The GUI registers its tray icon here on Windows and macOS,
    where there is no notify-send. fn is called from worker threads."""
    global _notifier
    _notifier = fn


def notify(msg, urgency="normal", timeout_ms=1500):
    if _notifier is not None:
        try:
            _notifier(msg, urgency, timeout_ms)
        except Exception:
            log.exception("notifier failed")
        return
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", str(timeout_ms), "Dictate", msg],
            check=False,
        )
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Getting the text into the focused window.
#
# Two ways, chosen in the main window (config "output_method"):
#   paste  — the text goes onto the clipboard, one paste shortcut is sent, the
#            previous clipboard content is restored. One key press instead of
#            one per character, so long dictations arrive at once, also in
#            terminals that handle each fake key press on its own (issue #14).
#   type   — one key press per character (xdotool / ydotool / pynput).
# Pasting is possible on Windows and on X11; elsewhere (Wayland, macOS) the
# text is typed and the user is told once.

OUTPUT_METHODS = ("paste", "type")

# Windows: keep real keystrokes in terminals. They handle them reliably, and a
# paste there can raise a multi-line warning or end up collapsed by the
# shell's input line.
WINDOWS_TERMINALS = {"windowsterminal", "openconsole", "conhost", "cmd", "powershell", "pwsh",
                     "wezterm-gui", "alacritty", "mintty", "putty"}

# X11: the paste shortcut depends on the window. Terminals take Ctrl+Shift+V,
# xterm and its relatives Shift+Insert, everything else Ctrl+V. Matched
# against the WM_CLASS names (lower case).
X11_TERMINALS = ("gnome-terminal", "kgx", "konsole", "xfce4-terminal", "lxterminal", "mate-terminal",
                 "terminator", "tilix", "alacritty", "kitty", "wezterm", "foot", "guake", "yakuake",
                 "terminology", "sakura", "qterminal", "tabby", "cool-retro-term", "ptyxis",
                 "deepin-terminal", "xterm")
X11_SHIFT_INSERT = ("xterm", "uxterm", "urxvt", "rxvt", "st", "st-256color")
PASTE_SETTLE_S = 0.5    # time the focused program gets to read the clipboard before it is restored

_output_lock = threading.Lock()     # one dictation at a time, so two results never interleave
_paste_fallback_told = False


class PasteUnavailable(Exception):
    """Pasting cannot work here (no X11, no clipboard tool); typing is the fallback."""


def paste_shortcut(window_class):
    """xdotool key name for the shortcut that pastes into a window with this
    WM_CLASS (its names separated by spaces)."""
    names = (window_class or "").lower().split()
    if any(n in X11_SHIFT_INSERT for n in names):
        return "shift+Insert"
    if any(t in n for n in names for t in X11_TERMINALS):
        return "ctrl+shift+v"
    return "ctrl+v"


def _x11_window_class():
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


def _x11_clipboard_commands():
    """(read, write) commands for the X11 clipboard, or None without xclip and xsel."""
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"])
    if shutil.which("xsel"):
        return (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"])
    return None


def _x11_write_clipboard(cmd, data):
    # The tool forks to keep owning the clipboard, so its output must not be piped back to us.
    subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=5)


def _x11_read_clipboard(cmd):
    """Current clipboard text as bytes, or None when it is empty or not text."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def paste_x11(text):
    """Put text on the clipboard, send the paste shortcut of the focused window,
    then restore the previous clipboard text. True when the shortcut was sent.
    Raises PasteUnavailable when pasting cannot work here (nothing was sent)."""
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not os.environ.get("DISPLAY"):
        raise PasteUnavailable("pasting needs an X11 session")
    if not shutil.which("xdotool"):
        raise PasteUnavailable("xdotool is not installed")
    tools = _x11_clipboard_commands()
    if tools is None:
        raise PasteUnavailable("no clipboard tool, install xclip or xsel")
    read_cmd, write_cmd = tools
    previous = _x11_read_clipboard(read_cmd)
    shortcut = paste_shortcut(_x11_window_class())
    try:
        _x11_write_clipboard(write_cmd, text.encode())
    except (OSError, subprocess.SubprocessError) as e:
        raise PasteUnavailable(f"could not write the clipboard: {e}") from e
    try:
        r = subprocess.run(["xdotool", "key", "--clearmodifiers", shortcut], check=False, timeout=5)
        if r.returncode != 0:
            log.error("xdotool exited with %s — paste shortcut %s not sent", r.returncode, shortcut)
            notify(tr("Pasting failed ({err})", err=f"xdotool exit {r.returncode}"), urgency="critical", timeout_ms=5000)
            return False
        time.sleep(PASTE_SETTLE_S)   # the program reads the clipboard after the key press
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.error("paste key press failed: %s", e)
        notify(tr("Pasting failed ({err})", err=e), urgency="critical", timeout_ms=5000)
        return False
    finally:
        # Only text can be put back; if the clipboard held something else, the dictation stays on it.
        if previous is not None:
            try:
                _x11_write_clipboard(write_cmd, previous)
            except (OSError, subprocess.SubprocessError):
                log.warning("could not restore the previous clipboard text")


def _paste(text, app):
    """Paste on the platforms that can. True when the text was pasted, False
    when the paste was tried and failed; raises PasteUnavailable otherwise."""
    if IS_WINDOWS:
        if app and app.lower() in WINDOWS_TERMINALS:
            raise PasteUnavailable("terminal, typing keeps the line intact")
        import winclip
        winclip.paste(text)
        return True
    if IS_LINUX:
        return paste_x11(text)
    raise PasteUnavailable("no clipboard paste on this platform")


def _type_keys(text):
    """One key press per character. True when the tool reported success."""
    if IS_WINDOWS or IS_MACOS:
        # No xdotool or ydotool here; pynput types through the OS input API
        # (SendInput with Unicode on Windows), so umlauts and any layout come
        # out right.
        try:
            keyboard.Controller().type(text)
            return True
        except Exception as e:
            log.exception("typing failed")
            notify(tr("Typing failed ({err})", err=e), urgency="critical", timeout_ms=5000)
            return False
    # xdotool respects the active XKB layout (correct German umlauts, no y/z
    # swap on QWERTZ). Prefer it on X11 where it actually works. On Wayland,
    # native windows ignore XTEST. wtype speaks the virtual-keyboard protocol
    # and sends keysyms, so the layout comes out right (KDE, wlroots); GNOME
    # does not implement that protocol, so wtype fails there and ydotool takes
    # over, which writes raw US-QWERTY scancodes via /dev/uinput — layout-
    # agnostic and therefore wrong for non-US keyboards, but the only option
    # for typing into native GNOME Wayland windows today.
    on_x11 = os.environ.get("XDG_SESSION_TYPE") == "x11"
    tools = ["xdotool", "ydotool"] if on_x11 else ["wtype", "ydotool", "xdotool"]
    commands = {
        "xdotool": ["xdotool", "type", "--delay", "0", "--", text],
        "wtype":   ["wtype", "--", text],
        "ydotool": ["ydotool", "type", "--key-delay", "0", "--", text],
    }
    for tool in tools:
        try:
            r = subprocess.run(commands[tool], check=False,
                               stderr=None if tool == "xdotool" else subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        if r.returncode != 0 and tool == "wtype":
            # Compositor without the virtual-keyboard protocol (GNOME): nothing
            # was typed, the next tool gets its turn.
            log.info("wtype exited with %s, trying ydotool", r.returncode)
            continue
        if r.returncode != 0:
            log.error("%s exited with %s — text not typed", tool, r.returncode)
            notify(tr("Typing failed ({tool} exit {code})", tool=tool, code=r.returncode), urgency="critical", timeout_ms=5000)
            return False
        return True
    log.error("no typing tool found (xdotool, ydotool, wtype) — text not typed")
    notify(tr("Typing failed: neither xdotool nor ydotool is installed"), urgency="critical", timeout_ms=5000)
    return False


def type_text(text, app="", method="paste"):
    """Put text into the focused window. app is its program name, if known;
    method is "paste" or "type" (see OUTPUT_METHODS). A paste that cannot work
    here falls back to typing, with one notice per run. Returns True when the
    text arrived as far as we can tell. Calls are served one at a time."""
    global _paste_fallback_told
    if not text:
        return True
    with _output_lock:
        t0 = time.monotonic()
        used, ok = "type", None
        if method != "type":
            try:
                ok = _paste(text, app)
                used = "paste"
            except PasteUnavailable as e:
                log.info("paste not possible, typing instead: %s", e)
                # Windows terminals are a deliberate choice, not something to tell about.
                if not _paste_fallback_told and not IS_WINDOWS:
                    _paste_fallback_told = True
                    notify(tr("Pasting is not possible here ({err}); typing the text instead", err=e),
                           urgency="normal", timeout_ms=6000)
            except Exception:
                log.exception("paste failed, typing instead")
        if ok is None:
            ok = _type_keys(text)
        log.info("output via %s: %d chars in %.2fs, %s%s", used, len(text), time.monotonic() - t0,
                 "ok" if ok else "failed", f" ({app})" if app else "")
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


FAILED_DIR = LOG_PATH.parent / "failed"
FAILED_KEEP = 3


def keep_failed_audio(audio):
    """Save a recording that held sound but came back empty, so the failure
    can be replayed. Only the newest FAILED_KEEP files are kept."""
    try:
        import time
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        path = FAILED_DIR / time.strftime("%Y%m%d-%H%M%S.wav")
        path.write_bytes(to_wav_bytes(audio).getvalue())
        for old in sorted(FAILED_DIR.glob("*.wav"))[:-FAILED_KEEP]:
            old.unlink()
        log.info("kept failed recording as %s", path)
    except Exception:
        log.exception("could not keep failed recording")


# ---------------------------------------------------------------------------
# Vocabulary: names and terms the recognizer spells by sound.
#
# One entry per line in cfg["vocabulary"], "#" starts a comment:
#   Parakeet                 a term: sent to Groq / OpenAI as the Whisper prompt,
#                            and given to the polish model as its word list
#   Parakit -> Parakeet      a replacement: applied to every transcript, whole
#                            words, case does not matter, also offline. The
#                            right side counts as a term as well.

PROMPT_MAX_CHARS = 800   # Whisper reads about 224 tokens of prompt


def parse_vocabulary(text):
    """(terms, replacements) from the vocabulary text; replacements are (wrong, right)."""
    terms, repl = [], []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" in line:
            wrong, right = (x.strip() for x in line.split("->", 1))
            if wrong and right:
                repl.append((wrong, right))
                terms.append(right)
        else:
            terms.append(line)
    return terms, repl


def vocabulary_terms(cfg):
    """The terms alone, unique, in order — what the polish model gets."""
    return list(dict.fromkeys(parse_vocabulary(cfg.get("vocabulary"))[0]))


def whisper_prompt(terms):
    """Comma-joined unique terms, cut to PROMPT_MAX_CHARS at a term boundary."""
    out, size = [], 0
    for t in dict.fromkeys(terms):
        size += len(t) + 2
        if size > PROMPT_MAX_CHARS:
            break
        out.append(t)
    return ", ".join(out)


def apply_replacements(text, repl):
    """Replace whole words, case-insensitive. Longer patterns go first, so
    "Post Gres" wins over "Gres"."""
    for wrong, right in sorted(repl, key=lambda r: -len(r[0])):
        text = re.sub(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", lambda m: right,
                      text, flags=re.IGNORECASE)
    return text


def transcribe(audio, cfg):
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    terms, repl = parse_vocabulary(cfg.get("vocabulary"))
    if p.get("is_local"):
        import local_stt
        threads = int(cfg.get("local_stt_num_threads", local_stt.DEFAULT_THREADS))
        text = local_stt.get_recognizer(num_threads=threads).transcribe(audio)
        return apply_replacements(text, repl)
    # HTTP providers
    key = cfg.get(p["key_field"], "")
    model = cfg.get("model") or p["default_model"]
    wav = to_wav_bytes(audio)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {"model": model, "response_format": "text"}
    prompt = whisper_prompt(terms)
    if prompt:
        data["prompt"] = prompt   # Yorik receives it too; an OpenAI-compatible server ignores it
    headers = {"Authorization": f"Bearer {key}"}
    r = requests.post(provider_url(cfg, p), files=files, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return apply_replacements(r.text.strip(), repl)


# ---------------------------------------------------------------------------
# Download progress: one status line with speed and time left, plus a
# fraction for a progress bar. Shared by the model downloads in local_stt and
# polish, which otherwise only said "Starting download..." for minutes (#7).

def format_duration(seconds):
    s = max(0, int(round(seconds)))
    if s < 60:
        return tr("{n} s", n=s)
    if s < 3600:
        return tr("{m} min {s} s", m=s // 60, s=s % 60)
    return tr("{h} h {m} min", h=s // 3600, m=s % 3600 // 60)


def format_speed(bytes_per_s):
    if bytes_per_s >= 1e6:
        return f"{bytes_per_s / 1e6:.1f} MB/s"
    return f"{bytes_per_s / 1e3:.0f} kB/s"


def format_progress(label, done, total, speed=0.0, eta=None):
    """E.g. "encoder.int8.onnx.data: 212 / 640 MB · 14.2 MB/s · about 30 s left"."""
    head = f"{label}: " if label else ""
    if total <= 0:
        return head + tr("{done} MB downloaded", done=f"{done / 1e6:.0f}")
    text = head + f"{done / 1e6:.0f} / {total / 1e6:.0f} MB"
    if speed > 0:
        text += " · " + format_speed(speed)
    if eta is None and done < total:
        text += " · " + tr("calculating time left ...")
    elif eta is not None and eta > 0:
        text += " · " + tr("about {t} left", t=format_duration(eta))
    return text


class ProgressMeter:
    """Turns byte counts of one download into status lines and a fraction.

    update(done, total) is cheap to call often: it reports at most every
    EMIT_S (and once more when finished). The speed is averaged over the last
    WINDOW_S, so a stalled connection shows a falling speed and a growing ETA.
    status_cb(text) and fraction_cb(fraction in 0..1, or None while the total
    is unknown) are optional and may hop onto a GUI thread themselves."""

    WINDOW_S = 5.0
    EMIT_S = 0.25
    MIN_SPAN_S = 1.0     # no speed or ETA before this much has been measured

    def __init__(self, label="", status_cb=None, fraction_cb=None, clock=time.monotonic):
        self.label = label
        self.status_cb = status_cb
        self.fraction_cb = fraction_cb
        self._clock = clock
        self._samples = []           # (time, done), oldest first
        self._last_emit = None

    def update(self, done, total, force=False):
        now = self._clock()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[1][0] >= self.WINDOW_S:
            self._samples.pop(0)
        finished = total > 0 and done >= total
        if (self._last_emit is not None and not finished and not force
                and now - self._last_emit < self.EMIT_S):
            return None
        self._last_emit = now
        t0, d0 = self._samples[0]
        span = now - t0
        speed = (done - d0) / span if span >= self.MIN_SPAN_S and done > d0 else 0.0
        if finished:
            eta = 0.0
        elif speed > 0 and total > done:
            eta = (total - done) / speed
        else:
            eta = None
        text = format_progress(self.label, done, total, speed, eta)
        if self.status_cb:
            self.status_cb(text)
        if self.fraction_cb:
            self.fraction_cb(min(1.0, done / total) if total > 0 else None)
        return text


def folder_size(folder):
    """Bytes in folder, half-written files included (the hub keeps those in a
    .cache subfolder until a file is complete)."""
    try:
        return sum(p.stat().st_size for p in Path(folder).rglob("*") if p.is_file())
    except OSError:
        return 0


def run_watching_folder(fn, folder, total_bytes, meter, interval=0.5):
    """Run fn(), a blocking download into folder, while a thread feeds the
    folder's size to meter. huggingface_hub reports progress only through
    its own tqdm bars, whose shape changes between versions and which have
    nothing to write to under pythonw.exe (issue #20), so the folder is the
    one source that is always there. Returns fn()'s result."""
    stop = threading.Event()
    start = folder_size(folder)

    def watch():
        while not stop.wait(interval):
            meter.update(min(folder_size(folder), total_bytes) if total_bytes else folder_size(folder), total_bytes)

    meter.update(start, total_bytes)
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join(timeout=2)
        meter.update(total_bytes if total_bytes else folder_size(folder), total_bytes, force=True)


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
