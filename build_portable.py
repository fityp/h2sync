#!/usr/bin/env python3
"""Builds the portable H2Sync folder: the app, launchers for macOS, Windows and
Linux, and a trimmed python-build-standalone runtime (with Tk) for each.

  python3 build_portable.py                 build into dist/H2Sync
  python3 build_portable.py --to /Volumes/H2 also copy it onto a card / player

Copying keeps the card's H2Sync/settings folder (servers and logins)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

PBS_RELEASE = "20261013"
PY_VERSION = "3.13.16"
TRIM_REV = "1"  # bump when TRIM changes, so launchers unpack the new runtime
TARGETS = {
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
    "windows-x86_64": "x86_64-pc-windows-msvc",
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
}
# Parts of the runtime H2Sync never uses (paths inside the archive, prefix match).
TRIM_UNIX = ["python/include/", "python/share/", "python/lib/pkgconfig/",
             "python/lib/python3.13/test/", "python/lib/python3.13/idlelib/",
             "python/lib/python3.13/ensurepip/", "python/lib/python3.13/turtledemo/",
             "python/lib/python3.13/pydoc_data/", "python/lib/python3.13/site-packages/pip",
             "python/lib/python3.13/config-3.13", "python/lib/python3.13/lib2to3/",
             "python/lib/tk8.6/demos/", "python/lib/tk9.0/demos/", "python/bin/idle", "python/bin/pip",
             "python/bin/pydoc", "python/bin/2to3", "python/lib/libpython3.13.a"]
TRIM_WINDOWS = ["python/include/", "python/libs/", "python/Scripts/", "python/Lib/test/",
                "python/Lib/idlelib/", "python/Lib/ensurepip/", "python/Lib/turtledemo/",
                "python/Lib/pydoc_data/", "python/Lib/site-packages/pip", "python/Lib/lib2to3/",
                "python/tcl/tk8.6/demos/", "python/tcl/tk9.0/demos/"]

SRC = Path(__file__).resolve().parent
CACHE = SRC / "build" / "downloads"
DIST = SRC / "dist" / "H2Sync"


def release_assets() -> dict[str, dict]:
    url = f"https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{PBS_RELEASE}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return {a["name"]: a for a in json.load(resp)["assets"]}


def fetch(target: str, assets: dict | None) -> Path:
    name = f"cpython-{PY_VERSION}+{PBS_RELEASE}-{TARGETS[target]}-install_only_stripped.tar.gz"
    path = CACHE / name
    if path.exists():
        return path
    assets = assets or release_assets()
    asset = assets[name]
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {name} ({asset['size'] // 1_000_000} MB)")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=600) as resp:
        data = resp.read()
    digest = (asset.get("digest") or "").removeprefix("sha256:")
    if digest and hashlib.sha256(data).hexdigest() != digest:
        sys.exit(f"checksum mismatch for {name}")
    path.write_bytes(data)
    return path


def trimmed(src: Path, target: str) -> bytes:
    trim = TRIM_WINDOWS if target.startswith("windows") else TRIM_UNIX
    out = io.BytesIO()
    with tarfile.open(src, "r:gz") as tin, tarfile.open(fileobj=out, mode="w:gz", compresslevel=9) as tout:
        for m in tin:
            if any(m.name.startswith(t) for t in trim) or "/__pycache__/" in m.name and "/test" in m.name:
                continue
            tout.addfile(m, tin.extractfile(m) if m.isfile() else None)
    return out.getvalue()


def build() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "app").mkdir(parents=True)
    (DIST / "runtime").mkdir()
    for name in ("h2sync.py", "ui_tk.py"):
        shutil.copy2(SRC / name, DIST / "app" / name)
    portable = SRC / "portable"
    shutil.copy2(portable / "launch.sh", DIST / "app" / "launch.sh")
    shutil.copytree(portable / "H2Sync.app", DIST / "H2Sync.app")
    for name in ("Start H2Sync (Linux).sh", "Start H2Sync (Windows).cmd", "README.txt"):
        shutil.copy2(portable / name, DIST / name)
    # No trailing newline: Windows' `set /p` reads it as is.
    (DIST / "runtime" / "VERSION").write_text(f"{PY_VERSION}-{PBS_RELEASE}-{TRIM_REV}")

    assets = None
    for target in TARGETS:
        src = fetch(target, assets)
        data = trimmed(src, target)
        (DIST / "runtime" / f"python-{target}.tar.gz").write_bytes(data)
        print(f"  {target}: {src.stat().st_size // 1_000_000} MB -> {len(data) // 1_000_000} MB")


def install(dest_root: Path) -> None:
    dest = dest_root / "H2Sync"
    print(f"copying to {dest}")
    for item in DIST.rglob("*"):
        rel = item.relative_to(DIST)
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() and target.stat().st_size == item.stat().st_size and \
                target.read_bytes() == item.read_bytes():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)  # FAT can't take permissions or xattrs
    # Runtimes from an earlier build that this one no longer has.
    keep = {p.name for p in (DIST / "runtime").iterdir()}
    for old in (dest / "runtime").iterdir():
        if old.name not in keep and not old.name.startswith("."):
            old.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--to", type=Path, help="also copy the folder onto this volume")
    args = parser.parse_args()
    print("building dist/H2Sync")
    build()
    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"built {DIST} ({total // 1_000_000} MB)")
    if args.to:
        install(args.to)


if __name__ == "__main__":
    main()
