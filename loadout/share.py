"""Sharing add-ons with someone else as a .loadout file.

A .loadout file is a zip:

  manifest.json   which add-ons, their settings (logins removed), data index,
                  and optionally Anki's preferences
  blobs/*.zip     add-on code (exact copies) and data, as Loadout syncs them

Installing one goes through the same machinery as mirroring (engine
install_shared): AnkiWeb add-ons download fresh unless the file carries an
exact copy (local edits, pinned versions, not on AnkiWeb); code copies need
the receiver's approval; the receiver's own logins are kept. Nothing here
imports Anki.
"""

from __future__ import annotations

import json
import os
import zipfile
from typing import Any

from . import blobs, owners, scan

FORMAT = "loadout-share"
VERSION = 1
SUFFIX = ".loadout"


def shareable(engine: Any) -> list[dict[str, Any]]:
    """What the share dialog lists: every add-on on this computer."""
    entries = engine.local_entries()
    out = []
    reports = sorted(engine.reports, key=lambda r: owners.display_name(engine.d.addons_dir(), r.folder).lower())
    for r in reports:
        a = entries.get(f"a/{r.folder}")
        if a is None:
            continue  # excluded completely in Loadout's settings
        out.append({
            "folder": r.folder,
            "name": owners.display_name(engine.d.addons_dir(), r.folder),
            "ankiweb": r.ankiweb_id is not None,
            "exact": _needs_exact_copy(engine, a),
            "files": r.file_count,
            "edited": bool(r.edited_files),
            "kept_private": r.secret_keys,
        })
    return out


def _needs_exact_copy(engine: Any, a: dict[str, Any]) -> bool:
    """A fresh AnkiWeb download would not give the receiver the same thing."""
    if a.get("ankiweb_id") is None:
        return True
    known = engine.state["ankiweb"].get(str(a["ankiweb_id"]), {})
    return bool(
        a.get("info", {}).get("edited")
        or not a.get("update_enabled", True)
        or known.get("available") is False
    )


def build(
    engine: Any,
    out_path: str,
    picks: dict[str, bool],
    include_prefs: bool = False,
    exact_all: bool = False,
) -> dict[str, Any]:
    """Write a .loadout file. picks: add-on folder -> include its data files.
    Returns a short summary for the user."""
    addons_dir = engine.d.addons_dir()
    entries = engine.local_entries()
    items = []
    written: set[str] = set()
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:

        def add_blob(name: str, data_fn: Any) -> None:
            if name not in written:
                z.writestr(f"blobs/{name}", data_fn())
                written.add(name)

        for folder, with_data in picks.items():
            a = entries.get(f"a/{folder}")
            if a is None:
                continue
            a = dict(a)
            if a.get("code"):  # not from AnkiWeb: the code always travels
                name = blobs.code_blob_name(a["code"])
                add_blob(name, lambda f=folder: blobs.build_code_blob(addons_dir, f))
                a["blob"] = name
            elif exact_all or _needs_exact_copy(engine, a):
                code = scan.code_hash(addons_dir, folder)
                name = blobs.code_blob_name(code)
                add_blob(name, lambda f=folder: blobs.build_code_blob(addons_dir, f))
                a["code"], a["blob"] = code, name
            item: dict[str, Any] = {"folder": folder, "name": a.get("name") or folder, "a": a}
            f = entries.get(f"f/{folder}")
            if with_data and f and f.get("files"):
                f = dict(f)
                name = blobs.files_blob_name(f)
                add_blob(name, lambda f=folder, idx=f["files"]: blobs.build_files_blob(
                    os.path.join(addons_dir, f, "user_files"), idx))
                f["blob"] = name
                item["f"] = f
            k = entries.get(f"k/{folder}")
            if k and k.get("config"):
                item["k"] = k  # its settings kept in the collection
            d = entries.get(f"d/{folder}")
            if with_data and d and d.get("files"):
                d = dict(d)
                name = blobs.files_blob_name(d, "d")
                profile = engine.d.profile_dir()
                add_blob(name, lambda idx=d["files"]: blobs.build_files_blob(profile, idx))
                d["blob"] = name
                item["d"] = d
            items.append(item)
        prefs = {k: v for k, v in entries.items() if k.startswith("p/")} if include_prefs else {}
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created": engine.d.now(),
            "addons": items,
            "prefs": prefs,
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
    os.replace(tmp, out_path)
    return {"addons": len(items), "bytes": os.path.getsize(out_path)}


def read(path: str) -> dict[str, Any]:
    """The manifest of a .loadout file; raises ValueError if it isn't one."""
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as e:
        raise ValueError(f"not a Loadout file ({e})") from e
    if manifest.get("format") != FORMAT:
        raise ValueError("not a Loadout file")
    if manifest.get("version", 0) > VERSION:
        raise ValueError("made by a newer Loadout; update Loadout first")
    for item in manifest.get("addons", []):
        if not isinstance(item, dict) or not scan.safe_folder(item.get("folder")):
            raise ValueError("it names an add-on folder that isn't safe to install")
    return manifest


def describe(engine: Any, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for the install dialog: what each shared add-on would do here."""
    local = engine.local_entries()
    rows = []
    for item in manifest["addons"]:
        folder = item["folder"]
        mine = local.get(f"a/{folder}")
        what = []
        if item["a"].get("code"):
            what.append("exact copy" if item["a"].get("ankiweb_id") is not None else "code included")
        else:
            what.append("from AnkiWeb")
        what.append("settings")
        files = len((item.get("f") or {}).get("files", {})) + len((item.get("d") or {}).get("files", {}))
        if files:
            what.append(f"{files} data file{'s' if files != 1 else ''}")
        rows.append({
            "folder": folder,
            "name": item.get("name") or folder,
            "includes": ", ".join(what),
            "here": "installed: its settings will be replaced" if mine else "new",
        })
    return rows


def install(engine: Any, path: str, folders: list[str], include_prefs: bool) -> int:
    """Install the chosen add-ons (and preferences) from a .loadout file.
    Returns how many items needed changing."""
    manifest = read(path)
    chosen = [item for item in manifest["addons"] if item["folder"] in folders]
    changes: list[tuple[str, dict[str, Any]]] = []
    needed: set[str] = set()
    for item in chosen:
        folder = item["folder"]
        a = dict(item["a"])
        if a.get("ankiweb_id") is not None and a.get("code") and engine.installed(folder):
            if scan.code_hash(engine.d.addons_dir(), folder) == a["code"]:
                a.pop("code")  # already exactly this code
                a.pop("blob", None)
        changes.append((f"a/{folder}", a))
        if a.get("blob"):
            needed.add(a["blob"])
        if item.get("f"):
            changes.append((f"f/{folder}", item["f"]))
            needed.add(item["f"]["blob"])
        if item.get("k"):
            changes.append((f"k/{folder}", item["k"]))
        if item.get("d"):
            changes.append((f"d/{folder}", item["d"]))
            needed.add(item["d"]["blob"])
    if include_prefs:
        changes += sorted(manifest.get("prefs", {}).items())
    with zipfile.ZipFile(path) as z:
        for name in sorted(needed):
            if not engine.d.media_has(name):
                engine.d.media_write(name, z.read(f"blobs/{name}"))
    return engine.install_shared(changes)
