"""Loadout's window and dialogs: status, review, rollback, approvals."""

from __future__ import annotations

import datetime
import html
import os
from typing import TYPE_CHECKING, Any

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QActionGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, disable_help_button, restoreGeom, saveGeom, showInfo, showWarning

if TYPE_CHECKING:
    from .engine import Engine


def _when(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def choose_code_installs(items: list[tuple[str, str]]) -> set[str]:
    """Ask which add-ons that are not on AnkiWeb may be installed here.

    items are (folder, name). Returns the approved folders."""
    d = QDialog(mw)
    d.setWindowTitle("Loadout: approve add-ons")
    disable_help_button(d)
    layout = QVBoxLayout(d)
    label = QLabel(
        "Your saved setup includes add-ons that did not come from AnkiWeb, so "
        "Loadout copies their code directly. Add-ons can run any code on this "
        "computer.<br><br><b>Only approve ones you recognise.</b> You are only "
        "asked again if an add-on's code changes."
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    lst = QListWidget()
    for folder, name in items:
        item = QListWidgetItem(f"{name}   ({folder})")
        item.setData(Qt.ItemDataRole.UserRole, folder)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        lst.addItem(item)
    layout.addWidget(lst)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(d.accept)
    layout.addWidget(buttons)
    d.exec()
    approved = set()
    for i in range(lst.count()):
        item = lst.item(i)
        if item.checkState() == Qt.CheckState.Checked:
            approved.add(item.data(Qt.ItemDataRole.UserRole))
    return approved


# What each kind of difference means, and the choices offered (label, action).
# action None leaves it for later.
REVIEW_CHOICES = {
    "new_elsewhere": ("On another computer, not this one", [
        ("Decide later", None), ("Install here", "install"),
        ("Keep off this computer", "keep_off"), ("Remove everywhere", "remove_everywhere"),
    ]),
    "removed_elsewhere": ("Removed on another computer", [
        ("Decide later", None), ("Remove here too", "remove_here"),
        ("Keep it here only (stop mirroring it)", "keep_here"),
    ]),
    "only_here": ("Only on this computer", [
        ("Decide later", None), ("Share to my other computers", "share"),
        ("Keep only on this computer", "keep_only_here"), ("Delete it", "remove_everywhere"),
    ]),
    "kept_off": ("Kept off this computer (your choice)", [
        ("Leave as is", None), ("Install here after all", "install"),
    ]),
    "kept_here": ("Kept only on this computer (your choice)", [
        ("Leave as is", None), ("Share to my other computers", "share"),
    ]),
}


def review_differences(items: list[dict]) -> dict[str, str]:
    """Ask what to do about add-ons that differ between computers.
    Returns folder -> action for every row the user decided."""
    d = QDialog(mw)
    d.setWindowTitle("Loadout: add-ons that differ")
    disable_help_button(d)
    layout = QVBoxLayout(d)
    label = QLabel(
        "These add-ons aren't the same on all your computers. Choose what to do "
        "with each one, or leave it for later. Settings and data of add-ons you "
        "share keep mirroring by themselves.<br><br>"
        "<small>Removed and deleted add-ons go to the system trash.</small>"
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    table = QTableWidget(len(items), 3)
    table.setHorizontalHeaderLabels(["Add-on", "Where it is", "What to do"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the choice boxes still take focus
    boxes = []
    for row, item in enumerate(items):
        situation, choices = REVIEW_CHOICES[item["kind"]]
        table.setItem(row, 0, QTableWidgetItem(item["name"]))
        table.setItem(row, 1, QTableWidgetItem(situation))
        box = QComboBox()
        for text, action in choices:
            box.addItem(text, action)
        table.setCellWidget(row, 2, box)
        boxes.append((item["folder"], box))
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    layout.addWidget(table)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout.addWidget(buttons)
    d.resize(760, 160 + 34 * len(items))
    if not d.exec():
        return {}
    return {folder: box.currentData() for folder, box in boxes if box.currentData()}


MODE_LABELS = {
    "mirror": "Mirror (automatic)",
    "preview": "Preview only (don't change this computer)",
    "paused": "Paused",
}


def _summary(engine: Engine, differences: list[dict]) -> str:
    """One plain sentence (or two) saying where things stand."""
    from .anki_device import sync_problems

    if engine.inactive_reason:
        return f"\u26a0\ufe0f {engine.inactive_reason}"
    problems = sync_problems()
    if problems:
        return f"\u26a0\ufe0f {problems[0]}"
    if engine.mode == "paused":
        return "Paused: nothing is mirrored until you switch back (More \u25be \u2192 Mirror)."
    if not engine.state["joined"]:
        return ("This computer isn't set up yet. Click <b>Sync now</b>: Loadout will "
                "offer to copy your saved setup here.")
    if engine.mode == "preview":
        waiting = engine.state["preview"]
        text = "Preview only: changes from your other computers are listed, not applied."
        if waiting:
            text += "<br>Waiting: " + html.escape(", ".join(waiting))
        return text
    count = len([d for d in differences if d["kind"] not in ("kept_off", "kept_here")])
    notes = []
    for other in engine.other_versions():
        if other["newer"]:
            notes.append(f"\u26a0\ufe0f \u201c{html.escape(other['computer'])}\u201d has a newer Loadout "
                         f"({other['version']}). Install it here too.")
        else:
            notes.append(f"\u26a0\ufe0f \u201c{html.escape(other['computer'])}\u201d has an older Loadout "
                         f"({other['version']}). Install this version there.")
    if count:
        main = f"<b>{count} add-on{'s' if count > 1 else ''} need{'' if count > 1 else 's'} your decision.</b>"
    else:
        main = "\u2705 This computer matches your other computers."
    from .version import parse as parse_version

    mine = engine.d.anki_version()
    short = lambda v: ".".join(str(n) for n in parse_version(v)[:2])  # noqa: E731
    for other in engine.other_anki_versions():
        notes.append(
            f"<small>\u2139\ufe0f \u201c{html.escape(other['computer'])}\u201d runs Anki {short(other['anki'])} "
            f"(this computer: {short(mine)}). Update whenever convenient: "
            '<a href="https://apps.ankiweb.net">download</a>.</small>'
        )
    return "<br>".join([main] + notes)


def _rows(engine: Engine, found: list[dict]) -> list[tuple[str, str, str, str, bool | None]]:
    """(add-on, status, details, folder, mirrored) for every add-on here or
    elsewhere. mirrored is None where the Mirror box doesn't apply: add-ons
    not on this computer, or excluded in Loadout's raw settings."""
    local = engine.local_entries()  # also refreshes engine.reports
    state = engine.state
    behind = engine.behind_on_ankiweb(local)
    differences = {d["folder"]: d for d in found}
    situations = {kind: text for kind, (text, _) in REVIEW_CHOICES.items()}
    rows = []
    for r in sorted(engine.reports, key=lambda r: r.name.lower()):
        details = [f"New computers get it: {r.source}"] if r.source else []
        diff = differences.get(r.folder)
        if diff and diff["kind"] in ("removed_elsewhere", "only_here"):
            status = f"{situations[diff['kind']]}: needs your decision"
        elif r.excluded:
            status = ("Only on this computer (your choice)" if r.folder in state["excluded_here"]
                      else "Not mirrored (Loadout setting)")
        elif f"a/{r.folder}" in state["failed"]:
            status = "\u26a0\ufe0f Couldn't install the latest version (More \u25be \u2192 Retry)"
        elif r.source.startswith("\u26a0"):
            status = r.source
        elif r.self_sync:
            status = "\u2705 Mirrored (keeps its own data in sync)"
        else:
            status = "\u2705 Mirrored"
        if not r.enabled:
            status += " \u00b7 disabled"
        if r.folder in behind:
            status += " \u00b7 newer version on AnkiWeb"
            details.append(
                "You've kept this version on purpose (auto-updates off or edited here). "
                "Updating it in Tools \u2192 Add-ons would replace your version."
            )
        if f"f/{r.folder}" in state["held_files"]:
            details.append("Data update waits until you close Anki")
        if r.secret_keys:
            details.append("Kept on each computer: " + ", ".join(r.secret_keys))
        if r.skipped_files:
            reasons: dict[str, int] = {}
            for _, why in r.skipped_files:
                reasons[why] = reasons.get(why, 0) + 1
            details.append("Files left out: " + ", ".join(f"{n} {why}" for why, n in reasons.items()))
        if r.folder in engine.settings.exclude_addons:
            mirrored = None
            details.append("Excluded in Loadout's settings (exclude_addons)")
        else:
            mirrored = r.folder not in state["excluded_here"]
        rows.append((r.name, status, "\n".join(details), r.folder, mirrored))
    for folder, d in differences.items():
        if d["kind"] in ("new_elsewhere", "kept_off"):
            status = situations[d["kind"]]
            if d["kind"] == "new_elsewhere":
                status += ": needs your decision"
            rows.append((d["name"], status, "", folder, None))
    return rows


class LoadoutWindow(QDialog):
    """Everything about Loadout in one place: the Sync tab (mirroring between
    computers) and the Menus tab (tidying add-on menu items). actions:
    callables from main.py (sync, review, set_mode, rollback, retry,
    open_log, use_profile, mirror); hub: the Add-on Hub, if running."""

    def __init__(self, engine: Engine, actions: dict, hub: Any = None) -> None:
        super().__init__(mw)
        self.engine = engine
        self.actions = actions
        self.hub = hub
        self.setWindowTitle("Loadout")
        disable_help_button(self)
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        sync_page = QWidget()
        layout = QVBoxLayout(sync_page)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setOpenExternalLinks(True)
        layout.addWidget(self.summary)

        banner = QHBoxLayout()
        self.review_button = QPushButton("Review add-on differences\u2026")
        self.review_button.clicked.connect(lambda: self._run("review"))
        self.profile_button = QPushButton("Use Loadout with this profile")
        self.profile_button.clicked.connect(lambda: self._run("use_profile"))
        banner.addWidget(self.review_button)
        banner.addWidget(self.profile_button)
        banner.addStretch()
        layout.addLayout(banner)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Add-on (\u2611 = mirrored from this computer)", "Status"])
        self.table.itemChanged.connect(self._mirror_changed)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        self.last = QLabel()
        self.last.setWordWrap(True)
        layout.addWidget(self.last)

        buttons = QHBoxLayout()
        sync = QPushButton("Sync now")
        sync.clicked.connect(lambda: self._run("sync"))
        more = QPushButton("More \u25be")
        more.setMenu(self._more_menu())
        more.setStyleSheet("QPushButton::menu-indicator { image: none; width: 0px; }")  # the label has its arrow
        buttons.addWidget(sync)
        buttons.addWidget(more)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.tabs.addTab(sync_page, "Sync")

        if hub is not None:
            self.tabs.addTab(self._menus_page(), "Menus")
        self.tabs.addTab(self._share_page(), "Share")

        bottom = QHBoxLayout()
        from .version import VERSION

        version = QLabel(f"<small>Loadout {VERSION}</small>")
        bottom.addWidget(version)
        bottom.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        outer.addLayout(bottom)

        self.resize(720, 600)
        restoreGeom(self, "loadout-window")
        self.refresh()

    # Share tab
    ##########################################################################

    def _share_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        give = QLabel(
            "<b>Share with a friend</b><br>Save your add-ons, with their settings, "
            "data and your own edits, as one <i>.loadout</i> file. Share everything "
            "or just one add-on. Your logins are never included."
        )
        give.setWordWrap(True)
        layout.addWidget(give)
        give_button = QPushButton("Choose what to share\u2026")
        give_button.clicked.connect(lambda: share_dialog(self.engine))
        layout.addWidget(give_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(18)
        take = QLabel(
            "<b>Install what a friend shared</b><br>Open their <i>.loadout</i> file, "
            "pick what you want, and Loadout installs it. Your own logins are kept."
        )
        take.setWordWrap(True)
        layout.addWidget(take)
        take_button = QPushButton("Open a .loadout file\u2026")
        take_button.clicked.connect(lambda: open_shared(self.engine))
        layout.addWidget(take_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return page

    # Menus tab
    ##########################################################################

    def _menus_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tidy = QCheckBox("Tidy add-on menus")
        self.tidy.toggled.connect(self._tidy_toggled)
        layout.addWidget(self.tidy)
        self.include_anki = QCheckBox("Also move Anki\u2019s own Tools items (Study Deck, Create Filtered Deck\u2026)")
        self.include_anki.toggled.connect(self._include_anki_toggled)
        layout.addWidget(self.include_anki)
        explain = QLabel(
            "Everything goes into <b>Tools \u2192 All add-ons</b>. Tick \u2605 to keep an item "
            "right in Tools. Double-click an item to open it."
        )
        explain.setWordWrap(True)
        layout.addWidget(explain)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search to find something to \u2605 (for example: sidebar, uworld)")
        self.search.textChanged.connect(self._filter_items)
        layout.addWidget(self.search)
        self.items = QTableWidget(0, 3)
        self.items.setHorizontalHeaderLabels(["\u2605  Menu item", "What it does", "Add-on"])
        self.items.verticalHeader().setVisible(False)
        self.items.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.items.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.items.itemChanged.connect(self._favorite_changed)
        self.items.cellDoubleClicked.connect(self._open_item)
        self.items.setMouseTracking(True)
        layout.addWidget(self.items)
        self.menus_note = QLabel()
        self.menus_note.setWordWrap(True)
        layout.addWidget(self.menus_note)
        return page

    def _tidy_toggled(self, on: bool) -> None:
        if self.hub is not None and on != self.hub.enabled:
            self.hub.set_enabled(on)
            self.refresh()

    def _include_anki_toggled(self, on: bool) -> None:
        if self.hub is not None and on != self.hub.include_anki:
            self.hub.set_include_anki(on)
            self.refresh()

    def _open_item(self, row: int, column: int) -> None:
        item = self.items.item(row, 0)
        if self.hub is None or item is None:
            return
        path, owner = item.data(Qt.ItemDataRole.UserRole)
        action = next((r["action"] for r in self.hub.inventory() if r["path"] == path), None)
        if action is None or action.menu() is not None:
            return
        kind, detail = self.hub.kind(path, action, owner)
        label = html.escape(path[-1])
        question = {
            "toggle": f"Switch \u201c{label}\u201d {'off' if detail == 'on' else 'on'}?",
            "link": f"Open the website for \u201c{label}\u201d?",
            "setting": f"Change \u201c{label}\u201d now?",
            "action": f"Run \u201c{label}\u201d now?",
            "unknown": f"Run \u201c{label}\u201d now?",
        }.get(kind)
        if question and not askUser(question, parent=self, defaultno=True, title="Loadout"):
            return
        self.hub.run(path)
        # The tooltip learns what it opened a few seconds later.
        mw.progress.single_shot(int(3500), self._refresh_menus)

    def _favorite_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or self.hub is None:
            return
        path, owner = item.data(Qt.ItemDataRole.UserRole)
        self.hub.set_favorite(path, owner, item.checkState() == Qt.CheckState.Checked)
        self._refresh_menus()

    def _filter_items(self, text: str) -> None:
        words = text.lower().split()
        for row in range(self.items.rowCount()):
            haystack = " ".join(
                (self.items.item(row, col).text() if self.items.item(row, col) else "")
                for col in (0, 1, 2)
            ).lower()
            self.items.setRowHidden(row, not all(w in haystack for w in words))

    def _refresh_menus(self) -> None:
        hub = self.hub
        if hub is None:
            return
        for box, value in ((self.tidy, hub.enabled), (self.include_anki, hub.include_anki)):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        if not hub.ready:
            self.items.setRowCount(0)
            self.menus_note.setText("<small>Identifying add-on menu items\u2026 (a few seconds after Anki starts)</small>")
            return
        rows = [r for r in hub.inventory() if not r["has_menu"]]  # submenus themselves: nothing to open
        favorite = [r for r in rows if hub.is_favorite(r["path"], r["owner"])]
        rows = favorite + sorted(
            (r for r in rows if r not in favorite),
            key=lambda r: (r["owner_name"].lower(), [p.lower() for p in r["path"]]),
        )
        self.items.blockSignals(True)
        self.items.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # The add-on is in its own column: show the item, plus any group inside its menu.
            label = " \u25b8 ".join(r["path"][1:] if len(r["path"]) > 1 else r["path"])
            first = QTableWidgetItem(label)
            first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            first.setCheckState(Qt.CheckState.Checked if r in favorite else Qt.CheckState.Unchecked)
            first.setData(Qt.ItemDataRole.UserRole, (r["path"], r["owner"]))
            does, picture = hub.summary(r["path"], r["action"], r["owner"])
            tip = f'<img src="{picture}" width="320">' if picture else ""
            for col, item in ((0, first), (1, QTableWidgetItem(does)), (2, QTableWidgetItem(r["owner_name"]))):
                item.setToolTip(tip)
                self.items.setItem(i, col, item)
        self.items.blockSignals(False)
        self._filter_items(self.search.text())
        count = len(favorite)
        self.menus_note.setText(
            f"<small>{len(rows)} add-on menu items \u00b7 {count} favorite{'s' if count != 1 else ''}"
            + (" \u00b7 tidying is on" if hub.enabled else " \u00b7 tidying is off") + "</small>"
        )

    def _more_menu(self) -> QMenu:
        menu = QMenu(self)
        modes = menu.addMenu("Mode")
        group = QActionGroup(modes)
        for mode, label in MODE_LABELS.items():
            action = QAction(label, modes)
            action.setCheckable(True)
            action.setChecked(self.engine.mode == mode)
            action.triggered.connect(lambda _=False, m=mode: self._run("set_mode", m))
            group.addAction(action)
            modes.addAction(action)
        menu.addAction("Roll back\u2026").triggered.connect(lambda: self._run("rollback"))
        menu.addAction("Retry skipped add-ons").triggered.connect(lambda: self._run("retry"))
        menu.addSeparator()
        menu.addAction("Open log folder").triggered.connect(lambda: self._run("open_log"))
        return menu

    def _mirror_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        folder = item.data(Qt.ItemDataRole.UserRole)
        self._run("mirror", folder, item.checkState() == Qt.CheckState.Checked)

    def _run(self, name: str, *args: object) -> None:
        self.actions[name](*args)
        self.refresh()

    def refresh(self) -> None:
        self._refresh_menus()
        engine = self.engine
        found = engine.differences(include_settled=True)
        self.summary.setText(_summary(engine, found))
        active = not engine.inactive_reason
        self.profile_button.setVisible(not active and not engine.other_copy)
        self.review_button.setVisible(active and bool(found))
        rows = _rows(engine, found) if active else []
        self.table.setVisible(active)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, (name, status, details, folder, mirrored) in enumerate(rows):
            for col, text in enumerate((name, status)):
                item = QTableWidgetItem(text)
                item.setToolTip(details or text)
                self.table.setItem(i, col, item)
            first = self.table.item(i, 0)
            first.setData(Qt.ItemDataRole.UserRole, folder)
            if mirrored is not None:
                first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                first.setCheckState(Qt.CheckState.Checked if mirrored else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        last = engine.state.get("last_changes") or {}
        if active and last.get("lines"):
            shown = last["lines"][:6] + (["\u2026"] if len(last["lines"]) > 6 else [])
            self.last.setText(
                f"<small><b>Last update from another computer</b> ({_when(last['time'])}):<br>"
                + "<br>".join(html.escape(x) for x in shown) + "</small>"
            )
        else:
            self.last.setText("")

    def closeEvent(self, event: object) -> None:
        saveGeom(self, "loadout-window")
        super().closeEvent(event)


def _checkable(text: str, checked: bool, enabled: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
    if not enabled:
        flags &= ~Qt.ItemFlag.ItemIsEnabled
    item.setFlags(flags)
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    return item


def _checked(item: QTableWidgetItem | None) -> bool:
    return item is not None and item.checkState() == Qt.CheckState.Checked


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # no focus frame on the first row
    header = table.horizontalHeader()
    # Names in full; the last column (notes) takes the room that is left.
    for col in range(len(headers) - 1):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    return table


def share_dialog(engine: Engine) -> None:
    """Pick add-ons (and whether to include their data), then save a
    .loadout file."""
    from . import share

    rows = share.shareable(engine)
    d = QDialog(mw)
    d.setWindowTitle("Share add-ons")
    disable_help_button(d)
    layout = QVBoxLayout(d)
    note = QLabel(
        "Tick the add-ons to share. <b>Data</b> is what an add-on saved for you "
        "(its own files): it can include personal things like game progress, so "
        "untick it where you'd rather not share. Logins are never included."
    )
    note.setWordWrap(True)
    layout.addWidget(note)
    table = _table(["Add-on", "Data", "Notes"])
    table.setRowCount(len(rows))
    for i, r in enumerate(rows):
        table.setItem(i, 0, _checkable(r["name"], True))
        table.setItem(i, 1, _checkable((f"{r['files']} file" + ("s" if r["files"] != 1 else "")) if r["files"] else "none", bool(r["files"]),
                                       enabled=bool(r["files"])))
        notes = []
        if not r["ankiweb"]:
            notes.append("not on AnkiWeb: its code is included (if you paid for it, check its licence allows sharing)")
        elif r["edited"]:
            notes.append("your edits included (exact copy)")
        elif r["exact"]:
            notes.append("exact copy of your version")
        if r["kept_private"]:
            notes.append("logins kept private")
        table.setItem(i, 2, QTableWidgetItem("; ".join(notes)))
    layout.addWidget(table)
    pick = QHBoxLayout()
    for label, on in (("Select all", True), ("Select none", False)):
        button = QPushButton(label)
        button.clicked.connect(lambda _=False, on=on: [
            table.item(i, 0).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            for i in range(table.rowCount())
        ])
        pick.addWidget(button)
    pick.addStretch()
    layout.addLayout(pick)
    prefs = QCheckBox("Include my Anki preferences (theme, language, review settings)")
    exact = QCheckBox("Exact copies of every add-on (bigger file; otherwise AnkiWeb add-ons download fresh)")
    layout.addWidget(prefs)
    layout.addWidget(exact)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout.addWidget(buttons)
    d.resize(760, 560)
    if not d.exec():
        return
    picks = {rows[i]["folder"]: _checked(table.item(i, 1)) for i in range(len(rows)) if _checked(table.item(i, 0))}
    if not picks:
        showInfo("Nothing was ticked, so nothing was saved.", parent=mw, title="Loadout")
        return
    name = "My Anki loadout" if len(picks) > 1 else rows[[r["folder"] for r in rows].index(next(iter(picks)))]["name"]
    default = os.path.join(os.path.expanduser("~/Desktop"), f"{name}{share.SUFFIX}")
    path, _ = QFileDialog.getSaveFileName(mw, "Save shared add-ons", default, f"Loadout files (*{share.SUFFIX})")
    if not path:
        return
    if not path.endswith(share.SUFFIX):
        path += share.SUFFIX
    try:
        summary = share.build(engine, path, picks, include_prefs=prefs.isChecked(), exact_all=exact.isChecked())
    except Exception as e:  # noqa: BLE001
        showWarning(f"Couldn't save the file: {e}", parent=mw, title="Loadout")
        return
    size = summary["bytes"] / 1e6
    showInfo(
        f"Saved {summary['addons']} add-on{'s' if summary['addons'] != 1 else ''} "
        f"({size:.1f} MB) to:<br><b>{html.escape(path)}</b><br><br>"
        "Send this file to your friend. They install Loadout, then open the file "
        "from Tools \u2192 Loadout\u2026 \u2192 Share.",
        parent=mw, title="Loadout", textFormat="rich",
    )


def open_shared(engine: Engine) -> None:
    """Install add-ons from a .loadout file someone shared."""
    from . import share

    path, _ = QFileDialog.getOpenFileName(mw, "Open shared add-ons", os.path.expanduser("~/Downloads"),
                                          f"Loadout files (*{share.SUFFIX})")
    if not path:
        return
    try:
        manifest = share.read(path)
    except ValueError as e:
        showWarning(f"That file can't be opened: {e}.", parent=mw, title="Loadout")
        return
    rows = share.describe(engine, manifest)
    d = QDialog(mw)
    d.setWindowTitle("Install shared add-ons")
    disable_help_button(d)
    layout = QVBoxLayout(d)
    warn = QLabel(
        "<b>Add-ons can run code on your computer.</b> Only install add-ons from people "
        "you trust. Add-ons whose code comes in the file (rather than from AnkiWeb) "
        "will ask for your approval."
    )
    warn.setWordWrap(True)
    layout.addWidget(warn)
    table = _table(["Add-on", "Includes", "On this computer"])
    table.setRowCount(len(rows))
    for i, r in enumerate(rows):
        table.setItem(i, 0, _checkable(r["name"], True))
        table.setItem(i, 1, QTableWidgetItem(r["includes"]))
        table.setItem(i, 2, QTableWidgetItem(r["here"]))
    layout.addWidget(table)
    prefs = QCheckBox("Also use their Anki preferences (theme, language, review settings)")
    prefs.setVisible(bool(manifest.get("prefs")))
    layout.addWidget(prefs)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Install")
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout.addWidget(buttons)
    d.resize(720, 480)
    if not d.exec():
        return
    folders = [rows[i]["folder"] for i in range(len(rows)) if _checked(table.item(i, 0))]
    if not folders and not prefs.isChecked():
        return
    try:
        if not share.install(engine, path, folders, include_prefs=prefs.isChecked()):
            showInfo("You already have exactly these add-ons and settings.", parent=mw, title="Loadout")
    except Exception as e:  # noqa: BLE001
        showWarning(f"Couldn't install from that file: {e}", parent=mw, title="Loadout")


_window: LoadoutWindow | None = None


def show_window(engine: Engine, actions: dict, hub: Any = None) -> None:
    """Open (or bring forward) the Loadout window."""
    global _window
    if _window is not None and _window.isVisible() and _window.engine is engine:
        _window.refresh()
        _window.raise_()
        _window.activateWindow()
        return
    _window = LoadoutWindow(engine, actions, hub)
    _window.show()


def refresh_window() -> None:
    if _window is not None and _window.isVisible():
        _window.refresh()


def show_rollback(engine: Engine) -> None:
    history = engine.state["history"]
    if not engine.state["joined"] or not history:
        showInfo("There is no saved history to roll back to yet.", parent=mw, title="Loadout")
        return
    d = QDialog(mw)
    d.setWindowTitle("Loadout: roll back")
    disable_help_button(d)
    layout = QVBoxLayout(d)
    label = QLabel(
        "Pick a point to return to. This computer is restored first, then the "
        "rollback uploads on your next sync so your other computers follow. "
        "Add-ons installed after that point are kept."
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    for i in reversed(range(len(history))):
        point = history[i]
        item = QListWidgetItem(f"{_when(point['time'])}   {point['label']}")
        item.setData(Qt.ItemDataRole.UserRole, i)
        lst.addItem(item)
    lst.setCurrentRow(0)
    layout.addWidget(lst)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout.addWidget(buttons)
    if d.exec() and lst.currentItem():
        engine.rollback(lst.currentItem().data(Qt.ItemDataRole.UserRole))
