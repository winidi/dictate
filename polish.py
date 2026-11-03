"""Optional text polishing: spoken words -> written text, through a local
Gemma 4 model served by llama.cpp.

Everything here is opt-in. The runtime (a pinned llama.cpp release build)
and the model (a pinned GGUF from HuggingFace) are downloaded on demand from
Settings into core.DATA_DIR, the same way the Parakeet model is. Nothing
leaves the machine: llama-server listens on 127.0.0.1 only.

A failed or slow polish never loses a dictation — the caller falls back to
the raw transcription.
"""
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

import core
from i18n import tr

# ---------------------------------------------------------------------------
# Pinned llama.cpp release build. Bump deliberately; every user then gets the
# bits that were tested with the models below.
LLAMA_BUILD = "b11658"
_RELEASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"

# (platform, device) -> release assets. The CUDA builds need the cudart
# archive next to them; both unpack into the same folder.
_ASSETS = {
    ("win32", "cuda"):   [f"llama-{LLAMA_BUILD}-bin-win-cuda-13.4-x64.zip",
                          "cudart-llama-bin-win-cuda-13.4-x64.zip"],
    ("win32", "vulkan"): [f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip"],
    ("win32", "cpu"):    [f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"],
    ("linux", "cuda"):   [f"llama-{LLAMA_BUILD}-bin-ubuntu-cuda-13.4-x64.tar.gz",
                          f"cudart-llama-{LLAMA_BUILD}-bin-ubuntu-cuda-13.4-x64.tar.gz"],
    ("linux", "vulkan"): [f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"],
    ("linux", "cpu"):    [f"llama-{LLAMA_BUILD}-bin-ubuntu-x64.tar.gz"],
}
# Rough unpacked sizes, so Settings can refuse a download that cannot fit.
_RUNTIME_MB = {"cuda": 1300, "vulkan": 150, "cpu": 100}

DEVICES = [
    ("auto",   "Auto (GPU if an NVIDIA driver is found, else CPU)"),
    ("cuda",   "NVIDIA GPU (CUDA 13.4 driver or newer)"),
    ("vulkan", "Any GPU (Vulkan)"),
    ("cpu",    "CPU only"),
]

# Pinned models. The QAT Q4_0 files are Google's own 4-bit quantisation
# (trained for it, so it loses less than a plain Q4); Q8_0 is the quality
# choice when there is VRAM to spare.
MODELS = {
    "gemma4-e2b-q4": {
        "label":    "Gemma 4 E2B — QAT Q4_0 (3.4 GB, fast, fits any GPU/CPU)",
        "repo":     "google/gemma-4-E2B-it-qat-q4_0-gguf",
        "revision": "675cff42a74c774d6cb76f76d8eacb49b48c9b93",
        "file":     "gemma-4-E2B_q4_0-it.gguf",
        "size_mb":  3350,
    },
    "gemma4-e2b-q8": {
        "label":    "Gemma 4 E2B — Q8_0 (5 GB, best E2B quality)",
        "repo":     "ggml-org/gemma-4-E2B-it-GGUF",
        "revision": "b4243c156154b6dca9324415f8c7ccc098b4aed1",
        "file":     "gemma-4-E2B-it-Q8_0.gguf",
        "size_mb":  4967,
    },
    "gemma4-e4b-q4": {
        "label":    "Gemma 4 E4B — QAT Q4_0 (5.2 GB, smarter, still quick)",
        "repo":     "google/gemma-4-E4B-it-qat-q4_0-gguf",
        "revision": "4b4a2c1d584be7264f87aac328a1bc739ce81b6c",
        "file":     "gemma-4-E4B_q4_0-it.gguf",
        "size_mb":  5155,
    },
    "gemma4-e4b-q8": {
        "label":    "Gemma 4 E4B — Q8_0 (8 GB, best quality)",
        "repo":     "ggml-org/gemma-4-E4B-it-GGUF",
        "revision": "b8093469224f83f5c38f691eb906c380e9e63114",
        "file":     "gemma-4-E4B-it-Q8_0.gguf",
        "size_mb":  8031,
    },
}
DEFAULT_MODEL = "gemma4-e2b-q4"

MODELS_DIR = core.DATA_DIR / "models"
RUNTIME_DIR = core.DATA_DIR / "llama.cpp"
SERVER_LOG = core.STATE_DIR / "llama-server.log"

CTX_SIZE = 4096          # a dictation plus style sample and instructions
DEFAULT_TIMEOUT = 8.0    # seconds; after that the raw text is typed
DEFAULT_THREADS = 4

# Shipped as examples; the user edits these in Settings. The match string is
# looked for (case-insensitively) in the window title and the program name.
DEFAULT_PROFILES = [
    {"match": "linkedin", "instructions": "Beruflich, aber locker. Kurze Absätze. Keine Floskeln."},
    {"match": "youtube",  "instructions": "Locker, direkt, Du-Form."},
    {"match": "terminal", "instructions": "Nur Rechtschreibung und Zeichensetzung korrigieren, Wortlaut unverändert lassen."},
]
DEFAULT_INSTRUCTIONS = "Natürlicher Schreibstil, so wie die Person spricht, nur in Schriftsprache."

SYSTEM_PROMPT = """You are a dictation editor. The user speaks a text; you return the same text the way it would be written.

What you do:
- Remove everything that only comes from speaking: filler words (äh, ähm, also, halt, quasi, sozusagen, im Endeffekt, tatsächlich, irgendwie, ja, ne, okay), false starts, abandoned or repeated phrases. For a self-correction keep only the corrected version.
- Turn rambling spoken sentences into clear written sentences. You may reorder words, split or join sentences, as long as every point the speaker made stays and nothing new is added.
- Fix spelling, punctuation and capitalisation. Proper German with umlauts and ß. Names of products and programs are spelled the usual way.
- Spoken formatting commands become formatting: "neuer Absatz" or "Absatz" is a blank line, "neue Zeile" a line break.
- Keep the language, the form of address (du/Sie), the tone and the speaker's own words where they work. Do not make the text more formal than it is. Do not answer questions in the text, do not comment, do not summarise.
- Keep the meaning exactly. A question stays a question, a statement stays a statement, "vielleicht" stays "vielleicht". When in doubt, change less.
- Output only the written text, nothing else.

Example:
Dictation: ähm ja also ich wollte fragen ob wir das meeting morgen äh auf nachmittag verschieben können weil ich vormittags hab ich einen termin beim zahnarzt der ist um zehn also ich wäre erst so gegen zwölf wieder da
Written: Ich wollte fragen, ob wir das Meeting morgen auf den Nachmittag verschieben können. Vormittags habe ich um zehn einen Termin beim Zahnarzt und wäre erst gegen zwölf wieder da.

Example:
Dictation: schick mir bitte die datei bis montag nein bis dienstag neuer absatz danke dir
Written: Schick mir bitte die Datei bis Dienstag.

Danke dir."""


# ---------------------------------------------------------------------------
# Platform / device

def _platform() -> str:
    if core.IS_WINDOWS:
        return "win32"
    if core.IS_LINUX:
        return "linux"
    return sys.platform


def supported() -> bool:
    return (_platform(), "cpu") in _ASSETS


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def resolve_device(device: str) -> str:
    if device == "auto":
        return "cuda" if has_nvidia() else "cpu"
    return device


def runtime_dir(device: str) -> Path:
    return RUNTIME_DIR / LLAMA_BUILD / resolve_device(device)


def server_binary(device: str) -> Optional[Path]:
    name = "llama-server.exe" if core.IS_WINDOWS else "llama-server"
    d = runtime_dir(device)
    if not d.exists():
        return None
    for p in d.rglob(name):
        return p
    return None


def model_path(model_id: str) -> Path:
    m = MODELS[model_id]
    return MODELS_DIR / m["repo"].split("/")[-1] / m["file"]


def model_installed(model_id: str) -> bool:
    return model_path(model_id).exists()


def installed(cfg) -> bool:
    """Built-in: runtime for the configured device and the model are present.
    External: a URL is configured (whether it answers is checked when used)."""
    if cfg.get("polish_backend", "builtin") == "external":
        return bool((cfg.get("polish_external_url") or "").strip())
    return (server_binary(cfg.get("polish_device", "auto")) is not None
            and model_installed(cfg.get("polish_model", DEFAULT_MODEL)))


def download_size_mb(cfg) -> int:
    """What a download from Settings would still have to fetch."""
    mb = 0
    device = cfg.get("polish_device", "auto")
    if server_binary(device) is None:
        mb += _RUNTIME_MB[resolve_device(device)]
    model_id = cfg.get("polish_model", DEFAULT_MODEL)
    if not model_installed(model_id):
        mb += MODELS[model_id]["size_mb"]
    return mb


def free_space_mb(path: Path = None) -> int:
    p = path or core.DATA_DIR
    while not p.exists():
        p = p.parent
    return int(shutil.disk_usage(p).free / 1e6)


# ---------------------------------------------------------------------------
# Downloads

def _download(url: str, dest: Path, progress_cb: Optional[Callable[[str], None]], label: str,
              fraction_cb=None) -> None:
    """Stream url to dest, resuming a .part file; progress_cb gets status
    lines with speed and time left, fraction_cb feeds a progress bar. A
    dropped connection (common on multi-GB files) is retried a few times from
    where it stopped before giving up."""
    last_err = None
    for attempt in range(5):
        try:
            _download_once(url, dest, progress_cb, label, fraction_cb)
            return
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            core.log.warning("download of %s interrupted (attempt %d): %s", label, attempt + 1, e)
            if progress_cb:
                progress_cb(tr("{label}: connection lost, resuming ...", label=label))
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download of {label} failed after retries: {last_err}")


def _download_once(url: str, dest: Path, progress_cb: Optional[Callable[[str], None]], label: str,
                   fraction_cb=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with requests.get(url, stream=True, headers=headers, timeout=60) as r:
        if r.status_code == 416:            # the .part file is already complete
            part.replace(dest)
            return
        r.raise_for_status()
        mode = "ab" if r.status_code == 206 else "wb"
        if mode == "wb":
            have = 0
        total = int(r.headers.get("Content-Length") or 0) + have
        meter = core.ProgressMeter(label, progress_cb, fraction_cb)
        meter.update(have, total)
        with open(part, mode) as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                have += len(chunk)
                meter.update(have, total)
    part.replace(dest)


def _extract(archive: Path, into: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(into, filter="data")
            else:
                t.extractall(into)


def download_runtime(device: str, progress_cb: Optional[Callable[[str], None]] = None,
                     fraction_cb=None) -> Path:
    """Fetch and unpack the pinned llama.cpp build for this platform/device."""
    key = (_platform(), resolve_device(device))
    if key not in _ASSETS:
        raise RuntimeError(f"no llama.cpp build for {key[0]} / {key[1]}")
    into = runtime_dir(device)
    into.mkdir(parents=True, exist_ok=True)
    for asset in _ASSETS[key]:
        archive = into / asset
        if not archive.exists():
            _download(_RELEASE_URL + asset, archive, progress_cb, f"llama.cpp {asset}", fraction_cb)
        if progress_cb:
            progress_cb(tr("Unpacking {asset} ...", asset=asset))
        if fraction_cb:
            fraction_cb(None)   # busy: unpacking has no byte count
        _extract(archive, into)
        archive.unlink()
    binary = server_binary(device)
    if binary is None:
        raise RuntimeError(f"llama-server not found after unpacking into {into}")
    if not core.IS_WINDOWS:
        binary.chmod(0o755)
    return binary


def download_model(model_id: str, progress_cb: Optional[Callable[[str], None]] = None,
                   fraction_cb=None) -> Path:
    """Fetch the pinned GGUF through huggingface_hub, like the Parakeet model.
    The hub client speaks HF's chunked Xet protocol, resumes and retries on
    its own; a plain ranged HTTP download of these multi-GB files kept
    breaking. Progress comes from watching the folder grow."""
    m = MODELS[model_id]
    dest = model_path(model_id)
    if dest.exists():
        return dest
    # No console to draw on under pythonw.exe (issue #20); the folder watcher
    # reports progress instead. Must be set before the hub is first imported.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise RuntimeError("huggingface_hub is not installed — run: pip install huggingface_hub") from e
    dest.parent.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb(tr("{file}: starting ...", file=m["file"]))
    meter = core.ProgressMeter(m["file"], progress_cb, fraction_cb)
    core.run_watching_folder(
        lambda: hf_hub_download(repo_id=m["repo"], filename=m["file"], revision=m["revision"],
                                local_dir=str(dest.parent), etag_timeout=60),
        dest.parent, m["size_mb"] * 1_000_000, meter,
    )
    if not dest.exists():
        raise RuntimeError(f"{m['file']} missing after download")
    return dest


def download_all(cfg, progress_cb: Optional[Callable[[str], None]] = None, fraction_cb=None) -> None:
    """Runtime first (small), then the model. Blocks; raises on failure.
    progress_cb(text) gets status lines, fraction_cb(0..1 or None) feeds a
    progress bar."""
    need = download_size_mb(cfg)
    free = free_space_mb()
    if need and free < need + 500:
        raise RuntimeError(tr("not enough disk space: {need} MB needed, {free} MB free", need=need, free=free))
    device = cfg.get("polish_device", "auto")
    model_id = cfg.get("polish_model", DEFAULT_MODEL)
    if server_binary(device) is None:
        download_runtime(device, progress_cb, fraction_cb)
    if not model_installed(model_id):
        download_model(model_id, progress_cb, fraction_cb)
    if progress_cb:
        progress_cb(tr("Installed."))


# ---------------------------------------------------------------------------
# llama-server process

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tie_child_to_parent(proc: subprocess.Popen):
    """Make sure llama-server dies with Dictate, even if Dictate is killed.
    Windows: a job object with KILL_ON_JOB_CLOSE. Linux: handled through
    PR_SET_PDEATHSIG in the preexec hook. Best effort; returns a handle to
    keep alive or None."""
    if not core.IS_WINDOWS:
        return None
    try:
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, wintypes.HANDLE(proc._handle))
        return job
    except Exception:
        core.log.exception("could not tie llama-server to this process")
        return None


def _linux_preexec():
    try:
        import signal
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:
        pass


def server_command(binary: Path, model: Path, port: int, device: str, threads: int):
    ngl = "0" if resolve_device(device) == "cpu" else "all"
    return [
        str(binary),
        "-m", str(model),
        "--host", "127.0.0.1", "--port", str(port),
        "-c", str(CTX_SIZE),
        "-ngl", ngl,
        "-t", str(threads),
        "--no-webui",
        "--reasoning", "off",   # Gemma 4 is a hybrid-thinking model; we want text, not thoughts
    ]


class Server:
    """One llama-server for one model. start() returns at once; the model
    loads in the background and is_ready() flips when /health says ok."""

    def __init__(self, model_id: str, device: str, threads: int):
        self.model_id = model_id
        self.device = device
        self.threads = threads
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._proc: Optional[subprocess.Popen] = None
        self._job = None
        self._ready = threading.Event()
        self._failed: Optional[str] = None
        self._log = None

    def start(self) -> None:
        binary = server_binary(self.device)
        model = model_path(self.model_id)
        if binary is None or not model.exists():
            raise RuntimeError(tr("polishing runtime or model not installed"))
        cmd = server_command(binary, model, self.port, self.device, self.threads)
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(SERVER_LOG, "w", encoding="utf-8", errors="replace")
        kw = {}
        if core.IS_WINDOWS:
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        elif core.IS_LINUX:
            kw["preexec_fn"] = _linux_preexec
        self._proc = subprocess.Popen(
            cmd, cwd=str(binary.parent), stdout=self._log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **kw,
        )
        self._job = _tie_child_to_parent(self._proc)
        core.log.info("llama-server started (pid %s, %s, %s on %s)",
                      self._proc.pid, self.model_id, LLAMA_BUILD, resolve_device(self.device))
        threading.Thread(target=self._wait_health, daemon=True).start()

    def _wait_health(self) -> None:
        t0 = time.monotonic()
        while True:
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is not None:
                self._failed = tr("llama-server exited with {code} (see {log})", code=proc.returncode, log=SERVER_LOG)
                core.log.error(self._failed)
                self._ready.set()
                return
            try:
                r = requests.get(self.url + "/health", timeout=2)
                if r.status_code == 200:
                    core.log.info("llama-server ready after %.1fs", time.monotonic() - t0)
                    self._ready.set()
                    return
            except requests.RequestException:
                pass
            time.sleep(0.25)

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._failed is None

    def error(self) -> Optional[str]:
        return self._failed

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout) and self._failed is None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
        core.log.info("llama-server stopped")

    def chat(self, messages, max_tokens: int, timeout: float) -> str:
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            # Rewriting wants faithfulness, not creativity: low temperature,
            # Gemma's recommended top_p / top_k otherwise.
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 64,
            "reasoning_format": "none",
        }
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class ExternalServer:
    """A model server somebody already runs: Ollama, LM Studio, llama-server,
    vLLM, anything with an OpenAI-compatible /chat/completions. Nothing to
    start or stop; Dictate only sends requests."""

    model_id = "external"
    device = "external"
    threads = 0

    def __init__(self, url: str, model: str, key: str):
        self.url = url.strip().rstrip("/")
        self.model = model.strip()
        self.key = key.strip()

    def start(self): pass
    def stop(self): pass
    def alive(self): return True
    def is_ready(self): return True
    def error(self): return None
    def wait_ready(self, timeout=None): return True

    def _headers(self):
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def chat(self, messages, max_tokens: int, timeout: float) -> str:
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "top_p": 0.95,
            # Hybrid-thinking models (Gemma 4, Qwen 3) should answer, not think.
            # llama-server and vLLM honour this; others ignore the field.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.model:
            body["model"] = self.model
        r = requests.post(self.url + "/chat/completions", json=body, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def test_external(url: str, key: str = "", model: str = "", timeout: float = 5.0):
    """(ok, message). Lists the server's models; also checks the chosen one is among them."""
    s = ExternalServer(url, model, key)
    try:
        r = requests.get(s.url + "/models", headers=s._headers(), timeout=timeout)
        r.raise_for_status()
        names = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception as e:
        return False, tr("No OpenAI-compatible server at {url}: {err}", url=s.url, err=e)
    if s.model and names and s.model not in names:
        return False, tr("Server is up but has no model '{model}'. It offers: {names}", model=s.model, names=", ".join(names[:8]))
    return True, tr("Server is up. Models: {names}", names=", ".join(names[:8]) or tr("(none listed)"))


_server = None
_server_lock = threading.Lock()


def get_server(cfg):
    """Start (or reuse) the server for the configured model/device. Returns
    None when the feature is off or nothing is installed."""
    global _server
    if not cfg.get("polish_enabled"):
        return None
    if cfg.get("polish_backend", "builtin") == "external":
        with _server_lock:
            if isinstance(_server, Server):
                _server.stop()   # the built-in one is not wanted any more
            url = cfg.get("polish_external_url") or ""
            if not url.strip():
                return None
            _server = ExternalServer(url, cfg.get("polish_external_model") or "", cfg.get("polish_external_key") or "")
            return _server
    model_id = cfg.get("polish_model", DEFAULT_MODEL)
    device = cfg.get("polish_device", "auto")
    threads = int(cfg.get("polish_threads", DEFAULT_THREADS))
    with _server_lock:
        s = _server
        if isinstance(s, ExternalServer):
            s = _server = None
        if s is not None and (s.model_id != model_id or s.device != device
                              or s.threads != threads or not s.alive()):
            s.stop()
            s = _server = None
        if s is None:
            if not installed(cfg):
                return None
            s = Server(model_id, device, threads)
            s.start()
            _server = s
        return s


def stop_server() -> None:
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
            _server = None


# ---------------------------------------------------------------------------
# Context: which window will receive the text

def active_window():
    """(program name, window title) of the foreground window, or ("", "")
    where the desktop does not tell (Wayland)."""
    try:
        if core.IS_WINDOWS:
            from ctypes import wintypes
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            h = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(h) + 1
            buf = ctypes.create_unicode_buffer(n)
            u.GetWindowTextW(h, buf, n)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            exe = ""
            ph = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if ph:
                try:
                    pbuf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k.QueryFullProcessImageNameW(ph, 0, pbuf, ctypes.byref(size)):
                        exe = Path(pbuf.value).stem
                finally:
                    k.CloseHandle(ph)
            return exe, buf.value
        if core.IS_LINUX and os.environ.get("XDG_SESSION_TYPE") == "x11" and shutil.which("xdotool"):
            title = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                                   capture_output=True, text=True, timeout=1).stdout.strip()
            pid = subprocess.run(["xdotool", "getactivewindow", "getwindowpid"],
                                 capture_output=True, text=True, timeout=1).stdout.strip()
            exe = ""
            if pid.isdigit():
                try:
                    exe = Path(f"/proc/{pid}/comm").read_text().strip()
                except OSError:
                    pass
            return exe, title
    except Exception:
        core.log.exception("active window lookup failed")
    return "", ""


def profile_for(cfg, app: str, title: str) -> str:
    hay = f"{app} {title}".lower()
    profiles = cfg.get("polish_profiles")
    if profiles is None:
        profiles = DEFAULT_PROFILES
    for p in profiles:
        m = (p.get("match") or "").strip().lower()
        if m and m in hay:
            return p.get("instructions") or ""
    return cfg.get("polish_default_instructions") or DEFAULT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# The polish itself

_THOUGHT = re.compile(r"<\|channel>.*?<channel\|>|<think>.*?</think>", re.S)


def _clean(out: str) -> str:
    out = _THOUGHT.sub("", out).strip()
    # A model that wraps its answer in quotes gets them taken off again.
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'„“":
        out = out[1:-1].strip()
    return out


def build_messages(cfg, text: str, app: str, title: str, mode: str = ""):
    system = [SYSTEM_PROMPT]
    vocab = core.vocabulary_terms(cfg)
    if vocab:
        system.append("Names and terms the speaker uses. Speech recognition spells them "
                      "wrong by sound (\"Parakit\" for \"Parakeet\", \"Winiki\" for "
                      "\"Winiecki\"). Whenever a word in the dictation sounds like one of "
                      "these, replace it with the listed form, copied exactly, including "
                      "its capitalisation and punctuation:\n"
                      + "\n".join("- " + w for w in vocab))
    style = (cfg.get("polish_style") or "").strip()
    if style:
        system.append("Sample of the writer's own texts. Match this voice, "
                      "vocabulary and sentence length:\n---\n" + style + "\n---")
    # Destination and its notes travel with the dictation, labelled, so a
    # small model does not mistake them for the text to rewrite.
    user = []
    where = " — ".join(x for x in (app, title) if x)
    if where:
        user.append("Destination (the program and window the text is typed into): " + where)
    instr = profile_for(cfg, app, title).strip()
    if instr:
        user.append("Notes for this destination: " + instr)
    if mode and mode.strip():
        user.append("Special instruction for this dictation, it takes precedence over "
                    "the general rules (including the one about keeping the language): "
                    + mode.strip())
    user.append("Dictation: " + text.strip())
    return [
        {"role": "system", "content": "\n\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


SIGNATURE_MARK = "{signature}"   # in a mode's instructions: add polish_signature to the result


def polish(cfg, text: str, app: str = "", title: str = "", mode: str = "") -> str:
    """Return the polished text. Raises when the server is not ready or the
    request fails; the caller then types the raw text."""
    # The signature is added here, not by the model, so it arrives word for word.
    signature = (cfg.get("polish_signature") or "").strip() if SIGNATURE_MARK in mode else ""
    mode = mode.replace(SIGNATURE_MARK, "")
    s = get_server(cfg)
    if s is None:
        raise RuntimeError(tr("polishing is not installed"))
    if not s.wait_ready(timeout=0.0):
        raise RuntimeError(tr("polishing model is still loading"))
    if s.error():
        raise RuntimeError(s.error())
    timeout = float(cfg.get("polish_timeout", DEFAULT_TIMEOUT))
    # Written text is rarely longer than the spoken original; allow some slack.
    # A mode can make it longer: an email adds a subject, greeting and closing.
    max_tokens = min(CTX_SIZE // 2, len(text) // 2 + (384 if mode.strip() else 128))
    out = s.chat(build_messages(cfg, text, app, title, mode), max_tokens=max_tokens, timeout=timeout)
    out = _clean(out)
    if not out:
        raise RuntimeError(tr("model returned nothing"))
    return f"{out}\n\n{signature}" if signature else out
