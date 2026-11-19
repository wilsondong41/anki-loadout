"""Read this device's add-ons folder into Loadout entries.

Nothing here imports Anki, so it can be tested on its own. Each add-on gives
two entries:

  a/<folder>  install state: name, AnkiWeb id or code hash, enabled, config
  f/<folder>  index of mirrored user_files: {relative path: sha256}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import rules

ANKIWEB_ID = re.compile(r"^\d+$")
SYNC_HOOK = re.compile(r"sync_will_start|sync_did_finish")
MEDIA_WRITE = re.compile(r"media\.(dir|write_data|add_file)\(|col\.media\b|collection\.media\b")
DETECT_SKIP_DIRS = {"user_files", "lib", "libs", "vendor", "_vendor", "site-packages", "tests"}
# Installing an add-on writes all its files within moments; a file changed
# this long after the earliest one was edited afterwards.
EDIT_GAP_SECONDS = 600

# Entry fields that describe an entry but are not part of what gets mirrored.
# "name" is display only: Anki records different names for the same add-on
# depending on how it was installed.
UNHASHED = {"h", "t", "by", "blob", "info", "name", "prev", "fork"}


@dataclass
class Settings:
    """The parts of Loadout's config the scanner needs."""

    exclude_addons: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    exclude_config_keys: dict[str, list[str]] = field(default_factory=dict)
    self_sync: dict[str, bool] = field(default_factory=dict)
    # "when_needed", "always" or "never": when to keep an exact copy of an
    # AnkiWeb add-on's code instead of relying on a fresh download.
    backup_ankiweb_addons: str = "when_needed"
    max_file_mb: float = 20
    max_addon_mb: float = 50
    max_code_mb: float = 90

    @classmethod
    def from_config(cls, conf: dict[str, Any] | None) -> "Settings":
        conf = conf or {}
        known = {k: conf[k] for k in cls.__dataclass_fields__ if k in conf}
        return cls(**known)

    # Anki's media sync skips files over 100 MiB, so nothing larger (the zip
    # of an add-on's code or data) would ever reach another computer.
    MEDIA_LIMIT_MB = 95

    @property
    def max_file_bytes(self) -> int:
        return int(min(self.max_file_mb, self.MEDIA_LIMIT_MB) * 1024 * 1024)

    @property
    def max_addon_bytes(self) -> int:
        return int(min(self.max_addon_mb, self.MEDIA_LIMIT_MB) * 1024 * 1024)

    @property
    def max_code_bytes(self) -> int:
        return int(min(self.max_code_mb, self.MEDIA_LIMIT_MB) * 1024 * 1024)


@dataclass
class AddonReport:
    """What the status screen shows for one add-on."""

    folder: str
    name: str
    ankiweb_id: int | None
    enabled: bool
    self_sync: bool
    secret_keys: list[str]
    skipped_files: list[tuple[str, str]]
    file_count: int
    excluded: bool = False
    source: str = ""  # how a new device gets it, filled in by the engine
    edited_files: list[str] = field(default_factory=list)


def entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k not in UNHASHED}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def finish(entry: dict[str, Any]) -> dict[str, Any]:
    entry["h"] = entry_hash(entry)
    return entry


# Scans run several times per sync on Anki's main thread, so work is reused
# while files are unchanged (same size and modification time).
_SHA_CACHE: dict[str, tuple[int, int, str]] = {}  # path -> (size, mtime_ns, sha256)
_ADDON_CACHE: dict[tuple[str, str], tuple[Any, Any]] = {}  # (root, what) -> (signature, result)


def file_sha(path: str) -> str:
    st = os.stat(path)
    cached = _SHA_CACHE.get(path)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached[2]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _SHA_CACHE[path] = (st.st_size, st.st_mtime_ns, h.hexdigest())
    return h.hexdigest()


def _cached(root: str, what: str, signature: Any, compute: Any) -> Any:
    key = (root, what)
    hit = _ADDON_CACHE.get(key)
    if hit and hit[0] == signature:
        return hit[1]
    result = compute()
    _ADDON_CACHE[key] = (signature, result)
    return result


def read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# Names Windows refuses for a file or folder, whatever the extension.
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
VERSION_LINE = re.compile(r'^VERSION = "([^"]+)"', re.M)


def safe_folder(name: Any) -> bool:
    """Whether name, from synced or shared entries, is a plain add-on folder
    name on macOS and Windows alike: nothing that leads outside the add-ons
    folder (../, absolute paths, drive letters) or that Windows can't hold."""
    if not isinstance(name, str) or not name or len(name) > 200:
        return False
    if name.startswith(".") or name[-1] in ". " or any(c in name for c in '/\\:*?"<>|'):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    return name.split(".")[0].lower() not in WINDOWS_RESERVED


def loadout_version(addons_dir: str, folder: str) -> str | None:
    """The version of the copy of Loadout in this folder, or None if the
    folder isn't Loadout (known by its version.py and engine.py)."""
    path = os.path.join(addons_dir, folder)
    if not os.path.isfile(os.path.join(path, "engine.py")):
        return None
    try:
        with open(os.path.join(path, "version.py"), encoding="utf-8") as f:
            text = f.read(4096)
    except OSError:
        return None
    match = VERSION_LINE.search(text)
    return match.group(1) if match and text.startswith('"""Loadout') else None


def loadout_copies(addons_dir: str) -> list[str]:
    """Every folder that holds a copy of Loadout: installed from a file it is
    "loadout", from AnkiWeb a number, so one computer can end up with two."""
    try:
        names = sorted(os.listdir(addons_dir))
    except OSError:
        return []
    return [n for n in names if loadout_version(addons_dir, n) is not None]


def addon_folders(addons_dir: str, own_folder: str) -> list[str]:
    """Installed add-ons: folders with an __init__.py, minus Loadout itself
    (any copy of it)."""
    out = []
    for name in sorted(os.listdir(addons_dir)):
        path = os.path.join(addons_dir, name)
        if name == own_folder or name == "files_backup" or name.startswith("."):
            continue
        if loadout_version(addons_dir, name) is not None:
            continue
        if os.path.isfile(os.path.join(path, "__init__.py")):
            out.append(name)
    return out


def _walk(root: str):
    """os.walk that prunes skipped folders and reports why."""
    skipped: list[tuple[str, str]] = []
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        keep = []
        for d in sorted(dirnames):
            try:
                children = os.listdir(os.path.join(dirpath, d))
            except OSError:
                children = []
            reason = rules.skip_dir_reason(d, children)
            if reason:
                skipped.append((rel_dir + d + "/", reason))
            else:
                keep.append(d)
        dirnames[:] = keep
        files.extend(rel_dir + f for f in sorted(filenames))
    return files, skipped


def user_files_index(
    addons_dir: str, folder: str, settings: Settings
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    root = os.path.join(addons_dir, folder, "user_files")
    if not os.path.isdir(root):
        return {}, []
    files, skipped = _walk(root)
    index: dict[str, str] = {}
    total = 0
    for rel in files:
        path = os.path.join(root, rel)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        reason = rules.skip_file_reason(
            f"{folder}/{rel}", size, settings.max_file_bytes, settings.exclude_files
        )
        if reason is None and total + size > settings.max_addon_bytes:
            reason = f"add-on data over {settings.max_addon_mb:g} MB"
        if reason:
            skipped.append((rel, reason))
            continue
        total += size
        index[rel] = file_sha(path)
    return index, skipped


# What Anki itself keeps in a profile folder; anything else there was made
# by an add-on.
ANKI_PROFILE_ITEMS = {
    "collection.anki2", "collection.anki2-wal", "collection.anki2-shm", "collection.anki2-journal",
    "collection.media", "collection.media.db2", "collection.media.db2-wal", "collection.media.db2-shm",
    "collection.media.db", "collection.log", "backups", "deleted.txt", "media.trash",
}


def is_anki_profile_item(name: str) -> bool:
    """Whether a top-level name in the profile folder is Anki's own. macOS
    and Windows ignore case in file names, and Windows drops trailing dots
    and spaces, so this does too."""
    name = name.lower().rstrip(". ")
    return name in ANKI_PROFILE_ITEMS or name.startswith(("collection.", "."))


def profile_items(profile_dir: str) -> list[str]:
    """Top-level files and folders in the profile folder that Anki didn't make."""
    try:
        names = os.listdir(profile_dir)
    except OSError:
        return []
    return sorted(n for n in names if n not in ANKI_PROFILE_ITEMS and not n.startswith("."))


def profile_index(
    profile_dir: str, items: list[str], folder: str, settings: "Settings"
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Files under the given profile-folder items: {"Item/sub/file": sha256}."""
    index: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    total = 0
    for item in items:
        path = os.path.join(profile_dir, item)
        if os.path.isdir(path):
            reason = rules.skip_dir_reason(item, os.listdir(path))
            if reason:
                skipped.append((item + "/", reason))
                continue
            files, inner = _walk(path)
            skipped += [(f"{item}/{rel}", why) for rel, why in inner]
            files = [f"{item}/{rel}" for rel in files]
        else:
            files = [item]
        for rel in files:
            full = os.path.join(profile_dir, rel)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            reason = rules.skip_file_reason(f"{folder}/{rel}", size, settings.max_file_bytes, settings.exclude_files)
            if reason is None and total + size > settings.max_addon_bytes:
                reason = f"add-on data over {settings.max_addon_mb:g} MB"
            if reason:
                skipped.append((rel, reason))
                continue
            total += size
            index[rel] = file_sha(full)
    return index, skipped


def code_listing(addons_dir: str, folder: str) -> list[tuple[str, int, int]]:
    """(relative path, size, mtime_ns) of an add-on's code files, sorted."""
    root = os.path.join(addons_dir, folder)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        top = dirpath == root
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "__MACOSX", ".git")
                       and not (top and d in rules.CODE_SKIP_TOP)]
        # Slicing, not os.path.relpath: relpath was most of a scan's time.
        rel_dir = "" if top else dirpath[len(root) + 1:].replace(os.sep, "/") + "/"
        for f in filenames:
            path = os.path.join(dirpath, f)
            rel = rel_dir + f
            if rules.skip_code_path(rel):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((rel, st.st_size, st.st_mtime_ns))
    return sorted(out)


def code_files(addons_dir: str, folder: str) -> list[str]:
    return [rel for rel, _, _ in code_listing(addons_dir, folder)]


def code_hash(addons_dir: str, folder: str, listing: list | None = None) -> str:
    listing = listing if listing is not None else code_listing(addons_dir, folder)
    root = os.path.join(addons_dir, folder)

    def compute() -> str:
        h = hashlib.sha256()
        for rel, _, _ in listing:
            h.update(rel.encode("utf-8"))
            h.update(file_sha(os.path.join(root, rel)).encode("ascii"))
        return h.hexdigest()[:24]

    return _cached(root, "code_hash", tuple(listing), compute)


def edited_after_install(addons_dir: str, folder: str, listing: list | None = None) -> list[str]:
    """Code files changed after the add-on was installed (local patches)."""
    listing = listing if listing is not None else code_listing(addons_dir, folder)
    times = [(mtime / 1e9, rel) for rel, _, mtime in listing if rel != "manifest.json"]
    if not times:
        return []
    first = min(t for t, _ in times)
    return sorted(rel for t, rel in times if t - first > EDIT_GAP_SECONDS)


def detect_self_sync(addons_dir: str, folder: str, listing: list | None = None) -> bool:
    """Does this add-on sync its own data through Anki's media folder?

    Looks for a sync hook plus media-folder writes anywhere in its code."""
    listing = listing if listing is not None else code_listing(addons_dir, folder)
    root = os.path.join(addons_dir, folder)
    py = tuple(item for item in listing if item[0].endswith(".py"))
    return _cached(root, "self_sync", py, lambda: _scan_for_self_sync(root))


def _scan_for_self_sync(root: str) -> bool:
    hooks = media = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in DETECT_SKIP_DIRS and not rules.skip_dir_reason(d, ())
        ]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            hooks = hooks or bool(SYNC_HOOK.search(text))
            media = media or bool(MEDIA_WRITE.search(text))
            if hooks and media:
                return True
    return False


def secret_paths(obj: Any, prefix: str = "") -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if rules.is_secret(k, v):
                out.append(path)
            else:
                out.extend(secret_paths(v, path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(secret_paths(v, f"{prefix}[{i}]"))
    return out


def snapshot_addon(
    addons_dir: str, folder: str, settings: Settings, with_code_hash: bool = True
) -> tuple[dict[str, Any], dict[str, Any], AddonReport]:
    meta = read_json(os.path.join(addons_dir, folder, "meta.json"))
    manifest = read_json(os.path.join(addons_dir, folder, "manifest.json"))
    name = meta.get("name") or manifest.get("name") or folder
    ankiweb_id = int(folder) if ANKIWEB_ID.match(folder) else None
    keep_local = settings.exclude_config_keys.get(folder, [])
    raw_config = meta.get("config")
    listing = code_listing(addons_dir, folder)
    self_sync = settings.self_sync.get(folder)
    if self_sync is None:
        self_sync = detect_self_sync(addons_dir, folder, listing)

    a: dict[str, Any] = {
        "name": name,
        "enabled": not meta.get("disabled", False),
        "update_enabled": meta.get("update_enabled", True),
        "config": rules.strip_secrets(raw_config, keep_local) if raw_config is not None else None,
        "info": {"self_sync": self_sync, "version": meta.get("human_version")},
    }
    edited: list[str] = []
    if ankiweb_id is not None:
        a["ankiweb_id"] = ankiweb_id
        a["mod"] = int(meta.get("mod") or 0)
        edited = edited_after_install(addons_dir, folder, listing)
        a["info"]["edited"] = edited
    elif with_code_hash:
        a["code"] = code_hash(addons_dir, folder, listing)

    index, skipped = user_files_index(addons_dir, folder, settings)
    f = {"files": index}

    report = AddonReport(
        folder=folder,
        name=name,
        ankiweb_id=ankiweb_id,
        enabled=a["enabled"],
        self_sync=self_sync,
        secret_keys=secret_paths(raw_config) + [f"(kept local) {k}" for k in keep_local],
        skipped_files=skipped,
        file_count=len(index),
        edited_files=edited,
    )
    return finish(a), finish(f), report


def snapshot(
    addons_dir: str, own_folder: str, settings: Settings
) -> tuple[dict[str, dict[str, Any]], list[AddonReport]]:
    """All a/ and f/ entries for this device."""
    entries: dict[str, dict[str, Any]] = {}
    reports: list[AddonReport] = []
    for folder in addon_folders(addons_dir, own_folder):
        if folder in settings.exclude_addons:
            meta = read_json(os.path.join(addons_dir, folder, "meta.json"))
            reports.append(AddonReport(
                folder, meta.get("name") or folder,
                int(folder) if ANKIWEB_ID.match(folder) else None,
                not meta.get("disabled", False), False, [], [], 0, excluded=True,
            ))
            continue
        a, f, report = snapshot_addon(addons_dir, folder, settings)
        entries[f"a/{folder}"] = a
        entries[f"f/{folder}"] = f
        reports.append(report)
    return entries, reports
