"""One popup at a time.

At startup (above all on a computer that has been away a while) Anki and
several add-ons each open a popup on their own timer. Qt stacks them, and the
one you must answer first can sit under one you can't click yet. Loadout
lines them up instead: while one popup is open, others that arrive by
themselves wait and open, one by one, when it closes. Anki's own popups go
first and Loadout's own go last.

Never held back: popups opened by your click or key press, and popups from the
same add-on as the open one (its follow-ups, e.g. "wrong password").

It needs no setting because it steps aside by itself: a window that isn't
really on screen (some add-ons run an invisible one as a wait) doesn't hold
the line, the next popup opens anyway after STUCK_SECONDS without a click or
key press (the open popup may be waiting for it), and any error of its own
switches it off for the session, leaving popups as plain Anki shows them.

Covers QDialog.exec/exec_/open and QMessageBox.information/question/warning/
critical, on Qt5 and Qt6 builds of Anki. Non-modal windows (show()) block
nothing, so they are left alone.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Any, Callable

import aqt
from aqt.qt import QApplication, QDialog, QEvent, QEventLoop, QMessageBox, QObject, QTimer

try:
    from aqt.qt import sip
except ImportError:  # Qt5 builds of older Anki
    from PyQt5 import sip

INPUT_SECONDS = 1.5
STUCK_SECONDS = 30
WATCH_MS = 1000
NEVER = float("-inf")  # time.monotonic() may start near 0 when Anki starts
_INPUT_EVENTS = {QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress, QEvent.Type.Shortcut}

_own = ""  # Loadout's folder: its popups wait for everyone else's
_addons: set[str] = set()  # installed add-on folders
_current: QDialog | None = None
_current_origin = ""
_waiting: list[dict] = []
_loops: list[QEventLoop] = []  # waiting exec() calls, innermost last
_last_input = NEVER
_quiet_since = NEVER  # when the open popup opened, or the last click or key press
_seq = 0
_broken = False
_log: Callable[[str], None] = lambda text: None
_watchdog: QTimer | None = None


def _origin() -> str:
    """The add-on folder that asked for this popup, or "anki" for Anki itself
    (and for anything that isn't an add-on, e.g. a shared library)."""
    f = sys._getframe(1)
    while f and f.f_globals.get("__name__") == __name__:
        f = f.f_back
    while f:
        name = f.f_globals.get("__name__") or ""
        if name == __name__:
            break  # an outer popup's frame: the rest of the stack isn't this popup's
        top = name.split(".")[0]
        if top in _addons:
            return top
        f = f.f_back
    return "anki"


class _Watcher(QObject):
    """Installed on the app only while a popup is open: notes clicks and key
    presses, and when the open popup goes away."""

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        global _last_input, _quiet_since
        try:
            kind = event.type()
            if kind in _INPUT_EVENTS:
                _last_input = _quiet_since = time.monotonic()
            elif kind == QEvent.Type.Hide and obj is _current:
                _gone(obj)
        except Exception:  # noqa: BLE001
            _fail()
        return False


_watcher: _Watcher | None = None


def _on_screen(dlg: QDialog) -> bool:
    """Whether someone could be looking at it and answering it."""
    if sip.isdeleted(dlg) or not dlg.isVisible() or dlg.isMinimized():
        return False
    # Not WA_DontShowOnScreen: Qt sets it on a box it shows as a native one (macOS).
    if dlg.windowOpacity() < 0.1:
        return False
    box = dlg.frameGeometry()
    if box.width() < 10 or box.height() < 10:
        return False
    return any(screen.geometry().intersects(box) for screen in QApplication.screens())


def _set_current(dlg: QDialog, origin: str) -> None:
    global _current, _current_origin, _last_input, _quiet_since
    _current, _current_origin, _last_input = dlg, origin, NEVER
    _quiet_since = time.monotonic()
    dlg.destroyed.connect(_on_destroyed)  # no lambda: it would keep the dialog alive
    QApplication.instance().installEventFilter(_watcher)


def _on_destroyed(*_: Any) -> None:
    try:
        if _current is not None and sip.isdeleted(_current):
            _gone(_current)
    except Exception:  # noqa: BLE001 - Anki quitting
        pass


def _gone(dlg: Any) -> None:
    global _current
    if dlg is not _current:
        return
    _current = None
    app = QApplication.instance()
    if app is not None and not sip.isdeleted(_watcher):  # else Anki is quitting
        app.removeEventFilter(_watcher)
    # Let Qt finish closing it before the next one opens.
    QTimer.singleShot(0, _advance)


def _advance() -> None:
    try:
        if _broken or _current is not None or not _waiting:
            return
        if _loops:
            _loops[-1].quit()  # the innermost waiting exec() opens the next one
        else:
            _start(_take())  # only open() popups are waiting: open the next here
    except Exception:  # noqa: BLE001
        _fail()


def _check() -> None:
    """Every WATCH_MS while popups wait: never hold them for a popup nobody
    can answer, or for one that may itself be waiting for them."""
    try:
        if _broken or not _waiting:
            _watchdog.stop()
        elif _current is None:
            _advance()
        elif not _on_screen(_current):
            _gone(_current)
        elif time.monotonic() - _quiet_since > STUCK_SECONDS:
            _log(f"popup line: nothing answered for {STUCK_SECONDS} s; opening the next popup anyway")
            _gone(_current)
    except Exception:  # noqa: BLE001
        _fail()


def _fail() -> None:
    """Something here went wrong: step aside for the rest of the session and
    let every popup open the way Anki opens it."""
    global _broken, _current
    if _broken:
        return
    _broken = True
    _log(f"popup line switched itself off for this session:\n{traceback.format_exc()}")
    try:
        _current = None
        QApplication.instance().removeEventFilter(_watcher)
        _watchdog.stop()
        for loop in _loops:
            loop.quit()  # each waiting exec() then opens its own popup
        for w in [w for w in _waiting if w["kind"] != "exec"]:
            _waiting.remove(w)
            if not sip.isdeleted(w["dlg"]):
                w["run"](w["dlg"], *w["args"])
    except Exception:  # noqa: BLE001
        pass


def _take() -> dict:
    nxt = min(_waiting, key=lambda w: (w["rank"], w["seq"]))
    _waiting.remove(nxt)
    return nxt


def _start(w: dict) -> None:
    dlg = w["dlg"]
    if sip.isdeleted(dlg):
        w["done"] = True
        _advance()
        return
    _set_current(dlg, w["origin"])
    if w["kind"] == "exec":
        try:
            w["result"] = _run(dlg, w["run"], w["args"])
        finally:
            w["done"] = True
    else:
        _run(dlg, w["run"], w["args"])


def _run(dlg: QDialog, run: Any, args: tuple) -> Any:
    """Open it for real. A popup that never showed, or failed to, must not
    hold up the line."""
    try:
        return run(dlg, *args)
    finally:
        if dlg is _current and (sip.isdeleted(dlg) or not dlg.isVisible()):
            _gone(dlg)


def _must_wait(origin: str) -> bool:
    if _broken or _current is None:
        return False
    if not _on_screen(_current):
        _gone(_current)  # e.g. an invisible window an add-on runs as a wait
        return False
    if time.monotonic() - _last_input < INPUT_SECONDS:
        return False  # you asked for it
    return origin == "anki" or origin != _current_origin


def _queue(dlg: QDialog, kind: str, run: Any, origin: str, args: tuple) -> dict:
    global _seq
    _seq += 1
    parent = dlg.parentWidget()
    if parent is not None and (parent is _current or _current.isAncestorOf(parent)):
        # Anki parents popups to the active window, often the open popup: it
        # would take this one with it when it closes. Wait on the main window.
        dlg.setParent(_current.parentWidget() or aqt.mw, dlg.windowFlags())
    rank = 0 if origin == "anki" else 2 if origin == _own else 1
    w = {"dlg": dlg, "kind": kind, "run": run, "origin": origin, "args": args,
         "rank": rank, "seq": _seq, "done": False, "result": 0}
    _waiting.append(w)
    if not _watchdog.isActive():
        _watchdog.start(WATCH_MS)
    return w


def _exec_with(run: Any):
    def exec_(self: QDialog, *args: Any) -> int:
        if _broken:
            return run(self, *args)
        try:
            origin = _origin()
            w = _queue(self, "exec", run, origin, args) if _must_wait(origin) else None
            if w is None and _current is None:
                _set_current(self, origin)
        except Exception:  # noqa: BLE001
            _fail()
            return run(self, *args)
        if w is None:
            return _run(self, run, args)
        loop = QEventLoop()
        _loops.append(loop)
        try:
            while not w["done"] and not _broken:
                if _current is None and _waiting:
                    _start(_take())
                elif loop.exec() != 0:
                    # Anki is quitting: answer "cancel" for everything still waiting.
                    for other in _waiting:
                        other["done"] = True
                    _waiting.clear()
                    w["done"] = True
        except Exception:  # noqa: BLE001
            _fail()
        finally:
            _loops.remove(loop)
        if not w["done"]:  # the line switched itself off: open it now
            if w in _waiting:
                _waiting.remove(w)
            return run(self, *args)
        return w["result"]

    return exec_


def _open_with(run: Any):
    def open_(self: QDialog, *args: Any) -> None:
        if _broken:
            return run(self, *args)
        try:
            origin = _origin()
            if _must_wait(origin):
                _queue(self, "open", run, origin, args)
                return None
            if _current is None:
                _set_current(self, origin)
        except Exception:  # noqa: BLE001
            _fail()
            return run(self, *args)
        _run(self, run, args)
        return None

    return open_


def _box(icon: Any, default_buttons: Any, parent: Any, title: Any, text: Any,
         buttons: Any = None, defaultButton: Any = None) -> QMessageBox:  # noqa: N803 - Qt's names
    buttons = default_buttons if buttons is None else buttons
    if defaultButton:
        buttons = buttons | defaultButton  # old style: question(p, t, x, Yes, No)
    box = QMessageBox(icon, title or "", text or "", buttons, parent)
    if defaultButton:
        box.setDefaultButton(defaultButton)
    return box


def _static(orig: Any, icon: Any, default_buttons: Any):
    """QMessageBox.question() and friends run their dialog inside Qt, out of
    reach; this builds the same box in Python so it goes through exec()."""
    B = QMessageBox.StandardButton

    def ask(*args: Any, **kwargs: Any) -> Any:
        try:
            box = _box(icon, default_buttons, *args, **kwargs)
        except TypeError:
            return orig(*args, **kwargs)  # a call style this doesn't know: Qt's own box
        if box.exec() == -1:
            return B.Cancel
        return box.standardButton(box.clickedButton())

    return staticmethod(ask)


def install(own_folder: str, addon_folders: set[str], log: Callable[[str], None] | None = None) -> bool:
    """Patch once per run (two copies of Loadout must not both patch)."""
    global _own, _addons, _watcher, _watchdog, _log
    if getattr(QDialog, "_loadout_popup_line", False):
        return False
    QDialog._loadout_popup_line = True
    _own, _addons, _watcher = own_folder, set(addon_folders) | {own_folder}, _Watcher()
    _log = log or _log
    _watchdog = QTimer()
    _watchdog.timeout.connect(_check)
    # Wherever a class defines these itself: Qt5 has exec_(), older Anki
    # aliases exec_ to exec on Qt6, and Qt5's QMessageBox has its own exec.
    for cls in (QDialog, QMessageBox):
        for name, wrap in (("exec", _exec_with), ("exec_", _exec_with), ("open", _open_with)):
            if name in cls.__dict__:
                setattr(cls, name, wrap(getattr(cls, name)))
    B, I = QMessageBox.StandardButton, QMessageBox.Icon
    for name, icon, buttons in (
        ("information", I.Information, B.Ok), ("warning", I.Warning, B.Ok),
        ("critical", I.Critical, B.Ok), ("question", I.Question, B.Yes | B.No),
    ):
        setattr(QMessageBox, name, _static(getattr(QMessageBox, name), icon, buttons))
    return True
