"""Checks the Add-on Hub against Anki's real main-window menus, offscreen.

Needs Anki's own Python (PyQt), so it is not part of the unittest suite:
    QT_QPA_PLATFORM=offscreen <Anki's python> tests/qt_hub_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anki.lang  # noqa: E402

anki.lang.set_lang("en_US")

from aqt.qt import QAction, QApplication, QKeySequence, QMainWindow, QMenu  # noqa: E402

app = QApplication.instance() or QApplication([])
from _aqt.forms.main_qt6 import Ui_MainWindow  # noqa: E402

import loadout.hub as hub  # noqa: E402

win = QMainWindow()
form = Ui_MainWindow()
form.setupUi(win)


class FakeProgress:
    """Anki's timers: callbacks queue up and run when flush() is called."""

    def __init__(self):
        self.queue = []

    def single_shot(self, ms, fn):
        self.queue.append(fn)

    def flush(self):
        while self.queue:
            self.queue.pop(0)()


win.form = form
win.progress = FakeProgress()
hub.mw = win  # a real main window standing in for Anki's

# Add-ons' menu items, the way they add them.
tools = form.menuTools
uworld = tools.addAction("Find cards from UWorld test")
terminator = QMenu("🤖Anki Terminator (Created by Shige)", win)
term_item = terminator.addAction("Anki Terminator")
term_item.setShortcut(QKeySequence("Ctrl+G"))
terminator.addAction("👍️RateThis")
tools.addMenu(terminator)
pomodoro = tools.addAction("Pomodoro Bar")
loadout_item = tools.addAction("Loadout…")
leaderboard = QMenu("&Leaderboard", win)
leaderboard.addAction("Open leaderboard")
form.menubar.addMenu(leaderboard)


def layout() -> dict[str, list[str]]:
    out = {}
    for top in form.menubar.actions():
        if top.menu() is not None:
            out[top.text()] = [a.text() or "---" for a in top.menu().actions()]
    return out


before = layout()
before_ids = {t.text(): [id(a) for a in t.menu().actions()] for t in form.menubar.actions() if t.menu()}

h = hub.Hub(tempfile.mkdtemp(), "loadout", lambda: loadout_item,
            os.path.join(tempfile.mkdtemp(), "hub.json"), print)
h.store = {"enabled": True, "favorites": [
    {"path": ["🤖Anki Terminator (Created by Shige)", "Anki Terminator"], "owner": "1468920185"},
    {"path": ["Find cards from UWorld test"], "owner": "1939527074"},
]}
h.found = {
    "Find cards from UWorld test": ["1939527074"],
    "🤖Anki Terminator (Created by Shige)": ["1468920185"],
    "Anki Terminator": ["1468920185"],
    "👍️RateThis": ["1468920185", "1045980020"],
    "Pomodoro Bar": ["239058710"],
    "&Leaderboard": [],
    "Open leaderboard": ["175794613"],
}
h.names = {"1939527074": "Find cards from UWorld test Updated", "1468920185": "Anki Terminator V2",
           "239058710": "Pomodoro Bar", "175794613": "Anki Leaderboard", "1045980020": "Always On Top V2"}
h.ready = True

failures = []


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


h.apply()
h._watch()
after = layout()
tools_after = after[form.menuTools.menuAction().text()]
print("Tools now:", tools_after)
check("&Leaderboard" not in after, "Leaderboard menu left the menu bar")
check(tools_after[-3:] == ["---", "All add-ons", "Loadout…"], "All add-ons and Loadout at the end of Tools")
check("Anki Terminator" in tools_after and "Find cards from UWorld test" in tools_after,
      "both favorites directly in Tools")
check("Pomodoro Bar" not in tools_after, "other add-on items moved out of Tools")
all_menu = [a for a in tools.actions() if a.text() == "All add-ons"][0].menu()
print("All add-ons:", [a.text() for a in all_menu.actions()])
check([a.text() for a in all_menu.actions()] ==
      ["Anki Leaderboard", "Anki Terminator V2", "Find cards from UWorld test", "Pomodoro Bar"]
      or [a.text() for a in all_menu.actions()] ==
      ["&Leaderboard", "🤖Anki Terminator (Created by Shige)", "Find cards from UWorld test", "Pomodoro Bar"],
      "one entry per add-on, sorted")
check(term_item in win.actions(), "Ctrl+G item still attached to the main window (shortcut works)")
check(len([a for a in tools.actions() if a is uworld]) == 1, "UWorld item in Tools once (as favorite)")

late = tools.addAction("Stop Reading")  # an add-on adding an item after tidying
h.found["Stop Reading"] = ["read_aloud"]
h.names["read_aloud"] = "Read Aloud"
win.progress.flush()
check("Stop Reading" not in [a.text() for a in tools.actions()], "late item tidied immediately")
new_all = [a for a in tools.actions() if a.text() == "All add-ons"][0].menu()
inside = [a.text() for a in new_all.actions()]
inside += [x.text() for a in new_all.actions() if a.menu() for x in a.menu().actions()]
check("Stop Reading" in inside, "late item now under All add-ons")

# Previews: an item that opens a window gets its picture remembered.
from aqt.qt import QDialog, QLabel  # noqa: E402

def open_settings():
    d = QDialog(win)
    d.setWindowTitle("Pomodoro settings")
    QLabel("Minutes per session: 25", d)
    d.resize(300, 120)
    d.show()
    open_settings.dialog = d

pomodoro.triggered.connect(open_settings)
check(h.summary(["Pomodoro Bar"], pomodoro, "239058710") == ("", None), "unexplained item: no guess, no picture")
switch = QAction("Keep on top", win)
switch.setCheckable(True)
check(h.summary(["Keep on top"], switch, "x")[0] == "On/off switch (off)", "switches say so, with their state")
check(h.kind(["Keep on top"], switch, "x") == ("toggle", "off"), "double-click knows it's a switch")
h.run(["Pomodoro Bar"])
win.progress.flush()
info = h.previews.get("Pomodoro Bar", {})
check(info.get("kind") == "window" and info.get("title") == "Pomodoro settings", "window title remembered")
check(os.path.exists(os.path.join(h.preview_dir, info.get("image", "x"))), "preview picture saved")
check(h.summary(["Pomodoro Bar"], pomodoro, "239058710")[0] == "Opens a window"
      and h.summary(["Pomodoro Bar"], pomodoro, "239058710")[1], "seen window: words and its picture")
check(h.kind(["Pomodoro Bar"], pomodoro, "239058710")[0] == "window", "once seen, double-click just opens it")
check(h.summary(["🤖Anki Terminator (Created by Shige)"], terminator.menuAction())[0] == "Submenu", "submenus say so")

# Anki's own Tools items can move too.
h.store["include_anki"] = True
h.apply()
tools_now = [a.text() for a in tools.actions()]
all_tools = [a for a in tools.actions() if a.text() == "All tools"]
check(bool(all_tools) and "Study Deck..." not in tools_now, "Anki's own items moved into All tools")
inside_all = [a.text() for a in all_tools[0].menu().actions()] if all_tools else []
check(inside_all[:2] == ["Study Deck...", "Create Filtered Deck..."], "Anki's items first, in order")
shown = [a.text() or "---" for a in tools.actions() if a.isVisible()]
print("Tools with Anki's items moved (visible):", shown)
check(shown[0] != "---" and "---" not in [x for x, y in zip(shown, shown[1:]) if x == y == "---"],
      "no leftover divider lines")
h.store["include_anki"] = False

h.restore()
restored = layout()
print("Tools before :", before[form.menuTools.menuAction().text()])
print("Tools restored:", restored[form.menuTools.menuAction().text()])
expected = dict(before)
expected[form.menuTools.menuAction().text()] = before[form.menuTools.menuAction().text()][:-1] + ["Stop Reading", "Loadout…"]
tools_key = form.menuTools.menuAction().text()
check(restored["&Leaderboard"] == before["&Leaderboard"], "Leaderboard menu back on the menu bar")
check([k for k in restored] == [k for k in before], "menu bar order restored")
check(restored[tools_key][: len(before[tools_key]) - 1] == before[tools_key][:-1],
      "Tools items back in their original order")
check(term_item not in win.actions(), "shortcut attachment removed again")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
