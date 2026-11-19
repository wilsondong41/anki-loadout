"""What Loadout mirrors and what stays on this device.

Every rule here is generic: it looks at names, extensions and sizes, never at
which add-on a file belongs to. Per-add-on exceptions come from the user's
config (see config.md).
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable

# Config keys and file names that look like logins or keys. "auth" must not
# match "author".
SECRET_NAME = re.compile(
    r"api[_-]?key|apikey|token|secret|passw(or)?d|credential|cookie|auth(?!or)"
    r"|bearer|private[_-]?key|access[_-]?key|session[_-]?id|licen[cs]e[_-]?key|serial|activation",
    re.I,
)
SECRET_EXT = {".pem", ".key", ".p12", ".pfx", ".env", ".keychain"}

# Bookkeeping an add-on's own sync keeps about *this* device.
SYNC_BOOKKEEPING = re.compile(r"sync[_-]?state|last[_-]?sync", re.I)

SKIP_DIR = re.compile(
    r"^(__pycache__|__MACOSX|\.git|\.svn|node_modules|site-packages|logs?|tmp|temp"
    r"|caches?|backups?)$|cache$|\.(dist|egg)-info$",
    re.I,
)
SKIP_NAME = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".lock", ".corrupt", ".swp", ".part"}
# A database's companion files are only meaningful next to the running copy.
SKIP_SUFFIXES = ("-wal", "-shm", "-journal")
# Compiled code that only runs on the OS it was built for.
BINARY_EXT = {".so", ".dylib", ".dll", ".pyd", ".whl", ".exe"}

# A file named as local ("local_config.json", "settings.local") belongs to one computer.
LOCAL_NAME = re.compile(r"(^|[_.\-])local([_.\-]|$)", re.I)

# Reason given for files excluded by the user's settings or because they keep
# changing by themselves; shared with other devices (see engine.kept_local).
KEPT_LOCAL = "kept on each computer"

# Files that never belong in an add-on's code bundle.
CODE_SKIP_TOP = {"user_files", "meta.json", "__pycache__", "__MACOSX", ".git"}


def is_secret_key(key: Any) -> bool:
    return bool(SECRET_NAME.search(str(key)))


def is_secret(key: Any, value: Any) -> bool:
    """A setting holding a login: secret-looking name and a value that could
    carry one. On/off switches and numbers (e.g. "enable_browser_cookies")
    are ordinary settings."""
    return is_secret_key(key) and not isinstance(value, (bool, int, float))


def strip_secrets(obj: Any, extra_keys: Iterable[str] = ()) -> Any:
    """Copy of an add-on config without anything that looks like a login.

    extra_keys are top-level keys the user asked to keep on this device."""
    extra = set(extra_keys)

    def walk(value: Any, top: bool) -> Any:
        if isinstance(value, dict):
            return {
                k: walk(v, False)
                for k, v in value.items()
                if not is_secret(k, v) and not (top and k in extra)
            }
        if isinstance(value, list):
            return [walk(v, False) for v in value]
        return value

    return walk(obj, True)


def restore_secrets(incoming: Any, local: Any, extra_keys: Iterable[str] = ()) -> Any:
    """Put this device's secret (and kept-local) values back into a config
    that arrived from another device."""
    extra = set(extra_keys)

    def walk(inc: Any, loc: Any, top: bool) -> Any:
        if not isinstance(inc, dict) or not isinstance(loc, dict):
            return inc
        out = dict(inc)
        for k, v in loc.items():
            if is_secret(k, v) or (top and k in extra):
                out[k] = v
            elif isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k] = walk(out[k], v, False)
        return out

    return walk(incoming, local, True)


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def skip_dir_reason(name: str, child_names: Iterable[str]) -> str | None:
    if SKIP_DIR.search(name):
        return "cache or temporary folder"
    if any(c.endswith((".dist-info", ".egg-info")) for c in child_names):
        return "bundled Python package"
    return None


def skip_file_reason(
    rel_path: str, size: int, max_bytes: int, user_patterns: Iterable[str] = ()
) -> str | None:
    """Why a user_files file stays on this device, or None to mirror it.

    rel_path uses forward slashes and starts with the add-on folder name,
    e.g. "1011095603/settings_Anking.json", so user patterns can target one
    add-on."""
    name = rel_path.rsplit("/", 1)[-1]
    ext = _ext(name)
    for pattern in user_patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return KEPT_LOCAL
    if LOCAL_NAME.search(name):
        return KEPT_LOCAL  # e.g. local_config.json: meant for this computer only
    if (
        name in SKIP_NAME or ext in SKIP_EXT or ".tmp" in name or "cache" in name.lower()
        or name.endswith(SKIP_SUFFIXES)
    ):
        return "cache or temporary file"
    if ext in BINARY_EXT:
        return "compiled for one operating system"
    if ext in SECRET_EXT or SECRET_NAME.search(name):
        return "looks like a login or key"
    if SYNC_BOOKKEEPING.search(name):
        return "the add-on's own sync bookkeeping"
    if size > max_bytes:
        return f"larger than {max_bytes // (1024 * 1024)} MB"
    return None


def skip_code_path(rel_path: str) -> bool:
    """True for paths left out of an add-on's code copy: its data, metadata,
    and leftovers (compiled caches, logs, temp and backup files)."""
    parts = rel_path.split("/")
    if parts[0] in CODE_SKIP_TOP:
        return True
    if any(p in ("__pycache__", "__MACOSX", ".git") for p in parts) or parts[-1] in SKIP_NAME:
        return True
    return _ext(parts[-1]) in SKIP_EXT
