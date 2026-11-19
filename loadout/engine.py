"""Loadout's sync logic, independent of Anki's GUI.

Loadout keeps one synced entry per mirrored item, each in its own media file
(see blobs.py):

  a/<folder>  an add-on: installed?, enabled?, its Config-button settings
  f/<folder>  its data files (user_files)
  k/<folder>  its settings in the collection (AnkiWeb syncs those as one
              block from whichever computer changed anything last)
  d/<folder>  files it keeps in the profile folder
  c/<folder>  an exact copy of an AnkiWeb add-on's code
  p/<name>    Anki's preferences;  m/...  profile name and Loadout versions
 Media syncs file by file, so edits to
different add-ons on different computers all survive. For the same item,
an edit made without having seen this computer's last one is merged
(see _merge): an add-on's code and its settings are decided separately,
so a settings change on one computer never brings back old code there.

Each device remembers the entries it last saw (its "base"):

  before a sync  push items whose local state differs from the base
  after a sync   apply items whose synced state differs from the base

A device that has never synced with Loadout ("not joined") never pushes until
it has either pulled the saved setup or the user confirmed it is the first
device. That is what stops a fresh install from overwriting the saved setup.

Data files of an add-on that is running are not written while it runs: it
could save its old state over them. They are held and written when Anki
closes (apply_held_files), so they are in place on the next start.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import os
import shutil
import uuid
import zlib
from typing import Any, Callable, Protocol

from . import blobs, owners, rules, scan
from .version import VERSION, parse as parse_version

MODES = ("mirror", "preview", "paused")
HISTORY_LIMIT = 10
BLOB_GRACE_SECONDS = 3 * 86400
SELF_SYNC_SEED_SECONDS = 7 * 86400
ANKIWEB_CHECK_SECONDS = 86400
# Changes worth a notice; settings, data and preference updates stay quiet.
NOTABLE_PREFIXES = ("Installed ", "New version of ", "Removed ", "Enabled ", "Disabled ")
BACKUP_CHURN_LIMIT = 3  # same-version code changes per day before backing off
MAX_ENTRY_BYTES = 256 * 1024  # bigger settings stay local
# A setting or data file that changes by itself (caches, timestamps, counters)
# on this many separate days within a week, this often in all, is kept on
# each computer instead of being mirrored back and forth on every sync.
VOLATILE_WINDOW_SECONDS = 7 * 86400
VOLATILE_MIN_DAYS = 3
VOLATILE_MIN_CHANGES = 6
FORCE_PUSH = "force-push"
# A compressed copy of the synced entries, kept in the collection's settings.
# Those sync before media, which downloads oldest first: on a new computer
# with a lot of media, Loadout's files come last. With this copy the setup
# (AnkiWeb add-ons, settings, preferences) applies right after the
# collection sync; copied code and data files still come with media.
# AnkiWeb syncs the settings as one block, which suits a copy that is only
# read for entries media hasn't brought yet.
EARLY_SETUP_KEY = "loadout_early_setup"
EARLY_SETUP_MAX_BYTES = 64 * 1024
# The parts of an a/ entry that are its code; the rest are its settings.
CODE_FIELDS = ("code", "mod")
CODE_EXTRA = ("blob", "info", "name")  # travel with the code, not hashed


class Device(Protocol):
    """Everything the engine needs from Anki (see anki_device.py)."""

    def addons_dir(self) -> str: ...
    def own_folder(self) -> str: ...
    def profile_name(self) -> str: ...
    def now(self) -> float: ...
    def read_remote(self) -> dict[str, dict[str, Any]]: ...
    def write_remote(self, key: str, entry: dict[str, Any]) -> None: ...
    def media_has(self, name: str) -> bool: ...
    def media_read(self, name: str) -> bytes: ...
    def media_write(self, name: str, data: bytes) -> None: ...
    def media_list(self, prefix: str) -> list[tuple[str, float]]: ...
    def media_trash(self, names: list[str]) -> None: ...
    def get_prefs(self) -> dict[str, dict[str, Any]]: ...
    def set_prefs(self, key: str, value: dict[str, Any]) -> None: ...
    def install_ankiweb(self, ids: list[int], on_done: Callable[[dict[int, str | None]], None]) -> None: ...
    def install_code(self, folder: str, name: str, data: bytes, mod: int | None = None) -> str | None: ...
    def computer_name(self) -> str: ...
    def anki_version(self) -> str: ...
    def collection_settings(self) -> dict[str, Any]: ...
    def set_collection_setting(self, key: str, value: Any) -> None: ...
    def is_anki_setting(self, key: str) -> bool: ...
    def profile_dir(self) -> str: ...
    def is_loaded(self, folder: str) -> bool: ...
    def ankiweb_info(self, ids: list[int]) -> dict[int, int] | None: ...
    def set_enabled(self, folder: str, enabled: bool) -> None: ...
    def set_update_enabled(self, folder: str, enabled: bool) -> None: ...
    def write_config(self, folder: str, config: dict[str, Any] | None) -> None: ...
    def delete_addon(self, folder: str) -> None: ...
    def confirm(self, text: str) -> bool: ...
    def review(self, items: list[dict[str, Any]]) -> dict[str, str]: ...
    def request_sync(self) -> None: ...
    def choose_code_installs(self, items: list[tuple[str, str]]) -> set[str]: ...
    def notify(self, title: str, lines: list[str], restart: bool) -> None: ...
    def log(self, text: str) -> None: ...


def new_state() -> dict[str, Any]:
    return {
        "device_id": uuid.uuid4().hex[:12],
        "joined": False,
        "base": {},
        "approved_code": {},
        "declined_code": {},
        "failed": {},
        "ankiweb": {},  # id -> {"available", "checked"}
        "too_big": {},  # folder -> MB, code too large to copy
        "backup_pushes": {},  # c/<folder> -> recent upload times
        "entry_too_big": {},  # key -> KB, settings too large to mirror
        # folder -> {"keys": [...], "files": [...]}: kept on each computer, found
        # here (changing by themselves) or adopted from another computer.
        "kept_local": {},
        "change_log": {},  # "a/<folder>|<setting>" or "f/<folder>|<file>" -> times
        # Add-on review (see differences()): which add-ons exist where is the
        # user's call; settings and data of shared add-ons mirror by themselves.
        "excluded_here": [],  # kept off / kept only on this computer
        "unshared": [],  # here when this computer joined; not shared until decided
        "install_approved": [],
        "last_changes": {},  # {"time", "lines"}: what the last update from elsewhere did
        "remove_approved": [],
        "held_files": {},  # f/<folder> -> {"entry", "rollback"}, written when Anki closes
        "history": [],
        "preview": [],
    }


def load_state(path: str) -> dict[str, Any]:
    state = new_state()
    try:
        with open(path, encoding="utf-8") as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    state["kept_local"] = {f: v for f, v in state["kept_local"].items() if v.get("keys") or v.get("files")}
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)  # compact: saved on every sync
    os.replace(tmp, path)


def tombstone() -> dict[str, Any]:
    return scan.finish({"del": True})


# Two copies of Loadout
##############################################################################
# Installed from a file Loadout's folder is "loadout", from AnkiWeb a number,
# so one computer can end up with both. Only one may run: two would each
# mirror the add-ons as a separate computer.


def _state_files(user_files: str) -> list[str]:
    try:
        names = os.listdir(user_files)
    except OSError:
        return []
    return sorted(n for n in names if n.startswith("state_") and n.endswith(".json"))


def _copy_rank(addons_dir: str, folder: str) -> tuple:
    """The newest version runs, then one that is set up, then the AnkiWeb
    one. Every copy ranks them the same way, so exactly one runs."""
    version = scan.loadout_version(addons_dir, folder) or "0"
    files = os.path.join(addons_dir, folder, "user_files")
    set_up = any(load_state(os.path.join(files, n))["joined"] for n in _state_files(files))
    return (parse_version(version), set_up, bool(scan.ANKIWEB_ID.match(folder)), folder)


def running_copy(addons_dir: str, own_folder: str) -> str:
    """The folder of the copy of Loadout that mirrors on this computer."""
    copies = [
        f for f in scan.loadout_copies(addons_dir)
        if f == own_folder or not scan.read_json(os.path.join(addons_dir, f, "meta.json")).get("disabled")
    ]
    return max(copies or [own_folder], key=lambda f: _copy_rank(addons_dir, f))


def take_over_setup(addons_dir: str, own_folder: str, user_files: str) -> str | None:
    """A copy new to this computer starts from another copy's state, so
    switching between the file and the AnkiWeb version keeps this
    computer's setup and identity (and doesn't ask to set it up again).
    Returns the folder it came from, if any."""
    if _state_files(user_files):
        return None
    others = [f for f in scan.loadout_copies(addons_dir) if f != own_folder]
    for folder in sorted(others, key=lambda f: _copy_rank(addons_dir, f), reverse=True):
        src = os.path.join(addons_dir, folder, "user_files")
        names = _state_files(src)
        if not names:
            continue
        os.makedirs(user_files, exist_ok=True)
        for name in names + ["computer.json", "hub.json"]:
            if os.path.isfile(os.path.join(src, name)):
                shutil.copy2(os.path.join(src, name), os.path.join(user_files, name))
        return folder
    return None


class Engine:
    def __init__(
        self,
        device: Device,
        settings: scan.Settings,
        state_path: str,
        mode: str = "mirror",
        confirm_new_code: bool = True,
        mirror_preferences: bool = True,
    ) -> None:
        self.d = device
        self.settings = settings
        self.state_path = state_path
        self.state = load_state(state_path)
        self.mode = mode if mode in MODES else "mirror"
        # Set when another profile on this computer uses Loadout: add-ons are
        # shared by all profiles, so only one may mirror them.
        self.inactive_reason: str | None = None
        # Set (to its folder) when another copy of Loadout runs instead.
        self.other_copy: str | None = None
        self.confirm_new_code = confirm_new_code
        self.mirror_preferences = mirror_preferences
        self.reports: list[scan.AddonReport] = []
        self.busy = False
        self.closing = False
        self.asked_match = False
        self.asked_first = False
        self.warned_profile = False
        self.warned_version = False
        self.reviewed: set[tuple[str, str]] = set()  # differences shown this session
        self._name_owners: dict[str, str | None] = {}  # setting/file name -> add-on folder
        self._remote: dict[str, dict[str, Any]] = {}
        # Anki runs the AnkiWeb check in the background instead (main.py).
        self.check_ankiweb_during_push = True

    # Helpers
    ##########################################################################

    def save(self) -> None:
        save_state(self.state_path, self.state)

    def off_limits(self, key: str) -> bool:
        """Entries Loadout never acts on, whatever their source: a folder name
        that isn't a plain one (could lead outside the add-ons folder), or a
        copy of Loadout itself (see main.py for two copies on one computer)."""
        if not key.startswith(("a/", "f/", "c/", "k/", "d/")):
            return False
        folder = key[2:]
        return (
            not scan.safe_folder(folder) or folder == self.d.own_folder()
            or scan.loadout_version(self.d.addons_dir(), folder) is not None
        )

    def excluded(self, key: str) -> bool:
        if self.off_limits(key):
            return True
        if key.startswith(("a/", "f/", "c/", "k/", "d/")):
            folder = key[2:]
            return folder in self.settings.exclude_addons or folder in self.state["excluded_here"]
        if key.startswith("p/"):
            return not self.mirror_preferences
        return False

    def kept_local(self, folder: str) -> tuple[set[str], set[str]]:
        """Settings and data files of this add-on that every computer keeps
        for itself: chosen in Loadout's config, found to change by
        themselves, or adopted from another computer."""
        stored = self.state["kept_local"].get(folder, {})
        keys = set(self.settings.exclude_config_keys.get(folder, [])) | set(stored.get("keys", []))
        return keys, set(stored.get("files", []))

    def _adopt_kept_local(self, folder: str, keys: Any = (), files: Any = ()) -> None:
        if not keys and not files:
            return
        stored = self.state["kept_local"].setdefault(folder, {"keys": [], "files": []})
        for field, new in (("keys", keys), ("files", files)):
            merged = set(stored.get(field, [])) | set(new or [])
            stored[field] = sorted(merged)

    def _scan_settings(self) -> scan.Settings:
        excluded = sorted(set(self.settings.exclude_addons) | set(self.state["excluded_here"]))
        keys = {f: list(v) for f, v in self.settings.exclude_config_keys.items()}
        files = list(self.settings.exclude_files)
        for folder, stored in self.state["kept_local"].items():
            keys[folder] = sorted(set(keys.get(folder, [])) | set(stored.get("keys", [])))
            files += [f"{folder}/{rel}" for rel in stored.get("files", [])]
        return dataclasses.replace(
            self.settings, exclude_addons=excluded, exclude_config_keys=keys, exclude_files=files
        )

    def local_entries(self) -> dict[str, dict[str, Any]]:
        entries, self.reports = scan.snapshot(
            self.d.addons_dir(), self.d.own_folder(), self._scan_settings()
        )
        for report in self.reports:
            # Tell other computers which settings and files stay local here,
            # so they keep their own values instead of overwriting or deleting.
            keys, files = self.kept_local(report.folder)
            files |= {rel for rel, why in report.skipped_files if why == rules.KEPT_LOCAL}
            a, f = entries.get(f"a/{report.folder}"), entries.get(f"f/{report.folder}")
            if a is not None and keys:
                a["local_keys"] = sorted(keys)
                scan.finish(a)
            if f is not None and files:
                f["local_files"] = sorted(files)
                scan.finish(f)
        self._add_collection_and_profile(entries)
        if self.mirror_preferences:
            for key, value in self.d.get_prefs().items():
                entries[key] = scan.finish({"value": value})
        entries["m/profile"] = scan.finish({"name": self.d.profile_name()})
        entries[f"m/version/{self.state['device_id']}"] = scan.finish(
            {"version": VERSION, "computer": self.d.computer_name(), "anki": self.d.anki_version()}
        )
        for folder in self._backup_folders(entries):
            entries[f"c/{folder}"] = scan.finish({
                "code": scan.code_hash(self.d.addons_dir(), folder),
                "mod": entries[f"a/{folder}"].get("mod", 0),
            })
        for report in self.reports:
            report.source = self._source(report, entries)
        return entries

    def _owner_of_name(self, name: str, folders: list[str]) -> str | None:
        if name not in self._name_owners:
            texts = {f: owners.addon_text(self.d.addons_dir(), f) for f in folders}
            names = {f: owners.display_name(self.d.addons_dir(), f) for f in folders}
            self._name_owners[name] = owners.name_owner(name, texts, names)
        owner = self._name_owners[name]
        return owner if owner in folders else None

    def _add_collection_and_profile(self, entries: dict[str, dict[str, Any]]) -> None:
        """k/ and d/ entries: settings add-ons keep in the collection, and
        files they keep in the profile folder."""
        folders = sorted(k[2:] for k in entries if k.startswith("a/"))
        settings: dict[str, dict[str, Any]] = {}
        for key, value in self.d.collection_settings().items():
            if key == EARLY_SETUP_KEY or key.startswith(("loadout:", "_deck_", "_nt_")) or self.d.is_anki_setting(key):
                continue
            owner = self._owner_of_name(key, folders)
            if owner:
                settings.setdefault(owner, {})[key] = value
        for folder, values in settings.items():
            kept = self.kept_local(folder)[0]
            entry: dict[str, Any] = {
                "config": rules.strip_secrets({k: v for k, v in values.items() if k not in kept})
            }
            if kept & set(values):
                entry["local_keys"] = sorted(kept & set(values))
            entries[f"k/{folder}"] = scan.finish(entry)
        profile = self.d.profile_dir()
        items: dict[str, list[str]] = {}
        for name in scan.profile_items(profile):
            owner = self._owner_of_name(name, folders)
            if owner:
                items.setdefault(owner, []).append(name)
        for folder, names in items.items():
            index, skipped = scan.profile_index(profile, names, folder, self._scan_settings())
            kept = self.kept_local(folder)[1]
            entry = {"files": {rel: sha for rel, sha in index.items() if rel not in kept}}
            local = kept | {rel for rel, why in skipped if why == rules.KEPT_LOCAL}
            if local:
                entry["local_files"] = sorted(local)
            entries[f"d/{folder}"] = scan.finish(entry)

    def _backup_folders(self, local: dict[str, dict[str, Any]]) -> set[str]:
        """AnkiWeb add-ons whose exact code this device keeps a copy of:
        all of them ("always"), or ("when_needed") those a fresh download
        would not reproduce: edited here, updates turned off (pinned to this
        version), or no longer on AnkiWeb."""
        policy = self.settings.backup_ankiweb_addons
        if policy == "never":
            return set()
        out = set()
        for key, entry in local.items():
            if not key.startswith("a/") or entry.get("ankiweb_id") is None:
                continue
            known = self.state["ankiweb"].get(str(entry["ankiweb_id"]), {})
            if (
                policy == "always"
                or entry.get("info", {}).get("edited")
                or not entry.get("update_enabled", True)
                or known.get("available") is False
            ):
                out.add(key[2:])
        return out

    def _source(self, report: scan.AddonReport, local: dict[str, dict[str, Any]]) -> str:
        """How a new device will get this add-on, for the status screen."""
        if report.excluded:
            return ""
        if report.folder in self.state["too_big"]:
            return f"\u26a0\ufe0f Too big to copy ({self.state['too_big'][report.folder]} MB)"
        if f"a/{report.folder}" in self.state["entry_too_big"]:
            kb = self.state["entry_too_big"][f"a/{report.folder}"]
            return f"\u26a0\ufe0f Settings too large to mirror ({kb} KB)"
        if report.ankiweb_id is None:
            return "Copied from this computer"
        available = self.state["ankiweb"].get(str(report.ankiweb_id), {}).get("available")
        if f"c/{report.folder}" in local:
            if available is False:
                return "Exact copy (AnkiWeb no longer has it)"
            if report.edited_files:
                return f"Exact copy (edited here: {len(report.edited_files)} files)"
            if not local[f"a/{report.folder}"].get("update_enabled", True):
                return "Exact copy (updates off: this version)"
            return "Exact copy"
        if available is False:
            return "\u26a0\ufe0f Not on AnkiWeb (backup on next sync)"
        if available is None:
            return "AnkiWeb download (not checked yet)"
        return "AnkiWeb download"

    def ankiweb_ids_due(self) -> list[int]:
        """Installed AnkiWeb add-ons, if it is time (daily) to ask AnkiWeb
        which of them it still serves; otherwise []."""
        if self.settings.backup_ankiweb_addons in ("always", "never"):
            return []
        cache = self.state["ankiweb"]
        now = self.d.now()
        ids = [
            int(f) for f in scan.addon_folders(self.d.addons_dir(), self.d.own_folder())
            if scan.ANKIWEB_ID.match(f) and f not in self.settings.exclude_addons
        ]
        if all(
            now - cache.get(str(i), {}).get("checked", 0) < ANKIWEB_CHECK_SECONDS
            and "latest" in cache.get(str(i), {})
            for i in ids
        ):
            return []
        return ids

    def record_ankiweb(self, ids: list[int], info: dict[int, int] | None) -> None:
        if info is None:
            return  # offline: keep what we knew
        now = self.d.now()
        for i in ids:
            self.state["ankiweb"][str(i)] = {"available": i in info, "checked": now, "latest": info.get(i, 0)}
        self.save()

    def behind_on_ankiweb(self, entries: dict[str, dict] | None = None) -> dict[str, int]:
        """Add-ons kept on an older version on purpose (auto-updates off or
        edited here) for which AnkiWeb has something newer: folder -> date.
        entries: a scan just made (saves scanning again)."""
        if entries is None:
            entries, _ = scan.snapshot(self.d.addons_dir(), self.d.own_folder(), self._scan_settings())
        out = {}
        for key, a in entries.items():
            if not key.startswith("a/") or a.get("ankiweb_id") is None:
                continue
            if a.get("update_enabled", True) and not a.get("info", {}).get("edited"):
                continue  # Anki keeps these up to date itself
            latest = self.state["ankiweb"].get(str(a["ankiweb_id"]), {}).get("latest", 0)
            if latest and latest > a.get("mod", 0):
                out[key[2:]] = latest
        return out

    def _check_ankiweb(self) -> None:
        ids = self.ankiweb_ids_due()
        if ids:
            self.record_ankiweb(ids, self.d.ankiweb_info(ids))

    def addon_path(self, folder: str) -> str:
        return os.path.join(self.d.addons_dir(), folder)

    def installed(self, folder: str) -> bool:
        return os.path.isfile(os.path.join(self.addon_path(folder), "__init__.py"))

    def _name(self, folder: str, *sources: dict[str, Any]) -> str:
        for src in sources:
            entry = src.get(f"a/{folder}") or {}
            if entry.get("name"):
                return entry["name"]
        return folder

    def _remember(self, label: str) -> None:
        history = self.state["history"]
        history.append({
            "time": self.d.now(),
            "label": label,
            "base": copy.deepcopy(self.state["base"]),
        })
        del history[:-HISTORY_LIMIT]

    # Push (before a sync)
    ##########################################################################

    def push(self) -> list[str]:
        """Save this device's changes into the collection so the sync
        uploads them. Returns the keys written."""
        if self.mode == "paused" or self.inactive_reason or not self.state["joined"] or self.busy:
            return []
        if self.check_ankiweb_during_push:
            self._check_ankiweb()
        local = self.local_entries()
        remote = self.synced()
        base = self.state["base"]
        now = self.d.now()
        pushed = []
        for key in sorted(set(local) | set(base)):
            if self.excluded(key) or (key[:2] in ("a/", "f/", "c/", "k/", "d/") and key[2:] in self.state["unshared"]):
                continue
            entry = local.get(key)
            if entry is None:
                if base[key].get("del"):
                    continue
                if key.startswith("c/"):
                    # Withdraw a backup when its add-on is gone, or when this
                    # device made it and no longer needs to.
                    mine = base[key].get("by") == self.state["device_id"]
                    if self.installed(key[2:]) and not mine:
                        continue
                elif not key.startswith(("a/", "f/")):
                    continue
                entry = tombstone()
            known = base.get(key) or {}
            synced = remote.get(key)
            if known.get("h") == entry["h"]:
                blob = known.get("blob")
                if blob and (synced or {}).get("blob") == blob and not self.d.media_has(blob):
                    # The synced entry's file went missing (another computer
                    # cleaned it up in a race); this computer still has what
                    # it holds, so send it again.
                    try:
                        self._attach_blob(key, dict(entry))
                        self.d.log(f"re-sent missing {blob}")
                    except Exception as e:  # noqa: BLE001
                        self.d.log(f"push {key}: {e}")
                continue
            if synced and synced.get("h") == entry["h"]:
                base[key] = synced
                continue
            if key == "m/profile" and synced:
                continue  # the first device's profile name stays the reference
            if (
                key.startswith("a/") and synced and not synced.get("del")
                and not entry.get("del") and entry.get("mod", 0) < synced.get("mod", 0)
            ):
                continue  # never replace a newer version of an add-on with an older one
            if synced and known.get("h") not in (synced.get("h"), FORCE_PUSH):
                # Another device's change to this item has not been applied
                # here yet (data still downloading, install failed or not
                # approved). Uploading now would overwrite it.
                continue
            if synced and known.get("over") and synced.get("h") != known["over"]:
                continue  # a newer change arrived since ours won; pull merges it first
            if (
                key.startswith("f/")
                and (local.get("a/" + key[2:]) or {}).get("info", {}).get("self_sync")
                and known.get("h") not in (None, FORCE_PUSH)
                and now - known.get("t", 0) < SELF_SYNC_SEED_SECONDS
            ):
                # The add-on syncs this data itself; Loadout only keeps a
                # seed copy for new devices, refreshed weekly.
                continue
            recent: list[float] = []
            if key.startswith("c/") and not entry.get("del"):
                recent = [t for t in self.state["backup_pushes"].get(key, []) if now - t < 86400]
                if len(recent) >= BACKUP_CHURN_LIMIT and known.get("mod") == entry.get("mod"):
                    # This add-on keeps rewriting its own code folder; don't
                    # upload a new multi-MB copy on every sync.
                    continue
            self._note_changes(key, known, entry, now)
            entry = dict(entry)
            try:
                self._attach_blob(key, entry)
            except Exception as e:  # noqa: BLE001 - one bad add-on must not stop the rest
                self.d.log(f"push {key}: {e}")
                continue
            entry["t"] = now
            entry["by"] = self.state["device_id"]
            # What this edit replaced: None marks a first, blind upload of
            # something this computer had never seen from the others.
            entry["prev"] = known.get("over") or known.get("h")
            entry["fork"] = self._fork(key, known)
            size = len(json.dumps(entry, default=str))
            if size > MAX_ENTRY_BYTES:
                self.state["entry_too_big"][key] = size // 1024
                self.d.log(f"push {key}: {size // 1024} KB is too large to mirror")
                continue
            self.state["entry_too_big"].pop(key, None)
            self.d.write_remote(key, entry)
            if key.startswith("c/") and not entry.get("del"):
                self.state["backup_pushes"][key] = recent + [now]
            base[key] = entry
            pushed.append(key)
        if pushed:
            self._remember("Saved from this device")
            self.d.log(f"pushed {len(pushed)}: {', '.join(pushed)}")
        self._write_early_setup()
        self._collect_garbage()
        self.save()
        return pushed

    def synced(self) -> dict[str, dict[str, Any]]:
        """The synced entries: from media, plus those of the early copy
        (EARLY_SETUP_KEY) that media hasn't brought yet."""
        remote = self.d.read_remote()
        for key, entry in self._read_early_setup().items():
            remote.setdefault(key, entry)
        return remote

    def _read_early_setup(self) -> dict[str, dict[str, Any]]:
        raw = self.d.collection_settings().get(EARLY_SETUP_KEY)
        try:
            entries = json.loads(zlib.decompress(base64.b64decode(raw["z"])).decode("utf-8"))
        except Exception:  # noqa: BLE001 - missing, or written by something else
            return {}
        if not isinstance(entries, dict):
            return {}
        return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)}

    def _write_early_setup(self) -> None:
        # Version stamps belong to each computer; a new computer doesn't
        # need them. (Backup entries stay: they say which add-ons arrive as
        # exact copies rather than as downloads.)
        entries = {k: v for k, v in self.d.read_remote().items() if not k.startswith("m/version/")}
        raw = json.dumps(entries, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:24]
        if not entries or digest == self.state.get("early_setup"):
            return
        self.state["early_setup"] = digest
        packed = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
        value: dict[str, Any] | None = {"v": 1, "z": packed}
        if len(packed) > EARLY_SETUP_MAX_BYTES:
            # Too big to send with every sync: clear it rather than leave an
            # old one behind; new computers then wait for media.
            self.d.log(f"setup is {len(packed) // 1024} KB packed; new computers wait for media sync")
            value = None
        if self.d.collection_settings().get(EARLY_SETUP_KEY) != value:
            self.d.set_collection_setting(EARLY_SETUP_KEY, value)

    def _note_changes(self, key: str, known: dict, entry: dict, now: float) -> None:
        """Track which settings/files change on this computer, to spot ones
        that change by themselves (see VOLATILE_*)."""
        if known.get("h") in (None, FORCE_PUSH) or known.get("del") or entry.get("del"):
            return
        if key.startswith(("a/", "k/")):
            old, new, field = known.get("config") or {}, entry.get("config") or {}, "keys"
        elif key.startswith(("f/", "d/")):
            old, new, field = known.get("files") or {}, entry.get("files") or {}, "files"
        else:
            return
        folder = key[2:]
        log = self.state["change_log"]
        for item in set(old) | set(new):
            if old.get(item) == new.get(item):
                continue
            times = [t for t in log.get(f"{key}|{item}", []) if now - t < VOLATILE_WINDOW_SECONDS]
            times.append(now)
            log[f"{key}|{item}"] = times
            days = {int(t // 86400) for t in times}
            if len(times) >= VOLATILE_MIN_CHANGES and len(days) >= VOLATILE_MIN_DAYS:
                self._adopt_kept_local(folder, **{field: [item]})
                del log[f"{key}|{item}"]
                self.d.log(f"{folder}: {item} changes by itself; keeping it on each computer")

    def _attach_blob(self, key: str, entry: dict[str, Any]) -> None:
        if entry.get("del"):
            return
        folder = key[2:]
        if key.startswith(("f/", "d/")) and entry.get("files"):
            name = blobs.files_blob_name(entry, key[0])
            if not self.d.media_has(name):
                data = blobs.build_files_blob(self._files_root(key), entry["files"])
                self.d.media_write(name, data)
            entry["blob"] = name
        elif key.startswith(("a/", "c/")) and entry.get("code"):
            name = blobs.code_blob_name(entry["code"])
            if not self.d.media_has(name):
                data = blobs.build_code_blob(self.d.addons_dir(), folder)
                if len(data) > self.settings.max_code_bytes:
                    self.state["too_big"][folder] = round(len(data) / 1e6, 1)
                    raise ValueError(f"code is over {self.settings.max_code_mb:g} MB")
                self.d.media_write(name, data)
            self.state["too_big"].pop(folder, None)
            entry["blob"] = name

    def _collect_garbage(self) -> None:
        referenced = set()
        maps = [self.d.read_remote(), self.state["base"]]
        maps += [h["base"] for h in self.state["history"]]
        for m in maps:
            for e in m.values():
                referenced.update(x["blob"] for x in (e, e.get("mine") or {}) if x.get("blob"))
        now = self.d.now()
        stale = [
            name for name, mtime in self.d.media_list(blobs.PREFIX)
            if name.startswith(blobs.BLOB_PREFIXES)
            and name not in referenced and now - mtime > BLOB_GRACE_SECONDS
        ]
        if stale:
            self.d.media_trash(stale)
            self.d.log(f"removed {len(stale)} old Loadout files from media")

    # Pull (after a sync)
    ##########################################################################

    def pull(self, reason: str = "sync") -> None:
        """Make this device match the synced setup.

        reason is "sync" after a collection sync, "media" after a media sync
        (when synced entries have arrived), or "open"."""
        if self.mode == "paused" or self.inactive_reason or self.closing or self.busy:
            return
        remote = {k: v for k, v in self.synced().items() if not self.excluded(k)}
        self._remote = remote
        self._check_identity(remote)
        self._check_profile(remote)
        self._check_versions(remote)
        joining = not self.state["joined"]
        if joining and self.mode == "mirror" and not self._join(remote, reason):
            return
        scanned: list[dict[str, dict]] = []

        def local_now() -> dict[str, dict]:
            # Scanning the add-ons is most of a pull's time, and most pulls
            # bring nothing new, so it only happens when an entry needs it.
            if not scanned:
                scanned.append(self.local_entries())
            return scanned[0]

        if joining and self.mode == "mirror":
            local = local_now()
            # "Make this computer match" approves every add-on in the saved
            # setup; add-ons only here wait until the user decides to share.
            self.state["unshared"] = sorted(
                key[2:] for key in local
                if key.startswith("a/") and (remote.get(key) is None or remote[key].get("del"))
            )
            self.state["install_approved"] = sorted(
                key[2:] for key, r in remote.items()
                if key.startswith("a/") and not r.get("del") and key not in local
            )
        changes = self._changes(remote, local_now, joining)
        if self.mode == "preview":
            self.state["preview"] = self.describe(changes, local_now())
            self.save()
            if changes:
                self.d.notify("Loadout preview: would change", self.state["preview"], False)
            return
        if not changes:
            if joining:
                self.state["joined"] = True
            self.save()
            self._maybe_review()
            return
        self._remember("Before changes from another device")
        self._apply(changes, local_now(), joining, rollback=False)

    def _changes(
        self, remote: dict[str, dict], local_now: Callable[[], dict[str, dict]], joining: bool
    ) -> list[tuple[str, dict]]:
        base = self.state["base"]
        out = []
        for key in sorted(remote):  # a/ sorts first, so out already holds any install
            r = remote[key]
            if (base.get(key) or {}).get("h") == r.get("h"):
                continue
            if key.startswith("c/"):
                base[key] = r  # backups are only read when a download fails
                continue
            if key.startswith("a/") and not joining and self._awaits_decision(key[2:], r):
                continue
            folder = key[2:]
            if (key.startswith(("f/", "d/", "k/")) and not self.installed(folder)
                    and not any(k == f"a/{folder}" for k, _ in out)):
                # Its add-on isn't here (nothing local to scan): a removal is
                # settled, anything else waits until the add-on is installed.
                if r.get("del"):
                    base[key] = r
                continue
            mine = local_now().get(key)
            if (mine and mine["h"] == r["h"]) or (r.get("del") and mine is None):
                base[key] = r
                continue
            if joining and r.get("del"):
                # Never remove add-ons from a computer that is just joining.
                base[key] = r
                continue
            if key.startswith(("a/", "f/")) and not r.get("del"):
                # Lists of what stays local combine, whichever edit wins below.
                self._adopt_kept_local(key[2:], keys=r.get("local_keys"), files=r.get("local_files"))
            r = self._theirs(key, r, mine)
            if r is None:
                continue  # ours stays and goes up again on the next sync
            if self.state["failed"].get(key) == r["h"]:
                continue
            held = self.state["held_files"].get(key)
            if held and held["entry"].get("h") == r.get("h"):
                continue  # already waiting for Anki to close
            out.append((key, r))
        return out

    def _theirs(self, key: str, r: dict, mine: dict | None) -> dict | None:
        """What to apply of another computer's entry r: r, a merge of it
        with this computer's own edit, or None to keep ours. A merge or
        None marks the base to send ours again; until then later pulls
        skip r (ours already won against it)."""
        base = self.state["base"]
        known = base.get(key) or {}
        if known.get("h") == FORCE_PUSH and not known.get("over"):
            # A rollback here (or a 1.7.1 marker): it wins over what is
            # synced now, until it has been uploaded.
            base[key] = {"h": FORCE_PUSH, "over": r["h"]}
            return None
        if known.get("h") == FORCE_PUSH:
            if known["over"] == r.get("h"):
                return None
            known = known.get("mine") or {}
        merged = self._merge(key, known, r, mine)
        if merged is not r:
            settle = {"h": FORCE_PUSH, "over": r["h"], "mine": known}
            if merged is None:
                base[key] = settle
            else:
                merged["settle"] = settle
        return merged

    def _merge(self, key: str, known: dict, r: dict, mine: dict | None) -> dict | None:
        """Media sync keeps one upload per item, so r may be an edit made
        without having seen this computer's last upload (known): uploaded
        before that one arrived, or it replaced ours on AnkiWeb. Each part
        (an add-on's code, its settings; one part for other items) comes
        from whichever side changed it since the version both started from
        (each entry's "fork"). Changed on both: code stays ours unless
        theirs is a newer AnkiWeb version; otherwise the newer edit wins.
        Returns r, None (all ours) or a merged entry."""
        if (
            r.get("prev") is None and not r.get("del") and not known.get("del")
            and known.get("h") not in (None, FORCE_PUSH)
        ):
            # A first, blind upload from a computer that hadn't received this
            # item yet (its media sync hadn't brought it). It isn't an edit, so
            # the established value wins and goes up again: an edited one, or
            # of two first uploads the earlier.
            if known.get("prev") is not None or known.get("t", 0) < r.get("t", 0):
                return None
            return r
        if (
            known.get("by") != self.state["device_id"] or known.get("prev") is None
            or r.get("prev") == known.get("h")
        ):
            # Not our edit, or made on top of it. (Our own blind upload is
            # not an edit either: the established value wins.)
            return r
        split = key.startswith("a/") and not r.get("del") and not known.get("del")
        ours_now, theirs_now = self._parts(key, known), self._parts(key, r)
        ours_fork, theirs_fork = known.get("fork") or {}, r.get("fork") or {}
        take = {}
        for part in ("code", "settings") if split else ("all",):
            if ours_now[part] == theirs_now[part] or theirs_fork.get(part) == theirs_now[part]:
                take[part] = "ours"  # same, or they left it as it was
            elif ours_fork.get(part) == ours_now[part]:
                take[part] = "theirs"
            elif part == "code" and r.get("mod", 0) != known.get("mod", 0):
                take[part] = "theirs" if r.get("mod", 0) > known.get("mod", 0) else "ours"
            elif part == "code":
                take[part] = "ours"  # never install over code this computer changed
            else:
                take[part] = "theirs" if r.get("t", 0) > known.get("t", 0) else "ours"
        if all(v == "ours" for v in take.values()):
            return None
        if all(v == "theirs" for v in take.values()):
            return r
        ours = mine or known
        merged = {k: v for k, v in r.items() if k in ("h", "t", "by", "prev", "fork")}
        for k in set(ours) | set(r):
            part = "code" if k in CODE_FIELDS + CODE_EXTRA else "settings"
            src = ours if take[part] == "ours" else r
            if k not in merged and k in src:
                merged[k] = src[k]
        self.d.log(f"merged {key}: code from {take['code']}, settings from {take['settings']}")
        return merged

    def _parts(self, key: str, entry: dict) -> dict[str, str]:
        """Hashes of an entry's parts, as compared by _merge."""
        parts = {"all": entry.get("h") or ""}
        if key.startswith("a/"):
            body = {k: v for k, v in entry.items() if k not in scan.UNHASHED and k not in ("fork", "settle")}
            code = {k: body.pop(k) for k in CODE_FIELDS if k in body}
            parts.update(code=scan.entry_hash(code), settings=scan.entry_hash(body))
        return parts

    def _fork(self, key: str, known: dict) -> dict[str, str] | None:
        """The parts of the version this computer's edits started from: the
        last one it got from elsewhere (or its own first upload)."""
        own = (known.get("mine") or {}) if known.get("h") == FORCE_PUSH else known
        if not own.get("h"):
            return None
        if own.get("by") == self.state["device_id"] and own.get("prev") is not None:
            return own.get("fork")
        return self._parts(key, own)

    # Add-on review
    ##########################################################################

    def _awaits_decision(self, folder: str, r: dict) -> bool:
        """An add-on that would appear on or vanish from this computer waits
        for the user (except removals someone chose to make everywhere)."""
        installed = self.installed(folder)
        if not r.get("del") and not installed:
            return folder not in self.state["install_approved"]
        if r.get("del") and installed and not r.get("everywhere"):
            return folder not in self.state["remove_approved"]
        return False

    def differences(self, include_settled: bool = False) -> list[dict[str, Any]]:
        """Add-ons that differ between this computer and the saved setup.

        kind: new_elsewhere (another computer has it), removed_elsewhere
        (uninstalled on another computer), only_here (here when this computer
        joined, not shared yet). include_settled adds kept_off / kept_here:
        earlier "keep off" / "keep only here" choices, so they can be undone."""
        remote = self.d.read_remote()
        base = self.state["base"]
        out = []
        for key, r in remote.items():
            folder = key[2:]
            if not key.startswith("a/") or folder in self.settings.exclude_addons or self.off_limits(key):
                continue
            if folder in self.state["unshared"] or folder in self.state["install_approved"]:
                continue
            name = r.get("name") or folder
            installed = self.installed(folder)
            kept = folder in self.state["excluded_here"]
            if not r.get("del") and not installed:
                if kept:
                    if include_settled:
                        out.append({"folder": folder, "name": name, "kind": "kept_off"})
                elif self.state["failed"].get(key) != r.get("h") and not (
                    r.get("code") and self.state["declined_code"].get(folder) == r.get("code")
                ):
                    out.append({"folder": folder, "name": name, "kind": "new_elsewhere"})
            elif (
                r.get("del") and not r.get("everywhere") and installed and not kept
                and (base.get(key) or {}).get("h") != r.get("h")
                and folder not in self.state["remove_approved"]
            ):
                out.append({"folder": folder, "name": self._local_name(folder), "kind": "removed_elsewhere"})
        for folder in self.state["unshared"]:
            if self.installed(folder):
                out.append({"folder": folder, "name": self._local_name(folder), "kind": "only_here"})
        if include_settled:
            for folder in self.state["excluded_here"]:
                if self.installed(folder):
                    out.append({"folder": folder, "name": self._local_name(folder), "kind": "kept_here"})
        return sorted(out, key=lambda d: d["name"].lower())

    def _local_name(self, folder: str) -> str:
        meta = scan.read_json(os.path.join(self.addon_path(folder), "meta.json"))
        return meta.get("name") or folder

    def _maybe_review(self) -> None:
        """Ask about add-on differences not shown yet this session."""
        if self.mode != "mirror" or self.closing or self.busy or not self.state["joined"]:
            return
        items = self.differences()
        fresh = [d for d in items if (d["folder"], d["kind"]) not in self.reviewed]
        if not fresh:
            return
        self.reviewed |= {(d["folder"], d["kind"]) for d in items}
        self.decide(self.d.review(items))

    def review_now(self) -> bool:
        """The menu's Review: show every difference, settled ones too.
        Returns False if there is nothing to show."""
        items = self.differences(include_settled=True)
        if not items:
            return False
        self.reviewed |= {(d["folder"], d["kind"]) for d in items}
        self.decide(self.d.review(items))
        return True

    def decide(self, decisions: dict[str, str]) -> None:
        """Carry out review choices: folder -> install | keep_off |
        remove_here | keep_here | share | keep_only_here | remove_everywhere."""
        if not decisions:
            return
        remote = self.d.read_remote()
        base = self.state["base"]
        lists = {k: set(self.state[k]) for k in
                 ("excluded_here", "unshared", "install_approved", "remove_approved")}
        now = self.d.now()
        act = False
        for folder, action in decisions.items():
            key = f"a/{folder}"
            if action == "install":
                lists["excluded_here"].discard(folder)
                lists["install_approved"].add(folder)
                base.pop(key, None)  # so the add-on and its data are applied afresh
                base.pop(f"f/{folder}", None)
                act = True
            elif action in ("keep_off", "keep_here", "keep_only_here"):
                lists["excluded_here"].add(folder)
                lists["unshared"].discard(folder)
                if key in remote:
                    base[key] = remote[key]
            elif action == "share":
                lists["unshared"].discard(folder)
                lists["excluded_here"].discard(folder)
            elif action == "remove_here":
                lists["remove_approved"].add(folder)
                act = True
            elif action == "remove_everywhere":
                for name in lists:
                    lists[name].discard(folder)
                if self.installed(folder):
                    self.d.delete_addon(folder)
                stone = scan.finish({"del": True, "everywhere": True})
                stone.update(t=now, by=self.state["device_id"])
                for prefix in ("a/", "f/", "c/"):
                    k = prefix + folder
                    if prefix == "a/" or k in remote or k in base:
                        self.d.write_remote(k, dict(stone))
                        base[k] = dict(stone)
                self.d.log(f"removed {folder} on every computer")
        for name, values in lists.items():
            self.state[name] = sorted(values)
        self.save()
        if act:
            self.pull("review")
        if any(a in ("share", "remove_everywhere") for a in decisions.values()):
            self.push()
            self.d.request_sync()  # let other computers hear about it soon

    def _join(self, remote: dict[str, dict], reason: str) -> bool:
        addons = [k for k, v in remote.items() if k.startswith("a/") and not v.get("del")]
        if addons:
            if self.asked_match:
                return False
            self.asked_match = True
            profile = (remote.get("m/profile") or {}).get("name", "?")
            ok = self.d.confirm(
                f"Loadout found your saved Anki setup on AnkiWeb ({len(addons)} add-ons, "
                f"saved from profile “{profile}”).\n\n"
                "Make this computer match it? Add-ons will be installed and their "
                "settings copied. Your logins stay on each computer."
            )
            return ok
        # Saved setups arrive with media (or early, from the collection's
        # settings, see EARLY_SETUP_KEY), so "nothing saved" is only known
        # once a media sync has finished.
        if reason != "media" or self.asked_first:
            return False
        self.asked_first = True
        if self.d.confirm(
            "No saved Loadout setup was found on AnkiWeb.\n\n"
            "Is this the computer whose add-ons and settings you want copied to "
            "your other computers?\n\n"
            "If you already set up Loadout on another computer, click No: its "
            "setup hasn't arrived yet. Sync again and Loadout will ask you."
        ):
            self.state["joined"] = True
            self.save()
            self.push()
            self.d.notify("Loadout", ["This computer's setup will upload on your next sync."], False)
        return False

    def _other_computers(self, remote: dict[str, dict]) -> list[dict]:
        """The latest version stamp of each other computer seen in the last
        60 days. A computer that reinstalled Loadout (or copied a fresh one
        over) leaves its old stamp behind under the same name: only its
        newest counts, and stamps with this computer's own name are its
        earlier installs."""
        mine = f"m/version/{self.state['device_id']}"
        here = self.d.computer_name()
        now = self.d.now()
        latest: dict[str, dict] = {}
        for key, entry in remote.items():
            if not key.startswith("m/version/") or key == mine or entry.get("del"):
                continue
            if now - entry.get("t", now) > 60 * 86400:
                continue
            name = entry.get("computer") or key
            if name == here:
                continue
            if name not in latest or entry.get("t", 0) > latest[name].get("t", 0):
                latest[name] = entry
        return list(latest.values())

    def other_versions(self, remote: dict[str, dict] | None = None) -> list[dict[str, str]]:
        """Other computers whose Loadout version differs from this one:
        [{"computer", "version", "newer": "yes"/""}]."""
        remote = remote if remote is not None else self.d.read_remote()
        out = []
        for entry in self._other_computers(remote):
            if entry.get("version") == VERSION:
                continue
            newer = parse_version(entry.get("version", "0")) > parse_version(VERSION)
            out.append({"computer": entry.get("computer") or "another computer",
                        "version": entry.get("version", "?"), "newer": "yes" if newer else ""})
        return out

    def other_anki_versions(self) -> list[dict[str, str]]:
        """Other computers running a different Anki release (year.month;
        patch releases don't count)."""
        mine = self.d.anki_version()
        release = lambda v: parse_version(v)[:2]  # noqa: E731
        out = []
        for entry in self._other_computers(self.d.read_remote()):
            theirs = entry.get("anki")
            if theirs and release(theirs) != release(mine):
                out.append({"computer": entry.get("computer") or "another computer", "anki": theirs})
        return out

    def _check_versions(self, remote: dict[str, dict]) -> None:
        if self.warned_version:
            return
        others = self.other_versions(remote)
        if not others:
            return
        self.warned_version = True
        lines = []
        for o in others:
            if o["newer"]:
                lines.append(f"\u201c{o['computer']}\u201d has a newer Loadout ({o['version']}); "
                             f"this computer has {VERSION}. Install the newer one here.")
            else:
                lines.append(f"\u201c{o['computer']}\u201d has an older Loadout ({o['version']}); "
                             f"this computer has {VERSION}. Install this version there.")
        lines.append("Computers on different versions can keep re-sending the same changes.")
        self.d.notify("Loadout: versions differ", lines, False)

    def _check_identity(self, remote: dict[str, dict]) -> None:
        """Copying Anki's whole folder to a new computer copies Loadout's
        state too, so two computers would share one identity (and take each
        other's edits for their own). The copy notices when another computer
        has uploaded under its name, and takes a new one."""
        key = f"m/version/{self.state['device_id']}"
        theirs, mine = remote.get(key), self.state["base"].get(key)
        if (
            not theirs or not mine or theirs.get("h") == mine.get("h")
            or theirs.get("computer") in (None, self.d.computer_name())
        ):
            return
        old = self.state["device_id"]
        self.state["device_id"] = uuid.uuid4().hex[:12]
        self.state["base"].pop(key, None)
        self.d.log(f"computer {theirs.get('computer')!r} also uses id {old}; "
                   f"this computer is now {self.state['device_id']}")
        self.save()

    def _check_profile(self, remote: dict[str, dict]) -> None:
        saved = (remote.get("m/profile") or {}).get("name")
        here = self.d.profile_name()
        if saved and saved != here and not self.warned_profile:
            self.warned_profile = True
            self.d.notify(
                "Loadout: profile name differs",
                [
                    f"Your saved setup comes from a profile named “{saved}”, "
                    f"but this one is “{here}”.",
                    "Some add-ons name their data after the profile. Rename this profile "
                    f"to “{saved}” (File → Switch Profile → Rename) so they find it.",
                ],
                False,
            )

    # Applying changes
    ##########################################################################

    def _apply(
        self, changes: list[tuple[str, dict]], local: dict[str, dict], joining: bool, rollback: bool,
        imported: bool = False,
    ) -> None:
        """imported: the changes come from a shared .loadout file, not from
        the synced setup. They are applied like the user's own edits: the
        base is left alone, so they mirror to this user's other computers."""
        self.busy = True
        updates: list[tuple[str, int]] = []
        for key, r in changes:
            if not key.startswith("a/") or r.get("del") or not r.get("ankiweb_id"):
                continue
            if r.get("code"):
                continue  # comes with its exact code; nothing to download
            mine = local.get(key)
            if mine is None or r.get("mod", 0) > mine.get("mod", 0):
                if not self._backup_for(key[2:], r, exact=True):
                    updates.append((key[2:], r["ankiweb_id"]))

        def after_downloads(results: dict[int, str | None]) -> None:
            try:
                self._apply_rest(changes, local, joining, rollback, updates, results, imported)
            finally:
                self.busy = False
            self._maybe_review()

        if updates:
            self.d.install_ankiweb([i for _, i in updates], after_downloads)
        else:
            after_downloads({})

    def _backup_for(self, folder: str, a_entry: dict, exact: bool) -> dict | None:
        """The synced backup copy of an AnkiWeb add-on, if there is one.

        exact: only a copy of the very version wanted. Such a copy is used
        instead of downloading, so the add-on arrives exactly as it was on
        the other computer."""
        backup = self._remote.get(f"c/{folder}") or {}
        if not backup.get("code") or backup.get("del"):
            return None
        if exact and backup.get("mod", 0) != a_entry.get("mod", 0):
            return None
        return backup

    def _apply_rest(
        self,
        changes: list[tuple[str, dict]],
        local: dict[str, dict],
        joining: bool,
        rollback: bool,
        updates: list[tuple[str, int]],
        results: dict[int, str | None],
        imported: bool = False,
    ) -> None:
        base = self.state["base"]
        done: list[str] = []
        lines: list[str] = []
        problems: list[str] = []
        waiting: list[str] = []
        restart = False
        fresh: set[str] = set()
        held: list[str] = []
        blocked: set[str] = set()  # needed an install or update that did not happen
        remote = dict(changes)

        copies: list[tuple[str, str, str, str | None, int | None]] = []
        for key, r in changes:
            mine = local.get(key)
            if (
                key.startswith("a/") and not r.get("del") and r.get("code")
                and (mine is None or mine.get("code") != r["code"])
            ):
                copies.append((key[2:], r.get("name") or key[2:], r["code"], r.get("blob"), r.get("mod")))
        fallback: dict[str, str] = {}
        downloading = {folder for folder, _ in updates}
        for key, r in changes:
            folder = key[2:]
            mine = local.get(key)
            if (
                key.startswith("a/") and not r.get("del") and r.get("ankiweb_id")
                and not r.get("code") and folder not in downloading
                and (mine is None or r.get("mod", 0) > mine.get("mod", 0))
            ):
                backup = self._backup_for(folder, r, exact=True)
                if backup:
                    fallback[folder] = "backup copy not installed"
                    name = self._name(folder, remote, local)
                    copies.append((folder, name, backup["code"], backup.get("blob"), backup.get("mod")))
        for folder, aid in updates:
            name = self._name(folder, remote, local)
            err = results.get(aid)
            backup = self._backup_for(folder, remote[f"a/{folder}"], exact=False) or {}
            if err and backup:
                fallback[folder] = err
                copies.append((folder, name, backup["code"], backup.get("blob"), backup.get("mod")))
            elif err:
                self.state["failed"][f"a/{folder}"] = remote[f"a/{folder}"]["h"]
                problems.append(f"{name}: {err}")
                blocked.add(folder)
            else:
                fresh.add(folder)
                restart = True
                lines.append(("New version of " if f"a/{folder}" in local else "Installed ") + name)

        restart |= self._install_code(copies, local, fresh, blocked, lines, problems, waiting)
        for folder, err in fallback.items():
            if folder in blocked and self._name(folder, remote, local) not in waiting:
                # Neither AnkiWeb nor the backup worked (or it was not approved).
                self.state["failed"][f"a/{folder}"] = remote[f"a/{folder}"]["h"]
                problems.append(f"{self._name(folder, remote, local)}: {err}")

        for key, r in changes:
            if not key.startswith("a/"):
                continue
            folder = key[2:]
            name = self._name(folder, remote, local)
            if r.get("del"):
                if self.installed(folder):
                    self.d.delete_addon(folder)
                    lines.append(f"Removed {name}")
                    restart = True
                done.append(key)
                continue
            if not self.installed(folder):
                continue
            self._adopt_kept_local(folder, keys=r.get("local_keys"))
            restart |= self._apply_meta(folder, r, name, lines)
            if folder not in blocked:
                done.append(key)

        for key, r in changes:
            if not key.startswith("k/"):
                continue
            folder = key[2:]
            if r.get("del"):
                done.append(key)
                continue
            if not self.installed(folder) or folder in blocked:
                continue
            self._adopt_kept_local(folder, keys=r.get("local_keys"))
            if self._apply_collection_settings(folder, r):
                lines.append(f"Updated settings for {self._name(folder, remote, local)}")
                restart = True
            done.append(key)

        for key, r in changes:
            if not key.startswith(("f/", "d/")):
                continue
            folder = key[2:]
            name = self._name(folder, remote, local)
            if r.get("del"):
                done.append(key)
                continue
            if not self.installed(folder) or folder in blocked:
                continue
            info = (remote.get(f"a/{folder}") or local.get(f"a/{folder}") or {}).get("info", {})
            if (
                key.startswith("f/") and info.get("self_sync") and folder not in fresh and not joining
                and not imported and base.get(key) is not None
            ):
                # The add-on keeps this data in sync itself. (Without a base,
                # this computer never got the seed copy: e.g. it joined before
                # media brought the files.)
                done.append(key)
                continue
            self._adopt_kept_local(folder, files=r.get("local_files"))
            if self.d.is_loaded(folder):  # running, possibly old code after an update
                self.state["held_files"][key] = {"entry": r, "rollback": rollback, "imported": imported}
                held.append(name)
                continue
            try:
                result = self._apply_files(key, r, local.get(key), delete=not imported)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{name} data: {e}")
                self.d.log(f"files {folder}: {e}")
                continue
            if result is None:
                waiting.append(name)
                continue
            if result:
                lines.append(f"Updated {name} data ({result} files)")
                restart = True
            done.append(key)

        for key, r in changes:
            if key.startswith("p/"):
                self.d.set_prefs(key, r["value"])
                lines.append("Updated menu favorites" if key == "p/menus" else "Updated Anki preferences")
                restart = True
                done.append(key)
            elif key.startswith("m/"):
                done.append(key)

        for key in done:
            if not imported:
                base[key] = {"h": FORCE_PUSH} if rollback else remote[key].get("settle") or remote[key]
            self.state["failed"].pop(key, None)
            if key.startswith("a/"):
                for name in ("install_approved", "remove_approved"):
                    if key[2:] in self.state[name]:
                        self.state[name].remove(key[2:])
        if joining:
            self.state["joined"] = True
        if lines or rollback:
            label = "Rolled back" if rollback else "After changes from another device"
            self._remember("Installed shared add-ons" if imported else label)
        self.save()
        self.d.log(
            f"applied {len(done)} of {len(changes)}; held until close {held}; "
            f"waiting {waiting}; problems {problems}"
        )

        out = list(lines)
        if held:
            out.append("Data for " + ", ".join(held) + " updates when you close Anki")
        if waiting:
            out.append("Still downloading: " + ", ".join(waiting) + " (applies after media sync)")
        if problems:
            out.append("Problems: " + "; ".join(problems))
        if not out:
            return
        self.state["last_changes"] = {"time": self.d.now(), "lines": out}
        self.save()
        if joining or imported:
            # Setting up a computer, or installing what a friend shared: the
            # times a restart is worth asking for.
            title = "Loadout installed the shared add-ons" if imported else "Loadout set up this computer"
            self.d.notify(title, out, True)
            return
        # Day to day, only say something about add-ons themselves appearing,
        # changing version or going away, and about problems. Settings, data
        # and preferences update quietly (the Loadout window lists them).
        notable = [line for line in lines if line.startswith(NOTABLE_PREFIXES)]
        notable += [line for line in out if line.startswith("Problems")]
        if notable:
            notable.append("Takes effect next time you open Anki.")
            self.d.notify("Loadout: changes from your other computer", notable, False)

    def apply_held_files(self, closing_sync: bool = False) -> None:
        """Write data held back while its add-on was running. Called as Anki
        closes, after the other add-ons have saved their own state.

        closing_sync: the closing sync just finished, so also take any data
        it brought for add-ons that are running (they stop in a moment)."""
        if self.mode != "mirror" or self.inactive_reason or self.busy or not self.state["joined"]:
            return
        held = self.state["held_files"]
        base = self.state["base"]
        if closing_sync:
            remote = {k: v for k, v in self.d.read_remote().items() if not self.excluded(k)}
            for key, r in remote.items():
                folder = key[2:]
                if (
                    not key.startswith(("f/", "d/")) or r.get("del")
                    or (base.get(key) or {}).get("h") == r.get("h")
                    or self.state["failed"].get(f"a/{folder}")
                    or not self.installed(folder)
                ):
                    continue
                if key.startswith("f/") and ((remote.get(f"a/{folder}") or {}).get("info") or {}).get("self_sync"):
                    continue  # its own sync handles it; settled on the next normal pull
                if self._theirs(key, r, None) is None:
                    continue  # this computer's newer edit stays
                current = held.get(key)
                if not current or current["entry"].get("h") != r.get("h"):
                    held[key] = {"entry": r, "rollback": False}
        if not held:
            return
        local = self.local_entries()
        for key in sorted(held):
            item = held[key]
            folder = key[2:]
            if not self.installed(folder):
                del held[key]
                continue
            try:
                result = self._apply_files(key, item["entry"], local.get(key),
                                           delete=not item.get("imported"))
            except Exception as e:  # noqa: BLE001
                self.d.log(f"held files {folder}: {e}")
                del held[key]
                continue
            if result is None:
                continue  # still downloading; try again next close
            if not item.get("imported"):
                base[key] = {"h": FORCE_PUSH} if item["rollback"] else item["entry"]
            del held[key]
            self.d.log(f"wrote held data for {folder} ({result} files)")
        self.save()

    def _install_code(
        self,
        copies: list[tuple[str, str, str, str | None, int | None]],
        local: dict[str, dict],
        fresh: set[str],
        blocked: set[str],
        lines: list[str],
        problems: list[str],
        waiting: list[str],
    ) -> bool:
        """Install add-ons from code copied from another device: add-ons not
        on AnkiWeb, and backups of AnkiWeb add-ons that failed to download.

        copies are (folder, name, code hash, media blob, AnkiWeb mod time)."""
        if not copies:
            return False
        approved = self.state["approved_code"]
        declined = self.state["declined_code"]
        ask = [
            (folder, name) for folder, name, code, _, _ in copies
            if approved.get(folder) != code and declined.get(folder) != code
        ]
        if ask:
            yes = self.d.choose_code_installs(ask) if self.confirm_new_code else {f for f, _ in ask}
            for folder, name, code, _, _ in copies:
                if (folder, name) in ask:
                    target = approved if folder in yes else declined
                    target[folder] = code
        changed = False
        for folder, name, code, blob, mod in copies:
            blocked.add(folder)
            if approved.get(folder) != code:
                continue
            if not blob or not self.d.media_has(blob):
                waiting.append(name)
                continue
            err = self.d.install_code(folder, name, self.d.media_read(blob), mod)
            if err:
                problems.append(f"{name}: {err}")
                continue
            blocked.discard(folder)
            fresh.add(folder)
            changed = True
            lines.append(("New version of " if f"a/{folder}" in local else "Installed ") + name)
        return changed

    def _apply_meta(self, folder: str, r: dict, name: str, lines: list[str]) -> bool:
        meta = scan.read_json(os.path.join(self.addon_path(folder), "meta.json"))
        changed = False
        enabled = not meta.get("disabled", False)
        if r.get("enabled", True) != enabled:
            self.d.set_enabled(folder, r.get("enabled", True))
            lines.append(("Enabled " if r.get("enabled", True) else "Disabled ") + name)
            changed = True
        if r.get("update_enabled", True) != meta.get("update_enabled", True):
            self.d.set_update_enabled(folder, r.get("update_enabled", True))
        keep = self.kept_local(folder)[0]
        current = meta.get("config")
        want = rules.restore_secrets(r.get("config") or {}, current or {}, keep)
        if r.get("config") is None and not want:
            want = None
        if want != current and not (want is None and current is None):
            if rules.strip_secrets(want or {}, keep) != rules.strip_secrets(current or {}, keep):
                self.d.write_config(folder, want)
                lines.append(f"Updated settings for {name}")
                changed = True
        return changed

    def _files_root(self, key: str) -> str:
        """Where an f/ or d/ entry's files live."""
        if key.startswith("d/"):
            return self.d.profile_dir()
        return os.path.join(self.addon_path(key[2:]), "user_files")

    def _apply_collection_settings(self, folder: str, r: dict) -> bool:
        """Write an add-on's collection settings; returns whether any changed.
        This computer's own logins and kept-local settings stay as they are."""
        current = self.d.collection_settings()
        kept = self.kept_local(folder)[0] | set(r.get("local_keys") or [])
        changed = False
        for key, value in (r.get("config") or {}).items():
            if key in kept or self.d.is_anki_setting(key):
                continue
            if current.get(key) != value:
                self.d.set_collection_setting(key, value)
                changed = True
        return changed

    def _apply_files(self, key: str, r: dict, mine: dict | None, delete: bool = True) -> int | None:
        """Bring an add-on's files (f/: user_files, d/: profile folder) in
        line with r. Returns files written, or None if the data has not
        arrived through media sync yet."""
        folder = key[2:]
        user_files = self._files_root(key)
        have = (mine or {}).get("files", {})
        want = r.get("files", {})
        kept = self.kept_local(folder)[1] | set(r.get("local_files") or [])
        todo = {rel: sha for rel, sha in want.items() if have.get(rel) != sha and rel not in kept}
        if key.startswith("d/"):
            root = os.path.realpath(user_files)
            for rel in todo:
                top = os.path.relpath(blobs.safe_target(root, rel), root).split(os.sep)[0]
                if scan.is_anki_profile_item(top):
                    raise ValueError(f"refused to write Anki's own file {rel!r}")
        written = 0
        if todo:
            blob = r.get("blob")
            if not blob or not self.d.media_has(blob):
                return None
            written = len(blobs.write_files(self.d.media_read(blob), user_files, todo))
        known = (self.state["base"].get(key) or {}).get("files", {}) if delete else {}
        for rel, sha in known.items():
            # Delete only files this device had in sync and has not touched.
            if key.startswith("d/") and scan.is_anki_profile_item(rel.replace("\\", "/").split("/")[0]):
                continue
            if rel not in want and rel not in kept and have.get(rel) == sha:
                path = blobs.safe_target(user_files, rel)
                if os.path.isfile(path):
                    os.remove(path)
                    written += 1
        return written

    # Installing from a shared file (see share.py)
    ##########################################################################

    def install_shared(self, changes: list[tuple[str, dict]]) -> int:
        """Install add-ons, settings and data from a shared .loadout file.
        Returns how many items needed changing (0: already identical)."""
        if self.busy:
            raise RuntimeError("Loadout is busy; try again in a moment")
        local = self.local_entries()
        changes = [(k, r) for k, r in changes if not self.off_limits(k)]
        todo = [(k, r) for k, r in changes if r.get("code") or (local.get(k) or {}).get("h") != r.get("h")]
        if todo:
            self._remote = self.d.read_remote()
            self._remember("Before installing shared add-ons")
            self._apply(todo, local, joining=False, rollback=False, imported=True)
        return len(todo)

    # Rollback and status
    ##########################################################################

    def rollback(self, index: int) -> None:
        """Make this device match history point `index` and mark those items
        to upload, so every device follows on its next sync."""
        if self.busy or not self.state["joined"]:
            return
        target = self.state["history"][index]["base"]
        local = self.local_entries()
        changes = []
        for key, entry in sorted(target.items()):
            if self.excluded(key) or entry.get("h") == FORCE_PUSH or key.startswith("c/"):
                continue
            if (local.get(key) or {}).get("h") != entry.get("h"):
                if entry.get("del") and key.startswith("a/"):
                    continue  # rollback never removes add-ons
                changes.append((key, entry))
        self._remember("Before rollback")
        self._apply(changes, local, joining=False, rollback=True)

    def describe(self, changes: list[tuple[str, dict]], local: dict[str, dict]) -> list[str]:
        remote = dict(changes)
        out = []
        for key, r in changes:
            folder = key[2:]
            name = self._name(folder, remote, local)
            if key.startswith("a/"):
                mine = local.get(key)
                if r.get("del"):
                    out.append(f"Remove {name}")
                elif mine is None:
                    out.append(f"Install {name}")
                elif r.get("mod", 0) > mine.get("mod", 0) or r.get("code") != mine.get("code"):
                    out.append(f"Update {name}")
                else:
                    out.append(f"Change settings of {name}")
            elif key.startswith("f/") and not r.get("del"):
                out.append(f"Update data files of {name}")
            elif key.startswith("p/"):
                out.append("Change Anki preferences")
        return out

    def forget_failures(self) -> None:
        self.state["failed"] = {}
        self.state["declined_code"] = {}
        self.asked_match = False
        self.asked_first = False
        self.save()
