# TASK-002 — FEATURE: Status-Symbol in der Taskleiste

## Ziel
Der Status von Dictate (bereit / Aufnahme / Erkennung / Fehler) soll auch dann
sichtbar sein, wenn das Fenster minimiert oder ganz versteckt ist.

## Ursache
Das Tray-Symbol war statisch (immer das gleiche Mikrofon in dunkel). Der Status
stand nur im Tooltip und im (versteckten) Fenster. Minimieren parkte das Fenster
in der Taskleiste, das Tray-Symbol blieb farblich gleich.

## Lösung (gui.py)
- `STATUS_COLORS` / `STATUS_COLORS_DARK`: Mikrofon-Farbe je Status
  (bereit grau, Aufnahme rot, Erkennung gelb, Fehler dunkelrot).
  Die Dark-Varianten greifen auf dunkler Windows-Taskleiste.
- `make_tray_icon(state)`: Tray-Icon in der Statusfarbe.
- `set_status()` setzt jetzt **Tray-Icon**, **Fenster-Icon** und
  **Fenstertitel** (`Dictate — <Status>`) — der Status ist damit in Taskleiste,
  Tray-Tooltip und Titel sichtbar.
- `changeEvent()`: Minimieren blendet das Fenster aus, statt es in der
  Taskleiste zu parken. Das Tray-Symbol bleibt sichtbar und zeigt den Status.
  Ohne verfügbares Tray bleibt das alte Verhalten.

## Betroffene Dateien
- gui.py (Implementierung)
- _done/TASK-002-FEATURE-Tray-Status-Icon.md (dieses Dokument)

## Tests
- `python3 -m py_compile` über alle Module: OK
- Offscreen-Qt-Test: Icon-Farben je Status geprüft
  (ready #2b2b2b, recording #d32f2f, transcribing #f9a825, error #b71c1c),
  `set_status()` aktualisiert Titel und Fenster-Icon.
- Manuell zu prüfen: Fenster minimieren → Tray-Symbol wechselt die Farbe je nach Status.

## Status
Fertig, gemergt in `main`.
