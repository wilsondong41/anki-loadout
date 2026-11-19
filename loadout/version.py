"""Loadout's version. Computers compare it to notice when they run different
builds (an older build can disagree about how entries are written)."""

VERSION = "1.8.1"


def parse(version: str) -> tuple[int, ...]:
    parts = []
    for piece in str(version).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
