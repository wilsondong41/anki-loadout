"""Loadout: mirror your add-ons, their settings and Anki's preferences to
every computer you sync with, through your normal AnkiWeb sync."""

try:
    from aqt import mw
except ImportError:  # imported outside Anki, e.g. by the tests
    mw = None

if mw is not None:
    from . import main  # noqa: F401
