"""Prüft Dictate auf diesem PC, auch über eine Fernwartung.

Der Ablauf nutzt dieselben Funktionen wie die App:
  1. Einstellungen setzen (lokal, deutsche Spracherkennung, Aufbereitung an)
  2. fehlende Modelle laden
  3. Testsatz aus der Audiodatei erkennen und aufbereiten
  4. Text in ein eigenes Testfenster einfügen und zurücklesen
  5. Mikrofon: 6 Sekunden aufnehmen, der Mensch am PC spricht
  6. Taste F9: meldet, ob sie vom Menschen am PC oder vom Fernzugriff kam

    selbsttest.py [--ohne-laden] [--ohne-mikro] [--ohne-taste] [--app ORDNER]
"""
import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
HIER = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--app", default=str(HIER / "dictate"))
ap.add_argument("--wav", default=str(HIER / "testsatz.wav"))
ap.add_argument("--ohne-laden", action="store_true")
ap.add_argument("--ohne-mikro", action="store_true")
ap.add_argument("--ohne-taste", action="store_true")
ap.add_argument("--extern", default="", help="nur zum Probelauf: laufender Sprachmodell-Server")
ap.add_argument("--cpu", action="store_true", help="Sprachmodell auf dem Prozessor, auch mit NVIDIA-Karte")
args = ap.parse_args()
sys.path.insert(0, args.app)

import numpy as np

import core
import i18n
import local_stt
import polish
import pie

ERWARTET = {"termin", "donnerstag", "angebot", "prüfen"}
ergebnisse = []


def schritt(titel):
    print(f"\n=== {titel}", flush=True)


def melde(name, ok, info=""):
    ergebnisse.append((name, ok))
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}{'  ' + info if info else ''}", flush=True)


def fortschritt(msg):
    print("  " + str(msg), flush=True)


# --- 1 Einstellungen ------------------------------------------------------------
schritt("1 Einstellungen")
cfg = core.load_config()
neu = not core.CONFIG_PATH.exists()
cfg.update(provider="parakeet_local", model=core.PROVIDERS["parakeet_local"]["default_model"],
           polish_enabled=True, polish_model="gemma4-e2b-q4", polish_device="auto", ui_language="de",
           key="ctrl", mode="ptt", threshold=2.0)  # unter 2 s gehalten wird verworfen
cfg["polish_backend"] = "builtin"
if args.extern:
    cfg.update(polish_backend="external", polish_external_url=args.extern, polish_external_model="gemma")
if args.cpu:
    cfg["polish_device"] = "cpu"
hat_nvidia = polish.has_nvidia() and not args.cpu
if not hat_nvidia and not args.extern:
    cfg["polish_timeout"] = max(float(cfg.get("polish_timeout", 8.0)), 45.0)
core.save_config(cfg)
print(f"  Konfiguration: {core.CONFIG_PATH}  ({'neu angelegt' if neu else 'aktualisiert'})")
print(f"  NVIDIA-Grafikkarte: {'ja' if hat_nvidia else 'nein, das Sprachmodell läuft auf dem Prozessor'}")
print(f"  Freier Platz: {polish.free_space_mb() / 1024:.1f} GB (gebraucht bis etwa {('5,5' if hat_nvidia else '4,2')} GB)")

# --- 2 Modelle ------------------------------------------------------------------
schritt("2 Modelle")
if not args.ohne_laden:
    if not local_stt.model_installed():
        print("  Lade Spracherkennung, etwa 640 MB ...", flush=True)
        local_stt.download_model(fortschritt)
    if not args.extern and not polish.installed(cfg):
        print(f"  Lade Sprachmodell, noch {polish.download_size_mb(cfg) / 1024:.1f} GB ...", flush=True)
        polish.download_all(cfg, fortschritt)
melde("Spracherkennung vorhanden", local_stt.model_installed())
melde("Sprachmodell vorhanden", bool(args.extern) or polish.installed(cfg))


# --- 3 Audiodatei ---------------------------------------------------------------
schritt("3 Testsatz aus der Audiodatei")
def lies_wav(pfad):
    with wave.open(pfad, "rb") as w:
        assert w.getframerate() == core.SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


start = time.monotonic()
roh = core.transcribe(lies_wav(args.wav), cfg)
print(f"  Erkannt in {time.monotonic() - start:.1f} s: {roh}", flush=True)
treffer = {w for w in ERWARTET if w in roh.lower()}
melde("Spracherkennung", len(treffer) >= 3, f"{len(treffer)} von {len(ERWARTET)} Prüfwörtern")

i18n.set_language("de")
modi = pie.modes_for({**cfg, "polish_modes": None})
server = polish.get_server(cfg)
if hasattr(server, "wait_ready"):
    print("  Warte auf das Sprachmodell, beim ersten Mal bis zu 2 Minuten ...", flush=True)
    server.wait_ready(180)

texte = {}
for name, modus in [("Normal", ""), (modi[1]["name"], modi[1]["instructions"]), (modi[3]["name"], modi[3]["instructions"])]:
    start = time.monotonic()
    try:
        texte[name] = polish.polish(cfg, roh, mode=modus)
    except Exception as e:
        print(f"  Aufbereitung meldet: {e}", flush=True)
        texte[name] = ""
    dauer = time.monotonic() - start
    print(f"  {name} in {dauer:.1f} s:\n    " + texte[name].replace("\n", "\n    "), flush=True)
    melde(f"Aufbereitung {name}", bool(texte[name].strip()) and dauer <= float(cfg["polish_timeout"]),
          f"{dauer:.1f} s, Grenze {float(cfg['polish_timeout']):.0f} s")

# --- 4 Einfügen -----------------------------------------------------------------
schritt("4 Einfügen in ein Testfenster")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

qapp = QApplication(sys.argv[:1])


def warte(sek):
    ende = time.monotonic() + sek
    while time.monotonic() < ende:
        qapp.processEvents()
        time.sleep(0.02)


feld = QPlainTextEdit()
feld.setWindowTitle("Dictate Selbsttest")
feld.resize(700, 300)
feld.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
feld.show()
feld.raise_()
feld.activateWindow()
feld.setFocus()
warte(1.0)
probe = "Einfügetest: Grüße aus Köln, Maß und Öl.\nZweite Zeile."
import ctypes
ctypes.windll.user32.SwitchToThisWindow(int(feld.winId()), True)
warte(0.7)
feld.setFocus()
vorne = ctypes.windll.user32.GetForegroundWindow() == int(feld.winId())
if not vorne:
    # Einfügen ginge sonst in ein fremdes Fenster. Einmal Zeit lassen zum Anklicken.
    print("  Bitte das Fenster 'Dictate Selbsttest' anklicken, 10 Sekunden ...", flush=True)
    warte(10)
    vorne = ctypes.windll.user32.GetForegroundWindow() == int(feld.winId())
if vorne:
    # Eigener Faden: Das Testfenster muss das Einfügen lesen, während die App
    # noch wartet. Im echten Betrieb ist das Ziel ohnehin ein anderes Programm.
    import threading
    threading.Thread(target=core.type_text, args=(probe, "selbsttest"), daemon=True).start()
    warte(2.0)
    angekommen = feld.toPlainText().replace("\r", "")
    melde("Text kommt mit Umlauten im Fenster an", angekommen == probe, "" if angekommen == probe else repr(angekommen))
else:
    melde("Testfenster liegt vorne", False, "nichts eingefügt, damit kein anderes Fenster Text bekommt")

# --- 5 Mikrofon -----------------------------------------------------------------
if not args.ohne_mikro:
    schritt("5 Mikrofon")
    import sounddevice as sd
    try:
        geraet = sd.query_devices(kind="input")["name"]
    except Exception as e:
        geraet = f"keines gefunden ({e})"
    print(f"  Standard-Mikrofon: {geraet}")
    for s in (3, 2, 1):
        print(f"  Gleich sprechen, in {s} ...", flush=True)
        warte(1)
    print("  JETZT SPRECHEN, 6 Sekunden lang", flush=True)
    feld.setPlainText("JETZT SPRECHEN, 6 Sekunden lang")
    qapp.processEvents()
    try:
        aufnahme = core.Recorder()
        aufnahme.start()
        warte(6)
        audio = aufnahme.stop()
        spitze = int(np.abs(audio.astype(np.int32)).max()) if audio is not None else 0
        print(f"  Lautstärke-Spitze: {spitze} von 32768", flush=True)
        melde("Mikrofon liefert Ton", spitze >= 300, "unter 300 gilt als stumm")
        if spitze >= 300:
            text = core.transcribe(audio, cfg)
            print(f"  Verstanden: {text}", flush=True)
            melde("Gesprochenes erkannt", bool(text.strip()))
    except Exception as e:
        melde("Mikrofon lässt sich öffnen", False, str(e))

# --- 6 Taste --------------------------------------------------------------------
if not args.ohne_taste:
    schritt("6 Taste Strg")
    from pynput import keyboard
    ziel = set(core.KEY_MAP["ctrl"])
    gedrueckt = set()
    gesehen = []

    def beim_druck(taste, injected=False):
        # Gehaltene Tasten wiederholen sich, gezählt wird nur das erste Drücken.
        if taste in ziel and taste not in gedrueckt:
            gedrueckt.add(taste)
            gesehen.append(bool(injected))

    def beim_loslassen(taste, injected=False):
        gedrueckt.discard(taste)

    hoerer = keyboard.Listener(on_press=beim_druck, on_release=beim_loslassen)
    hoerer.start()
    print("  20 Sekunden: Strg einmal über den Fernzugriff drücken,", flush=True)
    print("  danach Strg einmal auf der Tastatur am PC.", flush=True)
    feld.setPlainText("Bitte Strg drücken: einmal aus der Ferne, einmal am PC")
    warte(20)
    hoerer.stop()
    fern = sum(gesehen)
    echt = len(gesehen) - fern
    print(f"  Am PC gedrückt: {echt} mal. Vom Fernzugriff gesendet: {fern} mal.")
    print("  Dictate reagiert nur auf Strg vom PC selbst. Gesendete Tasten ignoriert es.")
    melde("Strg am PC gedrückt", echt >= 1)

feld.close()

# --- Ergebnis -------------------------------------------------------------------
fehler = [n for n, ok in ergebnisse if not ok]
print("\n" + "=" * 60)
print(f"ERGEBNIS: {len(ergebnisse) - len(fehler)} von {len(ergebnisse)} Prüfungen OK")
for n in fehler:
    print("  FEHLER:", n)
print(json.dumps({"provider": cfg["provider"], "polish": cfg["polish_model"], "nvidia": hat_nvidia}))
sys.stdout.flush()
os._exit(1 if fehler else 0)
