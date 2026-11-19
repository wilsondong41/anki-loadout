"""Work out which add-on put an item into Anki's menus.

Loadout loads after most add-ons (Anki loads them in folder-name order), so it
cannot watch them add their menu items. Instead it looks for each item's
label in the add-ons' own files: "Show sidebar" is usually written in exactly
one add-on's code or translations. Nothing here imports Anki.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import rules, scan

TEXT_EXT = {".py", ".json", ".txt", ".md", ".html", ".js", ".ui", ".yaml", ".yml", ".ftl"}
MAX_FILE_BYTES = 2_000_000
MIN_LABEL_LENGTH = 3

_TEXT_CACHE: dict[tuple[str, tuple], str] = {}


def clean_label(text: str) -> str:
    """A menu label as a person reads it: no "&" accelerators, shortcut hints
    or trailing ellipsis."""
    text = text.split("\t", 1)[0]
    text = re.sub(r"&(?!&)", "", text).replace("&&", "&")
    return text.strip().rstrip("…").rstrip(".").strip()


def addon_text(addons_dir: str, folder: str) -> str:
    """Lower-cased text of an add-on's own code and translation files
    (not its data, caches or bundled libraries)."""
    root = os.path.join(addons_dir, folder)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in scan.DETECT_SKIP_DIRS and not rules.skip_dir_reason(d, os.listdir(os.path.join(dirpath, d)))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size <= MAX_FILE_BYTES:
                files.append((path, st.st_size, st.st_mtime_ns))
    signature = tuple(sorted(files))
    key = (root, signature)
    if key not in _TEXT_CACHE:
        parts = []
        for path, _, _ in signature:
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    parts.append(f.read().lower())
            except OSError:
                continue
        _TEXT_CACHE[key] = "\n".join(parts)
    return _TEXT_CACHE[key]


def find_owners(labels: list[str], addons_dir: str, folders: list[str]) -> dict[str, list[str]]:
    """label -> add-on folders that contain it, best matches only.

    A label written as a whole string ("Show sidebar" in quotes) beats one
    merely appearing inside other text; an empty list means no add-on's files
    contain it (e.g. the label is built at runtime)."""
    texts = {folder: addon_text(addons_dir, folder) for folder in folders}
    out: dict[str, list[str]] = {}
    for label in labels:
        cleaned = clean_label(label).lower()
        if len(cleaned) < MIN_LABEL_LENGTH:
            out[label] = []
            continue
        # Add-ons often write the label with its "&" keyboard marker.
        needles = {cleaned, label.split("\t", 1)[0].strip().rstrip("…").rstrip(".").lower()}
        quoted = [f for f, t in texts.items()
                  if any(f'"{n}' in t or f"'{n}" in t for n in needles)]
        anywhere = [f for f, t in texts.items() if any(n in t for n in needles)]
        out[label] = sorted(quoted or anywhere)
    return out


GENERIC_NAME_WORDS = {
    "anki", "the", "for", "and", "with", "your", "a", "an", "of", "to", "in", "on", "by",
    "v2", "v3", "beta", "fixed", "updated", "created", "add", "addon", "add-on",
}


def short_name(name: str) -> str:
    """An add-on's name without the tagline: "Anki Terminator V2 - ChatGPT
    DeepSeek Sidebar…" -> "Anki Terminator V2"."""
    for sep in (" - ", " — ", " – ", " (", " ~ ", " | ", "  "):
        if sep in name:
            name = name.split(sep, 1)[0]
    name = name.strip()
    return name if len(name) <= 40 else name[:39].rstrip() + "…"


def display_name(addons_dir: str, folder: str) -> str:
    """The shortest sensible name for an add-on: its AnkiWeb title or the
    name its author put in manifest.json, without taglines."""
    found = []
    for file in ("meta.json", "manifest.json"):
        name = scan.read_json(os.path.join(addons_dir, folder, file)).get("name")
        if name:
            found.append(short_name(name))
    return min(found, key=len) if found else folder


def _key_words(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", short_name(name).lower())
    return [w for w in words if w not in GENERIC_NAME_WORDS and len(w) > 2]


def resolve_owner(
    label: str,
    child_labels: list[str],
    found: dict[str, list[str]],
    names: dict[str, str],
) -> str | None:
    """Which add-on a menu (or menu item) belongs to.

    found: label -> candidate folders from find_owners. Tried in order:
    a single file match; the add-on most of its children belong to; an
    add-on whose name appears in the label. None when it stays unclear."""
    candidates = found.get(label, [])
    if len(candidates) == 1:
        return candidates[0]
    votes: dict[str, int] = {}
    for child in child_labels:
        child_candidates = found.get(child, [])
        if len(child_candidates) == 1:
            votes[child_candidates[0]] = votes.get(child_candidates[0], 0) + 1
    if votes:
        best = max(votes, key=lambda f: votes[f])
        if not candidates or best in candidates:
            return best
    text = clean_label(label).lower()
    pool = candidates or list(names)
    exact = [f for f in pool if short_name(names.get(f, f)).lower() == text]
    if len(exact) == 1:
        return exact[0]
    by_name = [f for f in pool if any(w in text for w in _key_words(names.get(f, f)))]
    if len(by_name) == 1:
        return by_name[0]
    return None


# What a menu item does, read from the add-on's code
##############################################################################

_WINDOW = re.compile(
    r"\.exec_?\(\)|\.show\(\)|showInfo\(|showText\(|showWarning\(|askUser\(|Dialog\(|"
    r"QMainWindow|QMessageBox|aqt\.dialogs\.open|getFile\(|QFileDialog"
)
_LINK = re.compile(r"openLink\(|webbrowser\.open|openUrl\(")
_URL = re.compile(r"https?://[^\s'\"\)]+")
_TOGGLE = re.compile(r"=\s*not\s|setWindowFlag|\.toggle\(|isChecked\(\)")
_SETTING = re.compile(r"writeConfig\(")
_CALL = re.compile(r"([A-Za-z_][\w\.]*)\s*\(")
_EXPLAIN_CACHE: dict[tuple[str, str], dict[str, str] | None] = {}
_DEF = re.compile(r"\s*def\s+(\w+)\s*\(")
_CONSTANT = re.compile(r"""\s*([A-Z_][A-Z0-9_]*)\s*=\s*["'](.*)["']\s*$""")
# Per add-on: its code, the functions it defines, and its string constants
# (value -> names). Built once, so explaining a menu of ~50 items doesn't
# re-read and re-search the same code for every item.
_CODE_INDEX: dict[str, tuple[list[list[str]], set[str], dict[str, set[str]]]] = {}


def _code_index(addons_dir: str, folder: str) -> tuple[list[list[str]], set[str], dict[str, set[str]]]:
    root = os.path.join(addons_dir, folder)
    if root not in _CODE_INDEX:
        sources = _py_sources(addons_dir, folder)
        defined: set[str] = set()
        constants: dict[str, set[str]] = {}
        for lines in sources:
            for line in lines:
                if m := _DEF.match(line):
                    defined.add(m.group(1))
                elif m := _CONSTANT.match(line):
                    constants.setdefault(m.group(2), set()).add(m.group(1))
        _CODE_INDEX[root] = (sources, defined, constants)
    return _CODE_INDEX[root]


def _py_sources(addons_dir: str, folder: str) -> list[list[str]]:
    root = os.path.join(addons_dir, folder)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in scan.DETECT_SKIP_DIRS and not rules.skip_dir_reason(d, os.listdir(os.path.join(dirpath, d)))
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as f:
                    out.append(f.read().splitlines())
            except OSError:
                continue
    return out


def _handler_name(expression: str) -> str | None:
    """The function a signal is connected to: "self.on_open" -> "on_open",
    "lambda: show(x)" -> "show", "partial(run, 1)" -> "run"."""
    expression = expression.strip()
    if expression.startswith("lambda"):
        expression = expression.split(":", 1)[-1]
        match = _CALL.search(expression)
        return match.group(1).split(".")[-1] if match else None
    if expression.startswith(("partial(", "functools.partial(")):
        expression = expression.split("(", 1)[1].split(",", 1)[0]
    name = re.match(r"[A-Za-z_][\w\.]*", expression)
    return name.group(0).split(".")[-1] if name else None


_NOT_HANDLERS = {"mw", "self", "None", "True", "False", "parent", "menu", "tr", "_"}


def _find_handler(lines: list[str], index: int, needle: str, defined: Any) -> str | None:
    """Which function the menu item mentioned on line `index` is wired to.
    defined(name) says whether the add-on defines a function of that name."""
    line = lines[index]
    same = re.search(r"addAction\([^,]+,\s*([^)]+)\)", line) or re.search(r"triggered\s*=\s*([\w\.]+)", line)
    if same:
        name = _handler_name(same.group(1))
        if name and defined(name):
            return name
    # A helper call or a list of items: the function right after the label,
    # e.g. addMenu(MENU, "Open leaderboard", self.rebuild_leaderboard, "Shift+L")
    # or ("Prep Station", open_prep_station).
    at = line.find(needle)
    if at >= 0:
        after = line[at + len(needle):]
        nxt = re.match(r"""^[^,\n]*?,\s*(?:lambda[^:]*:\s*)?([A-Za-z_][\w\.]*)""", after)
        if nxt:
            name = nxt.group(1).split(".")[-1]
            if name not in _NOT_HANDLERS and defined(name):
                return name
    var = re.match(r"\s*([\w\.]+)\s*=\s*(?:QAction\(|[\w\.]+\.addAction\()", line)
    if not var:
        # The QAction( call may have started on a line above.
        for back in range(1, 4):
            if index - back >= 0:
                var = re.match(r"\s*([\w\.]+)\s*=\s*QAction\(\s*$", lines[index - back])
                if var:
                    break
    if not var:
        return None
    name = re.escape(var.group(1))
    wiring = re.compile(
        rf"{name}\.triggered\.connect\((.+)\)\s*$|qconnect\(\s*{name}\.triggered\s*,\s*(.+)\)\s*$"
    )
    following = lines[index + 1: index + 40]
    for i, later in enumerate(following):
        match = wiring.search(later)
        if not match and re.search(rf"{name}\.triggered\.connect\(\s*$", later) and i + 1 < len(following):
            match = re.match(r"\s*(.+?)\)?\s*$", following[i + 1])  # connect(\n    handler)
            if match:
                return _handler_name(match.group(1))
        if match:
            return _handler_name(match.group(1) or match.group(2))
    return None


def _search_handler(
    sources: list[list[str]], needles: list[tuple[str, str]], defined: Any,
    skip_definitions: bool = False,
) -> str | None:
    """needles: (text that must appear on the line, text the handler follows)."""
    for lines in sources:
        for i, line in enumerate(lines):
            for probe, anchor in needles:
                if probe not in line:
                    continue
                if skip_definitions and re.match(rf"\s*{re.escape(probe)}\s*=", line):
                    continue
                found = _find_handler(lines, i, anchor, defined)
                if found:
                    return found
    return None


def _function_body(sources: list[list[str]], name: str) -> str | None:
    pattern = re.compile(rf"^(\s*)def\s+{re.escape(name)}\s*\(")
    for lines in sources:
        for i, line in enumerate(lines):
            match = name in line and pattern.match(line)  # substring test first: far cheaper
            if not match:
                continue
            indent = len(match.group(1))
            body = []
            for later in lines[i + 1: i + 120]:
                if later.strip() and len(later) - len(later.lstrip()) <= indent:
                    break
                body.append(later)
            return "\n".join(body)
    return None


def _humanize(name: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").split()
    return " ".join(words).strip().capitalize()


def explain_action(addons_dir: str, folder: str, label: str) -> dict[str, str] | None:
    """What a menu item does, from the add-on's code: kind is "window",
    "link", "toggle", "setting" or "action"; None if it can't be found
    (for example the label is built while Anki runs)."""
    key = (os.path.join(addons_dir, folder), label)
    if key in _EXPLAIN_CACHE:
        return _EXPLAIN_CACHE[key]
    sources, defined_names, constant_names = _code_index(addons_dir, folder)

    def defined(name: str) -> bool:
        return name in defined_names

    texts = {clean_label(label), label.split("\t", 1)[0].strip().rstrip("…").rstrip(".")}
    texts = {t for t in texts if len(t) >= MIN_LABEL_LENGTH}
    handler = _search_handler(sources, [(f'"{t}', t) for t in texts] + [(f"'{t}", t) for t in texts], defined)
    if handler is None:
        # The label may be stored in a constant first: ADDON_TITLE = "Anki Terminator".
        constants = set().union(*(constant_names.get(t, ()) for t in texts))
        handler = _search_handler(sources, [(c, c) for c in constants], defined, skip_definitions=True)
    result = None
    if handler:
        body = _function_body(sources, handler) or ""
        seen = {handler}
        # Short wrappers often just call the real function: follow up to two.
        for _ in range(2):
            if _WINDOW.search(body) or _LINK.search(body) or _TOGGLE.search(body):
                break
            calls = [c.split(".")[-1] for c in _CALL.findall(body)]
            nxt = next((c for c in calls if c not in seen and _function_body(sources, c)), None)
            if not nxt:
                break
            seen.add(nxt)
            body = _function_body(sources, nxt) or body
        if _LINK.search(body):
            url = _URL.search(body)
            result = {"kind": "link", "detail": url.group(0) if url else ""}
        elif _TOGGLE.search(body):
            result = {"kind": "toggle", "detail": _humanize(handler)}
        elif _WINDOW.search(body):
            result = {"kind": "window", "detail": ""}
        elif re.match(r"(toggle|switch)", handler, re.I):
            result = {"kind": "toggle", "detail": _humanize(handler)}
        elif re.match(r"(open|show|display|launch|invoke)", handler, re.I):
            result = {"kind": "window", "detail": ""}
        elif _SETTING.search(body):
            result = {"kind": "setting", "detail": _humanize(handler)}
        else:
            result = {"kind": "action", "detail": _humanize(handler)}
    _EXPLAIN_CACHE[key] = result
    return result


# Which add-on a collection setting or profile-folder item belongs to
##############################################################################


def name_owner(name: str, texts: dict[str, str], names: dict[str, str] | None = None) -> str | None:
    """The add-on whose code mentions a setting or file name: quoted exactly,
    else by its leading words (for names built like f"modern_menu_{part}").
    When several add-ons mention it, the one that writes it wins.

    texts: folder -> lower-cased code (addon_text)."""
    lowered = name.lower()
    exact = [f for f, t in texts.items() if f'"{lowered}"' in t or f"'{lowered}'" in t]
    if not exact:
        parts = lowered.split("_")
        for n in range(len(parts) - 1, 1, -1):
            prefix = "_".join(parts[:n]) + "_"
            if len(prefix) < 8:
                break
            exact = [f for f, t in texts.items() if f'"{prefix}' in t or f"'{prefix}" in t]
            if exact:
                break
    if len(exact) <= 1:
        return exact[0] if exact else None
    writers = [f for f in exact if re.search(rf"set_config\(\s*[\"']{re.escape(lowered)}", texts[f])]
    if len(writers) == 1:
        return writers[0]
    # Several add-ons mention it: the one named after it ("heatmap" -> Review Heatmap).
    named = [f for f in exact if names and any(w in lowered for w in _key_words(names.get(f, "")))]
    return named[0] if len(named) == 1 else None
