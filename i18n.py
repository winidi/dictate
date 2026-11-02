"""UI language: English (the source strings) or German.

tr(text, **values) looks the English source string up in DE and fills in
{placeholders} afterwards, so numbers and paths never end up inside the
dictionary keys. A string without a translation is returned as it is, so a
missing entry shows English rather than breaking anything.

"auto" follows the system: the Windows UI language, or LANG on Linux.
"""
import locale
import os
import sys

LANGUAGES = [("auto", "System"), ("en", "English"), ("de", "Deutsch")]

_lang = "en"


def system_language():
    try:
        if sys.platform == "win32":
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "de" if (lid & 0xFF) == 0x07 else "en"
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            v = os.environ.get(var)
            if v:
                return "de" if v.lower().startswith("de") else "en"
        loc = locale.getlocale()[0] or ""
        return "de" if loc.lower().startswith("de") else "en"
    except Exception:
        return "en"


def set_language(code):
    global _lang
    _lang = system_language() if code in (None, "", "auto") else code


def language():
    return _lang


def tr(text, **values):
    t = DE.get(text, text) if _lang == "de" else text
    return t.format(**values) if values else t


DE = {
    # --- Settings: transcription tab ---------------------------------------
    "Dictate — Settings": "Dictate — Einstellungen",
    "Language:": "Sprache:",
    "Provider:": "Anbieter:",
    "show": "zeigen",
    "Yorik URL:": "Yorik-URL:",
    "Push-to-talk (hold key)": "Push-to-talk (Taste halten)",
    "Toggle (tap to start, tap to stop)": "Umschalten (tippen für Start, tippen für Stopp)",
    "Mode:": "Modus:",
    "Hotkey:": "Hotkey:",
    "Model:": "Modell:",
    "Local model:": "Lokales Modell:",
    "sherpa-onnx CPU thread count. 4 = best latency/resource balance on\n"
    "this system; more brings little. Changing this rebuilds the recognizer.":
        "Anzahl der CPU-Threads für sherpa-onnx. 4 ist der beste Kompromiss aus\n"
        "Latenz und Auslastung; mehr bringt wenig. Eine Änderung baut den Erkenner neu auf.",
    "CPU threads:": "CPU-Threads:",
    "Recording starts immediately on press; release sends for transcription.\n"
    "If you release before this duration, the recording is discarded. 0 =\n"
    "always send. Recommended ~2.0s when bound to Ctrl/Alt/Shift so brief\n"
    "taps and shortcuts don't transcribe.":
        "Die Aufnahme startet beim Drücken; Loslassen schickt sie zur Erkennung.\n"
        "Lässt du vor dieser Dauer los, wird die Aufnahme verworfen. 0 = immer\n"
        "senden. Empfohlen ~2,0 s bei Strg/Alt/Umschalt als Hotkey, damit kurze\n"
        "Tastendrücke und Tastenkombinationen nichts auslösen.",
    "Min hold to send:": "Mindestens halten:",
    "Test API key": "API-Key testen",
    "Transcription": "Erkennung",
    "Written text": "Schrifttext",
    "{label} API key:": "{label} API-Key:",
    "Runs entirely offline on your CPU. Model: parakeet-primeline "
    "(German, ~640 MB, CC-BY-4.0). Attribution: primeline · NVIDIA · "
    "<a href='https://github.com/k2-fsa/sherpa-onnx'>k2-fsa/sherpa-onnx</a>.":
        "Läuft komplett offline auf der CPU. Modell: parakeet-primeline "
        "(Deutsch, ~640 MB, CC-BY-4.0). Nennung: primeline · NVIDIA · "
        "<a href='https://github.com/k2-fsa/sherpa-onnx'>k2-fsa/sherpa-onnx</a>.",
    "OpenAI key: <a href='https://platform.openai.com/api-keys'>platform.openai.com/api-keys</a>.":
        "OpenAI-Key: <a href='https://platform.openai.com/api-keys'>platform.openai.com/api-keys</a>.",
    "Get a free Groq API key at <a href='https://console.groq.com'>console.groq.com</a>.":
        "Einen kostenlosen Groq-API-Key gibt es auf <a href='https://console.groq.com'>console.groq.com</a>.",
    "sherpa-onnx not installed — run: pip install sherpa-onnx huggingface_hub":
        "sherpa-onnx ist nicht installiert — ausführen: pip install sherpa-onnx huggingface_hub",
    "Download model (~640 MB)": "Modell herunterladen (~640 MB)",
    "Installed and ready.": "Installiert und bereit.",
    "Re-download model": "Modell erneut herunterladen",
    "Not installed.": "Nicht installiert.",
    "Downloading...": "Lade herunter...",
    "Starting download...": "Download startet...",
    "Installed.": "Installiert.",
    "Download failed: {err}": "Download fehlgeschlagen: {err}",
    "Retry download": "Download wiederholen",
    "Enter an API key first.": "Bitte zuerst einen API-Key eingeben.",
    "Testing...": "Teste...",
    "API key works.": "API-Key funktioniert.",
    "API key rejected.": "API-Key abgelehnt.",
    # provider / model labels from core.PROVIDERS
    "Yorik (home server)": "Yorik (Heimserver)",
    "Local (Parakeet DE, CPU offline)": "Lokal (Parakeet DE, CPU offline)",
    "whisper-large-v3-turbo (fastest)": "whisper-large-v3-turbo (am schnellsten)",
    "whisper-large-v3 (most accurate)": "whisper-large-v3 (am genauesten)",
    "gpt-4o-mini-transcribe (fast · cheap)": "gpt-4o-mini-transcribe (schnell · günstig)",
    "gpt-4o-transcribe (best quality)": "gpt-4o-transcribe (beste Qualität)",
    "whisper-1 (classic)": "whisper-1 (klassisch)",
    "Yorik's configured engine (Parakeet, CPU)": "Yoriks konfigurierte Engine (Parakeet, CPU)",
    "parakeet-primeline (German, int8)": "parakeet-primeline (Deutsch, int8)",

    # --- Settings: written-text tab ---------------------------------------
    "Turn the dictation into written text before typing it":
        "Diktat vor dem Tippen in Schrifttext umwandeln",
    "Optional. A local Gemma 4 model (llama.cpp) removes filler words and\n"
    "false starts, fixes punctuation and spelling, and keeps your wording.\n"
    "If the model is slow or missing, the raw transcript is typed instead.":
        "Optional. Ein lokales Gemma-4-Modell (llama.cpp) entfernt Füllwörter und\n"
        "Versprecher, korrigiert Zeichensetzung und Rechtschreibung und behält deine\n"
        "Formulierungen. Ist das Modell langsam oder fehlt es, wird der Rohtext getippt.",
    "Built-in — Dictate downloads llama.cpp and a Gemma 4 model":
        "Eingebaut — Dictate lädt llama.cpp und ein Gemma-4-Modell herunter",
    "A server that is already running (Ollama, LM Studio, llama-server, vLLM …)":
        "Ein bereits laufender Server (Ollama, LM Studio, llama-server, vLLM …)",
    "Model source:": "Modellquelle:",
    "Base URL of an OpenAI-compatible server, usually ending in /v1.\n"
    "Ollama: http://127.0.0.1:11434/v1   LM Studio: http://127.0.0.1:1234/v1\n"
    "llama-server: http://127.0.0.1:8080/v1   vLLM: http://host:8000/v1\n"
    "Text leaves this machine only if the URL points to another one.":
        "Basis-URL eines OpenAI-kompatiblen Servers, meist mit /v1 am Ende.\n"
        "Ollama: http://127.0.0.1:11434/v1   LM Studio: http://127.0.0.1:1234/v1\n"
        "llama-server: http://127.0.0.1:8080/v1   vLLM: http://host:8000/v1\n"
        "Text verlässt diesen Rechner nur, wenn die URL auf einen anderen zeigt.",
    "Server URL:": "Server-URL:",
    "e.g. gemma4:e2b — leave empty if the server serves one model":
        "z. B. gemma4:e2b — leer lassen, wenn der Server nur ein Modell bedient",
    "Model name:": "Modellname:",
    "optional": "optional",
    "API key:": "API-Key:",
    "Test connection": "Verbindung testen",
    "Where llama.cpp runs the model.\n"
    "GPU: much faster, usually well under a second per dictation; the model\n"
    "    stays in VRAM (about 2.5 GB for E2B) while Dictate runs.\n"
    "CPU: works on any machine, but slower — expect a second or more per\n"
    "    dictation and several seconds for a long one, depending on the CPU.\n"
    "    Raise \"Give up after\" accordingly.\n"
    "Auto picks the NVIDIA build when an NVIDIA driver is present, else CPU.\n"
    "Changing this downloads the matching llama.cpp build (CUDA ~1.3 GB,\n"
    "Vulkan ~150 MB, CPU ~30 MB) and restarts the model.":
        "Wo llama.cpp das Modell ausführt.\n"
        "GPU: wesentlich schneller, meist deutlich unter einer Sekunde pro Diktat;\n"
        "    das Modell bleibt im VRAM (etwa 2,5 GB bei E2B), solange Dictate läuft.\n"
        "CPU: läuft auf jedem Rechner, aber langsamer — eine Sekunde oder mehr pro\n"
        "    Diktat, mehrere Sekunden bei langen, je nach Prozessor.\n"
        "    \"Aufgeben nach\" entsprechend erhöhen.\n"
        "Auto nimmt den NVIDIA-Build, wenn ein NVIDIA-Treiber da ist, sonst CPU.\n"
        "Ein Wechsel lädt den passenden llama.cpp-Build (CUDA ~1,3 GB,\n"
        "Vulkan ~150 MB, CPU ~30 MB) und startet das Modell neu.",
    "Run on:": "Ausführen auf:",
    "CPU threads for llama-server. Only matters when running on the CPU.":
        "CPU-Threads für llama-server. Nur relevant bei Ausführung auf der CPU.",
    "If the model takes longer than this, the raw transcript is typed.":
        "Braucht das Modell länger, wird der Rohtext getippt.",
    "Give up after:": "Aufgeben nach:",
    "Runtime + model:": "Laufzeit + Modell:",
    "Download": "Herunterladen",
    "Names and terms — one per line (optional). Fixes words the recognizer spells by sound:":
        "Namen und Begriffe — einer pro Zeile (optional). Korrigiert Wörter, die die Erkennung nach Gehör schreibt:",
    "How you write — paste a few of your own texts (optional):":
        "So schreibst du — ein paar eigene Texte einfügen (optional):",
    "Your own messages, posts or mails. The model matches this voice.":
        "Eigene Nachrichten, Beiträge oder Mails. Das Modell übernimmt diesen Ton.",
    "Per window — one line each, \"word in window title = instructions\"; \"* = ...\" is the default:":
        "Pro Fenster — eine Zeile je Ziel, \"Wort im Fenstertitel = Anweisung\"; \"* = ...\" ist der Standard:",
    "The word on the left is looked for in the title and program name of the\n"
    "window that will receive the text (case does not matter). The first\n"
    "match wins. On GNOME Wayland the window title is not available, so\n"
    "the default line is used there.":
        "Das Wort links wird in Titel und Programmname des Fensters gesucht, das\n"
        "den Text bekommt (Groß-/Kleinschreibung egal). Der erste Treffer gewinnt.\n"
        "Auf GNOME Wayland ist der Fenstertitel nicht verfügbar, dort gilt die\n"
        "Standardzeile.",
    "Pie menu — hold the hotkey and move the mouse towards a mode (or tap an arrow key). "
    "One per line, \"Name = instructions\"; empty = no pie menu:":
        "Kreismenü — Hotkey halten und die Maus Richtung Modus bewegen (oder eine Pfeiltaste tippen). "
        "Eine Zeile je Modus, \"Name = Anweisung\"; leer = kein Kreismenü:",
    "Email signature (optional) — added word for word below the email. "
    "Leave empty if your mail program adds one:":
        "E-Mail-Signatur (optional) — wird wörtlich unter die E-Mail gesetzt. "
        "Leer lassen, wenn dein Mailprogramm sie selbst einfügt:",
    "Best regards\nYour name": "Viele Grüße\nDein Name",
    "Added to every mode with {signature} in its instructions, like the Email mode.":
        "Wird an jeden Modus mit {signature} in der Anweisung angehängt, zum Beispiel an E-Mail.",
    "The first mode sits at 12 o'clock, the rest follow clockwise. No\n"
    "movement = plain written text. Only the direction counts, the pointer\n"
    "may leave the circle. Arrow keys pick the nearest segment; with a\n"
    "modifier as hotkey prefer the mouse, since Ctrl+Arrow also reaches\n"
    "the program you are typing into.":
        "Der erste Modus liegt auf 12 Uhr, die weiteren folgen im Uhrzeigersinn.\n"
        "Keine Bewegung = normaler Schrifttext. Nur die Richtung zählt, der Zeiger\n"
        "darf den Kreis verlassen. Pfeiltasten wählen das nächste Segment; mit\n"
        "einer Modifier-Taste als Hotkey lieber die Maus nehmen, weil Strg+Pfeil\n"
        "auch im Zielprogramm ankommt.",
    "Not available on this platform.": "Auf dieser Plattform nicht verfügbar.",
    "Installed and ready (llama.cpp {build}, {device}).": "Installiert und bereit (llama.cpp {build}, {device}).",
    "Re-download": "Erneut herunterladen",
    "Not installed. Download is about {need} GB; {free} GB free on disk.":
        "Nicht installiert. Download etwa {need} GB; {free} GB frei auf der Platte.",
    "Download (~{need} GB)": "Herunterladen (~{need} GB)",
    "Dictate — written text": "Dictate — Schrifttext",
    "Written text needs a language model.": "Schrifttext braucht ein Sprachmodell.",
    "Do you already run one (Ollama, LM Studio, llama-server, vLLM …)? Then Dictate "
    "only sends requests to it and downloads nothing.\n\n"
    "Otherwise Dictate can download llama.cpp and Gemma 4 (about {need} GB) into "
    "{dir}. Nothing is downloaded until you confirm.":
        "Läuft bei dir schon eines (Ollama, LM Studio, llama-server, vLLM …)? Dann schickt "
        "Dictate nur Anfragen dorthin und lädt nichts herunter.\n\n"
        "Sonst kann Dictate llama.cpp und Gemma 4 (etwa {need} GB) nach {dir} laden. "
        "Ohne deine Bestätigung wird nichts heruntergeladen.",
    "Use my running server": "Meinen laufenden Server nutzen",
    "Download Gemma 4 ({need} GB)": "Gemma 4 herunterladen ({need} GB)",
    "Dictate — download": "Dictate — Download",
    "Download about {need} GB (llama.cpp {build} for {device} and {model}) into\n"
    "{dir}?\n\n{free} GB free on that disk.":
        "Etwa {need} GB herunterladen (llama.cpp {build} für {device} und {model}) nach\n"
        "{dir}?\n\n{free} GB frei auf dieser Platte.",
    # device labels from polish.DEVICES
    "Auto (GPU if an NVIDIA driver is found, else CPU)": "Auto (GPU, wenn ein NVIDIA-Treiber da ist, sonst CPU)",
    "NVIDIA GPU (CUDA 13.4 driver or newer)": "NVIDIA-GPU (CUDA-13.4-Treiber oder neuer)",
    "Any GPU (Vulkan)": "Beliebige GPU (Vulkan)",
    "CPU only": "Nur CPU",
    # model labels from polish.MODELS
    "Gemma 4 E2B — QAT Q4_0 (3.4 GB, fast, fits any GPU/CPU)": "Gemma 4 E2B — QAT Q4_0 (3,4 GB, schnell, passt auf jede GPU/CPU)",
    "Gemma 4 E2B — Q8_0 (5 GB, best E2B quality)": "Gemma 4 E2B — Q8_0 (5 GB, beste E2B-Qualität)",
    "Gemma 4 E4B — QAT Q4_0 (5.2 GB, smarter, still quick)": "Gemma 4 E4B — QAT Q4_0 (5,2 GB, klüger, noch schnell)",
    "Gemma 4 E4B — Q8_0 (8 GB, best quality)": "Gemma 4 E4B — Q8_0 (8 GB, beste Qualität)",

    # --- Main window ----------------------------------------------------------
    "Settings": "Einstellungen",
    "Ready": "Bereit",
    "Last transcription:": "Letzte Erkennung:",
    "Show window": "Fenster anzeigen",
    "Written text (polish)": "Schrifttext (Aufbereitung)",
    "Quit": "Beenden",
    "Polishing...": "Aufbereiten...",
    "Polishing is on but not installed — open Settings": "Schrifttext ist an, aber nicht installiert — Einstellungen öffnen",
    "Polishing: {err}": "Aufbereitung: {err}",
    "Welcome to Dictate": "Willkommen bei Dictate",
    "Open Settings to pick a transcription provider:\n\n"
    "  * Groq (cloud, free tier — console.groq.com)\n"
    "  * OpenAI (cloud — platform.openai.com)\n"
    "  * Local (offline, German, CPU — one-time 640 MB download)":
        "Wähle in den Einstellungen einen Anbieter für die Erkennung:\n\n"
        "  * Groq (Cloud, kostenlos — console.groq.com)\n"
        "  * OpenAI (Cloud — platform.openai.com)\n"
        "  * Lokal (offline, Deutsch, CPU — einmaliger Download, 640 MB)",
    "Push-to-talk": "Push-to-talk",
    "Toggle": "Umschalten",
    "     Min hold: {thr}s": "     Mindestens halten: {thr}s",
    "     Written text: on": "     Schrifttext: an",
    "Mode: {mode}     Hotkey: {key}     Model: {model}{thr}{pol}": "Modus: {mode}     Hotkey: {key}     Modell: {model}{thr}{pol}",
    "Cancelled (combo)": "Abgebrochen (Tastenkombination)",
    "Recording cancelled after {held}s — another key or mouse button was pressed":
        "Aufnahme nach {held}s abgebrochen — eine andere Taste oder Maustaste wurde gedrückt",
    "Discarded (held {dur}s)": "Verworfen ({dur}s gehalten)",
    "Standard": "Standard",
    "Email": "E-Mail",
    "Formatted": "Formatiert",
    "Short message": "Kurznachricht",
    "Recording... ({name})": "Aufnahme... ({name})",
    "Recording...": "Aufnahme...",
    "No API key — open Settings": "Kein API-Key — Einstellungen öffnen",
    "Microphone: {err}": "Mikrofon: {err}",
    "Transcribing...": "Erkenne...",
    "No audio captured — check the microphone": "Kein Audio aufgenommen — Mikrofon prüfen",
    "microphone was silent": "Mikrofon war stumm",
    "no speech recognised": "keine Sprache erkannt",
    "Nothing transcribed ({secs}s) — {why}": "Nichts erkannt ({secs}s) — {why}",
    "Typed as spoken — polishing skipped: {err}": "Wie gesprochen getippt — Aufbereitung übersprungen: {err}",
    "— as spoken{tag}: {raw}": "— wie gesprochen{tag}: {raw}",
    "Error: {msg}": "Fehler: {msg}",
    "Dictate is already running": "Dictate läuft bereits",
    "Dictate is already running.": "Dictate läuft bereits.",

    # --- core / polish / local_stt messages ----------------------------------
    "Typing failed ({tool} exit {code})": "Tippen fehlgeschlagen ({tool} Exit {code})",
    "Typing failed ({err})": "Tippen fehlgeschlagen ({err})",
    "Downloading model (~640 MB, one-time) ...": "Lade Modell herunter (~640 MB, einmalig) ...",
    "Model downloaded.": "Modell heruntergeladen.",
    "Unpacking {asset} ...": "Entpacke {asset} ...",
    "{label}: connection lost, resuming ...": "{label}: Verbindung verloren, setze fort ...",
    "{file}: starting ...": "{file}: startet ...",
    "not enough disk space: {need} MB needed, {free} MB free": "zu wenig Speicherplatz: {need} MB nötig, {free} MB frei",
    "No OpenAI-compatible server at {url}: {err}": "Kein OpenAI-kompatibler Server unter {url}: {err}",
    "Server is up but has no model '{model}'. It offers: {names}": "Server läuft, hat aber kein Modell '{model}'. Er bietet: {names}",
    "Server is up. Models: {names}": "Server läuft. Modelle: {names}",
    "(none listed)": "(keine gelistet)",
    "polishing is not installed": "Aufbereitung ist nicht installiert",
    "polishing model is still loading": "Modell lädt noch",
    "model returned nothing": "Modell hat nichts geliefert",
    "llama-server exited with {code} (see {log})": "llama-server hat sich mit {code} beendet (siehe {log})",
    "polishing runtime or model not installed": "Laufzeit oder Modell für die Aufbereitung nicht installiert",
}
