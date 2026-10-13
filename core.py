"""Shared dictation core: config, recording, transcription, typing."""
import io
import json
import logging
import logging.handlers
import re
import subprocess
import threading
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


# Swiss German (XKB "ch") on top of evdev keycodes, taken from
# /usr/share/X11/xkb/symbols/ch. ydotool injects *physical key positions* as
# on a US keyboard; the compositor applies the active layout. So "y" must be
# sent as KEY_Z (44) and "z" as KEY_Y (21). Value: (keycode, mods, dead) with
# mods "" / "S" (Shift) / "A" (AltGr) / "C" (CapsLock toggle); dead=True means a dead key that needs a
# following Space to yield the bare accent character.
_CH_KEYS = {}
_US_LETTER_CODES = dict(zip("abcdefghijklmnopqrstuvwxyz", [
    30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50,
    49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 44, 21]))
for _c, _k in _US_LETTER_CODES.items():
    _CH_KEYS[_c] = (_k, "", False)
    _CH_KEYS[_c.upper()] = (_k, "S", False)
_CH_KEYS.update({
    "§": (41, "", False), "°": (41, "S", False),
    "1": (2, "", False), "+": (2, "S", False),
    "2": (3, "", False), '"': (3, "S", False), "@": (3, "A", False),
    "3": (4, "", False), "*": (4, "S", False), "#": (4, "A", False),
    "4": (5, "", False), "ç": (5, "S", False),
    "5": (6, "", False), "%": (6, "S", False),
    "6": (7, "", False), "&": (7, "S", False), "¬": (7, "A", False),
    "7": (8, "", False), "/": (8, "S", False), "|": (8, "A", False),
    "8": (9, "", False), "(": (9, "S", False), "¢": (9, "A", False),
    "9": (10, "", False), ")": (10, "S", False),
    "0": (11, "", False), "=": (11, "S", False),
    "'": (12, "", False), "?": (12, "S", False), "´": (12, "A", True),
    "^": (13, "", True), "`": (13, "S", True), "~": (13, "A", True),
    "ü": (26, "", False), "è": (26, "S", False), "[": (26, "A", False),
    "¨": (27, "", True), "!": (27, "S", False), "]": (27, "A", False),
    "ö": (39, "", False), "é": (39, "S", False),
    "ä": (40, "", False), "à": (40, "S", False), "{": (40, "A", False),
    "$": (43, "", False), "£": (43, "S", False), "}": (43, "A", False),
    "<": (86, "", False), ">": (86, "S", False), "\\": (86, "A", False),
    ",": (51, "", False), ";": (51, "S", False),
    ".": (52, "", False), ":": (52, "S", False),
    "-": (53, "", False), "_": (53, "S", False),
    " ": (57, "", False), "\n": (28, "", False), "\t": (15, "", False),
    "€": (18, "A", False),
    # Capital umlauts: no direct key on Swiss German; CapsLock turns ü/ö/ä into Ü/Ö/Ä.
    "Ü": (26, "C", False), "Ö": (39, "C", False), "Ä": (40, "C", False),
})
_MOD_CODES = {"S": 42, "A": 100}  # KEY_LEFTSHIFT, KEY_RIGHTALT
_KEY_SPACE = 57


def _ch_key_args(text):
    """ydotool key arguments ("code:state") that type `text` on a Swiss German
    layout. Characters without a key position are skipped with a warning."""
    args = []
    text = text.replace("ß", "ss")  # Swiss German has no ß key; "ss" is the Swiss spelling
    for ch in text:
        if ch == "\r":
            continue
        entry = _CH_KEYS.get(ch)
        if entry is None:
            log.warning("no Swiss German key for %r — skipped", ch)
            continue
        code, mods, dead = entry
        if mods == "C":
            args += ["58:1", "58:0", f"{code}:1", f"{code}:0", "58:1", "58:0"]
            continue
        mod_code = _MOD_CODES.get(mods)
        if mod_code:
            args.append(f"{mod_code}:1")
        args += [f"{code}:1", f"{code}:0"]
        if mod_code:
            args.append(f"{mod_code}:0")
        if dead:
            args += [f"{_KEY_SPACE}:1", f"{_KEY_SPACE}:0"]
    return args


def _gnome_active_layout():
    """XKB layout name of GNOME's *active* input source (e.g. 'de', 'ch'), or ''."""
    try:
        def get(key):
            return subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.input-sources", key],
                capture_output=True, text=True, timeout=2,
            ).stdout
        sources = re.findall(r"\('xkb', '([^']+)'\)", get("sources"))
        idx = int(get("current").split()[-1])  # "uint32 0" -> 0
        return sources[idx].split("+")[0] if idx < len(sources) else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError, ValueError):
        return ""


def type_text(text):
    if not text:
        return
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
        return
    on_x11 = os.environ.get("XDG_SESSION_TYPE") == "x11"
    # Wayland: wtype speaks the virtual-keyboard protocol and sends keysyms,
    # so umlauts, ß and QWERTZ come out right. ydotool only sends raw US-QWERTY
    # scancodes (wrong Y/Z, no umlauts) and is kept purely as a fallback.
    tools = ["xdotool", "ydotool"] if on_x11 else ["wtype", "ydotool", "xdotool"]
    commands = {
        "wtype": ["wtype", "--", text],
        "xdotool": ["xdotool", "type", "--delay", "0", "--", text],
        "ydotool": ["ydotool", "type", "--key-delay", "0", "--", text],
    }
    if not on_x11 and _gnome_active_layout() == "ch":
        # ydotool with Swiss German key positions: correct umlauts and Y/Z on QWERTZ.
        args = _ch_key_args(text)
        if args:
            try:
                subprocess.run(["ydotool", "key", *args], check=False, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                log.error("ydotool not installed — text not typed")
                notify("Typing failed (ydotool not installed)", urgency="critical", timeout_ms=5000)
        return
    for tool in tools:
        try:
            r = subprocess.run(
                commands[tool],
                check=False,
                stderr=subprocess.DEVNULL if tool != "xdotool" else None,
            )
        except FileNotFoundError:
            continue
        if r.returncode != 0 and tool == "wtype":
            # Compositor without virtual-keyboard support (e.g. GNOME): nothing
            # was typed, so try the next tool.
            log.warning("wtype exited with %s — falling back", r.returncode)
            continue
        if r.returncode != 0:
            log.error("%s exited with %s — text not typed", tool, r.returncode)
            notify(f"Typing failed ({tool} exit {r.returncode})", urgency="critical", timeout_ms=5000)
        return


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
