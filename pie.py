"""Pie menu: pick a mode for this dictation while the hotkey is held.

Hold the hotkey, move the mouse a little in a direction (or tap an arrow
key), release: the segment the pointer points into is the mode — "English",
"Formatted", whatever is configured. No movement means the plain written
text. Only the direction counts, so the pointer may leave the circle.

The selection never depends on the picture. GestureTracker works from raw
pointer motion on every platform (absolute positions from pynput, relative
counts from evdev on Wayland). PieOverlay is the feedback where a window
can be drawn at the pointer without taking focus (Windows, X11); on GNOME
Wayland the shell's own OSD shows the current mode instead.
"""
import math
import os
import shutil
import subprocess
import threading

from PyQt6.QtCore import Qt, QPoint, QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QWidget

import core
from i18n import tr

DEAD_ZONE = 60   # pointer must move this far (px / mouse counts) before a segment counts
RING = 170       # radius of the drawn circle
LABEL_RADIUS = (DEAD_ZONE + RING) / 2   # labels sit between the centre and the rim

# Clockwise from 12 o'clock, so four modes land on the four arrow keys.
DEFAULT_MODES = [
    {"name": "English",   "instructions": "Translate the text into natural English."},
    {"name": "Email",     "instructions": "Write the dictation as a complete email, everything in the language of the dictation (in German: \"Betreff:\", \"Hallo\"): a subject line, a greeting, the body. The body contains every fact, name, date and number from the dictation and nothing else. When the dictation says who to write to (\"schreib dem Peter, dass ich morgen komme\"), that person is the recipient: greet them by name and write the message itself to them (\"Hallo Peter, ich komme morgen.\"). Use a placeholder in square brackets, like [Name], for anything an email needs that was not said. The email ends with the last sentence of the body: closing and signature are added later. {signature}"},
    {"name": "Formatted", "instructions": "Structure the text: short paragraphs, bullet points or numbered steps where the content calls for it."},
    {"name": "Short message", "instructions": "Write only the message the speaker wants to send, in the language of the dictation, in as few words as possible. Keep every fact, name, date, number and reason. Use short correct sentences; cut greetings, politeness and everything that repeats. Words like \"schreib dem Peter\" or \"sag der Anna\" name the recipient and stay out of the message, which speaks directly to that person. Examples: \"also ich wollte sagen dass ich heute leider später komme so gegen halb zehn weil die bahn ausgefallen ist\" -> \"Komme heute gegen halb zehn, die Bahn ist ausgefallen.\" | \"schreib dem jonas dass ich krank bin und er die präsentation am freitag übernehmen soll\" -> \"Ich bin krank. Übernimm bitte am Freitag die Präsentation.\" | \"sag der lena bitte ob sie mir bis montag die rechnung schicken kann\" -> \"Kannst du mir bis Montag die Rechnung schicken?\""},
]


def modes_for(cfg):
    """Configured modes, or the examples when nothing was ever saved."""
    m = cfg.get("polish_modes")
    if m is None:
        m = [{**x, "name": tr(x["name"])} for x in DEFAULT_MODES]
    return [x for x in m if (x.get("name") or "").strip()]


def segment_for(dx, dy, n):
    """Index of the segment the vector (dx right, dy down) points into, or
    None inside the dead zone. Segment 0 is centred at 12 o'clock, the rest
    follow clockwise."""
    if n <= 0 or math.hypot(dx, dy) < DEAD_ZONE:
        return None
    ang = math.degrees(math.atan2(dx, -dy)) % 360     # 0 = up, 90 = right
    step = 360.0 / n
    return int(((ang + step / 2) % 360) // step)


_ARROW_ANGLES = {"up": 0, "right": 90, "down": 180, "left": 270}


def segment_for_arrow(name, n):
    """The segment an arrow key stands for: the one whose centre is nearest."""
    if n <= 0:
        return None
    step = 360.0 / n
    return int(((_ARROW_ANGLES[name] + step / 2) % 360) // step)


def use_overlay():
    """Can a window be drawn at the pointer without touching focus? Not on
    Wayland, where the compositor places windows and hands out focus."""
    return not (core.IS_LINUX and os.environ.get("XDG_SESSION_TYPE") == "wayland")


class GestureTracker:
    """Pointer motion since the hotkey went down, reduced to a segment.
    Called from listener threads; cheap. on_change(segment) fires only when
    the segment changes."""

    def __init__(self, on_change):
        self._lock = threading.Lock()
        self._on_change = on_change
        self.active = False
        self.n = 0
        self.dx = self.dy = 0.0
        self._origin = None
        self.segment = None

    def start(self, n, origin=None):
        with self._lock:
            self.active = True
            self.n = n
            self.dx = self.dy = 0.0
            self._origin = origin
            self.segment = None

    def stop(self):
        with self._lock:
            self.active = False
            return self.segment

    def abs_move(self, x, y):
        with self._lock:
            if not self.active:
                return
            if self._origin is None:
                self._origin = (x, y)
                return
            self.dx, self.dy = x - self._origin[0], y - self._origin[1]
            self._update()

    def rel_move(self, ddx, ddy):
        with self._lock:
            if not self.active:
                return
            self.dx += ddx
            self.dy += ddy
            self._update()

    def set_segment(self, segment):
        """From an arrow key: jump straight to a segment (None = centre)."""
        with self._lock:
            if not self.active:
                return
            self.dx = self.dy = 0.0
            if segment != self.segment:
                self.segment = segment
                self._on_change(segment)

    def vector(self):
        return self.dx, self.dy

    def _update(self):
        seg = segment_for(self.dx, self.dy, self.n)
        if seg != self.segment:
            self.segment = seg
            self._on_change(seg)


def gnome_osd(label, icon="audio-input-microphone-symbolic"):
    """Show text in GNOME Shell's own on-screen display (the volume pill).
    Drawn by the shell, so it never takes focus and works on Wayland. Fire
    and forget; returns False when gdbus is missing."""
    if not shutil.which("gdbus"):
        return False
    label = label.replace("'", "").replace("\\", "")
    try:
        subprocess.Popen(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
             "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.ShowOSD",
             f"{{'icon': <'{icon}'>, 'label': <'{label}'>}}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:
        core.log.exception("gnome osd failed")
        return False


class PieOverlay(QWidget):
    """Translucent circle at the pointer. Never takes focus or mouse events:
    a tool window shown without activation, transparent for input, above
    everything, bypassing the window manager on X11."""

    def __init__(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        if core.IS_LINUX:
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        size = 2 * RING + 60
        self.resize(size, size)
        self.modes = []
        self.segment = None
        self.vec = (0.0, 0.0)

    def show_at(self, modes, pos: QPoint):
        self.modes = modes
        self.segment = None
        self.vec = (0.0, 0.0)
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = min(max(pos.x() - w // 2, g.left()), g.right() - w)
        y = min(max(pos.y() - h // 2, g.top()), g.bottom() - h)
        self.move(x, y)
        self.show()
        self.raise_()
        self.update()

    def set_state(self, segment, vec):
        self.segment = segment
        self.vec = vec
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        active = QColor(0, 120, 215, 235)
        idle = QColor(55, 55, 55, 215)
        edge = QPen(QColor(0, 0, 0, 170), 2)
        white = QColor(255, 255, 255)
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        p.setFont(font)

        n = len(self.modes)
        rect = QRectF(c.x() - RING, c.y() - RING, 2 * RING, 2 * RING)
        if n:
            step = 360.0 / n
            for i, m in enumerate(self.modes):
                p.setBrush(active if i == self.segment else idle)
                p.setPen(edge)
                # Qt angles: 0 at 3 o'clock, counter-clockwise, 1/16 degree.
                start = 90 - i * step + step / 2
                p.drawPie(rect, int(start * 16), int(-step * 16))
                mid = math.radians(i * step)
                lx = c.x() + math.sin(mid) * LABEL_RADIUS
                ly = c.y() - math.cos(mid) * LABEL_RADIUS
                p.setPen(white)
                p.drawText(QRectF(lx - 70, ly - 16, 140, 32), Qt.AlignmentFlag.AlignCenter, m["name"])
        # Centre: no movement, plain written text.
        p.setBrush(active if self.segment is None else QColor(30, 30, 30, 235))
        p.setPen(edge)
        p.drawEllipse(c, DEAD_ZONE, DEAD_ZONE)
        p.setPen(white)
        p.drawText(QRectF(c.x() - DEAD_ZONE, c.y() - DEAD_ZONE, 2 * DEAD_ZONE, 2 * DEAD_ZONE),
                   Qt.AlignmentFlag.AlignCenter, tr("Standard"))
        # Pointer dot, clamped to the ring so it stays visible.
        dx, dy = self.vec
        d = math.hypot(dx, dy)
        if d > 0:
            s = min(d, RING - 12) / d
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(white)
            p.drawEllipse(QPointF(c.x() + dx * s, c.y() + dy * s), 6, 6)
        p.end()
