"""Package the add-on:

  dist/loadout.ankiaddon          to share as a file (Tools → Add-ons → Install from file)
  dist/loadout-ankiweb.ankiaddon  to upload to AnkiWeb (no manifest.json)
"""

import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "loadout")
DIST = os.path.join(ROOT, "dist")


def package(out: str, skip: set) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(SRC):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "user_files")]
            for name in sorted(filenames):
                if name.endswith(".pyc") or name == ".DS_Store" or name in skip:
                    continue
                path = os.path.join(dirpath, name)
                z.write(path, os.path.relpath(path, SRC))
    print(f"built {out}")


def main() -> None:
    os.makedirs(DIST, exist_ok=True)
    package(os.path.join(DIST, "loadout.ankiaddon"), set())
    package(os.path.join(DIST, "loadout-ankiweb.ankiaddon"), {"manifest.json"})


if __name__ == "__main__":
    main()
