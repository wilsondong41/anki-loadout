These settings apply to **this computer only**. Add-ons are named by their folder
name, which for AnkiWeb add-ons is their number (shown in Tools → Add-ons → View Files).

- **mode**: `"mirror"` uploads and applies changes automatically. `"preview"` still
  uploads this computer's changes, but only *lists* changes from other computers
  (in the Loadout window). `"paused"` does nothing.
- **confirm_new_code**: ask before installing add-ons that are not on AnkiWeb
  (their code is copied from your other computer). Strongly recommended.
- **mirror_preferences**: also mirror Anki's own preferences (theme, language,
  review and scheduling options). Interface size, window positions, the graphics
  driver and sync settings are never mirrored: they belong to each computer.
- **exclude_addons**: folders Loadout should ignore completely on this computer,
  e.g. `["1468920185"]`.
- **exclude_files**: patterns for data files to keep on this computer, matched
  against `"<folder>/<path inside user_files>"`, e.g. `["175794613/*.db"]`.
- **exclude_config_keys**: settings to keep on this computer, per add-on,
  e.g. `{"124672614": ["port"]}`. Settings whose names look like logins or keys
  (`token`, `api_key`, `password`, …) are always kept local.
- **self_sync**: override the "syncs itself" detection: `{"<folder>": false}` makes
  Loadout mirror that add-on's data normally; `true` makes it seed only.
- **backup_ankiweb_addons**: when to send an exact copy of an AnkiWeb add-on
  instead of letting other computers download it. `"when_needed"` (default)
  copies add-ons whose code was edited after installing, add-ons with
  auto-updates turned off (so other computers get this exact version), and
  add-ons AnkiWeb no longer serves (checked daily). `"always"` copies every one
  (about the size of your add-ons folder in extra media, which also syncs to
  phones). `"never"` always downloads from AnkiWeb.
- **max_file_mb** / **max_addon_mb**: data files above these sizes stay local.
- **max_code_mb**: add-on code above this size is not copied (AnkiWeb media sync
  does not carry very large files).
