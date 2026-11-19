"""Anki hooks and Loadout's entry in the Tools menu."""

from __future__ import annotations

import json
import os
import traceback
from typing import Callable

from aqt import gui_hooks, mw
from aqt.qt import QAction

from . import gui, hub
from .anki_device import AnkiDevice, sync_problems
from .engine import Engine, running_copy, take_over_setup
from .scan import Settings

OWN_FOLDER = __name__.split(".")[0]
USER_FILES = os.path.join(os.path.dirname(__file__), "user_files")
LOG_PATH = os.path.join(USER_FILES, "loadout.log")
# Which profile Loadout mirrors on this computer. Add-ons are shared by every
# profile, so two profiles (e.g. two people with their own AnkiWeb accounts)
# must not both mirror them.
COMPUTER_PATH = os.path.join(USER_FILES, "computer.json")
SETUP_WARNING_SECONDS = 7 * 86400

_engine: Engine | None = None
_device = AnkiDevice(OWN_FOLDER, LOG_PATH)
_close_hook_added = False


# Two copies of Loadout (see engine.running_copy)
##############################################################################

try:
    _taken_from = take_over_setup(_device.addons_dir(), OWN_FOLDER, USER_FILES)
    if _taken_from:
        _device.log(f"took over this computer's setup from the copy of Loadout in {_taken_from}")
except Exception:  # noqa: BLE001 - must never stop Loadout from loading
    _device.log(f"taking over setup failed:\n{traceback.format_exc()}")

try:
    from . import popups  # here, so a Qt build it can't handle never stops Loadout

    popups.install(OWN_FOLDER, set(os.listdir(_device.addons_dir())), _device.log)
except Exception:  # noqa: BLE001 - must never stop Loadout from loading
    _device.log(f"popup line failed:\n{traceback.format_exc()}")

# Menus belong to the main window, not a profile: one Hub for the whole run.
_hub = hub.Hub(
    _device.addons_dir(), OWN_FOLDER, lambda: _menu_action,
    os.path.join(USER_FILES, "hub.json"), _device.log,
)
_hub_started = False
_device.hub = _hub


def _config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def _state_path(profile: str | None) -> str:
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in profile or "profile")
    return os.path.join(USER_FILES, f"state_{safe_name}.json")


def _make_engine() -> Engine:
    conf = _config()
    return Engine(
        _device,
        Settings.from_config(conf),
        _state_path(mw.pm.name),
        mode=conf.get("mode", "mirror"),
        confirm_new_code=conf.get("confirm_new_code", True),
        mirror_preferences=conf.get("mirror_preferences", True),
    )


def _claim() -> dict:
    try:
        with open(COMPUTER_PATH, encoding="utf-8") as f:
            claim = json.load(f)
    except (OSError, ValueError):
        return {}
    return claim if isinstance(claim, dict) else {}


def _active_profile() -> str | None:
    return _claim().get("profile")


def _collection_id() -> str:
    """Tells collections apart: when it was created (a profile synced with
    the same AnkiWeb account has the same one)."""
    try:
        return str(mw.col.crt)
    except Exception:  # noqa: BLE001
        return ""


def _claim_profile() -> None:
    """Make the open profile the one Loadout mirrors on this computer."""
    os.makedirs(USER_FILES, exist_ok=True)
    with open(COMPUTER_PATH, "w", encoding="utf-8") as f:
        json.dump({"profile": mw.pm.name, "collection": _collection_id()}, f)


def _follow_renamed_profile() -> None:
    """Loadout keeps its state, and which profile it mirrors, by profile
    name, and suggests renaming a profile when names differ. When the
    mirrored profile no longer exists and the open one has its collection,
    it was renamed: carry the state over instead of starting again."""
    claim = _claim()
    old = claim.get("profile")
    if not old or old == mw.pm.name or old in mw.pm.profiles():
        return
    if not claim.get("collection") or claim["collection"] != _collection_id():
        return
    src, dst = _state_path(old), _state_path(mw.pm.name)
    if os.path.exists(src) and not os.path.exists(dst):
        os.replace(src, dst)
    _claim_profile()
    _device.log(f"profile {old!r} is now {mw.pm.name!r}; Loadout follows it")


def _apply_profile_rule() -> None:
    if not _engine:
        return
    running = running_copy(_device.addons_dir(), OWN_FOLDER)
    if running != OWN_FOLDER:
        _engine.other_copy = running
        _engine.inactive_reason = (
            f"Two copies of Loadout are installed (folders \u201c{OWN_FOLDER}\u201d and "
            f"\u201c{running}\u201d). The one in \u201c{running}\u201d is mirroring, so this one "
            "is off. Delete one of them in Tools \u2192 Add-ons."
        )
        return
    _engine.other_copy = None
    active = _active_profile()
    if active and active != mw.pm.name:
        _engine.inactive_reason = (
            f"Loadout is mirroring the \u201c{active}\u201d profile on this computer, so it "
            "is off in this one: add-ons are shared by every profile here."
        )
    else:
        _engine.inactive_reason = None
        if _engine.state["joined"] and (not active or not _claim().get("collection")):
            _claim_profile()


def _use_this_profile() -> None:
    _claim_profile()
    _apply_profile_rule()
    _rebuild_menu()


def _pull(reason: str) -> None:
    if not _engine:
        return
    _safely(lambda: _engine.pull(reason), "pull")
    if _engine.state["joined"] and not _active_profile():
        _claim_profile()
    _rebuild_menu()


def _warn_about_setup() -> None:
    """Say once a week if sync settings stop Loadout from working."""
    if not _engine or _engine.inactive_reason:
        return
    problems = sync_problems()
    now = _device.now()
    if problems and now - _engine.state.get("setup_warned_at", 0) > SETUP_WARNING_SECONDS:
        _engine.state["setup_warned_at"] = now
        _engine.save()
        _device.notify("Loadout can't mirror yet", problems, False)


def _safely(fn: Callable[[], object], what: str) -> None:
    """Loadout must never break Anki's sync or startup."""
    try:
        fn()
    except Exception:  # noqa: BLE001
        _device.log(f"{what} failed:\n{traceback.format_exc()}")
        if _engine:
            _engine.busy = False


# Hooks
##############################################################################


def _check_ankiweb() -> None:
    """Find out, in the background and at most daily, which add-ons AnkiWeb
    no longer serves, so their code gets a backup copy on the next sync."""
    if not _engine:
        return
    ids = _engine.ankiweb_ids_due()
    if not ids:
        return
    engine = _engine

    def done(fut) -> None:
        _safely(lambda: engine.record_ankiweb(ids, fut.result()), "AnkiWeb check")

    mw.taskman.run_in_background(lambda: _device.ankiweb_info(ids), done)


def write_held_data() -> None:
    """Profile closing without a closing sync: other add-ons have saved
    their state, so data held back while they ran can be written now.

    With auto sync on, this waits for the closing sync instead (on_sync_finish):
    Anki closes its windows after this hook, and add-ons may save again then."""
    if _engine and not mw.can_auto_sync():
        _safely(_engine.apply_held_files, "writing held data")


def _start_hub() -> None:
    global _hub_started
    _safely(lambda: hub.survey(
        _device.addons_dir(), OWN_FOLDER, os.path.join(USER_FILES, "menu_survey.json"), _device.log
    ), "menu survey")
    if not _hub_started:
        _hub_started = True
        _safely(_hub.start, "menu hub")


def on_profile_open() -> None:
    global _engine, _close_hook_added
    if not _close_hook_added:
        # Added now, after every add-on has loaded, so it runs after their
        # own close handlers have saved their state.
        gui_hooks.profile_will_close.append(write_held_data)
        _close_hook_added = True
    _safely(_follow_renamed_profile, "following a renamed profile")
    _engine = _make_engine()
    _engine.check_ankiweb_during_push = False
    mw.taskman.run_in_background(_device.warm_up, lambda fut: None)
    _apply_profile_rule()
    if _engine.other_copy:
        _rebuild_menu()
        _device.notify("Loadout is installed twice", [_engine.inactive_reason], False)
        return
    _safely(lambda: _device.migrate_legacy_config(
        keep=bool(_engine.state["joined"]) and not _engine.inactive_reason
    ), "moving old entries")
    _rebuild_menu()
    if _engine.inactive_reason:
        return
    _check_ankiweb()
    mw.progress.single_shot(3000, lambda: _safely(_warn_about_setup, "setup check"))
    # After add-ons that add menu items late have done so.
    mw.progress.single_shot(5000, _start_hub)
    # Auto-sync on open will pull right after this; otherwise apply whatever
    # the last sync brought down.
    if not (mw.pm.auto_syncing_enabled() and mw.pm.sync_auth()):
        mw.progress.single_shot(1000, lambda: _pull("open"))


def on_profile_close() -> None:
    if _engine:
        _engine.closing = True


def on_sync_start() -> None:
    if _engine:
        _safely(_engine.push, "push")


def on_sync_finish() -> None:
    if _engine and _engine.closing:
        # The closing sync: write held data now that windows are closed;
        # the media sync that follows may bring more (on_media_sync).
        _safely(lambda: _engine.apply_held_files(closing_sync=True), "writing held data")
        return
    # Let Anki finish refreshing first.
    if _engine and not _engine.inactive_reason:
        mw.progress.single_shot(300, lambda: _pull("sync"))
        _check_ankiweb()


def on_media_sync(running: bool) -> None:
    # Loadout's entries and files travel as media, which syncs after the
    # collection, so this is when changes from other computers arrive.
    if running or not _engine or not mw.col:
        return
    if _engine.closing:
        # The closing media sync: write data it brought for add-ons that are
        # about to stop; everything else applies on the next start.
        _safely(lambda: _engine.apply_held_files(closing_sync=True), "writing held data")
    else:
        _pull("media")


def on_config_saved(conf: dict) -> None:
    if _engine:
        _engine.settings = Settings.from_config(conf)
        _engine.mode = conf.get("mode", "mirror")
        _engine.confirm_new_code = conf.get("confirm_new_code", True)
        _engine.mirror_preferences = conf.get("mirror_preferences", True)
        _rebuild_menu()


# Menu
##############################################################################

_menu_action: QAction | None = None


def _set_mode(mode: str) -> None:
    conf = _config()
    conf["mode"] = mode
    mw.addonManager.writeConfig(__name__, conf)
    on_config_saved(conf)


def _review() -> None:
    if not _engine:
        return
    result: list[bool] = []
    _safely(lambda: result.append(_engine.review_now()), "review")
    if result and not result[0]:
        from aqt.utils import tooltip

        tooltip("Loadout: your add-ons are the same on all your computers.", parent=mw)
    _rebuild_menu()


def _retry() -> None:
    if _engine:
        _engine.forget_failures()
        _pull("sync")


def _open_log() -> None:
    from aqt.utils import openFolder

    openFolder(USER_FILES)


def _set_mirrored(folder: str, on: bool) -> None:
    """The window's per-add-on Mirror checkbox (this computer only)."""
    if _engine:
        _safely(lambda: _engine.decide({folder: "share" if on else "keep_here"}), "mirror choice")
        _rebuild_menu()


def _open_window() -> None:
    if not _engine:
        return
    actions = {
        "sync": mw.on_sync_button_clicked,
        "review": _review,
        "set_mode": _set_mode,
        "rollback": lambda: gui.show_rollback(_engine),
        "retry": _retry,
        "open_log": _open_log,
        "use_profile": _use_this_profile,
        "mirror": _set_mirrored,
    }
    gui.show_window(_engine, actions, _hub)


def _rebuild_menu() -> None:
    """Loadout has a single entry in Tools; the window holds the rest."""
    global _menu_action
    if _menu_action is None:
        _menu_action = QAction("Loadout", mw)
        _menu_action.triggered.connect(_open_window)
        mw.form.menuTools.addAction(_menu_action)
    label = "Loadout"
    if _engine and _engine.other_copy:
        label = "Loadout (off: installed twice)"
    elif _engine and _engine.inactive_reason:
        label = "Loadout (off in this profile)"
    elif _engine and _engine.mode == "mirror" and _engine.state["joined"]:
        try:
            count = len(_engine.differences())
        except Exception:  # noqa: BLE001 - the menu label must never break Anki
            count = 0
        if count:
            label = f"Loadout ({count} to review)"
    elif _engine and _engine.mode == "paused":
        label = "Loadout (paused)"
    _menu_action.setText(label + "\u2026")
    gui.refresh_window()


gui_hooks.profile_did_open.append(on_profile_open)
gui_hooks.profile_will_close.append(on_profile_close)
gui_hooks.sync_will_start.append(on_sync_start)
gui_hooks.sync_did_finish.append(on_sync_finish)
gui_hooks.media_sync_did_start_or_stop.append(on_media_sync)
mw.addonManager.setConfigUpdatedAction(__name__, on_config_saved)
