"""Windows: put text into the focused program through the clipboard.

Fast synthetic keystrokes can get garbled on the way in: Windows 11 Notepad cut
a dictation off after 13 characters and in a replay turned capitals into "E"
and moved line breaks. A typed line break is also an Enter key, which sends the
message in most chat windows. A paste arrives as one piece. The clipboard is
put back afterwards, and the dictation is kept out of the clipboard history.
"""
import ctypes
import threading
import time
from ctypes import wintypes

from pynput import keyboard

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# Handles that are not global memory (bitmaps, metafiles, palettes, owner
# drawn): they cannot be copied as bytes. Windows rebuilds a bitmap from CF_DIB.
UNCOPYABLE = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
MAX_FORMAT_BYTES = 64 * 1024 * 1024
RESTORE_AFTER = 0.5     # seconds the target program gets to read the clipboard
VK_V = 0x56             # the V key on every layout

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
_user32.EnumClipboardFormats.restype = wintypes.UINT
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL

_lock = threading.Lock()    # one paste at a time, so dictations stay in order


class _Clipboard:
    """The open clipboard. Another program may hold it for a moment."""

    def __enter__(self):
        for _ in range(40):
            if _user32.OpenClipboard(None):
                return self
            time.sleep(0.025)
        raise OSError("clipboard is held by another program")

    def __exit__(self, *exc):
        _user32.CloseClipboard()


def _snapshot():
    """Everything on the clipboard that can be copied, as (format, bytes)."""
    saved = []
    with _Clipboard():
        fmt = _user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in UNCOPYABLE:
                handle = _user32.GetClipboardData(fmt)
                size = _kernel32.GlobalSize(handle) if handle else 0
                if 0 < size <= MAX_FORMAT_BYTES:
                    ptr = _kernel32.GlobalLock(handle)
                    if ptr:
                        saved.append((fmt, ctypes.string_at(ptr, size)))
                        _kernel32.GlobalUnlock(handle)
            fmt = _user32.EnumClipboardFormats(fmt)
    return saved


def _replace(items):
    """Make items, a list of (format, bytes), the clipboard content."""
    with _Clipboard():
        if not _user32.EmptyClipboard():
            raise OSError("could not empty the clipboard")
        for fmt, data in items:
            handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                raise MemoryError("GlobalAlloc failed")
            ptr = _kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            _kernel32.GlobalUnlock(handle)
            # On success the clipboard owns the memory.
            if not _user32.SetClipboardData(fmt, handle):
                _kernel32.GlobalFree(handle)
                raise OSError(f"could not set clipboard format {fmt}")


def paste(text):
    """Paste text into the focused window, then restore the clipboard, unless
    somebody copied something else in the meantime."""
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\0\0"
    no_history = (b"\0\0\0\0", "ExcludeClipboardContentFromMonitorProcessing")
    items = [(CF_UNICODETEXT, data),
             (_user32.RegisterClipboardFormatW(no_history[1]), no_history[0])]
    controller = keyboard.Controller()
    with _lock:
        saved = _snapshot()
        _replace(items)
        ours = _user32.GetClipboardSequenceNumber()
        with controller.pressed(keyboard.Key.ctrl):
            controller.tap(keyboard.KeyCode.from_vk(VK_V))
        time.sleep(RESTORE_AFTER)
        if _user32.GetClipboardSequenceNumber() == ours:
            _replace(saved)
