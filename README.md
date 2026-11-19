# Loadout

An Anki add-on that mirrors your add-ons, their settings and Anki's preferences to
every computer you sync with. It rides on your normal AnkiWeb sync, with no extra
account. Install it once per computer, sync, and that computer matches your setup.

## How it works

- **Before every sync**, Loadout notes anything you changed on this computer and
  saves it as small hidden files in your media folder (`_loadout_*`), so the sync
  uploads them. Media syncs file by file, which is why Loadout uses it rather than
  the collection's settings: AnkiWeb syncs those as one block, from whichever
  computer changed anything last.
- **A new computer doesn't wait for your card media.** Media downloads oldest
  first, so on a new computer Loadout's files come after years of card images and
  audio. Loadout also keeps a small compressed copy of your setup in the
  collection's settings, which sync first: right after the first sync, the new
  computer can match your setup, installing AnkiWeb add-ons and copying settings and
  preferences. Add-ons copied from your other computer and add-on data files
  follow when media finishes.
- **After every sync** (once media has synced), it applies changes from your other computers. This means
  installing or updating add-ons, copying their settings, enabling or disabling
  them, removing ones you deleted elsewhere, and copying Anki's preferences.
  Data files of add-ons that are currently running wait until Anki closes, so a
  running add-on can't save its old state over them. Everything is in place the
  next time you open Anki.
- **No pop-ups day to day.** A small notice says what changed. Two things do ask:
  setting up a new computer ("Restart Anki to start using them"), and add-ons
  appearing on or disappearing from your computers (see below).
- **One popup at a time.** When Anki and several add-ons all open a popup at once
  (typically when you open Anki on a computer you haven't used in a while), Loadout
  lines them up so the one on screen can always be answered. Anki's own come first,
  Loadout's own last. Popups you open yourself, and an add-on's follow-ups to its own
  popup, are never held back. It needs no setting: a popup nobody can see or answer
  never holds up the others, and if anything goes wrong it steps aside by itself.
- **You decide which add-ons each computer has.** Settings and data of shared
  add-ons mirror by themselves. When the *set* of add-ons differs, Loadout asks:
  - Another computer has an add-on this one doesn't: Install here, Keep off this
    computer, or Remove everywhere.
  - An add-on was uninstalled on another computer: Remove here too, or Keep it here
    (only here, no longer mirrored).
  - This computer had add-ons the saved setup didn't when it joined: Share them,
    Keep only on this computer, or Delete.

  "Remove everywhere" removes the add-on on every computer without asking again.
  "Decide later" asks again the next time Anki opens. The **Review add-on differences…** button in
  the Loadout window shows everything, including earlier "keep off" choices, so you can
  change your mind.
- Each add-on is its own entry, so edits to different add-ons on different
  computers all survive. If two computers change the same add-on between syncs,
  its code and its settings are decided separately: each comes from the computer
  that changed it, and if both changed the settings, the newer edit wins. A settings
  change on one computer never brings back old code on the other.
  This can take one extra sync.
- **Every installed add-on is carried over, wherever it came from:**
  - *From AnkiWeb:* downloaded by its ID on the new computer.
  - *Installed from a file, shared by a friend, or written yourself:* the code is
    copied from your computer. You approve these once per computer.
  - *On AnkiWeb once, but no longer available there* (removed, private): once a
    day Loadout asks AnkiWeb which add-ons it still serves and keeps a backup
    copy of any it doesn't. A new computer installs that copy.
  - *A download fails anyway:* the backup copy is used if there is one.
  - Set `backup_ankiweb_addons` to `"always"` to copy every add-on exactly as it
    is, local edits included.
- Copied code and each add-on's `user_files` data travel as hidden
  `_loadout_*.zip` files in your media folder. Anki's Check Media leaves files
  starting with `_` alone.
- **Tools → Loadout…** opens one window: a one-line summary, every add-on with its
  status (hover for details, such as how a new computer will get it), the last
  changes from your other computers, **Sync now**, and **More ▾** for the rest.

## What stays on each computer

- **Logins:** settings named like `token`, `api_key`, `password` or `cookie`
  (when they hold text), and files such as `credentials.json`. Log in once per
  computer.
- **Caches and leftovers:** `.bak`, `.log` and temp files, cache folders, bundled
  Python packages, and compiled `.so`/`.dll` files that only run on one operating
  system.
- **Data from add-ons that sync themselves** (detected from their code, e.g.
  Onigiri). Loadout seeds that data once on a new computer, then leaves it to
  the add-on.
- **Computer-specific settings:** interface size (screens differ), window positions,
  the graphics driver, sync settings.

## Tidy menus (Loadout window → Menus)

- **Tidy add-on menus** moves every add-on's menu items, and the menus add-ons put on
  the menu bar, into **Tools → All add-ons**, one submenu per add-on. Items you ★ stay
  directly in Tools. Untick it to put everything back exactly where it was.
- **Also move Anki's own Tools items** moves Study Deck, Create Filtered Deck and the
  rest there too (then the submenu is **All tools**). On a Mac, items macOS shows in
  the Anki menu (Preferences, About, Quit) stay where they are.
- **Hover** over an item in the list to see what it does: a picture of the window it
  opens, whether it's an on/off switch, what a submenu contains, and its shortcut.
  Loadout learns what a window looks like the first time you open that item, from
  anywhere; **double-click** an item in the list to open it. The pictures stay on
  this computer.
- Keyboard shortcuts keep working either way.

## Sharing with a friend (Loadout window → Share)

- **Choose what to share…** saves your add-ons as one `.loadout` file: everything, or
  just the ones you tick. It includes their settings, their data where you leave
  **Data** ticked (some of it can be personal, like game progress), and exact copies
  of add-ons you've edited, pinned or that aren't on AnkiWeb. Other AnkiWeb add-ons
  download fresh, unless you ask for exact copies of everything. Optionally it also
  includes your Anki preferences. Logins are never included.
- **Open a .loadout file…** shows what the file contains and what it would change. Pick
  what to install. Copied code asks for your approval, your own logins are kept, and
  your existing data files are never deleted. What you install then mirrors to your
  own other computers like any change you make.

## Safety

- A new computer never uploads anything until it has pulled your saved setup.
  You confirm that once ("Make this computer match it?").
- An item still waiting to be applied (data still downloading, an install that
  failed, an add-on you didn't approve) is never uploaded back over the saved copy.
- Add-ons that aren't on AnkiWeb need your approval once per computer, and again
  only if their code changes.
- Removed add-ons go to the system trash, not straight to deletion.
- A `.loadout` file (or anything synced) can't write Anki's own files in your
  profile folder (your collection, media, backups) or anywhere outside the
  add-ons folder; such a file is refused.
- If Loadout is installed twice on one computer (once from a file, once from
  AnkiWeb), only one copy runs and the other says so in its menu entry. A copy
  added later takes over this computer's setup, so nothing needs setting up
  again. Delete the copy you don't want in Tools → Add-ons.
- Renaming the profile Loadout mirrors keeps its setup. Copying your whole Anki
  folder to a new computer works too: the copy notices it is a different
  computer and takes its own identity.
- **More ▾ → Roll back…** restores one of the last 10 saved points. The
  rollback then spreads to your other computers.
- **More ▾ → Mode → Preview only** lists incoming changes without applying them.
  **Paused** does nothing at all.

## Limits

- Desktop Anki only. AnkiMobile and AnkiDroid ignore Loadout.
- Anki loads add-ons at startup, so after Loadout installs or reconfigures
  something you'll be asked to restart.
- Data an add-on stores outside Anki's add-ons folder (e.g. AnkiHub's login,
  browser logins inside an add-on) can't be carried.
- AnkiWeb only serves the latest version of an add-on, so older versions can't
  be pinned.
- If two computers change the same data file between syncs, the newer file wins
  as a whole.

## Who it works for

Anyone using desktop Anki 2.1.50 or newer on Windows, macOS or Linux, with any
add-ons. Nothing in Loadout is specific to particular add-ons: what gets mirrored
is decided by generic rules (names, file types, sizes), and the Status screen
shows how each add-on is handled. Every Anki function and hook it uses has been
checked to exist from 2.1.50 to 25.09.

What a user needs:

- **An AnkiWeb account, signed in, with media syncing on.** Loadout travels inside
  the normal sync. If either is missing, Loadout says so once a week and in Status.
- **One mirrored profile per computer.** Add-ons are shared by every profile on a
  computer, so Loadout mirrors only the first profile that sets it up. In any other
  profile it stays off. The Loadout window's *Use Loadout with this profile* button
  switches.

## Sharing it

- **As a file:** send `dist/loadout.ankiaddon`. People install it with Tools →
  Add-ons → Install from file.
- **On AnkiWeb:** upload `dist/loadout-ankiweb.ankiaddon` at
  https://ankiweb.net/shared/upload. People can then install it with a code, and
  updates reach them automatically. On a new computer, installing Loadout from
  AnkiWeb is the only manual step.

## Development

```
python3 -m unittest discover -s tests   # simulated multi-computer tests
python3 build.py                        # builds dist/loadout.ankiaddon
```

`tests/qt_hub_check.py` and `tests/qt_popups_check.py` need Anki's own Python (PyQt);
run them with `QT_QPA_PLATFORM=offscreen`.

The core (`rules.py`, `scan.py`, `blobs.py`, `engine.py`) doesn't import Anki.
`anki_device.py`, `gui.py` and `main.py` connect it to Anki.

## Bug Reports & Feedback

Please open an [issue](https://github.com/wilsondong41/anki-loadout/issues) with your OS(es), Anki version, and what happened.

---

**COMPLETELY VIBED CODED WITH CLAUDE AI, THE GOAT.**
