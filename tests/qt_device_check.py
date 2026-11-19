"""Check AnkiDevice's cached listing of Loadout's media files. Needs Anki's
Python (imports aqt):

    QT_QPA_PLATFORM=offscreen <Anki's python> tests/qt_device_check.py
"""

import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loadout import anki_device, blobs  # noqa: E402

media = tempfile.mkdtemp(prefix="loadout-media-")
logs = tempfile.mkdtemp(prefix="loadout-log-")


def write_data(name, data):
    with open(os.path.join(media, name), "wb") as f:
        f.write(data)
    return name


def trash_files(names):
    for n in names:
        os.remove(os.path.join(media, n))


anki_device.mw = types.SimpleNamespace(col=types.SimpleNamespace(media=types.SimpleNamespace(
    dir=lambda: media, write_data=write_data, trash_files=trash_files)))

listings = []
real_listdir = os.listdir


def counting_listdir(path):
    if path == media:
        listings.append(path)
    return real_listdir(path)


anki_device.os.listdir = counting_listdir
dev = anki_device.AnkiDevice(own_folder="loadout", log_path=os.path.join(logs, "log.txt"))
P = blobs.PREFIX


def names():
    return sorted(n for n, _ in dev.media_list(P))


def add_outside(name, keep_folder_time):
    """A file arriving from a sync; keep_folder_time simulates a drive whose
    folder modification time doesn't change (network drive, exFAT)."""
    st = os.stat(media)
    with open(os.path.join(media, name), "wb") as f:
        f.write(b"x")
    if keep_folder_time:
        os.utime(media, ns=(st.st_atime_ns, st.st_mtime_ns))


try:
    for i in range(3):
        add_outside(f"card{i}.jpg", False)
    add_outside(P + "f_one", False)
    assert names() == [P + "f_one"], names()

    # Loadout's own write: seen at once, without listing the folder again.
    before = len(listings)
    dev.media_write(P + "f_two", b"2")
    assert names() == [P + "f_one", P + "f_two"], names()
    assert len(listings) == before, "own write made Loadout list the folder again"

    # Loadout's own delete.
    dev.media_trash([P + "f_one"])
    assert names() == [P + "f_two"], names()

    # A file from a sync on a normal drive: the folder time changes.
    add_outside(P + "f_three", False)
    assert P + "f_three" in names(), names()

    # A drive that doesn't update folder times: seen after the sync clears the cache.
    add_outside(P + "f_four", True)
    dev.forget_media_listing()
    assert P + "f_four" in names(), names()

    # Entry files written through write_remote-style replace stay listed.
    dev.media_write(blobs.ENTRY_PREFIX + "k.json", b"{}")
    assert blobs.ENTRY_PREFIX + "k.json" in dev._entry_names(media)
    print("device listing cache OK")
finally:
    anki_device.os.listdir = real_listdir
    shutil.rmtree(media, ignore_errors=True)
    shutil.rmtree(logs, ignore_errors=True)
