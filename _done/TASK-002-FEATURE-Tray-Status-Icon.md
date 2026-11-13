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
  "Bereit" ist ein mittleres Grau (#82868b), kein Schwarz: das Mikrofon hängt
  frei auf dem Panel, das je nach Desktop hell oder dunkel ist — so bleibt der
  Kontrast auf beiden über 3:1.
- `make_tray_icon(state)`: Tray-Icon in der Statusfarbe.
- `STATUS_DOT_COLORS`: eigene Palette für den Status-Punkt, weil er immer auf
  dem blau/violetten Logo-Quadrat sitzt und dort kräftige Farben braucht.
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
  (Tray: ready #82868b, recording #e53935, transcribing #f9a825, error #d32f2f;
  Punkt: recording #ff5252, transcribing #ffca28, error #ff1744),
  `set_status()` aktualisiert Titel und Fenster-Icon.
- Kontrastprüfung des Tray-Mikrofons gegen helle (#f0f0f0, #ffffff), dunkle
  (#2b2b2b, #1c1b20) und GNOME-Shell-Hintergründe (#3d3846):
  "bereit" liegt überall bei ≥ 3:1 (3.21 / 3.66 / 3.86 / 3.09 / 4.67).
- Manuell zu prüfen: Fenster minimieren → Tray-Symbol wechselt die Farbe je nach Status.

## Status
Fertig, gemergt in `main`.
