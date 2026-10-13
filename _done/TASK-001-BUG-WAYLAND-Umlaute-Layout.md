# TASK-001-BUG: Wayland-Typing verliert Umlaute/ß und vertauscht Y/Z

## Verstanden
Diktat-Vorschau im GUI zeigt korrekten Text (ä/ö/ü/ß, Y/Z). Beim Tippen in
fremde Fenster (Konsole) fehlen Umlaute und ß, Y und Z sind vertauscht.

Ursache: `core.type_text` nutzt unter Wayland zuerst `ydotool`. `ydotool`
schreibt rohe US-QWERTY-Scancodes via `/dev/uinput` und kennt das XKB-Layout
nicht → QWERTZ-Layout falsch, Nicht-ASCII nicht tippbar.

## Plan
1. Unter Wayland zuerst `wtype` (Virtual-Keyboard-Protokoll, sendet Keysyms → layout-korrekt).
2. Fallback auf `ydotool`, dann `xdotool` (nur wenn wtype fehlt/nicht unterstützt).
3. `wtype` in `install.sh` und Doku aufnehmen.

## DoD
- [x] Code kompiliert (`python -m py_compile core.py`)
- [x] Reihenfolge wtype → ydotool → xdotool, Fallback bei wtype-Fehler
- [x] install.sh / README / WAYLAND_NOTES aktualisiert
- [x] Manueller Test (GNOME, Layout ch, App neu gestartet): "Übung Zürich Yak ä ö" → korrekt (ß wird als "ss" getippt)
