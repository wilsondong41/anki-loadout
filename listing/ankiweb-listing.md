# AnkiWeb listing for Loadout 1.8.0

Upload: dist/loadout-ankiweb.ankiaddon (not loadout.ankiaddon)

## Title

Loadout: your add-ons and their settings on every computer you sync

## Tags

sync addons settings backup computers share preferences config

## Support Page

https://github.com/wilsondong41/anki-loadout/issues

## Branches

Min: 2.1.50
Max: 26.09   (no "-" prefix, so newer Anki versions can still download it)

## Description

Paste everything below this line. (Images are hosted on imgur: https://imgur.com/a/2PWDRTi)

---

**Set up Anki on a new computer once, and every add-on follows.** Loadout mirrors your add-ons, their settings and Anki's preferences between all the computers you sync with. It works through your normal AnkiWeb sync: no extra account, no server, nothing to configure.

<img src="https://i.imgur.com/u5SY6EJ.png" width="640" alt="The Loadout window: every add-on mirrored">

## What it does

- **New computer? One click.** After the first sync, Loadout offers to make the computer match your saved setup. It installs your AnkiWeb add-ons and copies their settings and your preferences right away, without waiting for years of card media to download.
- **Add-ons that aren't on AnkiWeb come too:** ones you wrote yourself, installed from a file or edited. Their exact code is copied, and you approve each one once per computer.
- **Settings and data follow automatically.** Change an add-on's settings on your laptop, and your desktop has them after its next sync. That includes the add-on's own data files and settings it keeps in your collection or profile folder.
- **Your logins stay put.** Tokens, passwords and API keys are never uploaded. Each computer keeps its own.
- **You decide what goes where.** An add-on installed or removed on one computer isn't installed or removed elsewhere until you say so. Keep some add-ons on one computer only if you like.
- **Safe when two computers change the same add-on.** An add-on's code and its settings are handled separately, so a settings change on one computer never brings back old code on the other.
- **Undo.** Roll back to any of the last 10 saved points, and your other computers follow. Removed add-ons go to the system trash.
- **One popup at a time.** Opening Anki after a while away can bring a pile of popups from Anki and several add-ons at once, stacked so the one on top can't be clicked. Loadout lines them up and shows them one by one, Anki's own first. Popups you open yourself are never held back.

<img src="https://i.imgur.com/8vwYjMG.png" width="560" alt="A new computer: Make this computer match it?">

<img src="https://i.imgur.com/V4DCCWn.png" width="640" alt="Choosing what to do with add-ons that differ between computers">

## Share with a friend

Save your add-ons (all of them, or just one) with their settings as a single *.loadout* file. Your friend installs Loadout and opens the file. Logins are never included, and you choose whether add-on data goes along.

<img src="https://i.imgur.com/2GvyLom.png" width="640" alt="Choosing which add-ons to share">

## Bonus: tidy menus (off unless you turn it on)

Too many add-on items in Tools? Loadout can move them into **Tools → All add-ons**, one submenu per add-on, and keep your favourites right in Tools. Keyboard shortcuts keep working, and switching it off puts everything back.

## Getting started

1. Install Loadout on each computer and restart Anki.
2. Make sure you're signed in to AnkiWeb and media syncing is on (Preferences → Syncing).
3. Sync on the computer whose setup you want to keep. Loadout asks whether it's the first computer: click **Yes**.
4. Sync on your other computers. Loadout asks to make each one match: click **Yes**, then restart Anki.

After that, just sync as usual. **Tools → Loadout…** shows what is mirrored and how each add-on reaches a new computer.

## Good to know

- Loadout saves small files starting with `_loadout_` in your collection's media folder, so they sync with your media (also to phones). Anki's Check Media leaves them alone.
- AnkiWeb only serves the latest version of an add-on. To keep a particular version on every computer, turn off its automatic updates; Loadout then copies your exact version.
- Data an add-on keeps outside Anki (browser logins, files elsewhere on your computer) can't be carried.
- Preview mode lists incoming changes without applying them; Paused mode does nothing (Tools → Loadout… → More → Mode).
- Install Loadout only once per computer. If two copies are installed, only one runs.

**COMPLETELY VIBED CODED WITH CLAUDE AI, THE GOAT.**

**This is a beta.** It's in use syncing a Mac and a Windows PC, but setups vary. If something looks wrong, **Tools → Loadout… → More → Open log folder** has a log that helps me fix it. Please report problems rather than leaving a low rating, so I can fix them.

---

## Note: getting image addresses

AnkiWeb doesn't host pictures, so each image needs a public web address. The images are in this folder:

1. 1-loadout-window.png → IMAGE_URL_1
2. 2-new-computer.png → IMAGE_URL_2
3. 3-review-differences.png → IMAGE_URL_3
4. 4-share-dialog.png → IMAGE_URL_4

Two easy ways to host them:

- A free GitHub repository: upload the four files, open each one, and use its "raw" link. The repo's Issues page also makes a good Support Page.
- imgur.com: upload each image and use its direct link (ending in .png).
