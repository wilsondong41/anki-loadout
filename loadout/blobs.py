"""Files Loadout keeps in the media folder, which AnkiWeb syncs file by file.

  _loadout_e_<key hash>.json   one synced entry (what an add-on/setting should be)
  _loadout_f_<hash>.zip        an add-on's data files
  _loadout_d_<hash>.zip        an add-on's files in the Anki profile folder
  _loadout_c_<hash>.zip        an add-on's code

Names start with an underscore so Anki's Check Media never deletes them. Zip
names contain a content hash so two devices never write different data under
one name.

Entries live here rather than in the collection's config because AnkiWeb
syncs the config as one block, taken whole from whichever computer changed
anything last; media files sync one by one.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from typing import Any

from . import scan

PREFIX = "_loadout_"
ENTRY_PREFIX = f"{PREFIX}e_"
BLOB_PREFIXES = (f"{PREFIX}f_", f"{PREFIX}c_", f"{PREFIX}d_")


def entry_file_name(key: str) -> str:
    return f"{ENTRY_PREFIX}{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}.json"


def encode_entry(key: str, entry: dict[str, Any]) -> bytes:
    return json.dumps({"key": key, "entry": entry}, sort_keys=True).encode("utf-8")


def decode_entry(data: bytes) -> tuple[str, dict[str, Any]] | None:
    try:
        payload = json.loads(data.decode("utf-8"))
        key, entry = payload["key"], payload["entry"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(key, str) or not isinstance(entry, dict):
        return None
    return key, entry


def files_blob_name(f_entry: dict, kind: str = "f") -> str:
    return f"{PREFIX}{kind}_{f_entry['h'][:20]}.zip"


def code_blob_name(code_hash: str) -> str:
    return f"{PREFIX}c_{code_hash[:20]}.zip"


def build_files_blob(root: str, index: dict[str, str]) -> bytes:
    """A zip of the indexed files under root (an add-on's user_files, or the
    profile folder)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(index):
            z.write(os.path.join(root, rel), rel)
    return buf.getvalue()


def build_code_blob(addons_dir: str, folder: str) -> bytes:
    root = os.path.join(addons_dir, folder)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in scan.code_files(addons_dir, folder):
            z.write(os.path.join(root, rel), rel)
    return buf.getvalue()


def safe_target(root: str, rel: str) -> str:
    """Path for rel inside root; refuses anything that escapes root."""
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"unsafe path in Loadout data: {rel!r}")
    return target


def write_files(
    data: bytes, user_files: str, wanted: dict[str, str]
) -> list[str]:
    """Write each wanted file (path -> sha256) from the zip into user_files.

    Returns the paths written. A file whose content does not match its
    expected hash is refused."""
    written = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        for rel, sha in sorted(wanted.items()):
            if rel not in names:
                raise ValueError(f"{rel!r} missing from Loadout data")
            content = z.read(rel)
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError(f"{rel!r} does not match its recorded hash")
            target = safe_target(user_files, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".loadout-tmp"
            with open(tmp, "wb") as f:
                f.write(content)
            os.replace(tmp, target)
            written.append(rel)
    return written
