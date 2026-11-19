"""The Add-on Hub: tidying add-on items out of Anki's menus.

When switched on, every menu item an add-on put into Anki's menus (and every
menu an add-on put on the menu bar) moves into Tools → All add-ons, one
submenu per add-on; optionally Anki's own Tools items move there too (then
it's "All tools"). Items the user stars as favorites also stay directly in
Tools. Switching off puts everything back where it was.

It also remembers what each item does: when an item is used, the window it
opens is captured as a small picture (kept on this computer only) for the
Loadout window's hover previews.

Items are moved, never copied or rebuilt, so add-ons keep updating them, and
each stays attached to the main window so its keyboard shortcut keeps
working. Nothing here touches other add-ons' code or files.

survey() also writes user_files/menu_survey.json describing the menus.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aqt import mw
from aqt.qt import QAction, QApplication, QEvent, QMenu, QObject, Qt

from . import owners, scan

OTHER = "Other add-ons"
ANKI = "anki"  # owner of Anki's own Tools items
CAPTURE_SECONDS = 3.0  # how long after a click a new window counts as "opened by it"
PREVIEW_WIDTH = 480

# On a Mac, Qt shows these in the application ("Anki") menu instead of where
# they were added; moving them could make them disappear from there.
_MAC_APP_MENU_WORDS = ("about", "config", "options", "setup", "settings", "preferences", "quit", "exit")


def _in_mac_app_menu(action: QAction) -> bool:
    if sys.platform != "darwin":
        return False
    role = action.menuRole()
    roles = QAction.MenuRole
    if role in (roles.PreferencesRole, roles.AboutRole, roles.QuitRole,
                roles.AboutQtRole, roles.ApplicationSpecificRole):
        return True
    if role == roles.TextHeuristicRole:
        return owners.clean_label(action.text()).lower().startswith(_MAC_APP_MENU_WORDS)
    return False


def _preview_key(path: list[str]) -> str:
    return " \u25b8 ".join(path)


def _anki_objects() -> set[int]:
    """Menus and items defined by Anki's own main-window layout."""
    out = set()
    for value in vars(mw.form).values():
        if isinstance(value, QMenu):
            out.add(id(value))
            out.add(id(value.menuAction()))
        elif isinstance(value, QAction):
            out.add(id(value))
    return out


def _anki_menus() -> list[QMenu]:
    return [v for v in vars(mw.form).values() if isinstance(v, QMenu)]


# Survey
##############################################################################


def _walk(menu: QMenu, anki: set[int], path: list[str]) -> list[dict[str, Any]]:
    items = []
    for action in menu.actions():
        if action.isSeparator():
            continue
        label = owners.clean_label(action.text())
        item: dict[str, Any] = {
            "label": label,
            "raw": action.text(),
            "path": path,
            "anki": id(action) in anki,
            "shortcut": action.shortcut().toString(),
            "checkable": action.isCheckable(),
            "visible": action.isVisible(),
        }
        submenu = action.menu()
        if submenu is not None:
            item["anki"] = item["anki"] or id(submenu) in anki
            item["children"] = _walk(submenu, anki, path + [label])
        items.append(item)
    return items


def read_menus() -> list[dict[str, Any]]:
    """Every top-level menu with its items. Must run on the main thread."""
    anki = _anki_objects()
    menus = []
    for action in mw.form.menubar.actions():
        menu = action.menu()
        if menu is None:
            continue
        title = owners.clean_label(action.text())
        menus.append({
            "title": title,
            "raw": action.text(),
            "anki": id(menu) in anki or id(action) in anki,
            "items": _walk(menu, anki, [title]),
        })
    return menus


# Moving items
##############################################################################


@dataclass
class Entry:
    """Something an add-on put into Anki's menus: an item (possibly a
    submenu) inside one of Anki's menus, or a whole menu on the menu bar."""

    action: QAction
    origin: Any  # the QMenu or QMenuBar it came from
    before: QAction | None  # its next sibling there, for putting it back
    label: str = ""
    anki: bool = False  # one of Anki's own Tools items
    position: int = 0  # order among all entries when found, for putting back
    owner: str | None = None
    target: QMenu | None = None
    children: list[str] = field(default_factory=list)


def _raw_labels(action: QAction) -> list[str]:
    """Raw texts of everything inside a submenu."""
    out: list[str] = []
    menu = action.menu()
    if menu is not None:
        for child in menu.actions():
            if not child.isSeparator():
                out.append(child.text())
                out += _raw_labels(child)
    return out


def _actions_within(action: QAction, path: list[str]) -> list[tuple[list[str], QAction]]:
    """(label path, action) for an entry and everything inside it."""
    out = [(path, action)]
    menu = action.menu()
    if menu is not None:
        for child in menu.actions():
            if not child.isSeparator():
                out += _actions_within(child, path + [owners.clean_label(child.text())])
    return out


class Hub(QObject):
    def __init__(
        self,
        addons_dir: str,
        own_folder: str,
        own_action: Callable[[], QAction | None],
        store_path: str,
        log: Callable[[str], None],
    ) -> None:
        super().__init__(mw)
        self.addons_dir = addons_dir
        self.own_folder = own_folder
        self.own_action = own_action
        self.store_path = store_path
        self.log = log
        self.store = self._load()
        self.found: dict[str, list[str]] = {}  # raw label -> candidate add-on folders
        self.names: dict[str, str] = {}
        self.ready = False
        self.applied = False
        self.busy = False
        self.records: list[Entry] = []
        self.extras: list[QAction] = []  # what the Hub added to Tools
        self.favorite_actions: list[QAction] = []
        self.attached: list[QAction] = []  # added to the main window for shortcuts
        self.hidden_separators: list[QAction] = []
        self._reapply_pending = False
        self._watching = False
        self.preview_dir = os.path.join(os.path.dirname(store_path), "previews")
        self.previews = self._load_previews()
        self._keys: dict[int, list[str]] = {}  # id(action) -> path, for previews
        self._watched: set[int] = set()
        self._pending: tuple[list[str], QAction, float] | None = None
        self._captured = False

    # Settings
    ##########################################################################

    def _load(self) -> dict[str, Any]:
        store: dict[str, Any] = {"enabled": False, "include_anki": False, "favorites": []}
        try:
            with open(self.store_path, encoding="utf-8") as f:
                store.update(json.load(f))
        except (OSError, ValueError):
            pass
        return store

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(self.store, f, indent=1, ensure_ascii=False)

    @property
    def enabled(self) -> bool:
        return bool(self.store.get("enabled"))

    def set_enabled(self, on: bool) -> None:
        self.store["enabled"] = on
        self._save()
        if on and self.ready:
            self.apply()
        elif not on and self.applied:
            self.restore()

    # Settings that follow the user to their other computers (via Loadout).
    SHARED_KEYS = ("enabled", "include_anki", "favorites")

    def shared_settings(self) -> dict[str, Any]:
        return {k: self.store.get(k) for k in self.SHARED_KEYS}

    def apply_shared_settings(self, value: dict[str, Any]) -> None:
        """Menu settings arriving from another computer."""
        if not isinstance(value, dict) or value == self.shared_settings():
            return
        for key in self.SHARED_KEYS:
            if key in value:
                self.store[key] = value[key]
        self._save()
        if not self.ready:
            return  # start() applies them once Anki has finished loading
        if self.enabled:
            self.apply()
        elif self.applied:
            self.restore()

    @property
    def include_anki(self) -> bool:
        return bool(self.store.get("include_anki"))

    def set_include_anki(self, on: bool) -> None:
        self.store["include_anki"] = on
        self._save()
        if self.applied:
            self.apply()

    def is_favorite(self, path: list[str], owner: str | None) -> bool:
        return any(f["path"] == path for f in self.store["favorites"])

    def set_favorite(self, path: list[str], owner: str | None, on: bool) -> None:
        favorites = [f for f in self.store["favorites"] if f["path"] != path]
        if on:
            favorites.append({"path": path, "owner": owner})
        self.store["favorites"] = favorites
        self._save()
        if self.applied:
            self.apply()

    # Finding add-on items
    ##########################################################################

    def _ours(self, action: QAction) -> bool:
        return action is self.own_action() or action in self.extras or action in self.favorite_actions

    def entries(self) -> list[Entry]:
        """Add-on items still in their original places, in menu order."""
        anki = _anki_objects()
        out = []
        bar = mw.form.menubar
        bar_actions = bar.actions()
        for i, top in enumerate(bar_actions):
            menu = top.menu()
            if menu is None or self._ours(top):
                continue
            if id(top) in anki or id(menu) in anki:
                actions = menu.actions()
                with_anki = self.include_anki and menu is mw.form.menuTools
                for j, action in enumerate(actions):
                    sub = action.menu()
                    if action.isSeparator() or self._ours(action) or _in_mac_app_menu(action):
                        continue
                    native = id(action) in anki or (sub is not None and id(sub) in anki)
                    if native and not with_anki:
                        continue
                    nxt = actions[j + 1] if j + 1 < len(actions) else None
                    out.append(Entry(action, menu, nxt, anki=native))
            else:
                nxt = bar_actions[i + 1] if i + 1 < len(bar_actions) else None
                out.append(Entry(top, bar, nxt))
        for position, entry in enumerate(out):
            entry.label = owners.clean_label(entry.action.text())
            entry.children = _raw_labels(entry.action)
            entry.position = position
        return out

    def start(self) -> None:
        """Look up which add-on owns each item (in the background), then
        tidy if switched on. Call once Anki has finished loading add-ons."""
        found_entries = self.entries()
        labels = sorted({l for e in found_entries for l in [e.action.text()] + e.children})
        folders = scan.addon_folders(self.addons_dir, self.own_folder)
        self.names = {f: owners.display_name(self.addons_dir, f) for f in folders}

        def find() -> dict[str, list[str]]:
            return owners.find_owners(labels, self.addons_dir, folders)

        def done(fut: Any) -> None:
            try:
                self.found.update(fut.result())
            except Exception as e:  # noqa: BLE001
                self.log(f"menu owners lookup failed: {e}")
            self.ready = True
            self._watch()
            if self.enabled:
                self.apply()

        mw.taskman.run_in_background(find, done)

    def _owner(self, entry: Entry) -> str | None:
        if entry.anki:
            return ANKI
        missing = [l for l in [entry.action.text()] + entry.children if l not in self.found]
        if missing:  # an item added after start(); file texts are cached by now
            folders = list(self.names)
            self.found.update(owners.find_owners(missing, self.addons_dir, folders))
        return owners.resolve_owner(entry.action.text(), entry.children, self.found, self.names)

    def inventory(self) -> list[dict[str, Any]]:
        """Every add-on menu item for the Menus tab: path, label, add-on."""
        source = self.records if self.applied else self.entries()
        out = []
        for entry in source:
            owner = entry.owner if self.applied else self._owner(entry)
            name = self._group_name(owner)
            for path, action in _actions_within(entry.action, [entry.label]):
                out.append({"path": path, "owner": owner, "owner_name": name,
                            "has_menu": action.menu() is not None, "action": action})
        self._track(out)
        return out

    def _group_name(self, owner: str | None) -> str:
        if owner == ANKI:
            return "Anki"
        return self.names.get(owner, OTHER) if owner else OTHER

    # Applying and restoring
    ##########################################################################

    def apply(self) -> None:
        """Move add-on items into Tools → All add-ons; favorites into Tools."""
        if self.applied:
            self.restore()
        self.busy = True
        try:
            tools = mw.form.menuTools
            found = self.entries()
            for entry in found:
                entry.owner = self._owner(entry)
            index: dict[tuple, QAction] = {}
            by_leaf: dict[tuple, list[QAction]] = {}
            for entry in found:
                for path, action in _actions_within(entry.action, [entry.label]):
                    index[tuple(path)] = action
                    by_leaf.setdefault((entry.owner, path[-1]), []).append(action)
            favorites = []
            for fav in self.store["favorites"]:
                action = index.get(tuple(fav["path"]))
                if action is None:
                    candidates = by_leaf.get((fav.get("owner"), fav["path"][-1]), [])
                    action = candidates[0] if len(candidates) == 1 else None
                if action is not None and action not in favorites:
                    favorites.append(action)

            groups: dict[str, list[Entry]] = {}
            for entry in found:
                groups.setdefault(self._group_name(entry.owner), []).append(entry)
            native = groups.pop("Anki", [])

            # Move everything first: a favorite may itself have been in Tools.
            separator = tools.addSeparator()
            self.extras.append(separator)
            all_menu = QMenu("All tools" if native else "All add-ons", mw)
            self.extras.append(tools.addMenu(all_menu))
            for entry in native:  # Anki's own items first, as they were
                entry.origin.removeAction(entry.action)
                all_menu.addAction(entry.action)
                entry.target = all_menu
                self.records.append(entry)
                for _, action in _actions_within(entry.action, [entry.label]):
                    if action not in mw.actions():
                        mw.addAction(action)
                        self.attached.append(action)
            if native and groups:
                all_menu.addSeparator()
            for name in sorted(groups, key=lambda n: (n == OTHER, n.lower())):
                members = groups[name]
                target = all_menu if len(members) == 1 else all_menu.addMenu(name)
                for entry in members:
                    entry.origin.removeAction(entry.action)
                    target.addAction(entry.action)
                    entry.target = target
                    self.records.append(entry)
                    for _, action in _actions_within(entry.action, [entry.label]):
                        if action not in mw.actions():
                            mw.addAction(action)  # keeps its keyboard shortcut working
                            self.attached.append(action)
            if favorites:
                self.extras.append(tools.insertSeparator(separator))
                for action in favorites:
                    tools.insertAction(separator, action)
                    self.favorite_actions.append(action)
            own = self.own_action()
            if own is not None and own in tools.actions():
                tools.removeAction(own)
                tools.addAction(own)
            self._hide_orphan_separators(tools)
            self.applied = True
            self.inventory()  # starts tracking every item for previews
        finally:
            self.busy = False

    def restore(self) -> None:
        """Put every moved item back exactly where it was."""
        self.busy = True
        try:
            tools = mw.form.menuTools
            for action in self.favorite_actions:
                tools.removeAction(action)
            for action in self.extras:
                tools.removeAction(action)
            # Last first, so each item's original next sibling is back already.
            for entry in sorted(self.records, key=lambda e: e.position, reverse=True):
                if entry.target is not None:
                    entry.target.removeAction(entry.action)
                before = entry.before if entry.before in entry.origin.actions() else None
                entry.origin.insertAction(before, entry.action)
            for action in self.attached:
                mw.removeAction(action)
            for action in self.hidden_separators:
                action.setVisible(True)
            self.hidden_separators = []
            self.records, self.extras, self.favorite_actions, self.attached = [], [], [], []
            self.applied = False
        finally:
            self.busy = False

    def _hide_orphan_separators(self, menu: QMenu) -> None:
        """Hide divider lines left at the top, at the bottom or doubled up
        once items moved away."""
        visible = [a for a in menu.actions() if a.isVisible() and not _in_mac_app_menu(a)]
        previous_was_separator = True  # nothing above the first item
        for i, action in enumerate(visible):
            if not action.isSeparator():
                previous_was_separator = False
                continue
            rest = [a for a in visible[i + 1:] if not a.isSeparator()]
            if previous_was_separator or not rest:
                action.setVisible(False)
                self.hidden_separators.append(action)
            previous_was_separator = True

    # Items added later
    ##########################################################################

    def _watch(self) -> None:
        if self._watching:
            return
        self._watching = True
        mw.form.menubar.installEventFilter(self)
        for menu in _anki_menus():
            menu.installEventFilter(self)

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if kind == QEvent.Type.ActionAdded:
            if (
                obj in (mw.form.menubar, *_anki_menus())
                and self.applied and not self.busy and not self._reapply_pending
            ):
                # An add-on added an item after tidying: tidy again once it is done.
                self._reapply_pending = True
                mw.progress.single_shot(300, self._reapply)
        elif kind == QEvent.Type.MouseButtonRelease and isinstance(obj, QMenu):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            self._note_trigger(obj.actionAt(pos))
        elif kind == QEvent.Type.KeyPress and isinstance(obj, QMenu):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._note_trigger(obj.activeAction())
        elif kind == QEvent.Type.Shortcut and isinstance(obj, QAction):
            self._note_trigger(obj)
        elif kind == QEvent.Type.Show and self._pending is not None:
            self._maybe_capture(obj)
        return False

    def _reapply(self) -> None:
        self._reapply_pending = False
        if self.applied and self.entries():
            try:
                self.apply()
            except Exception as e:  # noqa: BLE001 - never leave menus half-moved
                self.log(f"menu tidy failed: {e}")
                self.restore()


    # Previews: what an item opens
    ##########################################################################

    def _load_previews(self) -> dict[str, Any]:
        try:
            with open(os.path.join(self.preview_dir, "previews.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_previews(self) -> None:
        os.makedirs(self.preview_dir, exist_ok=True)
        with open(os.path.join(self.preview_dir, "previews.json"), "w", encoding="utf-8") as f:
            json.dump(self.previews, f, indent=1, ensure_ascii=False)

    def _track(self, items: list[dict[str, Any]]) -> None:
        """Watch clicks and shortcuts on every listed item, to learn what
        each one opens."""
        menus = [mw.form.menuTools] + [e.target for e in self.records if e.target is not None]
        for item in items:
            action = item["action"]
            self._keys[id(action)] = item["path"]
            if id(action) not in self._watched:
                self._watched.add(id(action))
                action.installEventFilter(self)  # shortcut presses
            if action.menu() is not None:
                menus.append(action.menu())
        for menu in menus:
            if id(menu) not in self._watched:
                self._watched.add(id(menu))
                menu.installEventFilter(self)  # clicks inside it

    def run(self, path: list[str]) -> bool:
        """Open an item from the Loadout window (double-click)."""
        for item in self.inventory():
            if item["path"] == path and item["action"].menu() is None:
                self._note_trigger(item["action"])
                item["action"].trigger()
                return True
        return False

    def _note_trigger(self, action: Any) -> None:
        if action is None or action.menu() is not None or id(action) not in self._keys:
            return
        self._pending = (self._keys[id(action)], action, time.time())
        self._captured = False
        QApplication.instance().installEventFilter(self)  # only for the next few seconds
        mw.progress.single_shot(int(CAPTURE_SECONDS * 1000), self._finish_capture)

    def _maybe_capture(self, obj: Any) -> None:
        pending = self._pending
        if (
            pending is None or self._captured or time.time() - pending[2] > CAPTURE_SECONDS
            or not getattr(obj, "isWindow", lambda: False)() or isinstance(obj, QMenu)
            or obj is mw or (obj.windowFlags() & Qt.WindowType.ToolTip) == Qt.WindowType.ToolTip
        ):
            return
        self._captured = True
        path = pending[0]
        mw.progress.single_shot(600, lambda: self._capture(obj, path))

    def _capture(self, window: Any, path: list[str]) -> None:
        try:
            if not window.isVisible():
                return
            picture = window.grab()
            if picture.width() > PREVIEW_WIDTH:
                picture = picture.scaledToWidth(PREVIEW_WIDTH, Qt.TransformationMode.SmoothTransformation)
            os.makedirs(self.preview_dir, exist_ok=True)
            name = hashlib.sha1(_preview_key(path).encode("utf-8")).hexdigest()[:16] + ".png"
            picture.save(os.path.join(self.preview_dir, name), "PNG")
            self.previews[_preview_key(path)] = {
                "kind": "window", "title": window.windowTitle(), "image": name, "time": time.time(),
            }
            self._save_previews()
        except Exception as e:  # noqa: BLE001 - previews are a nicety
            self.log(f"preview capture failed: {e}")

    def _finish_capture(self) -> None:
        pending, self._pending = self._pending, None
        QApplication.instance().removeEventFilter(self)
        if pending is None or self._captured:
            return
        path, action, _ = pending
        known = self.previews.get(_preview_key(path), {})
        if known.get("kind") == "window":
            return  # it opened a window before; keep that picture
        self.previews[_preview_key(path)] = {
            "kind": "toggle" if action.isCheckable() else "none", "time": time.time(),
        }
        self._save_previews()

    # Anki's own Tools items that run something rather than open a window.
    _ANKI_ACTIONS = ("check database", "check media", "empty cards")

    def kind(self, path: list[str], action: Any, owner: str | None) -> tuple[str, str]:
        """What using an item does: (kind, detail). kind is "menu", "window",
        "toggle", "link", "setting", "action" or "unknown"; learned from
        using it where possible, otherwise read from the add-on's code."""
        if action.menu() is not None:
            return "menu", ""
        known = self.previews.get(_preview_key(path), {})
        if known.get("kind") == "window":
            return "window", known.get("title") or ""
        if action.isCheckable():
            return "toggle", "on" if action.isChecked() else "off"
        if owner == ANKI:
            label = owners.clean_label(action.text()).lower()
            return ("action", "runs and shows the result") if label.startswith(self._ANKI_ACTIONS) else ("window", "")
        code = None
        if owner:
            try:
                code = owners.explain_action(self.addons_dir, owner, action.text())
            except Exception as e:  # noqa: BLE001 - explanations are a nicety
                self.log(f"explain {path}: {e}")
        if known.get("kind") == "none":
            return ("link", code["detail"]) if code and code["kind"] == "link" else ("action", (code or {}).get("detail", ""))
        if code:
            return code["kind"], code["detail"]
        return "unknown", ""

    def summary(self, path: list[str], action: Any, owner: str | None = None) -> tuple[str, str | None]:
        """For the Menus tab: a few words on what the item does, and the
        picture of the window it opens (if Loadout has seen it)."""
        kind, detail = self.kind(path, action, owner)
        words = {
            "menu": "Submenu",
            "window": "Opens a window",
            "link": "Opens a website",
            "setting": "Changes a setting",
            "action": "Runs right away",
            "unknown": "",
        }
        text = f"On/off switch ({detail})" if kind == "toggle" and detail in ("on", "off") else \
            "On/off switch" if kind == "toggle" else words[kind]
        known = self.previews.get(_preview_key(path), {})
        image = os.path.join(self.preview_dir, known.get("image", "")) if known.get("kind") == "window" else ""
        return text, image if image and os.path.isfile(image) else None


# Survey file
##############################################################################


def _addon_labels(menus: list[dict[str, Any]]) -> list[str]:
    """Raw texts (with "&" markers) of add-on menus and items."""
    labels: list[str] = []

    def visit(items: list[dict[str, Any]]) -> None:
        for item in items:
            if not item["anki"] and item["raw"] not in labels:
                labels.append(item["raw"])
            visit(item.get("children", []))

    for menu in menus:
        if not menu["anki"]:
            labels.append(menu["raw"])
        visit(menu["items"])
    return labels


def survey(addons_dir: str, own_folder: str, out_path: str,
           log: Callable[[str], None]) -> None:
    """Write menu_survey.json: the menus now, plus a guess at the add-on
    behind each item. Reading Qt happens here; the file search runs in the
    background so Anki stays responsive."""
    menus = read_menus()
    labels = _addon_labels(menus)
    folders = scan.addon_folders(addons_dir, own_folder)
    names = {f: owners.display_name(addons_dir, f) for f in folders}

    def find() -> dict[str, list[str]]:
        found = owners.find_owners(labels, addons_dir, folders)
        # Explain each item now, off the main thread, so the Menus tab opens at once.
        for raw, fs in found.items():
            for folder in fs:
                try:
                    owners.explain_action(addons_dir, folder, raw)
                except Exception:  # noqa: BLE001 - explanations are a nicety
                    pass
        return found

    def done(fut: Any) -> None:
        try:
            found = fut.result()
        except Exception as e:  # noqa: BLE001
            log(f"menu survey failed: {e}")
            return
        result = {
            "time": time.time(),
            "menus": menus,
            "owners": {
                owners.clean_label(raw): [{"folder": f, "name": names[f]} for f in fs]
                for raw, fs in found.items()
            },
        }
        try:
            # A fresh install from AnkiWeb has no user_files folder yet.
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=1, ensure_ascii=False)
        except OSError as e:
            log(f"menu survey not saved: {e}")
            return
        log(f"menu survey: {len(labels)} add-on menu items")

    mw.taskman.run_in_background(find, done)
