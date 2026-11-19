"""Simulated multi-computer tests for Loadout's engine.

FakeCloud behaves like AnkiWeb's media sync, which carries everything Loadout
stores: files sync one by one, a computer first downloads what changed on the
server (the server's copy wins over an unsynced local change), then uploads
its own changes. Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loadout import blobs, engine, owners, rules, scan, share  # noqa: E402

CLOCK = [1_000_000.0]


def tick() -> float:
    CLOCK[0] += 1
    return CLOCK[0]


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# AnkiWeb's add-on catalog: id -> what downloading it installs.
CATALOG = {
    111: {"name": "Heatmap", "mod": 500, "default": {"color": "green", "authToken": None}},
    222: {
        "name": "Selfsyncer",
        "mod": 600,
        "code": "from aqt import gui_hooks\n"
                "gui_hooks.sync_will_start.append(lambda: mw.col.media.dir())\n",
    },
    333: {"name": "Old thing", "mod": 700},
}


class FakeCloud:
    def __init__(self) -> None:
        self.media: dict[str, tuple[bytes, int]] = {}  # name -> (data, version)
        self.version = 0
        # AnkiWeb sometimes keeps the last upload of a file instead: a
        # computer's unsynced change then replaces the newer server copy.
        self.last_upload_wins = False
        # The collection's settings on AnkiWeb, synced as one block before
        # media. Only Loadout's early copy of the setup is modelled.
        self.early_setup = None

    @property
    def config(self) -> dict[str, tuple[dict, int]]:
        """Synced Loadout entries, decoded: key -> (entry, version)."""
        out = {}
        for name, (data, ver) in self.media.items():
            if name.startswith(blobs.ENTRY_PREFIX):
                key, entry = blobs.decode_entry(data)
                out[key] = (entry, ver)
        return out

    def set_entry(self, key: str, entry: dict) -> None:
        self.version += 1
        self.media[blobs.entry_file_name(key)] = (blobs.encode_entry(key, entry), self.version)

    def sync(self, dev: "FakeDevice", media: bool = True, closing: bool = False) -> None:
        """One sync, calling Loadout the way Anki does: push before the
        collection sync, pull after it, and pull again after media sync.

        closing: the sync Anki runs as it closes (after profile_will_close)."""
        dev.dirty.clear()
        if closing:
            dev.engine.closing = True  # auto sync on: held data waits for this sync
        dev.engine.push()
        self.sync_collection(dev)
        if closing:
            dev.engine.apply_held_files(closing_sync=True)
        else:
            dev.engine.pull("sync")
        if media:
            self.sync_media(dev)
            if closing:
                dev.engine.apply_held_files(closing_sync=True)
            else:
                dev.engine.pull("media")
        dev.engine.closing = False  # the next sync is a fresh start

    def sync_collection(self, dev: "FakeDevice") -> None:
        key = engine.EARLY_SETUP_KEY
        mine = dev.col_settings.get(key)
        if mine != dev.early_setup_synced:
            self.early_setup = copy.deepcopy(mine)  # changed here: this computer's block wins
        elif self.early_setup is not None:
            dev.col_settings[key] = copy.deepcopy(self.early_setup)
        dev.early_setup_synced = copy.deepcopy(dev.col_settings.get(key))

    def sync_media(self, dev: "FakeDevice", entries_only: bool = False) -> None:
        for name, (data, ver) in self.media.items():
            if entries_only and not name.startswith(blobs.ENTRY_PREFIX):
                continue
            if dev.media_seen.get(name) != ver:
                if self.last_upload_wins and name in dev.dirty_media:
                    continue  # uploaded below, over the server copy
                dev.media[name] = data  # server copy wins
                dev.media_time[name] = CLOCK[0]
                dev.media_seen[name] = ver
                dev.dirty_media.discard(name)
        if entries_only:
            return
        for name in list(dev.media):
            if name in dev.media_seen and name not in self.media and name not in dev.dirty_media:
                del dev.media[name]  # deleted on the server
        for name in sorted(dev.dirty_media):
            if name in dev.media:
                self.version += 1
                self.media[name] = (dev.media[name], self.version)
                dev.media_seen[name] = self.version
        dev.dirty_media.clear()
        for name in dev.trashed:
            self.media.pop(name, None)
        dev.trashed.clear()


class FakeDevice:
    def __init__(self, root: str, profile: str = "Anking", settings: scan.Settings | None = None, **opts) -> None:
        self.root = root
        self._addons = os.path.join(root, "addons21")
        os.makedirs(self._addons, exist_ok=True)
        self.profile = profile
        self.dirty: set[str] = set()  # entry keys written in the current sync
        self.media: dict[str, bytes] = {}
        self.media_time: dict[str, float] = {}  # name -> when it was written here
        self.media_seen: dict[str, int] = {}  # name -> server version last synced
        self.dirty_media: set[str] = set()
        self.trashed: list[str] = []
        self.prefs = {"p/meta": {"theme": 0, "uiScale": 1.0}, "p/profile": {}, "p/col": {"reviewing": {}}}
        self.answers: list[bool] = []
        self.asked: list[str] = []
        self.approve: set[str] | None = None
        self.code_prompts: list[list[tuple[str, str]]] = []
        self.notes: list[tuple[str, list[str], bool]] = []
        self.downloads: list[int] = []
        self.loaded: set[str] = set()  # add-ons running in this session
        self.col_settings: dict = {"rollover": 4, "curDeck": 1}  # the collection's settings
        self.early_setup_synced = None  # Loadout's early copy as of the last collection sync
        self._profile = os.path.join(root, "profile")
        os.makedirs(self._profile, exist_ok=True)
        self.reviews: list[list[dict]] = []  # what the review dialog showed
        self.review_answers: dict[str, str] = {}  # folder -> choice; others "decide later"
        self.download_fails: set[int] = set()
        self.offline = False
        self.engine = engine.Engine(
            self, settings or scan.Settings(), os.path.join(root, "state.json"), **opts
        )

    # Device protocol
    def addons_dir(self): return self._addons
    def own_folder(self): return "loadout"
    def profile_name(self): return self.profile
    def computer_name(self): return os.path.basename(self.root)
    def anki_version(self): return getattr(self, "anki", "26.9.3")
    def collection_settings(self): return copy.deepcopy(self.col_settings)
    def set_collection_setting(self, key, value): self.col_settings[key] = copy.deepcopy(value)
    def is_anki_setting(self, key): return key in ("rollover", "curDeck", "fsrs")
    def profile_dir(self): return self._profile
    def now(self): return tick()

    def read_remote(self):
        out = {}
        for name, data in self.media.items():
            if name.startswith(blobs.ENTRY_PREFIX):
                decoded = blobs.decode_entry(data)
                if decoded:
                    out[decoded[0]] = decoded[1]
        return out

    def write_remote(self, key, entry):
        self.media_write(blobs.entry_file_name(key), blobs.encode_entry(key, entry))
        self.dirty.add(key)

    def media_has(self, name): return name in self.media
    def media_read(self, name): return self.media[name]

    def media_write(self, name, data):
        self.media[name] = data
        self.media_time[name] = CLOCK[0]
        self.dirty_media.add(name)

    def media_list(self, prefix):
        return [(n, self.media_time.get(n, 0.0)) for n in self.media if n.startswith(prefix)]

    def media_trash(self, names):
        for n in names:
            self.media.pop(n, None)
            self.trashed.append(n)

    def get_prefs(self): return copy.deepcopy(self.prefs)
    def set_prefs(self, key, value): self.prefs[key] = copy.deepcopy(value)

    def request_sync(self):
        pass

    def review(self, items):
        self.reviews.append(items)
        return {d["folder"]: self.review_answers[d["folder"]] for d in items
                if d["folder"] in self.review_answers}

    def is_loaded(self, folder):
        return folder in self.loaded

    def ankiweb_info(self, ids):
        if self.offline:
            return None
        return {i: CATALOG[i]["mod"] for i in ids if i in CATALOG}

    def install_ankiweb(self, ids, on_done):
        results = {}
        for aid in ids:
            self.downloads.append(aid)
            item = CATALOG.get(aid)
            if not item or aid in self.download_fails:
                results[aid] = "download failed (404)"
                continue
            self.install_from_catalog(aid)
            results[aid] = None
        on_done(results)

    def install_from_catalog(self, aid: int, **meta_extra) -> str:
        item = CATALOG[aid]
        folder = os.path.join(self._addons, str(aid))
        write(os.path.join(folder, "__init__.py"), item.get("code", "# add-on\n"))
        if "default" in item:
            write(os.path.join(folder, "config.json"), json.dumps(item["default"]))
        meta = scan.read_json(os.path.join(folder, "meta.json"))
        meta.update({"name": item["name"], "mod": item["mod"]}, **meta_extra)
        write(os.path.join(folder, "meta.json"), json.dumps(meta))
        return folder

    def install_code(self, folder, name, data, mod=None):
        target = os.path.join(self._addons, folder)
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(target)
        meta = scan.read_json(os.path.join(target, "meta.json"))
        meta["name"] = name
        if mod:
            meta["mod"] = mod
        write(os.path.join(target, "meta.json"), json.dumps(meta))
        return None

    def _meta(self, folder):
        return scan.read_json(os.path.join(self._addons, folder, "meta.json"))

    def _write_meta(self, folder, meta):
        write(os.path.join(self._addons, folder, "meta.json"), json.dumps(meta))

    def set_enabled(self, folder, enabled):
        meta = self._meta(folder)
        meta["disabled"] = not enabled
        self._write_meta(folder, meta)

    def set_update_enabled(self, folder, enabled):
        meta = self._meta(folder)
        meta["update_enabled"] = enabled
        self._write_meta(folder, meta)

    def write_config(self, folder, config):
        meta = self._meta(folder)
        if config is None:
            meta.pop("config", None)
        else:
            meta["config"] = config
        self._write_meta(folder, meta)

    def delete_addon(self, folder):
        shutil.rmtree(os.path.join(self._addons, folder))

    def confirm(self, text):
        self.asked.append(text)
        return self.answers.pop(0) if self.answers else True

    def choose_code_installs(self, items):
        self.code_prompts.append(items)
        return {f for f, _ in items} if self.approve is None else set(self.approve)

    def notify(self, title, lines, restart):
        self.notes.append((title, lines, restart))

    def log(self, text):
        pass

    # Test conveniences
    def path(self, *parts):
        return os.path.join(self._addons, *parts)

    def config_of(self, folder):
        return self._meta(folder).get("config")


class LoadoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loadout-test-")
        self.cloud = FakeCloud()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def pushed(dev: FakeDevice) -> set[str]:
        """What a computer uploaded in its last sync, besides its own version stamp."""
        return {k for k in dev.dirty if not k.startswith("m/version/")}

    def device(self, name: str, **kw) -> FakeDevice:
        return FakeDevice(os.path.join(self.tmp, name), **kw)

    def make_main_computer(self, **kw) -> FakeDevice:
        """Computer A: the setup to copy, joined and uploaded."""
        a = self.device("A", **kw)
        a.install_from_catalog(111, config={"color": "red", "authToken": "A-secret"})
        uf = a.path("111", "user_files")
        write(os.path.join(uf, "settings.json"), '{"weeks": 12}')
        write(os.path.join(uf, "credentials.json"), '{"token": "x"}')
        write(os.path.join(uf, "cache", "thumb.png"), "img")
        write(os.path.join(uf, "native.so"), "elf")
        write(os.path.join(uf, "vendored", "pkg-1.0.dist-info", "METADATA"), "m")
        write(os.path.join(uf, "vendored", "pkg", "core.py"), "x")
        write(os.path.join(uf, "old.json.bak"), "x")

        a.install_from_catalog(222)
        write(a.path("222", "user_files", "settings_Anking.json"), '{"sync": true}')
        write(a.path("222", "user_files", "sync_state.json"), '{"hash": "abc"}')

        a.install_from_catalog(333, disabled=True)

        write(a.path("my_tool", "__init__.py"), "print('mine')\n")
        write(a.path("my_tool", "helper.py"), "X = 1\n")
        write(a.path("my_tool", "user_files", "data.json"), '{"n": 1}')
        write(a.path("loadout", "__init__.py"), "# Loadout itself\n")
        write(a.path("ankimon_backups", "backup.json"), "{}")  # data folder, not an add-on

        a.prefs["p/meta"]["theme"] = 2
        a.answers = [True]  # "is this the computer to copy?"
        self.cloud.sync(a)
        self.assertTrue(a.engine.state["joined"])
        self.cloud.sync(a)  # uploads
        return a

    def join(self, name: str = "B", **kw) -> FakeDevice:
        b = self.device(name, **kw)
        b.answers = [True]
        self.cloud.sync(b)
        return b

    # Rules
    ##########################################################################

    def test_rules(self) -> None:
        self.assertTrue(rules.is_secret_key("authToken"))
        self.assertTrue(rules.is_secret_key("http_api_key"))
        self.assertFalse(rules.is_secret_key("author"))
        self.assertFalse(rules.is_secret_key("sessions_until_long_break"))
        self.assertFalse(rules.is_secret("enable_browser_cookies", True))
        self.assertTrue(rules.is_secret("authToken", None))
        conf = {"color": "red", "authToken": "s", "nested": {"password": "p", "k": 1}}
        stripped = rules.strip_secrets(conf, ["color"])
        self.assertEqual(stripped, {"nested": {"k": 1}})
        back = rules.restore_secrets({"nested": {"k": 2}}, conf, ["color"])
        self.assertEqual(back, {"nested": {"k": 2, "password": "p"}, "color": "red", "authToken": "s"})
        mb = 20 * 1024 * 1024
        self.assertIsNone(rules.skip_file_reason("1/settings.json", 10, mb))
        self.assertIn("login", rules.skip_file_reason("1/credentials.json", 10, mb))
        self.assertIn("operating system", rules.skip_file_reason("1/x.dylib", 10, mb))
        self.assertIn("sync", rules.skip_file_reason("1/sync_state.json", 10, mb))
        self.assertIn("MB", rules.skip_file_reason("1/huge.db", mb + 1, mb))
        self.assertEqual(rules.skip_file_reason("1/a.db", 1, mb, ["1/*.db"]), rules.KEPT_LOCAL)
        self.assertIn("cache", rules.skip_file_reason("1/icon_dict_cache.json", 10, mb))
        self.assertEqual(rules.skip_file_reason("1/local_config.json", 10, mb), rules.KEPT_LOCAL)
        self.assertIsNone(rules.skip_file_reason("1/locale.json", 10, mb))

    def test_snapshot_skips_and_detects(self) -> None:
        a = self.make_main_computer()
        entries, reports = scan.snapshot(a.addons_dir(), "loadout", scan.Settings())
        self.assertEqual(sorted(entries["f/111"]["files"]), ["settings.json"])
        self.assertNotIn("authToken", entries["a/111"]["config"])
        self.assertTrue(entries["a/222"]["info"]["self_sync"])
        self.assertFalse(entries["a/111"]["info"]["self_sync"])
        self.assertIn("code", entries["a/my_tool"])
        folders = {r.folder for r in reports}
        self.assertEqual(folders, {"111", "222", "333", "my_tool"})

    def test_pull_scans_only_when_something_arrived(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        scans = []
        real = b.engine.local_entries
        b.engine.local_entries = lambda: scans.append(1) or real()
        b.engine.pull("media")
        self.assertEqual(scans, [])  # nothing new: no add-on scan
        write(a.path("111", "user_files", "settings.json"), '{"weeks": 9}')
        self.cloud.sync(a)
        self.cloud.sync_media(b)
        b.engine.pull("media")
        self.assertEqual(len(scans), 1)
        self.assertEqual(read(b.path("111", "user_files", "settings.json")), '{"weeks": 9}')

    # Setting up computers
    ##########################################################################

    def test_new_computer_gets_everything(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        self.assertTrue(b.engine.state["joined"])
        for folder in ("111", "222", "333", "my_tool"):
            self.assertTrue(os.path.isfile(b.path(folder, "__init__.py")), folder)
        self.assertNotIn("loadout", os.listdir(b.addons_dir()))
        self.assertEqual(b.config_of("111"), {"color": "red"})  # A's token stayed on A
        self.assertEqual(read(b.path("111", "user_files", "settings.json")), '{"weeks": 12}')
        for skipped in ("credentials.json", "cache", "native.so", "vendored", "old.json.bak"):
            self.assertFalse(os.path.exists(b.path("111", "user_files", skipped)), skipped)
        # Self-syncing add-on: seeded on a new computer, minus its sync bookkeeping.
        self.assertEqual(read(b.path("222", "user_files", "settings_Anking.json")), '{"sync": true}')
        self.assertFalse(os.path.exists(b.path("222", "user_files", "sync_state.json")))
        self.assertTrue(b._meta("333")["disabled"])
        self.assertEqual(read(b.path("my_tool", "helper.py")), "X = 1\n")
        self.assertEqual(read(b.path("my_tool", "user_files", "data.json")), '{"n": 1}')
        self.assertEqual(b.code_prompts, [[("my_tool", "my_tool")]])
        self.assertEqual(b.prefs["p/meta"]["theme"], 2)
        self.assertTrue(any(restart for _, _, restart in b.notes))
        # Nothing on A changed because B joined.
        a_before = {k: e["h"] for k, e in a.read_remote().items() if not k.startswith("m/version/")}
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(a.config_of("111"), {"color": "red", "authToken": "A-secret"})
        self.assertEqual({k: e["h"] for k, e in a.read_remote().items() if not k.startswith("m/version/")}, a_before)
        self.assertEqual(self.pushed(a), set())

    def test_new_computer_sets_up_before_media_arrives(self) -> None:
        # A new computer's media sync downloads years of card media first;
        # the collection sync brings the setup well before.
        a = self.make_main_computer()
        c = self.device("C")
        c.answers = [True]
        self.cloud.sync(c, media=False)  # collection synced, media still downloading
        self.assertTrue(c.engine.state["joined"])
        self.assertIn(111, c.downloads)
        self.assertEqual(c.config_of("111")["color"], "red")
        self.assertTrue(c._meta("333")["disabled"])
        self.assertEqual(c.prefs["p/meta"]["theme"], 2)
        self.assertFalse(os.path.exists(c.path("my_tool")))  # its code comes with media
        self.assertIn("Still downloading", " ".join(c.notes[-1][1]))
        self.cloud.sync(c, media=False)
        self.assertEqual(self.pushed(c), set())  # nothing uploaded over what hasn't arrived
        self.cloud.sync(c)
        self.assertEqual(read(c.path("my_tool", "helper.py")), "X = 1\n")
        self.assertEqual(read(c.path("my_tool", "user_files", "data.json")), '{"n": 1}')
        self.assertEqual(read(c.path("111", "user_files", "settings.json")), '{"weeks": 12}')
        self.quiet(a, c)

    def test_early_copy_never_overrides_synced_media(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        old = copy.deepcopy(b.col_settings[engine.EARLY_SETUP_KEY])
        a.write_config("111", {"color": "newer", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        b.col_settings[engine.EARLY_SETUP_KEY] = old  # a stale block from another computer
        b.engine.pull("sync")
        self.assertEqual(b.config_of("111")["color"], "newer")
        self.assertNotIn(engine.EARLY_SETUP_KEY, b.engine.local_entries().get("k/111", {}).get("config", {}))

    def test_oversized_early_copy_is_cleared_not_left_stale(self) -> None:
        a = self.make_main_computer()
        self.assertIsNotNone(a.col_settings.get(engine.EARLY_SETUP_KEY))
        limit = engine.EARLY_SETUP_MAX_BYTES
        engine.EARLY_SETUP_MAX_BYTES = 10
        try:
            a.write_config("111", {"color": "huge", "authToken": "A-secret"})
            self.cloud.sync(a)
        finally:
            engine.EARLY_SETUP_MAX_BYTES = limit
        self.assertIsNone(a.col_settings[engine.EARLY_SETUP_KEY])
        c = self.device("C")
        c.answers = [True]
        self.cloud.sync(c, media=False)
        self.assertEqual(c.asked, [])  # waits for media instead of an old copy
        self.cloud.sync(c)
        self.assertEqual(c.config_of("111")["color"], "huge")

    def test_copied_anki_folder_gets_its_own_identity(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        shutil.copytree(a.root, os.path.join(self.tmp, "A2"))  # e.g. moved to a new computer
        a2 = self.device("A2")
        a2.media, a2.media_seen = dict(a.media), dict(a.media_seen)
        a2.col_settings = copy.deepcopy(a.col_settings)
        a2.early_setup_synced = copy.deepcopy(a.early_setup_synced)
        self.assertEqual(a2.engine.state["device_id"], a.engine.state["device_id"])
        self.cloud.sync(a2)
        self.cloud.sync(a)
        self.assertNotEqual(a2.engine.state["device_id"], a.engine.state["device_id"])
        self.quiet(a, a2, b)
        computers = {e["computer"] for k, (e, _) in self.cloud.config.items() if k.startswith("m/version/")}
        self.assertEqual(computers, {"A", "A2", "B"})

    def test_size_limits_stay_within_what_media_sync_carries(self) -> None:
        big = scan.Settings(max_file_mb=500, max_addon_mb=500, max_code_mb=500)
        for limit in (big.max_file_bytes, big.max_addon_bytes, big.max_code_bytes):
            self.assertLess(limit, 100 * 1024 * 1024)
        self.assertEqual(scan.Settings().max_file_bytes, 20 * 1024 * 1024)

    def test_new_computer_with_own_defaults_does_not_overwrite(self) -> None:
        self.make_main_computer()
        c = self.device("C")
        c.install_from_catalog(111, config={"color": "green"})
        c.answers = [True]
        self.cloud.sync(c)
        self.assertEqual(c.config_of("111"), {"color": "red"})
        self.cloud.sync(c)
        self.assertEqual(self.cloud.config["a/111"][0]["config"], {"color": "red"})

    def test_declining_setup_never_uploads(self) -> None:
        self.make_main_computer()
        before = {k: v[0]["h"] for k, v in self.cloud.config.items()}
        c = self.device("C")
        c.install_from_catalog(111, config={"color": "green"})
        c.answers = [False]
        self.cloud.sync(c)
        self.cloud.sync(c)
        self.assertFalse(c.engine.state["joined"])
        self.assertEqual(len(c.asked), 1)  # asked once, not every sync
        self.assertEqual({k: v[0]["h"] for k, v in self.cloud.config.items()}, before)

    def test_empty_cloud_first_device_declined(self) -> None:
        a = self.device("A")
        a.install_from_catalog(111)
        a.answers = [False]
        self.cloud.sync(a)
        self.cloud.sync(a)
        self.assertEqual(self.cloud.config, {})

    # Mirroring
    ##########################################################################

    def test_edits_mirror_and_secrets_stay(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        conf = b.config_of("111")
        conf.update(color="blue", authToken="B-secret")
        b.write_config("111", conf)
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(a.config_of("111"), {"color": "blue", "authToken": "A-secret"})
        self.assertEqual(b.config_of("111"), {"color": "blue", "authToken": "B-secret"})

    def test_edits_to_different_addons_both_survive(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        a.write_config("111", {"color": "purple", "authToken": "A-secret"})
        write(b.path("my_tool", "user_files", "data.json"), '{"n": 2}')
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.cloud.sync(a)
        for dev in (a, b):
            self.assertEqual(dev.config_of("111")["color"], "purple")
            self.assertEqual(read(dev.path("my_tool", "user_files", "data.json")), '{"n": 2}')

    def test_same_item_newest_edit_wins(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        a.write_config("111", {"color": "older", "authToken": "A-secret"})
        a.engine.push()  # A saves its edit...
        b.write_config("111", {"color": "newer"})
        b.engine.push()  # ...then B saves a newer one
        self.cloud.sync(a)  # A's older edit reaches AnkiWeb first
        self.cloud.sync(b)  # B's upload loses to it, so B keeps its own and resends
        self.assertEqual(b.config_of("111"), {"color": "newer"})
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(a.config_of("111"), {"color": "newer", "authToken": "A-secret"})

    def b_saves_settings_before_media(self, b: FakeDevice) -> None:
        """B saves settings and syncs: its push runs before media sync has
        brought A's newer code, so its entry carries B's old code, and
        AnkiWeb keeps that upload over A's."""
        self.cloud.last_upload_wins = True
        b.write_config("my_tool", {"voice": "Ava"})
        b.engine.push()
        self.cloud.sync_media(b)
        b.engine.pull("media")
        self.cloud.last_upload_wins = False

    def test_settings_saved_before_media_keep_newer_code(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        write(a.path("my_tool", "helper.py"), "X = 2\n")
        self.cloud.sync(a)
        self.b_saves_settings_before_media(b)
        self.assertEqual(self.cloud.config["a/my_tool"][0]["config"], {"voice": "Ava"})
        self.cloud.sync(a)
        a.engine.pull("open")  # any later pull before A's next push
        self.assertEqual(read(a.path("my_tool", "helper.py")), "X = 2\n")  # A's code stays
        self.assertEqual(a.config_of("my_tool"), {"voice": "Ava"})  # B's settings arrive
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(read(b.path("my_tool", "helper.py")), "X = 2\n")
        self.assertEqual(b.config_of("my_tool"), {"voice": "Ava"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(self.pushed(a) | self.pushed(b), set())

    def test_stale_entry_not_applied_by_a_later_pull(self) -> None:
        # 2026-10-22: B's stale entry was on AnkiWeb; A then saved newer code,
        # but its upload lost to B's entry. The first pull kept A's code,
        # the next one installed B's old code.
        a = self.make_main_computer()
        b = self.join()
        write(a.path("my_tool", "helper.py"), "X = 2\n")
        self.cloud.sync(a)
        self.b_saves_settings_before_media(b)
        write(a.path("my_tool", "helper.py"), "X = 3\n")
        self.cloud.sync(a)
        a.engine.pull("open")
        self.assertEqual(read(a.path("my_tool", "helper.py")), "X = 3\n")
        self.assertEqual(a.config_of("my_tool"), {"voice": "Ava"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(read(b.path("my_tool", "helper.py")), "X = 3\n")
        self.assertEqual(b.config_of("my_tool"), {"voice": "Ava"})

    def test_code_changed_on_both_keeps_each_computers_own(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        write(a.path("my_tool", "helper.py"), "X = 2\n")
        self.cloud.sync(a)
        self.cloud.last_upload_wins = True
        write(b.path("my_tool", "helper.py"), "X = 9\n")  # B never saw A's edit
        b.engine.push()
        self.cloud.sync_media(b)
        self.cloud.last_upload_wins = False
        self.cloud.sync(a)
        a.engine.pull("open")
        self.assertEqual(read(a.path("my_tool", "helper.py")), "X = 2\n")
        self.cloud.sync(a)
        b.approve = set()
        self.cloud.sync(b)  # B is asked before A's code replaces its own
        self.assertEqual(b.code_prompts[-1], [("my_tool", "my_tool")])
        self.assertEqual(read(b.path("my_tool", "helper.py")), "X = 9\n")
        # Once settled, an edit made on top of the other's code still mirrors.
        b.engine.forget_failures()
        b.approve = None
        self.cloud.sync(b)
        self.assertEqual(read(b.path("my_tool", "helper.py")), "X = 2\n")
        write(b.path("my_tool", "helper.py"), "X = 10\n")
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(read(a.path("my_tool", "helper.py")), "X = 10\n")

    def test_removal_and_disable_mirror(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        a.delete_addon("333")
        a.set_enabled("111", False)
        os.remove(a.path("111", "user_files", "settings.json"))
        self.cloud.sync(a)
        b.review_answers = {"333": "remove_here"}
        self.cloud.sync(b)
        self.assertEqual(b.reviews[-1][0]["kind"], "removed_elsewhere")
        self.assertFalse(os.path.exists(b.path("333")))
        self.assertTrue(b._meta("111")["disabled"])
        self.assertFalse(os.path.exists(b.path("111", "user_files", "settings.json")))

    def test_new_addon_on_second_computer_reaches_first(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        b.install_from_catalog(333)  # reinstall happens to be harmless
        write(b.path("tool2", "__init__.py"), "y = 2\n")
        self.cloud.sync(b)
        self.cloud.sync(b)
        a.review_answers = {"tool2": "install"}
        self.cloud.sync(a)
        self.assertEqual(a.reviews[-1], [{"folder": "tool2", "name": "tool2", "kind": "new_elsewhere"}])
        self.assertEqual(read(a.path("tool2", "__init__.py")), "y = 2\n")

    def test_self_syncing_addon_left_alone_after_seed(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        write(a.path("222", "user_files", "settings_Anking.json"), '{"sync": true, "theme": "dark"}')
        write(b.path("222", "user_files", "settings_Anking.json"), '{"sync": true, "theme": "b-own"}')
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertIn("b-own", read(b.path("222", "user_files", "settings_Anking.json")))

    def test_profile_name_warning(self) -> None:
        self.make_main_computer()
        b = self.join(profile="User 1")
        self.assertTrue(any("profile name" in t for t, _, _ in b.notes))
        self.cloud.sync(b)
        self.assertEqual(self.cloud.config["m/profile"][0]["name"], "Anking")

    # Failure paths
    ##########################################################################

    def test_data_waits_for_media_sync(self) -> None:
        self.make_main_computer()
        b = self.device("B")
        b.answers = [True]
        b.engine.push()
        b.engine.pull("sync")  # collection synced; Loadout's files still downloading
        self.assertEqual(b.asked, [])  # so no "nothing saved" question yet
        self.cloud.sync_media(b, entries_only=True)  # entries arrive, zips not yet
        b.engine.pull("media")
        self.assertTrue(os.path.isfile(b.path("111", "__init__.py")))
        self.assertFalse(os.path.exists(b.path("111", "user_files", "settings.json")))
        self.assertFalse(os.path.exists(b.path("my_tool")))
        self.assertTrue(any("Still downloading" in " ".join(lines) for _, lines, _ in b.notes))
        b.dirty.clear()
        b.engine.push()
        self.assertEqual(self.pushed(b), set())  # nothing half-applied gets uploaded back
        self.cloud.sync_media(b)
        b.engine.pull("media")
        self.assertEqual(read(b.path("111", "user_files", "settings.json")), '{"weeks": 12}')
        self.assertTrue(os.path.isfile(b.path("my_tool", "helper.py")))

    def test_first_device_question_waits_for_media(self) -> None:
        self.make_main_computer()
        c = self.device("C")
        c.answers = [False]
        self.cloud.sync(c, media=False)  # the setup is known from the collection sync
        self.assertEqual(len(c.asked), 1)
        self.assertIn("found your saved Anki setup", c.asked[0])  # not "nothing found"
        self.cloud.sync(c)
        self.assertEqual(len(c.asked), 1)

    def add_unlisted_addon(self, dev: FakeDevice) -> None:
        """An add-on with an AnkiWeb-style number that AnkiWeb does not serve
        (removed, private, or installed from a file someone shared)."""
        write(dev.path("999", "__init__.py"), "# shared by a friend\n")
        write(dev.path("999", "meta.json"), json.dumps({"name": "Friend's tool", "mod": 42}))

    def test_addon_not_on_ankiweb_installs_from_backup(self) -> None:
        a = self.make_main_computer()
        self.add_unlisted_addon(a)
        self.cloud.sync(a)
        self.assertIn("c/999", self.cloud.config)
        b = self.join()
        self.assertNotIn(999, b.downloads)  # a matching copy exists, so no doomed download
        self.assertEqual(read(b.path("999", "__init__.py")), "# shared by a friend\n")
        self.assertEqual(b._meta("999")["mod"], 42)
        self.assertIn(("999", "Friend's tool"), b.code_prompts[0])
        self.cloud.sync(b)
        self.assertEqual(self.pushed(b), set())  # identical, nothing to send back

    def test_every_kind_of_addon_reaches_new_computer(self) -> None:
        a = self.make_main_computer()
        self.add_unlisted_addon(a)
        self.cloud.sync(a)
        b = self.join()
        here = set(scan.addon_folders(a.addons_dir(), "loadout"))
        there = set(scan.addon_folders(b.addons_dir(), "loadout"))
        self.assertEqual(here, there)
        self.assertEqual(here, {"111", "222", "333", "999", "my_tool"})

    def test_always_backup_copies_exact_code(self) -> None:
        a = self.make_main_computer(settings=scan.Settings(backup_ankiweb_addons="always"))
        write(a.path("111", "__init__.py"), "# locally edited\n")
        self.cloud.sync(a)
        b = self.join()
        self.assertEqual(b.downloads, [])
        self.assertEqual(read(b.path("111", "__init__.py")), "# locally edited\n")
        self.assertEqual(b._meta("111")["mod"], 500)

    def test_download_failure_falls_back_without_ping_pong(self) -> None:
        a = self.make_main_computer(settings=scan.Settings(backup_ankiweb_addons="always"))
        old = dict(self.cloud.config["c/111"][0], mod=1)
        self.cloud.set_entry("c/111", old)  # the backup is a version behind
        b = self.device("B")
        b.answers = [True]
        b.download_fails = {111}
        self.cloud.sync(b)
        self.assertEqual(b.downloads, [111])
        self.assertTrue(os.path.isfile(b.path("111", "__init__.py")))
        self.assertEqual(b.config_of("111"), {"color": "red"})
        for dev in (b, a, b, a):
            self.cloud.sync(dev)
        self.assertEqual(self.cloud.config["a/111"][0]["mod"], 500)  # older copy never uploaded
        self.assertEqual(b.downloads, [111])  # and not retried every sync

    def edit_later(self, path: str, text: str) -> None:
        write(path, text)
        later = os.path.getmtime(path) + scan.EDIT_GAP_SECONDS + 60
        os.utime(path, (later, later))

    def test_locally_edited_ankiweb_addon_arrives_exactly(self) -> None:
        a = self.make_main_computer()
        self.edit_later(a.path("111", "__init__.py"), "# patched here\n")
        self.cloud.sync(a)
        self.assertIn("c/111", self.cloud.config)
        b = self.join()
        self.assertNotIn(111, b.downloads)
        self.assertEqual(read(b.path("111", "__init__.py")), "# patched here\n")
        self.assertEqual(b._meta("111")["mod"], 500)
        self.cloud.sync(b)
        self.assertEqual(self.pushed(b), set())
        a.engine.local_entries()
        source = {r.folder: r.source for r in a.engine.reports}
        self.assertEqual(source["111"], "Exact copy (edited here: 1 files)")

    def test_pinned_addon_keeps_its_version(self) -> None:
        a = self.make_main_computer()
        a.set_update_enabled("222", False)
        self.cloud.sync(a)
        b = self.join()
        self.assertNotIn(222, b.downloads)
        self.assertFalse(b._meta("222")["update_enabled"])
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(self.pushed(b), set())

    def test_edits_upload_at_once_but_constant_churn_backs_off(self) -> None:
        a = self.make_main_computer()
        path = a.path("111", "__init__.py")
        uploads = []
        for n in range(5):
            self.edit_later(path, f"# edit {n}\n")
            write(a.path("111", "debug.log"), f"log {n}")  # logs never count
            self.cloud.sync(a)
            uploads.append(self.cloud.config["c/111"][0]["h"])
        self.assertEqual(len(set(uploads[:3])), 3)  # real edits go out immediately
        self.assertEqual(uploads[3], uploads[2])  # then it backs off for the day
        CLOCK[0] += 86400 + 1
        self.cloud.sync(a)
        self.assertNotEqual(self.cloud.config["c/111"][0]["h"], uploads[2])

    def test_failed_install_without_backup_is_not_pushed_back(self) -> None:
        a = self.make_main_computer(settings=scan.Settings(backup_ankiweb_addons="never"))
        self.add_unlisted_addon(a)
        self.cloud.sync(a)
        self.assertNotIn("c/999", self.cloud.config)
        b = self.join()
        self.assertFalse(os.path.exists(b.path("999")))
        self.assertIn("a/999", b.engine.state["failed"])
        self.cloud.sync(b)
        self.assertEqual(b.downloads.count(999), 1)
        self.assertFalse(self.cloud.config["a/999"][0].get("del"))

    def test_backup_withdrawn_when_addon_removed(self) -> None:
        a = self.make_main_computer()
        self.add_unlisted_addon(a)
        self.cloud.sync(a)
        a.delete_addon("999")
        self.cloud.sync(a)
        self.assertTrue(self.cloud.config["c/999"][0].get("del"))

    def test_offline_check_keeps_previous_answer(self) -> None:
        a = self.make_main_computer()
        self.add_unlisted_addon(a)
        a.offline = True
        self.cloud.sync(a)
        self.assertNotIn("c/999", self.cloud.config)  # unknown yet: no guessing
        a.offline = False
        engine.ANKIWEB_CHECK_SECONDS, saved = 0, engine.ANKIWEB_CHECK_SECONDS
        try:
            self.cloud.sync(a)
        finally:
            engine.ANKIWEB_CHECK_SECONDS = saved
        self.assertIn("c/999", self.cloud.config)

    def test_status_says_how_each_addon_arrives(self) -> None:
        a = self.make_main_computer(settings=scan.Settings(max_code_mb=0.000001))
        self.add_unlisted_addon(a)
        self.cloud.sync(a)
        a.engine.local_entries()
        source = {r.folder: r.source for r in a.engine.reports}
        self.assertEqual(source["111"], "AnkiWeb download")
        self.assertIn("Too big", source["999"])
        self.assertIn("Too big", source["my_tool"])

    def test_declined_code_is_not_pushed_back(self) -> None:
        a = self.make_main_computer()
        b = self.device("B")
        b.answers = [True]
        b.approve = set()
        self.cloud.sync(b)
        self.assertFalse(os.path.exists(b.path("my_tool")))
        write(a.path("my_tool", "helper.py"), "X = 2\n")
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(len(b.code_prompts), 2)  # asked again only because the code changed
        self.assertNotIn("del", self.cloud.config["a/my_tool"][0])

    # Running add-ons and notices
    ##########################################################################

    def running_b(self) -> tuple[FakeDevice, FakeDevice]:
        a = self.make_main_computer()
        b = self.join()
        b.loaded = set(scan.addon_folders(b.addons_dir(), "loadout"))  # B restarted
        b.notes.clear()
        return a, b

    def test_running_addon_data_waits_until_close(self) -> None:
        a, b = self.running_b()
        data = b.path("my_tool", "user_files", "data.json")
        write(a.path("my_tool", "user_files", "data.json"), '{"n": 5}')
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(read(data), '{"n": 1}')  # not written under the running add-on
        self.assertIn("f/my_tool", b.engine.state["held_files"])
        write(data, '{"n": 1, "running": "saved old state"}')  # saves as its window closes
        self.cloud.sync(b, closing=True)
        self.assertEqual(read(data), '{"n": 5}')
        self.assertEqual(self.cloud.config["f/my_tool"][0]["files"]["data.json"],
                         scan.file_sha(data))  # its old state was never uploaded
        self.assertEqual(b.engine.state["held_files"], {})

    def test_closing_sync_data_written_before_quit(self) -> None:
        a, b = self.running_b()
        write(a.path("my_tool", "user_files", "data.json"), '{"n": 9}')
        self.cloud.sync(a)
        self.cloud.sync(b, closing=True)  # the change arrives in the closing sync
        self.assertEqual(read(b.path("my_tool", "user_files", "data.json")), '{"n": 9}')
        self.cloud.sync(b)
        self.assertEqual(self.pushed(b), set())

    def test_held_without_closing_sync(self) -> None:
        a, b = self.running_b()
        write(a.path("my_tool", "user_files", "data.json"), '{"n": 6}')
        self.cloud.sync(a)
        self.cloud.sync(b)
        b.engine.apply_held_files()  # auto sync off: only profile_will_close runs
        self.assertEqual(read(b.path("my_tool", "user_files", "data.json")), '{"n": 6}')

    def test_day_to_day_changes_never_pop_up(self) -> None:
        a, b = self.running_b()
        a.write_config("111", {"color": "teal", "authToken": "A-secret"})
        write(a.path("my_tool", "user_files", "data.json"), '{"n": 7}')
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.cloud.sync(b)
        self.assertEqual(b.notes, [])  # settings and data update quietly
        self.assertEqual(b.config_of("111")["color"], "teal")
        last = b.engine.state["last_changes"]["lines"]
        self.assertIn("Updated settings for Heatmap", last)
        self.assertTrue(any("updates when you close Anki" in x for x in last))
        b.review_answers = {}
        a.set_enabled("111", False)  # but a change to the add-on itself is announced
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(len(b.notes), 1)
        self.assertFalse(b.notes[0][2])  # a small notice, not a dialog
        self.assertIn("Disabled Heatmap", b.notes[0][1])

    def test_setup_is_the_only_dialog(self) -> None:
        self.make_main_computer()
        b = self.join()
        self.assertEqual([d for _, _, d in b.notes].count(True), 1)

    # Anyone's setup
    ##########################################################################

    def test_oversized_settings_stay_local(self) -> None:
        a = self.make_main_computer()
        a.write_config("111", {"color": "red", "history": "x" * (engine.MAX_ENTRY_BYTES + 10)})
        self.cloud.sync(a)
        self.assertEqual(self.cloud.config["a/111"][0]["config"], {"color": "red"})
        self.assertIn("a/111", a.engine.state["entry_too_big"])
        a.engine.local_entries()
        source = {r.folder: r.source for r in a.engine.reports}
        self.assertIn("Settings too large", source["111"])
        a.write_config("111", {"color": "small again"})
        self.cloud.sync(a)
        self.assertEqual(self.cloud.config["a/111"][0]["config"], {"color": "small again"})
        self.assertNotIn("a/111", a.engine.state["entry_too_big"])

    def test_second_profile_on_same_computer_stays_out(self) -> None:
        a = self.make_main_computer()
        before = {k: v[0]["h"] for k, v in self.cloud.config.items()}
        a.engine.inactive_reason = "another profile mirrors this computer"
        a.write_config("111", {"color": "other person's"})
        self.cloud.sync(a)
        self.assertEqual({k: v[0]["h"] for k, v in self.cloud.config.items()}, before)
        a.engine.apply_held_files(closing_sync=True)  # no-op as well

    def test_many_addons_and_odd_folder_names(self) -> None:
        a = self.make_main_computer()
        for i in range(60):
            folder = f"tool {i} (copy) été"
            write(a.path(folder, "__init__.py"), f"N = {i}\n")
            write(a.path(folder, "user_files", "sub dir", "data #1.json"), str(i))
        self.cloud.sync(a)
        self.cloud.sync(a)
        b = self.join()
        self.assertEqual(read(b.path("tool 59 (copy) été", "user_files", "sub dir", "data #1.json")), "59")
        self.assertEqual(len(b.code_prompts[0]), 61)  # one approval list, not 61 dialogs

    # Settings in the collection, files in the profile folder
    ##########################################################################

    def test_addon_settings_in_the_collection_follow(self) -> None:
        a = self.make_main_computer()
        write(a.path("222", "sidebar.py"), 'mw.col.set_config(f"modern_menu_{part}_layout_mode", mode)\n'
                                          'mw.col.set_config("onigiri_secret_token", t)\n')
        b = self.join()
        write(b.path("222", "sidebar.py"), read(a.path("222", "sidebar.py")))  # same add-on code on both
        a.col_settings.update({
            "modern_menu_ready_layout_mode": "hidden",  # Onigiri's "hide Ready to go"
            "onigiri_secret_token": "A's login",
            "rollover": 2,  # Anki's own: AnkiWeb's business
            "someone_elses_key": 1,  # no add-on mentions it
        })
        b.col_settings["onigiri_secret_token"] = "B's login"
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.col_settings["modern_menu_ready_layout_mode"], "hidden")
        self.assertEqual(b.col_settings["onigiri_secret_token"], "B's login")
        self.assertEqual(b.col_settings["rollover"], 4)
        self.assertNotIn("someone_elses_key", b.col_settings)
        b.col_settings["modern_menu_ready_layout_mode"] = "full"  # changed back on B
        self.quiet(b, a)
        self.assertEqual(a.col_settings["modern_menu_ready_layout_mode"], "full")

    def test_addon_files_in_the_profile_folder_follow(self) -> None:
        a = self.make_main_computer()
        write(a.path("111", "storage.py"), 'DATA = Path(mw.pm.profileFolder()) / "MyAddon_Data"\n')
        b = self.join()
        write(os.path.join(a.profile_dir(), "MyAddon_Data", "maps", "map1.json"), '{"nodes": 3}')
        write(os.path.join(a.profile_dir(), "MyAddon_Data", "notes.db"), "db")
        write(os.path.join(a.profile_dir(), "MyAddon_Data", "notes.db-wal"), "in-flight")
        write(os.path.join(a.profile_dir(), "stranger.txt"), "nobody's")
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(read(os.path.join(b.profile_dir(), "MyAddon_Data", "maps", "map1.json")), '{"nodes": 3}')
        self.assertTrue(os.path.isfile(os.path.join(b.profile_dir(), "MyAddon_Data", "notes.db")))
        self.assertFalse(os.path.exists(os.path.join(b.profile_dir(), "MyAddon_Data", "notes.db-wal")))
        self.assertFalse(os.path.exists(os.path.join(b.profile_dir(), "stranger.txt")))
        self.quiet(a, b)

    def test_setting_mentioned_by_two_addons_goes_to_its_namesake(self) -> None:
        texts = {"onigiri": 'conf = mw.col.get_config("heatmap")', "review_heatmap": 'x = col.get_config("heatmap")'}
        names = {"onigiri": "Onigiri", "review_heatmap": "Review Heatmap"}
        self.assertEqual(owners.name_owner("heatmap", texts, names), "review_heatmap")
        self.assertIsNone(owners.name_owner("heatmap", texts))  # without names: too unclear

    def test_licence_keys_stay_private(self) -> None:
        self.assertTrue(rules.is_secret("license_key", "ABCD-1234"))
        self.assertTrue(rules.is_secret("serial", "XYZ"))
        self.assertTrue(rules.is_secret("activation_code", "123"))

    def test_share_carries_collection_settings_and_profile_files(self) -> None:
        a = self.make_main_computer()
        write(a.path("111", "storage.py"), 'mw.col.set_config("heatmap_colors", c)\nroot = "MyAddon_Data"\n')
        a.col_settings["heatmap_colors"] = ["red", "blue"]
        write(os.path.join(a.profile_dir(), "MyAddon_Data", "x.json"), "1")
        path = os.path.join(self.tmp, "mine.loadout")
        share.build(a.engine, path, {"111": True})
        friend = self.device("Friend")
        share.install(friend.engine, path, ["111"], include_prefs=False)
        self.assertEqual(friend.col_settings["heatmap_colors"], ["red", "blue"])
        self.assertEqual(read(os.path.join(friend.profile_dir(), "MyAddon_Data", "x.json")), "1")

    # Versions, menu settings, add-ons kept behind
    ##########################################################################

    def test_different_loadout_versions_are_pointed_out(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        self.assertFalse(any("versions differ" in t for t, _, _ in a.notes + b.notes))  # same version
        old = scan.finish({"version": "0.9.0", "computer": "Old PC"})
        old.update(t=CLOCK[0], by="old-pc")
        self.cloud.set_entry("m/version/old-pc", old)
        a.notes.clear()
        self.cloud.sync(a)
        self.cloud.sync(a)
        warnings = [lines for t, lines, _ in a.notes if "versions differ" in t]
        self.assertEqual(len(warnings), 1)  # once per session
        self.assertIn("Old PC", warnings[0][0])
        self.assertIn("older", warnings[0][0])
        self.assertEqual(a.engine.other_versions()[0]["computer"], "Old PC")

    def test_reinstalled_computer_leaves_no_phantom_version(self) -> None:
        # B deleted Loadout and installed it again (e.g. from AnkiWeb): its old
        # install's stamp, under the same computer name, is still synced.
        a = self.make_main_computer()
        b = self.join()
        old = scan.finish({"version": "1.7.6", "computer": "B", "anki": "25.02"})
        old.update(t=CLOCK[0] - 3600, by="b-before")
        self.cloud.set_entry("m/version/b-before", old)
        self.cloud.sync(b)  # B's current stamp goes up
        self.cloud.sync(a)
        self.assertEqual(a.engine.other_versions(), [])  # B's newest stamp is current
        self.assertEqual(a.engine.other_anki_versions(), [])
        self.assertEqual(b.engine.other_versions(), [])  # B's own earlier install
        self.assertFalse(any("versions differ" in t for t, _, _ in a.notes + b.notes))

    def test_different_anki_versions_are_noted_quietly(self) -> None:
        a = self.make_main_computer()
        b = self.device("B")
        b.anki = "25.9.2"
        b.answers = [True]
        self.cloud.sync(b)
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(a.engine.other_anki_versions(), [{"computer": "B", "anki": "25.9.2"}])
        self.assertFalse(any("Anki" in " ".join(lines) for _, lines, _ in a.notes))  # no pop-up
        b.anki = "26.9.1"  # same release, different patch: not worth mentioning
        b.engine.save()
        b2 = engine.Engine(b, scan.Settings(), os.path.join(b.root, "state.json"))
        b.engine = b2
        self.cloud.sync(b)
        self.cloud.sync(a)
        self.assertEqual(a.engine.other_anki_versions(), [])

    def test_menu_favorites_follow_to_other_computers(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        b.notes.clear()
        a.prefs["p/menus"] = {"enabled": True, "include_anki": True,
                              "favorites": [{"path": ["Find cards from UWorld test"], "owner": "1939527074"}]}
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.prefs["p/menus"], a.prefs["p/menus"])
        self.assertEqual(b.notes, [])  # quietly
        self.quiet(a, b)

    def test_pinned_and_edited_addons_show_newer_versions(self) -> None:
        a = self.make_main_computer()
        a.set_update_enabled("111", False)  # kept on purpose
        a.engine.record_ankiweb([111, 222, 333], {111: 900, 222: 900, 333: 900})
        behind = a.engine.behind_on_ankiweb()
        self.assertEqual(list(behind), ["111"])  # 222 and 333 update themselves
        a.engine.record_ankiweb([111], {111: 400})  # AnkiWeb older than what's installed
        self.assertEqual(a.engine.behind_on_ankiweb(), {})

    # Sharing with a friend
    ##########################################################################

    def test_share_everything_to_a_friend(self) -> None:
        a = self.make_main_computer()
        write(a.path("111", "__init__.py"), "# patched by me\n")
        later = os.path.getmtime(a.path("111", "__init__.py")) + scan.EDIT_GAP_SECONDS + 60
        os.utime(a.path("111", "__init__.py"), (later, later))
        rows = {r["folder"]: r for r in share.shareable(a.engine)}
        self.assertTrue(rows["111"]["exact"] and rows["my_tool"]["exact"])
        self.assertFalse(rows["333"]["exact"])
        path = os.path.join(self.tmp, "mine.loadout")
        summary = share.build(a.engine, path, {f: True for f in rows}, include_prefs=True)
        self.assertEqual(summary["addons"], 4)

        friend = self.device("Friend")
        friend.install_from_catalog(111, config={"color": "green", "authToken": "friend-token"})
        write(friend.path("111", "user_files", "notes.txt"), "friend's own file")
        manifest = share.read(path)
        described = {r["folder"]: r for r in share.describe(friend.engine, manifest)}
        self.assertIn("replaced", described["111"]["here"])
        self.assertEqual(described["my_tool"]["here"], "new")
        n = share.install(friend.engine, path, [i["folder"] for i in manifest["addons"]], include_prefs=True)
        self.assertGreater(n, 0)
        self.assertEqual(read(friend.path("111", "__init__.py")), "# patched by me\n")  # exact copy
        self.assertEqual(friend.config_of("111"), {"color": "red", "authToken": "friend-token"})
        self.assertEqual(read(friend.path("111", "user_files", "settings.json")), '{"weeks": 12}')
        self.assertEqual(read(friend.path("111", "user_files", "notes.txt")), "friend's own file")
        self.assertTrue(os.path.isfile(friend.path("my_tool", "helper.py")))
        self.assertTrue(friend._meta("333")["disabled"])
        self.assertEqual(friend.prefs["p/meta"]["theme"], 2)
        self.assertNotIn("A-secret", open(path, "rb").read().decode("latin-1"))  # no logins in the file
        self.assertTrue(any(dialog for _, _, dialog in friend.notes))  # asks for a restart
        self.assertTrue(friend.code_prompts)  # code copies need the friend's approval

    def test_share_one_addon_without_data(self) -> None:
        a = self.make_main_computer()
        path = os.path.join(self.tmp, "one.loadout")
        share.build(a.engine, path, {"111": False})
        manifest = share.read(path)
        self.assertEqual([i["folder"] for i in manifest["addons"]], ["111"])
        self.assertNotIn("f", manifest["addons"][0])
        friend = self.device("Friend")
        share.install(friend.engine, path, ["111"], include_prefs=False)
        self.assertEqual(friend.config_of("111"), {"color": "red"})
        self.assertFalse(os.path.exists(friend.path("111", "user_files", "settings.json")))
        self.assertEqual(friend.downloads, [111])  # plain AnkiWeb add-on: downloaded fresh

    def test_shared_install_mirrors_to_the_friends_other_computers(self) -> None:
        a = self.make_main_computer()
        path = os.path.join(self.tmp, "mine.loadout")
        share.build(a.engine, path, {"my_tool": True})
        friend_cloud = FakeCloud()
        f1 = self.device("F1")
        f1.answers = [True]
        friend_cloud.sync(f1)  # the friend's own first computer
        share.install(f1.engine, path, ["my_tool"], include_prefs=False)
        friend_cloud.sync(f1)
        self.assertIn("a/my_tool", friend_cloud.config)  # goes to the friend's own setup

    def test_not_a_loadout_file(self) -> None:
        bad = os.path.join(self.tmp, "bad.loadout")
        write(bad, "hello")
        with self.assertRaises(ValueError):
            share.read(bad)

    # Menu survey and the Mirror checkbox
    ##########################################################################

    def test_menu_owner_found_from_addon_files(self) -> None:
        a = self.device("A")
        write(a.path("111", "__init__.py"), 'action = QAction("Show &Sidebar", mw)\n')
        write(a.path("222", "__init__.py"), "# mentions show sidebar in a comment only\n")
        write(a.path("333", "i18n", "en.json"), '{"find": "Find cards from UWorld test"}')
        write(a.path("333", "__init__.py"), "x = 1\n")
        write(a.path("333", "lib", "pkg-1.0.dist-info", "METADATA"), "x")
        write(a.path("333", "lib", "pkg", "words.py"), "'settings'\n")  # bundled library: ignored
        folders = scan.addon_folders(a.addons_dir(), "loadout")
        found = owners.find_owners(["Show &Sidebar", "Find cards from UWorld test…", "Settings", "OK"],
                                   a.addons_dir(), folders)
        self.assertEqual(found["Show &Sidebar"], ["111"])  # quoted beats a mere mention
        self.assertEqual(found["Find cards from UWorld test…"], ["333"])
        self.assertEqual(found["Settings"], [])
        self.assertEqual(found["OK"], [])  # too short to match safely
        self.assertEqual(owners.clean_label("&Tools\tCtrl+T"), "Tools")
        self.assertEqual(owners.clean_label("Rock && Roll..."), "Rock & Roll")

    def test_explaining_menu_items_from_code(self) -> None:
        a = self.device("A")
        write(a.path("t1", "__init__.py"), """
from aqt import mw
from aqt.qt import QAction, QDialog
from aqt.utils import openLink

ADDON_TITLE = "Pin Window"

def always_on_top():
    flags = mw.windowFlags()
    mw.setWindowFlags(flags)
    mw.show()

def open_options():
    show_dialog()

def show_dialog():
    d = QDialog(mw)
    d.exec()

def rate():
    openLink("https://ankiweb.net/shared/review/1")

def stop_speech():
    player.stop()

def toggle_timer():
    timer.stop()

class Board:
    def build(self):
        self.addMenu(MENU, _translate(T, "&Sync and update"), self.start_sync, "Shift+S")
        items = [("Prep Station", open_prep)]
        pin = QAction(ADDON_TITLE, mw)
        pin.triggered.connect(always_on_top)

    def start_sync(self):
        upload()

def open_prep():
    pass

on_top = QAction("Enable Always on top", mw)
on_top.setCheckable(True)
on_top.triggered.connect(always_on_top)
options = QAction("⚙️Advanced Options", mw)
qconnect(options.triggered, lambda: open_options())
menu.addAction("Rate this", rate)
stop = QAction("Stop Reading", mw, triggered=stop_speech)
timer = QAction(
    "Pomodoro", mw)
timer.triggered.connect(
    toggle_timer)
""")
        explain = lambda label: (owners.explain_action(a.addons_dir(), "t1", label) or {}).get("kind")
        self.assertEqual(explain("Enable Always on top"), "toggle")  # re-showing after flags is a switch
        self.assertEqual(explain("⚙️Advanced Options"), "window")  # followed through open_options
        self.assertEqual(explain("Rate this"), "link")
        self.assertEqual(owners.explain_action(a.addons_dir(), "t1", "Rate this")["detail"],
                         "https://ankiweb.net/shared/review/1")
        self.assertEqual(explain("Stop Reading"), "action")
        self.assertEqual(explain("&Sync and update"), "action")  # helper call
        self.assertEqual(explain("Prep Station"), "window")  # list of items; "open" name
        self.assertEqual(explain("Pin Window"), "toggle")  # label kept in a constant
        self.assertEqual(explain("Pomodoro"), "toggle")  # wiring split over lines
        self.assertIsNone(owners.explain_action(a.addons_dir(), "t1", "Built at runtime"))

    def test_mirror_checkbox_off_and_on(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        b.engine.decide({"111": "keep_here"})  # untick on B
        b.write_config("111", {"color": "B only"})
        a.write_config("111", {"color": "from A", "authToken": "A-secret"})
        self.quiet(a, b)
        self.assertEqual(b.config_of("111"), {"color": "B only"})
        self.assertEqual(a.config_of("111")["color"], "from A")
        self.assertTrue(os.path.isfile(b.path("111", "__init__.py")))
        b.engine.decide({"111": "share"})  # tick again: B catches up with the shared setup
        self.quiet(b, a)
        self.assertEqual(b.config_of("111"), {"color": "from A"})
        b.write_config("111", {"color": "B again"})  # and its edits mirror again
        self.quiet(b, a)
        self.assertEqual(a.config_of("111")["color"], "B again")

    # Reviewing add-on differences
    ##########################################################################

    def b_adds_tool(self) -> tuple[FakeDevice, FakeDevice]:
        a = self.make_main_computer()
        b = self.join()
        write(b.path("tool2", "__init__.py"), "y = 2\n")
        self.cloud.sync(b)
        return a, b

    def test_keep_off_this_computer(self) -> None:
        a, b = self.b_adds_tool()
        a.review_answers = {"tool2": "keep_off"}
        self.cloud.sync(a)
        self.assertFalse(os.path.exists(a.path("tool2")))
        self.quiet(a, b)
        self.assertEqual(len(a.reviews), 1)  # not asked again
        self.assertTrue(os.path.isfile(b.path("tool2", "__init__.py")))
        a.review_answers = {"tool2": "install"}  # changed my mind, via the menu
        self.assertTrue(a.engine.review_now())
        self.assertEqual(a.reviews[-1][0]["kind"], "kept_off")
        self.assertTrue(os.path.isfile(a.path("tool2", "__init__.py")))
        self.quiet(a, b)

    def test_remove_everywhere_needs_no_second_question(self) -> None:
        a, b = self.b_adds_tool()
        a.review_answers = {"tool2": "remove_everywhere"}
        self.cloud.sync(a)  # decided after this sync's upload...
        self.cloud.sync(a)  # ...so it goes up with the next one
        self.cloud.sync(b)
        self.assertFalse(os.path.exists(b.path("tool2")))
        self.assertEqual(b.reviews, [])
        self.quiet(a, b)
        self.assertFalse(os.path.exists(a.path("tool2")))

    def test_decide_later_asks_once_per_session(self) -> None:
        a, b = self.b_adds_tool()
        self.cloud.sync(a)
        self.cloud.sync(a)
        self.assertEqual(len(a.reviews), 1)
        self.assertFalse(os.path.exists(a.path("tool2")))
        a.engine = engine.Engine(a, scan.Settings(), os.path.join(a.root, "state.json"))  # restart
        self.cloud.sync(a)
        self.assertEqual(len(a.reviews), 2)
        a.dirty.clear()
        a.engine.push()
        self.assertEqual(self.pushed(a), set())  # undecided: nothing uploaded about it

    def test_keep_it_here_after_removal_elsewhere(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        a.delete_addon("333")
        self.cloud.sync(a)
        b.review_answers = {"333": "keep_here"}
        self.cloud.sync(b)
        self.assertTrue(os.path.isfile(b.path("333", "__init__.py")))
        self.quiet(a, b)
        self.assertFalse(os.path.exists(a.path("333")))
        self.assertEqual(len(b.reviews), 1)

    def test_joining_computer_chooses_what_to_share(self) -> None:
        self.make_main_computer()
        c = self.device("C")
        write(c.path("mine_only", "__init__.py"), "# keep here\n")
        write(c.path("junk", "__init__.py"), "# delete\n")
        write(c.path("share_me", "__init__.py"), "# share\n")
        c.answers = [True]
        c.review_answers = {"mine_only": "keep_only_here", "junk": "remove_everywhere", "share_me": "share"}
        self.cloud.sync(c)
        self.assertEqual({d["kind"] for d in c.reviews[0]}, {"only_here"})
        self.cloud.sync(c)
        shared = {k for k, (e, _) in self.cloud.config.items() if not e.get("del")}
        self.assertIn("a/share_me", shared)
        self.assertNotIn("a/mine_only", shared)
        self.assertNotIn("a/junk", shared)
        self.assertTrue(os.path.isfile(c.path("mine_only", "__init__.py")))
        self.assertFalse(os.path.exists(c.path("junk")))

    # Settling down: nothing should bounce between computers
    ##########################################################################

    def quiet(self, *devices: FakeDevice, rounds: int = 3) -> None:
        """Sync back and forth; afterwards nobody should upload anything."""
        for _ in range(rounds):
            for dev in devices:
                self.cloud.sync(dev)
        for dev in devices:
            self.cloud.sync(dev)
            self.assertEqual(self.pushed(dev), set(), f"{dev.root} keeps uploading")

    def test_two_computers_settle_after_joining(self) -> None:
        a = self.make_main_computer()
        b = self.device("B")
        b.install_from_catalog(111, config={"color": "green"})  # B had its own copy first
        write(b.path("b_only", "__init__.py"), "# only on B\n")
        b.answers = [True]
        b.review_answers = {"b_only": "share"}
        a.review_answers = {"b_only": "install"}
        self.cloud.sync(b)
        self.assertEqual(b.reviews[0], [{"folder": "b_only", "name": "b_only", "kind": "only_here"}])
        self.quiet(a, b)
        self.assertTrue(os.path.isfile(a.path("b_only", "__init__.py")))

    def test_different_names_for_same_addon_do_not_bounce(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        meta = b._meta("111")
        meta["name"] = "Heatmap (short name Windows shows)"
        b._write_meta("111", meta)
        self.quiet(a, b)
        self.assertEqual(a._meta("111")["name"], "Heatmap")

    def test_setting_that_changes_by_itself_becomes_local(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        for day in range(4):
            for n in range(2):
                conf = a.config_of("111")
                conf["homescreen_data"] = f"cache {day}-{n}"
                a.write_config("111", conf)
                self.cloud.sync(a)
                self.cloud.sync(b)
            CLOCK[0] += 86400
        self.assertIn("homescreen_data", a.engine.state["kept_local"]["111"]["keys"])
        self.assertIn("homescreen_data", b.engine.state["kept_local"]["111"]["keys"])
        b_value = b.config_of("111").get("homescreen_data")
        conf = a.config_of("111")
        conf["homescreen_data"] = "A only"
        conf["color"] = "real change"
        a.write_config("111", conf)
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.config_of("111")["color"], "real change")  # real edits still mirror
        self.assertEqual(b.config_of("111").get("homescreen_data"), b_value)
        self.quiet(a, b)

    def test_setting_kept_local_on_one_computer_is_not_deleted_elsewhere(self) -> None:
        a = self.make_main_computer()
        b = self.join(settings=scan.Settings(exclude_config_keys={"111": ["port"]}))
        b.write_config("111", dict(b.config_of("111"), port=1234))
        a.write_config("111", dict(a.config_of("111"), port=9999))
        self.quiet(b, a)
        self.assertEqual(a.config_of("111")["port"], 9999)
        self.assertEqual(b.config_of("111")["port"], 1234)

    def test_file_kept_local_on_one_computer_is_not_deleted_elsewhere(self) -> None:
        a = self.make_main_computer()
        b = self.join(settings=scan.Settings(exclude_files=["111/settings.json"]))
        write(b.path("111", "user_files", "settings.json"), '{"weeks": 99}')
        self.quiet(b, a)
        self.assertEqual(read(a.path("111", "user_files", "settings.json")), '{"weeks": 12}')
        self.assertEqual(read(b.path("111", "user_files", "settings.json")), '{"weeks": 99}')

    # Modes and rollback
    ##########################################################################

    def test_preview_mode_changes_nothing(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        b.engine.mode = "preview"
        a.write_config("111", {"color": "yellow", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.config_of("111"), {"color": "red"})
        self.assertIn("Change settings of Heatmap", b.engine.state["preview"])

    def test_paused_mode_does_nothing(self) -> None:
        a = self.make_main_computer()
        a.engine.mode = "paused"
        a.write_config("111", {"color": "paused-edit"})
        self.cloud.sync(a)
        self.assertEqual(self.cloud.config["a/111"][0]["config"], {"color": "red"})

    def test_rollback_restores_and_spreads(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        good = len(a.engine.state["history"]) - 1
        a.write_config("111", {"color": "broken", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.config_of("111"), {"color": "broken"})
        a.engine.rollback(good)
        self.assertEqual(a.config_of("111"), {"color": "red", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.config_of("111"), {"color": "red"})

    def test_first_upload_does_not_replace_the_established_one(self) -> None:
        # Both computers get the same new add-on; B uploads it before its
        # media sync has brought A's, and AnkiWeb keeps B's upload.
        a = self.make_main_computer()
        b = self.join()
        for dev, who in ((a, "A"), (b, "B")):
            write(dev.path("tool3", "__init__.py"), "z = 3\n")
            dev.write_config("tool3", {"v": who})
        self.cloud.sync(a)
        self.cloud.last_upload_wins = True
        b.engine.push()
        self.cloud.sync_media(b)
        self.cloud.last_upload_wins = False
        self.assertEqual(self.cloud.config["a/tool3"][0]["config"], {"v": "B"})
        self.cloud.sync(a)
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(a.config_of("tool3"), {"v": "A"})
        self.assertEqual(b.config_of("tool3"), {"v": "A"})
        self.quiet(a, b)

    def test_missing_data_file_is_sent_again(self) -> None:
        a = self.make_main_computer()
        blob = self.cloud.config["f/my_tool"][0]["blob"]
        del self.cloud.media[blob]  # cleaned up elsewhere in a race
        b = self.join()
        self.assertIn("Still downloading", " ".join(b.engine.state["last_changes"]["lines"]))
        self.cloud.sync(a)  # the deletion reaches A
        self.assertNotIn(blob, a.media)
        self.cloud.sync(a)  # A still has the files, so it sends them again
        self.assertIn(blob, self.cloud.media)
        self.cloud.sync(b)
        self.assertEqual(read(b.path("my_tool", "user_files", "data.json")), '{"n": 1}')

    def test_rollback_survives_a_pull_before_it_uploads(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        good = len(a.engine.state["history"]) - 1
        a.write_config("111", {"color": "broken", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        a.engine.rollback(good)
        a.engine.pull("open")  # Anki reopened before the next sync
        a.engine.pull("media")
        self.assertEqual(a.config_of("111"), {"color": "red", "authToken": "A-secret"})
        self.cloud.sync(a)
        self.cloud.sync(b)
        self.assertEqual(b.config_of("111"), {"color": "red"})

    # Untrusted shared files
    ##########################################################################

    def shared_files(self, dev: FakeDevice, key: str, files: dict[str, bytes]) -> int:
        """Install a crafted shared entry that writes these files."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for rel, data in files.items():
                z.writestr(rel, data)
        dev.media["_loadout_d_crafted.zip"] = buf.getvalue()
        index = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
        return dev.engine.install_shared(
            [(key, {"h": "crafted", "files": index, "blob": "_loadout_d_crafted.zip"})]
        )

    def test_shared_file_cannot_overwrite_the_collection(self) -> None:
        a = self.make_main_computer()
        profile = a.profile_dir()
        write(os.path.join(profile, "collection.anki2"), "REAL")
        write(os.path.join(profile, "collection.media", "pic.jpg"), "REAL")
        for rel in ("collection.anki2", "Collection.ANKI2", "collection.media/pic.jpg",
                    "sub/../collection.anki2", "collection.anki2-wal", "backups./x.colpkg",
                    "media.trash /x"):
            self.shared_files(a, "d/my_tool", {rel: b"evil"})
            self.assertIn("refused to write Anki's own file", a.engine.state["last_changes"]["lines"][-1])
        self.assertEqual(read(os.path.join(profile, "collection.anki2")), "REAL")
        self.assertEqual(read(os.path.join(profile, "collection.media", "pic.jpg")), "REAL")
        self.assertFalse(os.path.exists(os.path.join(profile, "collection.anki2-wal")))
        self.shared_files(a, "d/my_tool", {"my_tool_data/notes.txt": b"fine"})  # an add-on's own
        self.assertEqual(read(os.path.join(profile, "my_tool_data", "notes.txt")), "fine")

    def test_folder_names_cannot_escape_the_addons_folder(self) -> None:
        a = self.make_main_computer()
        outside = os.path.join(a.root, "escaped")
        write(os.path.join(outside, "__init__.py"), "")
        for folder in ("../escaped", "..\\escaped", "C:escaped", "/tmp/escaped", ".."):
            a.engine.install_shared([(f"a/{folder}", {"h": "x", "config": {"x": 1}, "enabled": False})])
            self.cloud.set_entry(f"a/{folder}", scan.finish({"config": {"x": 2}, "enabled": False}))
        self.cloud.sync(a)
        self.assertEqual(os.listdir(outside), ["__init__.py"])
        self.assertFalse(any(d["folder"].startswith((".", "/", "C:")) for d in a.engine.differences()))
        path = os.path.join(self.tmp, "evil.loadout")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "format": share.FORMAT, "version": 1,
                "addons": [{"folder": "../escaped", "name": "x", "a": {}}],
            }))
        with self.assertRaisesRegex(ValueError, "isn't safe"):
            share.read(path)

    def test_safe_folder_names_on_mac_and_windows(self) -> None:
        for name in ("my_tool", "1468920185", "ok.name", "Read Aloud"):
            self.assertTrue(scan.safe_folder(name), name)
        for name in ("", ".", "..", "../x", "a/b", "a\\b", "C:x", "con", "NUL.txt", "com1",
                     "trailing.", "trailing ", ".hidden", "a*b", "x\x00", None, 5):
            self.assertFalse(scan.safe_folder(name), name)

    # Two copies of Loadout on one computer
    ##########################################################################

    @staticmethod
    def add_loadout_copy(dev: FakeDevice, folder: str, version: str = "1.7.2", joined: bool = False,
                         disabled: bool = False) -> None:
        write(dev.path(folder, "__init__.py"), "")
        write(dev.path(folder, "engine.py"), "")
        write(dev.path(folder, "version.py"), f'"""Loadout\'s version."""\n\nVERSION = "{version}"\n')
        write(dev.path(folder, "meta.json"), json.dumps({"name": "Loadout", "disabled": disabled}))
        if joined:
            state = engine.new_state()
            state["joined"] = True
            write(dev.path(folder, "user_files", "state_Anking.json"), json.dumps(state))

    def test_second_copy_of_loadout_is_never_mirrored(self) -> None:
        a = self.make_main_computer()
        b = self.join()
        self.add_loadout_copy(a, "1234567890")
        self.cloud.sync(a)
        self.assertNotIn("a/1234567890", self.cloud.config)
        # An old Loadout uploaded the copy; a removal of it is never applied.
        self.add_loadout_copy(b, "1234567890")
        self.cloud.set_entry("a/1234567890", scan.finish({"del": True, "everywhere": True}))
        self.cloud.sync(b)
        self.assertTrue(os.path.isdir(b.path("1234567890")))
        self.assertEqual([d for d in b.engine.differences() if d["folder"] == "1234567890"], [])
        self.assertNotIn("a/1234567890", b.engine.local_entries())

    def test_exactly_one_copy_of_loadout_runs(self) -> None:
        dev = self.device("A")
        addons = dev.addons_dir()
        self.add_loadout_copy(dev, "loadout", joined=True)
        self.add_loadout_copy(dev, "1234567890")
        # Set up beats new; both copies agree.
        self.assertEqual(engine.running_copy(addons, "loadout"), "loadout")
        self.assertEqual(engine.running_copy(addons, "1234567890"), "loadout")
        # The new copy takes over the setup; then the AnkiWeb one runs.
        files = dev.path("1234567890", "user_files")
        self.assertEqual(engine.take_over_setup(addons, "1234567890", files), "loadout")
        self.assertTrue(engine.load_state(os.path.join(files, "state_Anking.json"))["joined"])
        self.assertIsNone(engine.take_over_setup(addons, "1234567890", files))  # only once
        self.assertEqual(engine.running_copy(addons, "loadout"), "1234567890")
        self.assertEqual(engine.running_copy(addons, "1234567890"), "1234567890")
        # A newer version runs; a disabled copy doesn't count.
        self.add_loadout_copy(dev, "loadout", version="1.8.0", joined=True)
        self.assertEqual(engine.running_copy(addons, "1234567890"), "loadout")
        self.add_loadout_copy(dev, "loadout", version="1.8.0", joined=True, disabled=True)
        self.assertEqual(engine.running_copy(addons, "1234567890"), "1234567890")
        self.assertEqual(scan.loadout_copies(addons), ["1234567890", "loadout"])
        self.assertEqual(scan.addon_folders(addons, "loadout"), [])

    def test_garbage_collection_keeps_referenced_blobs(self) -> None:
        a = self.make_main_computer()
        for n in range(3):
            write(a.path("my_tool", "user_files", "data.json"), json.dumps({"n": n + 10}))
            self.cloud.sync(a)
        CLOCK[0] += engine.BLOB_GRACE_SECONDS + 10
        for n in range(engine.HISTORY_LIMIT + 2):
            a.write_config("111", {"color": str(n)})
            self.cloud.sync(a)
        current = self.cloud.config["f/my_tool"][0]["blob"]
        self.assertIn(current, a.media)
        loadout_blobs = [m for m in a.media if m.startswith("_loadout_f_")]
        self.assertLess(len(loadout_blobs), 5)


if __name__ == "__main__":
    unittest.main()
