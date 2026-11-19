"""The engine's Device, implemented on top of a running Anki."""

from __future__ import annotations

import enum
import os
import sys
import tempfile
import uuid
import time
from typing import Any, Callable

from aqt import mw
from aqt.utils import askUser, showInfo, tooltip

from . import blobs, gui

# Collection-config keys used by Loadout 1.0. The config syncs as one block,
# so entries now live in media files; these are moved there once.
LEGACY_KEY_PREFIX = "loadout:"
LOG_LIMIT = 1_000_000

# Interface preferences shared by every profile on a computer (pm.meta).
# Window positions, interface size (screens differ), the graphics driver and
# sync settings are deliberately left out: they belong to one computer.
META_KEYS = (
    "reduce_motion", "minimalist_mode", "spacebar_rates_card",
    "hide_top_bar", "top_bar_hide_mode", "hide_bottom_bar", "bottom_bar_hide_mode",
    "theme", "widget_style", "browser_layout", "legacy_import",
    "check_for_updates", "defaultLang", "answer_keys",
)
# Per-profile preferences (pm.profile) that are safe to copy.
PROFILE_KEYS = (
    "middleClickPasteEnabled", "browserTableTooltips", "networkTimeout",
    "allowedUrlSchemes",
)


# Collection settings that are Anki's own (more are found in Anki's code at
# runtime, see AnkiDevice.is_anki_setting). Loadout never mirrors these:
# AnkiWeb syncs them, and many describe this computer's current state.
ANKI_SETTINGS = {
    "activeCols", "activeDecks", "activeNoteCols", "addToCur", "browserTableShowNotesMode",
    "collapseTime", "creationOffset", "curDeck", "curModel", "dayLearnFirst", "dueCounts",
    "estTimes", "fsrs", "lastUnburied", "localOffset", "newSpread", "nextPos", "rollover",
    "schedVer", "sortBackwards", "sortType", "timeLim", "sched2021", "pasteImagesAsPng",
    "pasteStripsFormatting", "ignoreAccentsInSearch", "defaultSearchText",
}
_anki_code: str | None = None


def _anki_source() -> str:
    """Anki's own Python code, read once, to recognise its settings."""
    global _anki_code
    if _anki_code is None:
        import importlib

        parts = []
        roots: list[str] = []
        for package in ("anki", "aqt", "_aqt"):
            try:
                roots += list(getattr(importlib.import_module(package), "__path__", []))
            except ImportError:
                continue
        for root in roots:
            for dirpath, _, filenames in os.walk(root):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        if name.endswith(".py"):
                            with open(path, encoding="utf-8", errors="ignore") as f:
                                parts.append(f.read())
                        elif name.endswith(".pyc"):
                            # Packaged Anki (26.x) ships compiled code only.
                            parts.append(_strings_in_pyc(path))
                    except (OSError, ValueError, EOFError, TypeError):
                        continue
        _anki_code = "\n".join(parts)
    return _anki_code


def _strings_in_pyc(path: str) -> str:
    """The quoted string constants in a compiled Python file."""
    import importlib.util
    import marshal

    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != importlib.util.MAGIC_NUMBER:
        return ""
    found: list[str] = []

    def walk(code: Any) -> None:
        for const in code.co_consts:
            if isinstance(const, str):
                found.append(f'"{const}"')
            elif hasattr(const, "co_consts"):
                walk(const)

    walk(marshal.loads(data[16:]))
    return "\n".join(found)


def sync_problems() -> list[str]:
    """Settings that stop Loadout from working, in plain words."""
    if not mw.pm.sync_auth():
        return ["You're not signed in to AnkiWeb, so nothing is mirrored yet. "
                "Click Sync to sign in. Loadout works through your normal sync."]
    if not mw.pm.media_syncing_enabled():
        return ["Media syncing is off (Preferences → Syncing), so add-on data and add-ons "
                "that aren't on AnkiWeb can't reach your other computers. Turn it on."]
    return []


def _plain(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class AnkiDevice:
    def __init__(self, own_folder: str, log_path: str) -> None:
        self._own = own_folder
        self._log_path = log_path
        self._names: tuple | None = None  # see _entry_names
        self._contents: dict[str, tuple] = {}  # entry file -> ((mtime, size), bytes)
        self.hub: Any = None  # the Add-on Hub, whose menu settings mirror like preferences

    # Places
    ##########################################################################

    def addons_dir(self) -> str:
        return mw.addonManager.addonsFolder()

    def own_folder(self) -> str:
        return self._own

    def profile_name(self) -> str:
        return mw.pm.name or ""

    def collection_settings(self) -> dict[str, Any]:
        return mw.col.all_config()

    def set_collection_setting(self, key: str, value: Any) -> None:
        mw.col.set_config(key, value)

    def is_anki_setting(self, key: str) -> bool:
        if key in ANKI_SETTINGS:
            return True
        code = _anki_source()
        return f'"{key}"' in code or f"'{key}'" in code

    def profile_dir(self) -> str:
        return mw.pm.profileFolder()

    def warm_up(self) -> None:
        """Read Anki's and the add-ons' code and files ahead of the first
        sync (runs in the background), so working out who owns what, and
        scanning the add-ons (at a sync or when the Loadout window opens),
        doesn't hold Anki up."""
        from . import owners, scan

        _anki_source()
        addons = self.addons_dir()
        for folder in scan.addon_folders(addons, self._own):
            owners.addon_text(addons, folder)
        entries, _ = scan.snapshot(addons, self._own, scan.Settings())
        for key, a in entries.items():
            # AnkiWeb add-ons Loadout may keep an exact copy of (see
            # Engine._backup_folders): their code is hashed too.
            if key.startswith("a/") and a.get("ankiweb_id") is not None and (
                    a.get("info", {}).get("edited") or not a.get("update_enabled", True)):
                scan.code_hash(addons, key[2:])

    def anki_version(self) -> str:
        from anki.buildinfo import version

        return version

    def computer_name(self) -> str:
        import socket

        name = socket.gethostname()
        return name[:-6] if name.endswith(".local") else name

    def now(self) -> float:
        return time.time()

    # Collection config
    ##########################################################################

    def _loadout_names(self, folder: str) -> list[str]:
        """Loadout's files in the media folder. Listing a media folder of
        100,000+ card files takes a noticeable moment, so the names are kept
        until an outside change touches the folder or for five minutes at most."""
        stamp = os.stat(folder).st_mtime_ns
        cached = self._names
        if cached and cached[0] == (folder, stamp) and time.time() - cached[1] < 300:
            return cached[2]
        names = [n for n in os.listdir(folder) if n.startswith(blobs.PREFIX)]
        self._names = ((folder, stamp), time.time(), names)
        return names

    def _note_media_change(self, folder: str, added: str | None = None, removed: set[str] | None = None) -> None:
        """Keep the cached media listing valid when Loadout itself adds or
        removes one of its own files in collection.media."""
        cached = self._names
        if not cached or cached[0][0] != folder:
            return
        try:
            stamp = os.stat(folder).st_mtime_ns
        except OSError:
            self._names = None
            return
        names = list(cached[2])
        if removed:
            names = [n for n in names if n not in removed]
        if added and added.startswith(blobs.PREFIX) and added not in names:
            names.append(added)
        self._names = ((folder, stamp), cached[1], names)

    def forget_media_listing(self) -> None:
        """Drop the cached listing. Called around syncs, so files a sync
        brought are seen even where a folder's modification time is coarse
        or not updated (network drives, FAT/exFAT)."""
        self._names = None

    def _entry_names(self, folder: str) -> list[str]:
        return [
            n for n in self._loadout_names(folder)
            if n.startswith(blobs.ENTRY_PREFIX) and n.endswith(".json")
        ]

    def read_remote(self) -> dict[str, dict[str, Any]]:
        folder = mw.col.media.dir()
        out: dict[str, dict[str, Any]] = {}
        try:
            names = self._entry_names(folder)
        except OSError:
            return out
        for name in names:
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
                sig = (st.st_mtime_ns, st.st_size)
                cached = self._contents.get(path)
                if cached and cached[0] == sig:
                    data = cached[1]  # unchanged since last read
                else:
                    with open(path, "rb") as f:
                        data = f.read()
                    self._contents[path] = (sig, data)
            except OSError:
                continue
            decoded = blobs.decode_entry(data)  # fresh objects each time: callers change them
            if decoded:
                out[decoded[0]] = decoded[1]
        return out

    def write_remote(self, key: str, entry: dict[str, Any]) -> None:
        # Replace the file (rather than rewrite it in place) so the media
        # folder's modification time changes and Anki's next media sync
        # notices and uploads it.
        fname = blobs.entry_file_name(key)
        media_dir = mw.col.media.dir()
        target = os.path.join(media_dir, fname)
        data = blobs.encode_entry(key, entry)
        for tmp_dir in (os.path.dirname(self._log_path), media_dir):
            tmp = os.path.join(tmp_dir, f".loadout-{uuid.uuid4().hex}.tmp")
            try:
                os.makedirs(tmp_dir, exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, target)
                try:
                    st = os.stat(target)
                    self._contents[target] = ((st.st_mtime_ns, st.st_size), data)
                except OSError:
                    pass
                self._note_media_change(media_dir, added=fname)
                return
            except OSError:  # e.g. add-ons and media on different drives
                if os.path.exists(tmp):
                    os.remove(tmp)
        raise OSError(f"could not write {target}")

    def migrate_legacy_config(self, keep: bool) -> int:
        """Move entries Loadout 1.0 kept in the collection config into media
        files (keep=True, for a computer that was set up) and remove them."""
        legacy = {
            k[len(LEGACY_KEY_PREFIX):]: v
            for k, v in mw.col.all_config().items()
            if k.startswith(LEGACY_KEY_PREFIX)
        }
        if not legacy:
            return 0
        if keep:
            existing = self.read_remote()
            for key, entry in legacy.items():
                if key not in existing and isinstance(entry, dict):
                    self.write_remote(key, entry)
        for key in legacy:
            mw.col.remove_config(LEGACY_KEY_PREFIX + key)
        self.log(f"moved {len(legacy)} entries out of the collection config (kept: {keep})")
        return len(legacy)

    # Media
    ##########################################################################

    def _media(self, name: str) -> str:
        return os.path.join(mw.col.media.dir(), name)

    def media_has(self, name: str) -> bool:
        return os.path.isfile(self._media(name))

    def media_read(self, name: str) -> bytes:
        with open(self._media(name), "rb") as f:
            return f.read()

    def media_write(self, name: str, data: bytes) -> None:
        folder = mw.col.media.dir()
        stored = mw.col.media.write_data(name, data)
        if stored != name:
            raise ValueError(f"Anki stored {name} as {stored}")
        self._note_media_change(folder, added=name)

    def media_list(self, prefix: str) -> list[tuple[str, float]]:
        folder = mw.col.media.dir()
        candidates = self._loadout_names(folder) if prefix.startswith(blobs.PREFIX) else os.listdir(folder)
        out = []
        for name in candidates:
            if name.startswith(prefix):
                try:
                    out.append((name, os.path.getmtime(os.path.join(folder, name))))
                except OSError:
                    continue
        return out

    def media_trash(self, names: list[str]) -> None:
        folder = mw.col.media.dir()
        mw.col.media.trash_files(names)
        for n in names:
            self._contents.pop(os.path.join(folder, n), None)
        self._note_media_change(folder, removed=set(names))

    # Preferences
    ##########################################################################

    def get_prefs(self) -> dict[str, dict[str, Any]]:
        from google.protobuf.json_format import MessageToDict

        meta = {k: _plain(mw.pm.meta[k]) for k in META_KEYS if k in mw.pm.meta}
        for k, v in mw.pm.meta.items():
            if isinstance(k, str) and k.endswith("TagsCollapsed"):
                meta[k] = _plain(v)
        profile = {k: _plain(mw.pm.profile[k]) for k in PROFILE_KEYS if k in mw.pm.profile}
        col = MessageToDict(mw.col.get_preferences(), preserving_proto_field_name=True)
        prefs = {"p/meta": meta, "p/profile": profile, "p/col": col}
        if self.hub is not None:
            prefs["p/menus"] = self.hub.shared_settings()
        return prefs

    def set_prefs(self, key: str, value: dict[str, Any]) -> None:
        if key == "p/menus":
            if self.hub is not None:
                self.hub.apply_shared_settings(value)
            return
        if key == "p/col":
            from google.protobuf.json_format import ParseDict

            prefs = type(mw.col.get_preferences())()
            ParseDict(value, prefs, ignore_unknown_fields=True)
            mw.col.set_preferences(prefs)
            return
        store = mw.pm.meta if key == "p/meta" else mw.pm.profile
        allowed = META_KEYS if key == "p/meta" else PROFILE_KEYS
        for k, v in value.items():
            if k not in allowed and not (key == "p/meta" and k.endswith("TagsCollapsed")):
                continue  # e.g. interface size sent by an older Loadout
            if k == "answer_keys" and isinstance(v, dict):
                v = {int(ease): keyname for ease, keyname in v.items()}
            old = store.get(k)
            if isinstance(old, enum.Enum):
                v = type(old)(v)
            store[k] = v
            if k == "theme":
                from aqt.theme import Theme

                mw.set_theme(Theme(v))
        mw.pm.save()

    # Add-ons
    ##########################################################################

    def install_ankiweb(
        self, ids: list[int], on_done: Callable[[dict[int, str | None]], None]
    ) -> None:
        from aqt.addons import DownloadError, InstallError, InstallOk, download_addons

        def done(log: list) -> None:
            results: dict[int, str | None] = {}
            for aid, res in log:
                if isinstance(res, InstallOk):
                    results[aid] = None if res.compatible else "not compatible with this Anki version"
                elif isinstance(res, InstallError):
                    results[aid] = f"install failed ({res.errmsg})"
                elif isinstance(res, DownloadError):
                    results[aid] = f"download failed ({res.status_code or res.exception})"
                else:
                    results[aid] = "unknown result"
            for aid in ids:
                results.setdefault(aid, "not downloaded")
            on_done(results)

        try:
            download_addons(mw, mw.addonManager, ids, on_done=done)
        except Exception as e:  # noqa: BLE001 - the engine must always get an answer
            on_done({aid: str(e) for aid in ids})

    def ankiweb_info(self, ids: list[int]) -> dict[int, int] | None:
        """Which of these add-ons AnkiWeb serves (id -> last modified).
        Runs in a background thread."""
        import inspect

        from aqt.addons import fetch_update_info

        try:
            if list(inspect.signature(fetch_update_info).parameters)[:1] == ["client"]:
                # Anki 2.1.66 and older: fetch_update_info(client, ids) -> [dict]
                from anki.httpclient import HttpClient

                with HttpClient() as client:
                    return {int(item["id"]): 0 for item in fetch_update_info(client, list(ids))}
            return {info.id: info.modified for info in fetch_update_info(list(ids))}
        except Exception as e:  # noqa: BLE001 - offline or AnkiWeb unavailable
            self.log(f"AnkiWeb check failed: {e}")
            return None

    def install_code(
        self, folder: str, name: str, data: bytes, mod: int | None = None
    ) -> str | None:
        from aqt.addons import InstallError

        manifest = {"package": folder, "name": name}
        if mod:
            # Keeps an AnkiWeb add-on restored from a backup on its version,
            # so Anki's update check still works for it.
            manifest["mod"] = mod
        fd, path = tempfile.mkstemp(suffix=".ankiaddon")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            res = mw.addonManager.install(path, manifest=manifest)
        finally:
            os.remove(path)
        if isinstance(res, InstallError):
            return f"install failed ({res.errmsg})"
        return None

    def is_loaded(self, folder: str) -> bool:
        return folder in sys.modules

    def set_enabled(self, folder: str, enabled: bool) -> None:
        mw.addonManager.toggleEnabled(folder, enable=enabled)

    def set_update_enabled(self, folder: str, enabled: bool) -> None:
        meta = mw.addonManager.addonMeta(folder)
        meta["update_enabled"] = enabled
        mw.addonManager.writeAddonMeta(folder, meta)

    def write_config(self, folder: str, config: dict[str, Any] | None) -> None:
        meta = mw.addonManager.addonMeta(folder)
        if config is None:
            meta.pop("config", None)
        else:
            meta["config"] = config
        mw.addonManager.writeAddonMeta(folder, meta)

    def delete_addon(self, folder: str) -> None:
        # Anki moves it to the system trash, so a mistake can be undone.
        mw.addonManager.deleteAddon(folder)

    # Talking to the user
    ##########################################################################

    def confirm(self, text: str) -> bool:
        return askUser(text, parent=mw, title="Loadout")

    def choose_code_installs(self, items: list[tuple[str, str]]) -> set[str]:
        return gui.choose_code_installs(items)

    def review(self, items: list[dict[str, Any]]) -> dict[str, str]:
        return gui.review_differences(items)

    def request_sync(self) -> None:
        if sync_problems() or mw.media_syncer.is_syncing():
            return
        mw.media_syncer.start()

    def notify(self, title: str, lines: list[str], restart: bool) -> None:
        if restart:
            body = "<br>".join(lines) + "<br><br><b>Restart Anki to start using them.</b>"
            showInfo(body, parent=mw, title=title, textFormat="rich")
        else:
            tooltip(f"<b>{title}</b><br>" + "<br>".join(lines), period=8000, parent=mw)

    def log(self, text: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > LOG_LIMIT:
                os.replace(self._log_path, self._log_path + ".1")
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        except OSError:
            pass
