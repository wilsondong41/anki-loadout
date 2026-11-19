"""Checks the popup line with real Qt dialogs, offscreen.

Needs Anki's own Python (PyQt), so it is not part of the unittest suite:
    QT_QPA_PLATFORM=offscreen <Anki's python> tests/qt_popups_check.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aqt.qt import QApplication, QDialog, QEvent, QKeyEvent, QMessageBox, Qt, QTimer  # noqa: E402

app = QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)  # Anki's main window is always open
import loadout.popups as popups  # noqa: E402

ADDONS = {"addon_a", "addon_b"}
logged: list[str] = []
assert popups.install("loadout", ADDONS, logged.append)
assert not popups.install("loadout_copy", ADDONS), "second copy must not patch again"

SRC = """
from aqt.qt import QDialog, QMessageBox, QTimer
EXEC = "exec_" if hasattr(QDialog, "exec_") else "exec"  # older add-ons on Qt5 call exec_()
results = {}
kept = []  # real popups have a parent that keeps them alive
def run(name):
    d = QDialog(); d.setObjectName(name); results[name] = getattr(d, EXEC)()
def open_(name):
    d = QDialog(); d.setObjectName(name); kept.append(d); d.open(); return d
def ask(name):
    results[name] = QMessageBox.question(None, name, name)
def later(ms, fn, name):
    QTimer.singleShot(ms, lambda: fn(name))  # popups arrive from the source's own timers
"""


def addon(module: str) -> dict:
    g = {"__name__": module}
    exec(SRC, g)
    return g


A, B, ANKI, OWN = addon("addon_a.main"), addon("addon_b"), addon("aqt.fake"), addon("loadout.engine")
shown: list[str] = []
most_at_once = 0


def visible() -> list:
    return [w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]


def tick() -> None:
    """Notes what is on screen and answers the newest popup, like a user would."""
    global most_at_once
    vis = visible()
    names = [w.objectName() or w.text() for w in vis]  # a Mac message box has no title
    most_at_once = max(most_at_once, len(vis) - ("A2" in names))  # A2 is A1's follow-up
    for n in sorted(names, key=lambda n: n != "A1"):
        if n not in shown:
            shown.append(n)
    if vis:
        top = vis[-1]
        if isinstance(top, QMessageBox) and top.button(QMessageBox.StandardButton.Yes):
            top.button(QMessageBox.StandardButton.Yes).click()
        else:
            top.accept()


def burst() -> None:
    """Five popups from four sources arrive while A1 is open."""
    B["later"](10, B["run"], "B1")
    ANKI["later"](20, ANKI["open_"], "K1")
    OWN["later"](30, OWN["run"], "L1")
    A["later"](40, A["open_"], "A2")
    B["later"](50, B["ask"], "Q1")
    A["run"]("A1")


ticker = QTimer()
ticker.timeout.connect(tick)
QTimer.singleShot(200, lambda: ticker.start(60))
QTimer.singleShot(0, burst)
QTimer.singleShot(3000, app.quit)
app.exec()
ticker.stop()

results = {**A["results"], **B["results"], **OWN["results"]}
print("shown in order:", shown, "results:", results)
assert most_at_once == 1, f"{most_at_once} unrelated popups were on screen at once"
assert shown[:2] == ["A1", "A2"], shown  # same add-on's follow-up is not held back
assert shown[2] == "K1", shown  # Anki's own popup goes first
assert shown[-1] == "L1", shown  # Loadout's own goes last
assert set(shown) == {"A1", "A2", "K1", "B1", "Q1", "L1"}, shown
assert results["B1"] == QDialog.DialogCode.Accepted and results["L1"] == QDialog.DialogCode.Accepted
assert results["Q1"] == QMessageBox.StandardButton.Yes, results
assert popups._current is None and not popups._waiting and not popups._loops

# A popup you open yourself (right after a key press) is never held back.
first = ANKI["open_"]("first")
app.sendEvent(first, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
yours = B["open_"]("yours")
assert first.isVisible() and yours.isVisible(), "a popup you asked for must open at once"
later = ANKI["open_"]("later")  # nothing pressed in between counts; but within 1.5 s it does
popups._last_input = popups.NEVER
held = B["open_"]("held")
assert not held.isVisible(), "a popup nobody asked for waits"
for d in (yours, later):
    d.accept()
first.accept()
app.processEvents()  # not app.exec(): quitting would close every window
assert held.isVisible(), "the waiting popup opens when the line is free"
held.accept()

# A keyboard shortcut counts as asking for it, like a click or key press.
first = ANKI["open_"]("first")
app.sendEvent(first, QEvent(QEvent.Type.Shortcut))
yours = B["open_"]("yours")
assert yours.isVisible(), "a popup opened by a shortcut must open at once"
yours.accept()
first.accept()
app.processEvents()

# Old call style keeps both buttons; Qt's own names still work.
SB = QMessageBox.StandardButton
box = popups._box(QMessageBox.Icon.Question, SB.Yes | SB.No, None, "t", "x", SB.Yes, SB.No)
assert box.button(SB.Yes) and box.button(SB.No) and box.defaultButton() is box.button(SB.No)
box = popups._box(QMessageBox.Icon.Information, SB.Ok, None, "t", "x")
assert box.button(SB.Ok)


# Anki's own popup helpers on a main window (sheets on a Mac), as at startup.
try:
    import anki.lang

    anki.lang.set_lang("en_US")
    import aqt
    import aqt.utils  # noqa: F401
except ImportError:
    print("Anki's helpers aren't importable here (Qt5 stand-in): skipped that part")
else:
    from aqt.qt import QMainWindow

    win = QMainWindow()
    win.resize(800, 600)
    win.show()
    aqt.mw = win  # where a waiting popup goes when the popup it was parented to closes
    HELPERS = """
from aqt.qt import QApplication, QTimer
from aqt.utils import askUser, show_info, showInfo
results = {}
kept = []
def info(name, parent):
    results[name] = showInfo(name, parent=parent)
def sheet(name, parent):
    kept.append(show_info(name, parent=parent))
def sheet_on_active(name, _):
    kept.append(show_info(name, parent=QApplication.activeModalWidget()))  # Anki's default parent
def ask(name, parent):
    results[name] = askUser(name, parent=parent)
def later(ms, fn, name, parent):
    QTimer.singleShot(ms, lambda: fn(name, parent))
"""

    def source(module: str) -> dict:
        g = {"__name__": module}
        exec(HELPERS, g)
        return g

    X, Y, CORE = source("addon_a.hooks"), source("addon_b"), source("aqt.sync")
    shown.clear()
    most_at_once = 0

    X["later"](0, X["info"], "X1", win)
    CORE["later"](30, CORE["sheet"], "K2", win)
    Y["later"](60, Y["ask"], "Y1", win)
    CORE["later"](90, CORE["sheet_on_active"], "C1", None)
    QTimer.singleShot(300, lambda: ticker.start(80))  # all four are waiting by then
    QTimer.singleShot(3000, app.quit)
    app.exec()
    ticker.stop()
    print("Anki helpers shown in order:", shown, X["results"], Y["results"])
    assert most_at_once == 1, f"{most_at_once} popups were on screen at once"
    # Anki's own before the other add-on's; C1 outlived the popup it was parented to.
    assert shown == ["X1", "K2", "C1", "Y1"], shown
    assert Y["results"]["Y1"] is True, Y["results"]
    assert popups._current is None and not popups._waiting and not popups._loops

    # Nothing else waiting: X3 is deleted as soon as it closes (showInfo's box
    # is a local), and C3, parented to it, must still open.
    shown.clear()
    X["later"](0, X["info"], "X3", win)
    CORE["later"](30, CORE["sheet_on_active"], "C3", None)
    QTimer.singleShot(300, lambda: ticker.start(80))
    QTimer.singleShot(1500, app.quit)
    app.exec()
    ticker.stop()
    assert shown == ["X3", "C3"], shown
    assert popups._current is None and not popups._waiting


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


# It steps aside by itself (no setting to change), in three ways.
# 1. A window nobody can see (an add-on running one as a wait) holds nothing up.
hidden = QDialog()
hidden.setFixedSize(1, 1)
A["run"].__globals__["kept"].append(hidden)
hidden.open()
assert popups._current is hidden
seen = B["open_"]("seen")
assert seen.isVisible() and popups._current is seen, "an invisible window must not hold the line"
seen.accept()
hidden.accept()
pump(0.05)

# 2. Nothing clicked for STUCK_SECONDS: the next popup opens anyway.
popups.STUCK_SECONDS, popups.WATCH_MS = 0.3, 50
popups._watchdog.stop()  # restarts at the shorter interval
stuck = A["open_"]("stuck")
nxt = B["open_"]("next")
assert not nxt.isVisible()
pump(0.15)
assert not nxt.isVisible(), "it waits a while first"
pump(0.5)
assert nxt.isVisible(), "the next popup opens once nothing was answered for a while"
assert any("anyway" in line for line in logged), logged
nxt.accept()
stuck.accept()
pump(0.05)
assert popups._current is None and not popups._waiting

# 3. An error of its own: it switches off for the session, popups open as usual.
first = A["open_"]("first")
real_origin = popups._origin
popups._origin = lambda: 1 / 0
after = B["open_"]("after")
assert after.isVisible() and popups._broken, "after an error popups must still open"
assert any("switched itself off" in line for line in logged), logged
again = B["open_"]("again")
assert again.isVisible()
popups._origin = real_origin
for d in (first, after, again):
    d.accept()

print("popup line OK")
