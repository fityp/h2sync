#!/usr/bin/env python3
"""H2Sync: keep a Rockbox player (or any music folder) in sync with Navidrome.

A sync run, in order:

1. Scrobbles. Plays from Rockbox's playback log (/.rockbox/playback.log,
   written when Settings > Playback > Logging is on) are mapped from device path
   to Navidrome song and submitted with their original timestamps. Navidrome
   records them and forwards them to Last.fm / ListenBrainz if linked.
2. Organize. Every audio file on the device that matches a server song (same
   byte size and type) is moved into the standard layout:
       /Music/<Album Artist>/<Album> (<Year>)/<NN> - <Title>.<ext>
   Files you copied yourself become "keep" files: never deleted by H2Sync.
   Files that match nothing on the server are left where they are.
3. Transfer. Songs in the chosen playlists that are not on the device are
   downloaded (original files) into the standard layout.
4. Covers, playlists (/Playlists/<name>.m3u8) and cleanup: synced files that no
   chosen playlist uses any more are deleted, as are byte-identical
   duplicates of songs already in the layout.

Nothing is changed until a run is confirmed (UI button, or the CLI prompt).

Everything H2Sync keeps lives in two places, both portable:
  <home>/settings/settings.json   servers (URL, user, login token), last choices
  <device>/.h2sync/               device settings, manifest, scrobble ledger
<home> is the H2Sync folder the app was started from (--home).

Usage:
  h2sync.py [ui]                  window: pick device and server, plan, sync
  h2sync.py sync [--yes]          same run in the terminal
  h2sync.py plan                  print the plan and exit
  h2sync.py list                  server playlists and which ones sync
Options: --home DIR, --device PATH, --server NAME
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import getpass
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "H2Sync"
SETTINGS_REL = "settings/settings.json"
DEVICE_SETTINGS_REL = ".h2sync/device.json"
MANIFEST_REL = ".h2sync/manifest.json"
LEDGER_REL = ".h2sync/scrobbled.tsv"
ARCHIVE_REL = ".h2sync/archive"
SPEEDTEST_REL = ".h2sync/speedtest.tmp"
ROCKBOX_DIR = ".rockbox"
# playback.log is written by Rockbox core; playback_NNNN.log are splits made by
# the lastfm_scrobbler plugin. playback_old.log is the plugin's archive of plays
# it already exported, so it is not read.
LOG_PATTERN = re.compile(r"^playback(_\d{4})?\.log$")
AUDIO_EXTS = {"flac", "mp3", "m4a", "ogg", "opus", "wav", "aac", "ape", "wv", "aiff", "aif", "wma", "mpc"}
COMPANION_EXTS = {"jpg", "jpeg", "png", "bmp", "cue", "log", "txt", "nfo"}
SKIP_DIRS = {".rockbox", ".h2sync", ".Trashes", ".Spotlight-V100", ".fseventsd",
             "System Volume Information", "$RECYCLE.BIN", APP_NAME}
PART_SUFFIXES = (".h2part", ".part")
LEGACY_DIRS = ("Synced",)
SCROBBLE_BATCH = 50
LIBRARY_PAGE = 500
CHUNK_SIZE = 256 * 1024
RATE_WINDOW_S = 10.0
SPEEDTEST_BYTES = 32 * 1024 * 1024
PER_FILE_OVERHEAD_S = 0.4  # FAT directory updates + fsync per file, measured on the H2
API_VERSION = "1.16.1"
CLIENT_NAME = "h2sync"
USB2_HIGH_SPEED_MBPS = 480
NO_WINDOW = 0x08000000  # Windows CREATE_NO_WINDOW: no console flash from pythonw

DEVICE_DEFAULTS = {
    "music_dir": "Music",
    "playlist_dir": "Playlists",
    "organize": True,
    "covers": True,
    "device_clock": "local",
    "min_play_fraction": 0.5,
    "min_play_seconds": 240,
    "playlists": {},  # server key -> {"names": [...], "patterns": [...]}
}


class SyncError(Exception):
    pass


class Cancelled(Exception):
    pass


# ================================================================ small file helpers


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise SyncError(f"could not read {path}: {exc}") from exc


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)


# ================================================================ settings


class Settings:
    """Servers and last choices, kept in the portable H2Sync folder.

    A server's password is never stored: Subsonic token login takes
    md5(password + salt) with the salt, so that pair is saved instead. It works
    as a login until the Navidrome password is changed."""

    def __init__(self, home: Path):
        self.home = home
        self.path = home / SETTINGS_REL
        self.data = {"servers": [], "last_server": None, "last_device": None}
        self.data.update(read_json(self.path, {}))

    def save(self) -> None:
        write_json(self.path, self.data)

    @property
    def servers(self) -> list[dict]:
        return self.data["servers"]

    def server(self, ref: str | None) -> dict | None:
        for s in self.servers:
            if ref in (s["id"], s["name"]):
                return s
        return None

    def put_server(self, server: dict) -> dict:
        server.setdefault("id", uuid.uuid4().hex[:12])
        self.data["servers"] = [s for s in self.servers if s["id"] != server["id"]] + [server]
        self.save()
        return server

    def remove_server(self, sid: str) -> None:
        self.data["servers"] = [s for s in self.servers if s["id"] != sid]
        if self.data["last_server"] == sid:
            self.data["last_server"] = None
        self.save()

    def remember(self, device: Path | None = None, server: dict | None = None) -> None:
        if device is not None:
            self.data["last_device"] = str(device)
        if server is not None:
            self.data["last_server"] = server["id"]
        self.save()


def login_token(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(8)
    return salt, hashlib.md5((password + salt).encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def server_key(server: dict) -> str:
    """Names a server account independently of any settings file, so a
    device's playlist choices survive being synced from another H2Sync copy."""
    return f"{server['user']}@{urllib.parse.urlsplit(server['url']).netloc}"


def load_device_settings(device: Path) -> dict:
    return {**DEVICE_DEFAULTS, **read_json(device / DEVICE_SETTINGS_REL, {})}


def save_device_settings(device: Path, data: dict) -> None:
    write_json(device / DEVICE_SETTINGS_REL, data)


def playlist_selection(device_settings: dict, server: dict) -> dict:
    sel = device_settings["playlists"].get(server_key(server)) or {}
    return {"names": list(sel.get("names", [])), "patterns": list(sel.get("patterns", []))}


def selected(name: str, selection: dict) -> bool:
    return name in selection["names"] or any(fnmatch.fnmatchcase(name, p) for p in selection["patterns"])


def make_cfg(device: Path, server: dict) -> dict:
    ds = load_device_settings(device)
    if ds["device_clock"] not in ("local", "utc"):
        raise SyncError(f"{DEVICE_SETTINGS_REL}: device_clock must be 'local' or 'utc'")
    return {**ds, "device": device, "server": server["url"], "server_name": server["name"],
            "user": server["user"], "selection": playlist_selection(ds, server)}


# ================================================================ subsonic


class Subsonic:
    def __init__(self, server: dict):
        self.server = server["url"]
        self.user = server["user"]
        self.salt = server["salt"]
        self.token = server["token"]
        self.host = urllib.parse.urlsplit(self.server).netloc or self.server

    def _url(self, endpoint: str, params: list[tuple[str, str]]) -> str:
        auth = [("u", self.user), ("t", self.token), ("s", self.salt), ("v", API_VERSION),
                ("c", CLIENT_NAME), ("f", "json")]
        return f"{self.server}/rest/{endpoint}?{urllib.parse.urlencode(auth + params)}"

    def _unreachable(self, exc: Exception) -> SyncError:
        reason = getattr(exc, "reason", exc)
        msg = f"Can't reach the server at {self.host} ({reason})."
        if sys.platform == "darwin" and getattr(reason, "errno", None) == 65:
            msg += (" If the server is on your network, macOS may be blocking local network access:"
                    " System Settings > Privacy & Security > Local Network.")
        return SyncError(msg)

    def call(self, endpoint: str, params: list[tuple[str, str]] | None = None) -> dict:
        url = self._url(endpoint, params or [])
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SyncError(f"{self.host}: HTTP {exc.code} from {endpoint}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise self._unreachable(exc) from exc
        except json.JSONDecodeError as exc:
            raise SyncError(f"{self.host} did not answer like a Navidrome/Subsonic server") from exc
        data = body.get("subsonic-response", {})
        if data.get("status") != "ok":
            err = data.get("error", {})
            if err.get("code") in (40, 41, 44):
                raise SyncError(f"{self.host} rejected the login for {self.user}: wrong username or password")
            raise SyncError(f"{endpoint}: {err.get('message', 'unknown error')} (code {err.get('code')})")
        return data

    def ping(self) -> None:
        self.call("ping")

    def open_stream(self, endpoint: str, params: list[tuple[str, str]]):
        try:
            resp = urllib.request.urlopen(self._url(endpoint, params), timeout=120)
        except urllib.error.HTTPError as exc:
            raise SyncError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise self._unreachable(exc) from exc
        if resp.headers.get_content_type() in ("application/json", "text/xml"):
            resp.close()
            raise SyncError(f"{endpoint}: server returned an error instead of a file")
        return resp

    def playlists(self) -> list[dict]:
        return self.call("getPlaylists").get("playlists", {}).get("playlist", [])

    def playlist_songs(self, playlist_id: str) -> list[dict]:
        return self.call("getPlaylist", [("id", playlist_id)]).get("playlist", {}).get("entry", [])

    def all_songs(self) -> list[dict]:
        songs, offset = [], 0
        while True:
            page = self.call("search3", [
                ("query", '""'), ("songCount", str(LIBRARY_PAGE)), ("songOffset", str(offset)),
                ("artistCount", "0"), ("albumCount", "0"),
            ]).get("searchResult3", {}).get("song", [])
            if not page:
                return songs
            songs += page
            offset += LIBRARY_PAGE

    def scrobble(self, plays: list[tuple[str, int]]) -> None:
        params = [("submission", "true")]
        params += [("id", song_id) for song_id, _ in plays]
        params += [("time", str(ms)) for _, ms in plays]
        self.call("scrobble", params)


# ================================================================ formatting


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def song_label(song: dict) -> str:
    return f"{song.get('artist') or 'Unknown'} - {song.get('title') or song.get('id')}"


# ================================================================ device paths

# FAT32/exFAT reject these; Rockbox playlists treat a leading '#' as a comment.
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows refuses these as file or folder names, with any extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_part(value, fallback: str, limit: int = 80) -> str:
    part = _BAD_CHARS.sub("_", str(value or "")).strip().rstrip(". ").lstrip("#").strip()
    if not part:
        part = fallback
    if len(part) > limit:
        part = part[:limit].rstrip(". ")
    if part.split(".")[0].upper() in _RESERVED:
        part = "_" + part
    return part


def album_artist(song: dict) -> str:
    # OpenSubsonic albumArtists splits "2Pac feat. Big Syke" into its parts; the
    # first is the main artist, which keeps one album in one folder.
    names = song.get("albumArtists") or []
    main = names[0].get("name") if names else None
    return main or song.get("displayAlbumArtist") or song.get("albumArtist") or song.get("artist") or ""


def album_dir(cfg: dict, song: dict) -> str:
    artist = safe_part(album_artist(song), "Unknown Artist")
    album = safe_part(song.get("album"), "Unknown Album", 70)
    if song.get("year"):
        album = f"{album} ({song['year']})"
    return f"{cfg['music_dir']}/{artist}/{album}"


def standard_path(cfg: dict, song: dict) -> str:
    """/Music/<Album Artist>/<Album> (<Year>)/<NN> - <Title>.<ext>"""
    track, disc = song.get("track"), song.get("discNumber")
    prefix = ""
    if track:
        prefix = f"{int(disc)}-{int(track):02d} - " if disc and int(disc) > 1 else f"{int(track):02d} - "
    ext = (song.get("suffix") or Path(song.get("path", "")).suffix.lstrip(".") or "bin").lower()
    title = safe_part(song.get("title"), song["id"], 90 - len(prefix))
    return f"{album_dir(cfg, song)}/{prefix}{title}.{safe_part(ext, 'bin', 8)}"


def fold(path: str) -> str:
    """FAT is case-insensitive and macOS hands back decomposed Unicode, so
    device paths are compared by this key."""
    return unicodedata.normalize("NFC", path.replace("\\", "/")).casefold()


def disambiguate(path: str, song_id: str) -> str:
    stem, dot, ext = path.rpartition(".")
    return f"{stem} [{song_id[:8]}]{dot}{ext}"


def device_rel(log_path: str) -> str:
    """Normalise a path from playback.log to a device-relative path."""
    p = log_path.strip().lstrip("/")
    return re.sub(r"^<[^>]+>/", "", p)  # multi-volume targets prefix /<MICROSD1>/


def rel_of(path: Path | str, device: Path) -> str:
    rel = os.path.relpath(path, device)
    return "" if rel == "." else rel.replace(os.sep, "/")


def scan_device_audio(device: Path) -> dict[str, int]:
    """Every audio file on the device: device-relative path -> size."""
    found = {}
    for root, dirs, files in os.walk(device):
        rel_root = rel_of(root, device)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in AUDIO_EXTS:
                continue
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            rel = f"{rel_root}/{name}" if rel_root else name
            found[unicodedata.normalize("NFC", rel)] = size
    return found


def prune_empty_dirs(device: Path, roots: set[str], keep: set[str]) -> None:
    """Remove directories under the given device-relative roots that hold
    nothing but macOS litter; the paths in `keep` are never removed."""
    for rel_root in roots:
        top = device / rel_root
        if not top.is_dir():
            continue
        for dirpath, _dirs, _files in sorted(os.walk(top), key=lambda t: -len(t[0])):
            d = Path(dirpath)
            if rel_of(d, device) in keep:
                continue
            try:
                left = [p for p in d.iterdir() if p.name != ".DS_Store" and not p.name.startswith("._")]
            except OSError:
                continue
            if not left:
                shutil.rmtree(d, ignore_errors=True)


def move_companions(src: Path, dst: Path) -> None:
    """Move album art, cue sheets etc. from src to dst once src holds no audio."""
    if not src.is_dir() or not dst.is_dir() or fold(str(src)) == fold(str(dst)):
        return
    files = [f for f in src.iterdir() if f.is_file() and not f.name.startswith(".")]
    if any(f.suffix.lstrip(".").lower() in AUDIO_EXTS for f in files):
        return
    for f in files:
        if f.suffix.lstrip(".").lower() in COMPANION_EXTS and not (dst / f.name).exists():
            move_file(f, dst / f.name)


def move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if fold(str(src)) == fold(str(dst)) and str(src) != str(dst):
        # Case-only rename on FAT goes through a temporary name.
        tmp = src.with_name(src.name + ".h2mv")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)
    src.with_name("._" + src.name).unlink(missing_ok=True)


# ================================================================ manifest / ledger


def load_manifest(device: Path) -> dict:
    manifest = {"version": 2, "songs": {}, "history": {}, "playlists": {}}
    manifest.update(read_json(device / MANIFEST_REL, {}))
    for entry in manifest["songs"].values():
        entry.setdefault("origin", "sync")  # v1 entries were all playlist downloads
    manifest["version"] = 2
    return manifest


def save_manifest(device: Path, manifest: dict) -> None:
    write_json(device / MANIFEST_REL, manifest)


def read_ledger(device: Path) -> set[str]:
    path = device / LEDGER_REL
    return set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()


def append_ledger(device: Path, keys: list[str]) -> None:
    path = device / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for key in keys:
            fh.write(key + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def to_utc_epoch(ts: int, device_clock: str) -> int:
    """Rockbox stores mktime() of its RTC; with a local-time clock that is the
    local wall time read as if it were UTC."""
    if device_clock == "utc":
        return ts
    wall = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    return int(wall.astimezone().timestamp())


def parse_playback_logs(device: Path):
    rb = device / ROCKBOX_DIR
    if not rb.is_dir():
        return [], []
    files = sorted(p for p in rb.iterdir() if LOG_PATTERN.match(p.name))
    entries = []
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 3)  # timestamp:elapsed_ms:length_ms:path (path may hold ':')
            if len(parts) != 4:
                continue
            try:
                entries.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
            except ValueError:
                continue
    return files, entries


# ================================================================ volumes and connections
#
# Best effort, standard library plus tools every install of the OS has. When
# something can't be found out, the window says "unknown" and the sync works
# the same.


def _run(cmd: list[str], timeout: float = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW if os.name == "nt" else 0).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def volume_label(path: Path) -> str:
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        root = str(path)[:3] if len(str(path)) >= 2 and str(path)[1] == ":" else str(path)
        if ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), buf, 261,
                                                        None, None, None, None, 0) and buf.value:
            label = buf.value
            return label if path == Path(root) else f"{label} {path.name}"
        return str(path)
    return path.name or str(path)


def list_volumes() -> list[dict]:
    """Mounted removable-looking volumes, Rockbox players first."""
    candidates: list[Path] = []
    if sys.platform == "darwin":
        root_dev = os.stat("/").st_dev
        for p in Path("/Volumes").iterdir():
            try:
                if p.name.startswith(("com.apple", "Backups of")) or os.stat(p).st_dev == root_dev:
                    continue
            except OSError:
                continue
            if os.path.ismount(p):
                candidates.append(p)
    elif os.name == "nt":
        import ctypes
        system = os.environ.get("SystemDrive", "C:").upper()
        drives = os.listdrives() if hasattr(os, "listdrives") else [f"{c}:\\" for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
        for d in drives:
            # 2 = removable (card readers, players), 3 = fixed (USB disks often report this)
            if d.upper().startswith(system) or ctypes.windll.kernel32.GetDriveTypeW(d) not in (2, 3):
                continue
            if os.path.exists(d):
                candidates.append(Path(d))
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
            if base.is_dir():
                for p in base.iterdir():
                    if os.path.ismount(p) and p not in candidates:
                        candidates.append(p)
    vols = [{"path": p, "name": volume_label(p), "rockbox": (p / ROCKBOX_DIR).is_dir()} for p in candidates]
    return sorted(vols, key=lambda v: (not v["rockbox"], v["name"].lower()))


def volume_root(path: Path) -> Path:
    p = path.resolve()
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def _usb_label(mbps: float | None) -> dict:
    if mbps is None:
        return {"mbps": None, "label": "USB, speed unknown", "slow": False}
    if mbps >= 5000:
        return {"mbps": mbps, "label": "USB 3 SuperSpeed", "slow": False}
    if mbps >= USB2_HIGH_SPEED_MBPS:
        return {"mbps": mbps, "label": "USB 2.0 High Speed", "slow": False}
    return {"mbps": mbps, "label": "USB Full Speed: slow", "slow": True}


def _mac_link(path: Path) -> dict:
    out = _run(["diskutil", "info", "-plist", str(path)])
    try:
        info = plistlib.loads(out.encode()) if out else {}
    except plistlib.InvalidFileException:
        info = {}
    bus, disk = info.get("BusProtocol", ""), info.get("ParentWholeDisk", "")
    if bus != "USB":
        reader = "Built-in SD card reader" if bus == "Secure Digital" else (bus or "unknown")
        return {"mbps": None, "label": reader, "slow": False, "kind": "card" if bus == "Secure Digital" else "other"}
    raw = _run(["ioreg", "-a", "-l", "-p", "IOService", "-r", "-c", "IOUSBHostDevice"])
    try:
        roots = plistlib.loads(raw.encode()) if raw else []
    except plistlib.InvalidFileException:
        roots = []

    def holds(node) -> bool:
        return node.get("BSD Name") == disk or any(holds(c) for c in node.get("IORegistryEntryChildren", []))

    best, stack = None, list(roots)
    while stack:  # the deepest USB device whose subtree holds the disk
        node = stack.pop()
        if holds(node):
            if "UsbLinkSpeed" in node:
                best = node
            stack.extend(node.get("IORegistryEntryChildren", []))
    speed = best.get("UsbLinkSpeed") if best else None
    return {**_usb_label(speed / 1e6 if speed else None), "kind": "usb"}


def _linux_link(path: Path) -> dict:
    try:
        st = os.stat(path)
        node = Path(os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"))
    except OSError:
        return {"mbps": None, "label": "unknown", "slow": False}
    if "/mmc" in str(node):
        return {"mbps": None, "label": "SD card reader", "slow": False, "kind": "card"}
    for parent in node.parents:
        speed = parent / "speed"
        if (parent / "idVendor").exists() and speed.exists():
            try:
                return {**_usb_label(float(speed.read_text().strip())), "kind": "usb"}
            except (OSError, ValueError):
                break
    return {"mbps": None, "label": "not USB" if "/usb" not in str(node) else "USB, speed unknown", "slow": False}


def _windows_link(path: Path) -> dict:
    letter = str(path)[:1]
    ps = (f"(Get-Partition -DriveLetter {letter} | Get-Disk).BusType" if letter.isalpha() else "")
    bus = _run(["powershell", "-NoProfile", "-Command", ps]).strip() if ps else ""
    if bus == "USB":
        return {"mbps": None, "label": "USB (Windows doesn't report the speed)", "slow": False, "kind": "usb"}
    if bus in ("SD", "MMC"):
        return {"mbps": None, "label": "SD card reader", "slow": False, "kind": "card"}
    return {"mbps": None, "label": bus or "unknown", "slow": False}


def link_info(path: Path) -> dict:
    if sys.platform == "darwin":
        return _mac_link(path)
    if os.name == "nt":
        return _windows_link(path)
    return _linux_link(path)


def eject_device(device: Path) -> tuple[bool, str]:
    root = volume_root(device)
    if sys.platform == "darwin":
        out = subprocess.run(["diskutil", "eject", str(root)], capture_output=True, text=True)
        return out.returncode == 0, (out.stderr or out.stdout).strip()
    if os.name == "nt":
        letter = str(root)[:2]
        _run(["powershell", "-NoProfile", "-Command",
              f"(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{letter}').InvokeVerb('Eject')"])
        for _ in range(20):
            if not os.path.exists(str(root)):
                return True, ""
            time.sleep(0.25)
        return False, "Windows did not eject the drive; use Safely Remove Hardware"
    src = _run(["findmnt", "-no", "SOURCE", "--target", str(root)]).strip()
    if src and shutil.which("udisksctl"):
        out = subprocess.run(["udisksctl", "unmount", "-b", src], capture_output=True, text=True)
        if out.returncode == 0:
            subprocess.run(["udisksctl", "power-off", "-b", src], capture_output=True)
            return True, ""
        return False, (out.stderr or out.stdout).strip()
    out = subprocess.run(["umount", str(root)], capture_output=True, text=True)
    return out.returncode == 0, (out.stderr or out.stdout).strip()


def measure_device_write(device: Path) -> float | None:
    """Sustained write speed in bytes/s: 32 MB written and fsync'd, then removed."""
    path = device / SPEEDTEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    block = os.urandom(1024 * 1024)
    try:
        start = time.monotonic()
        with path.open("wb") as fh:
            for _ in range(SPEEDTEST_BYTES // len(block)):
                fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())
        elapsed = time.monotonic() - start
    except OSError:
        return None
    finally:
        path.unlink(missing_ok=True)
    return SPEEDTEST_BYTES / elapsed if elapsed > 0 else None


# ================================================================ the run


class Run:
    """One sync: build a plan, then (after confirmation) execute it.

    All progress lives in self.state, a JSON-able dict guarded by self.lock;
    the terminal and the window both render from it."""

    def __init__(self, cfg: dict, api: Subsonic):
        self.cfg = cfg
        self.api = api
        self.device = cfg["device"]
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.cancel_event = threading.Event()
        self.listeners: list = []
        self.plan_data: dict | None = None
        self.manifest: dict | None = None
        self.state = {
            "version": 0,
            "phase": "planning",
            "step": "Starting",
            "error": None,
            "device": {"name": volume_label(self.device), "path": str(self.device)},
            "server": {"url": cfg["server"], "name": cfg.get("server_name")},
            "plan": None,
            "transfer": None,
            "result": None,
            "log": [],
        }

    # ------------------------------------------------ state plumbing

    def update(self, **values) -> None:
        with self.lock:
            for key, value in values.items():
                if isinstance(value, dict) and isinstance(self.state.get(key), dict):
                    self.state[key].update(value)
                else:
                    self.state[key] = value
            self.state["version"] += 1
            self.changed.notify_all()

    def log(self, text: str, level: str = "info") -> None:
        entry = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "text": text}
        with self.lock:
            self.state["log"].append(entry)
            del self.state["log"][:-400]
            self.state["version"] += 1
            self.changed.notify_all()
        for listener in self.listeners:
            listener(entry)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    # ------------------------------------------------ planning

    def build_plan(self) -> dict:
        cfg, device = self.cfg, self.device
        if not device.is_dir():
            raise SyncError(f"{device} is not available. Is the card or player plugged in?")
        rockbox = (device / ROCKBOX_DIR).is_dir()

        self.update(step="Checking the connection")
        link = link_info(device)
        usage = shutil.disk_usage(device)
        self.update(device={"free": usage.free, "total": usage.total, "link": link})

        self.update(step="Measuring the device's write speed")
        write_bps = measure_device_write(device)
        self.update(device={"write_bps": write_bps})

        self.update(step=f"Reading the library on {self.api.host}")
        library = self.api.all_songs()
        by_id = {s["id"]: s for s in library}
        by_size: dict[tuple[int, str], list[dict]] = collections.defaultdict(list)
        for song in library:
            if song.get("size"):
                by_size[(int(song["size"]), (song.get("suffix") or "").lower())].append(song)

        self.update(step="Reading playlists")
        chosen = [p for p in self.api.playlists() if selected(p["name"], cfg["selection"])]
        playlist_tracks: dict[str, list[str]] = {}
        wanted: dict[str, dict] = {}
        for pl in chosen:
            entries = self.api.playlist_songs(pl["id"])
            playlist_tracks[pl["name"]] = [e["id"] for e in entries]
            for e in entries:
                wanted.setdefault(e["id"], by_id.get(e["id"], e))

        self.update(step="Scanning the device")
        manifest = load_manifest(device)
        self.manifest = manifest
        by_manifest_path = {fold(e["path"]): sid for sid, e in manifest["songs"].items()}

        # Identify every audio file on the device: manifest first, then an
        # exact size + type match against the server library.
        located: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
        unmatched_files = []
        for rel, size in scan_device_audio(device).items():
            sid = by_manifest_path.get(fold(rel))
            if sid is None:
                candidates = by_size.get((size, rel.rsplit(".", 1)[-1].lower()), [])
                if len(candidates) == 1:
                    sid = candidates[0]["id"]
            if sid is None:
                unmatched_files.append(rel)
            else:
                located[sid].append((rel, size))

        moves, duplicates, adopt = [], [], {}
        layout: dict[str, dict] = {}  # song id -> where it will be after organizing
        taken: set[str] = set()
        for sid, copies in located.items():
            song = by_id.get(sid)
            entry = manifest["songs"].get(sid)
            # A copy H2Sync did not write means you have it yourself: keep it,
            # even if a synced copy of the same song exists too.
            own_copy = any(fold(rel) not in by_manifest_path for rel, _ in copies)
            origin = "keep" if (own_copy or not entry) else entry["origin"]
            if not song:
                # Known to the manifest but gone from the server: leave it be.
                layout[sid] = {"path": copies[0][0], "size": copies[0][1], "origin": origin}
                taken.add(fold(copies[0][0]))
                continue
            target = standard_path(cfg, song) if cfg["organize"] else copies[0][0]
            if fold(target) in taken:
                target = disambiguate(target, sid)
            taken.add(fold(target))
            keep_copy = next((c for c in copies if fold(c[0]) == fold(target)), copies[0])
            if keep_copy[0] != target:
                moves.append({"id": sid, "from": keep_copy[0], "to": target, "label": song_label(song)})
            for other in copies:
                if other is not keep_copy:
                    duplicates.append({"id": sid, "path": other[0], "size": other[1], "label": song_label(song)})
            if not entry:
                adopt[sid] = keep_copy[0]
            layout[sid] = {"path": target, "size": keep_copy[1], "origin": origin}

        downloads = []
        for sid, song in wanted.items():
            if sid in layout:
                continue
            target = standard_path(cfg, song)
            if fold(target) in taken:
                target = disambiguate(target, sid)
            taken.add(fold(target))
            downloads.append({"id": sid, "path": target, "size": int(song.get("size") or 0),
                              "label": song_label(song)})

        deletions = [
            {"id": sid, "path": e["path"], "size": e["size"], "label": song_label(by_id.get(sid, {"id": sid}))}
            for sid, e in layout.items() if e["origin"] == "sync" and sid not in wanted
        ]

        covers = []
        if cfg["covers"]:
            seen_dirs = set()
            for sid in list(wanted) + list(layout):
                song = by_id.get(sid)
                if not song or not song.get("coverArt"):
                    continue
                d = album_dir(cfg, song)
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                if not any((device / d / name).exists() for name in ("cover.jpg", "folder.jpg", "cover.png", "folder.png")):
                    covers.append({"dir": d, "cover": song["coverArt"]})

        playlists = [{
            "name": name,
            "file": f"{cfg['playlist_dir']}/{safe_part(name, 'playlist')}.m3u8",
            "tracks": len(ids),
            "new": sum(1 for i in ids if i not in layout),
        } for name, ids in playlist_tracks.items()]
        stale_playlists = [{"name": n, "file": f} for n, f in manifest["playlists"].items()
                           if n not in playlist_tracks]

        # Plays from the playback log. Paths may be from before this run's
        # moves, so every old and new path maps to its song.
        log_files, entries = parse_playback_logs(device)
        path_to_id = {fold(k): v for k, v in manifest["history"].items()}
        path_to_id.update({fold(e["path"]): sid for sid, e in manifest["songs"].items()})
        for sid, copies in located.items():
            for rel, _ in copies:
                path_to_id[fold(rel)] = sid
        ledger = read_ledger(device)
        scrobbles, skipped_short, already, unknown = [], 0, 0, []
        for ts, elapsed_ms, length_ms, raw in entries:
            if length_ms <= 0 or (elapsed_ms < length_ms * cfg["min_play_fraction"]
                                  and elapsed_ms < cfg["min_play_seconds"] * 1000):
                skipped_short += 1
                continue
            rel = device_rel(raw)
            key = f"{ts}\t{rel}"
            if key in ledger:
                already += 1
                continue
            sid = path_to_id.get(fold(rel))
            if not sid:
                unknown.append(rel)
                continue
            utc = to_utc_epoch(ts, cfg["device_clock"])
            scrobbles.append({"id": sid, "ms": utc * 1000, "key": key,
                              "when": datetime.fromtimestamp(utc).strftime("%a %d %b %H:%M"),
                              "label": song_label(by_id.get(sid, {"id": sid}))})

        download_bytes = sum(d["size"] for d in downloads)
        freed = sum(d["size"] for d in deletions) + sum(d["size"] for d in duplicates)
        estimate = (download_bytes / write_bps + len(downloads) * PER_FILE_OVERHEAD_S) if write_bps else None

        self.plan_data = {
            "wanted": wanted, "playlist_tracks": playlist_tracks, "moves": moves,
            "duplicates": duplicates, "adopt": adopt, "layout": layout, "downloads": downloads,
            "deletions": deletions, "covers": covers, "playlists": playlists,
            "stale_playlists": stale_playlists, "scrobbles": scrobbles,
            "log_files": [str(p) for p in log_files],
        }
        plan = {
            "rockbox": rockbox,
            "library_songs": len(library),
            "selection": cfg["selection"],
            "playlists": playlists,
            "stale_playlists": stale_playlists,
            "tracks_total": len(wanted),
            "on_device": sum(1 for sid in wanted if sid in layout),
            "downloads": len(downloads),
            "download_bytes": download_bytes,
            "download_items": [{"label": d["label"], "size": d["size"]} for d in downloads[:500]],
            "estimate_s": estimate,
            "moves": len(moves),
            "move_items": [{"from": m["from"], "to": m["to"]} for m in moves[:500]],
            "kept_files": sum(1 for e in layout.values() if e["origin"] == "keep"),
            "synced_files": sum(1 for e in layout.values() if e["origin"] == "sync"),
            "duplicates": len(duplicates),
            "duplicate_items": [{"path": d["path"], "size": d["size"]} for d in duplicates[:300]],
            "deletions": len(deletions),
            "deletion_items": [{"label": d["label"], "size": d["size"]} for d in deletions[:500]],
            "covers": len(covers),
            "unmatched_files": len(unmatched_files),
            "unmatched_items": sorted(unmatched_files)[:500],
            "scrobbles": {
                "pending": len(scrobbles), "already": already, "short": skipped_short,
                "unknown": len(unknown), "logging": bool(log_files),
                "items": [{"when": s["when"], "label": s["label"]} for s in scrobbles[:300]],
                "unknown_items": sorted(set(unknown))[:100],
            },
            "fits": download_bytes < usage.free + freed - 50 * 1000 * 1000,
            "nothing_to_do": not (scrobbles or moves or downloads or deletions or duplicates
                                  or covers or stale_playlists)
                             and all((device / p["file"]).exists() for p in playlists),
        }
        self.update(plan=plan, phase="ready", step="Ready")
        return plan

    # ------------------------------------------------ execution

    def execute(self) -> None:
        p, manifest = self.plan_data, self.manifest
        assert p is not None and manifest is not None
        result = {"scrobbled": 0, "moved": 0, "downloaded": 0, "downloaded_bytes": 0, "failed": 0,
                  "covers": 0, "playlists": 0, "deleted": 0, "cancelled": False}
        started = time.monotonic()
        try:
            self._scrobbles(p, result)
            self._organize(p, manifest, result)
            self._transfer(p, manifest, result)
            self._covers(p, result)
        except Cancelled:
            result["cancelled"] = True
            self.log("Cancelled. Finished files are kept; the next sync continues from here.", "warn")
        # Playlists are written even after a cancel, with the tracks that made it.
        self._playlists(p, manifest, result)
        if not result["cancelled"]:
            self._cleanup(p, manifest, result)
        save_manifest(self.device, manifest)
        result["elapsed_s"] = time.monotonic() - started
        self.update(result=result, phase="cancelled" if result["cancelled"] else "done",
                    step="Cancelled" if result["cancelled"] else "Finished")

    def _scrobbles(self, p: dict, result: dict) -> None:
        self.update(phase="scrobbles", step=f"Submitting plays to {self.api.host}")
        pending = p["scrobbles"]
        for i in range(0, len(pending), SCROBBLE_BATCH):
            batch = pending[i:i + SCROBBLE_BATCH]
            self.api.scrobble([(s["id"], s["ms"]) for s in batch])
            append_ledger(self.device, [s["key"] for s in batch])
            result["scrobbled"] += len(batch)
        if pending:
            self.log(f"Submitted {len(pending)} plays to {self.api.host}", "ok")
        # Every log entry is now submitted, already sent, or not scrobblable, so
        # the logs can leave the device; Rockbox starts a new one.
        if p["log_files"]:
            archive = self.device / ARCHIVE_REL
            archive.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for f in map(Path, p["log_files"]):
                if f.exists():
                    shutil.move(str(f), archive / f"{f.stem}-{stamp}.log")

    def _organize(self, p: dict, manifest: dict, result: dict) -> None:
        for sid, rel in p["adopt"].items():
            manifest["songs"][sid] = {**p["layout"][sid], "path": rel}
            manifest["history"][rel] = sid
        for sid, e in p["layout"].items():
            if sid in manifest["songs"]:
                manifest["songs"][sid]["origin"] = e["origin"]
        moves = p["moves"]
        if not moves:
            save_manifest(self.device, manifest)
            return
        self.update(phase="organize", step=f"Moving {len(moves)} files into the standard layout")
        targets_by_source: dict[str, set[str]] = collections.defaultdict(set)
        for i, m in enumerate(moves, 1):
            self.check_cancel()
            try:
                move_file(self.device / m["from"], self.device / m["to"])
            except OSError as exc:
                self.log(f"Could not move {m['from']}: {exc}", "error")
                continue
            manifest["songs"][m["id"]]["path"] = m["to"]
            manifest["history"][m["from"]] = m["id"]
            manifest["history"][m["to"]] = m["id"]
            targets_by_source[str(Path(m["from"]).parent)].add(str(Path(m["to"]).parent))
            result["moved"] += 1
            if i % 25 == 0 or i == len(moves):
                self.update(step=f"Moved {i} of {len(moves)} files into the standard layout",
                            organize={"done": i, "total": len(moves)})
                save_manifest(self.device, manifest)
        # Album art and cue sheets follow their tracks when a folder moved as a whole.
        for src_dir, targets in targets_by_source.items():
            if len(targets) == 1:
                move_companions(self.device / src_dir, self.device / next(iter(targets)))
        self.log(f"Moved {result['moved']} files into /{self.cfg['music_dir']}", "ok")
        save_manifest(self.device, manifest)

    def _transfer(self, p: dict, manifest: dict, result: dict) -> None:
        downloads = p["downloads"]
        if not downloads:
            return
        self.update(phase="transfer", step=f"Copying {len(downloads)} tracks")
        self._sweep_parts()
        t = {"files_total": len(downloads), "files_done": 0, "failed": 0,
             "bytes_total": sum(d["size"] for d in downloads) or 1, "bytes_done": 0,
             "rate": 0.0, "eta_s": None, "elapsed_s": 0.0, "net_bps": None, "write_bps": None,
             "bottleneck": None, "current": None}
        start = time.monotonic()
        samples = collections.deque([(start, 0)])
        timing = {"read": 0.0, "write": 0.0, "bytes": 0}
        last_push = [0.0]

        def push(force=False):
            now = time.monotonic()
            if not force and now - last_push[0] < 0.25:
                return
            last_push[0] = now
            while len(samples) > 2 and samples[1][0] < now - RATE_WINDOW_S:
                samples.popleft()
            (t0, b0), (t1, b1) = samples[0], samples[-1]
            if t1 - t0 > 0.5:
                t["rate"] = (b1 - b0) / (t1 - t0)
            t["elapsed_s"] = now - start
            # The window spans file boundaries, so per-file overhead is in the rate.
            t["eta_s"] = (t["bytes_total"] - t["bytes_done"]) / t["rate"] if t["rate"] > 0 else None
            if timing["read"] > 0.2:
                t["net_bps"] = timing["bytes"] / timing["read"]
            if timing["write"] > 0.2:
                t["write_bps"] = timing["bytes"] / timing["write"]
            # Writes too fast to time meaningfully fall back to the planning speed test.
            write = t["write_bps"] or self.state["device"].get("write_bps")
            if t["net_bps"] and write:
                t["bottleneck"] = "device" if write < t["net_bps"] else "network"
            self.update(transfer=dict(t, current=dict(t["current"]) if t["current"] else None))

        for d in downloads:
            self.check_cancel()
            dest = self.device / d["path"]
            tmp = dest.with_name(dest.name + PART_SUFFIXES[0])
            t["current"] = {"label": d["label"], "path": d["path"], "size": d["size"], "done": 0}
            push(force=True)
            got = 0
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with self.api.open_stream("download", [("id", d["id"])]) as resp, tmp.open("wb") as fh:
                    while True:
                        if self.cancel_event.is_set():
                            raise Cancelled()
                        r0 = time.monotonic()
                        chunk = resp.read(CHUNK_SIZE)
                        r1 = time.monotonic()
                        if not chunk:
                            break
                        fh.write(chunk)
                        w1 = time.monotonic()
                        timing["read"] += r1 - r0
                        timing["write"] += w1 - r1
                        timing["bytes"] += len(chunk)
                        got += len(chunk)
                        t["bytes_done"] += len(chunk)
                        t["current"]["done"] = got
                        samples.append((w1, t["bytes_done"]))
                        push()
                    # Count the flush to the card as write time: the page cache
                    # otherwise hides how slow the device really is.
                    f0 = time.monotonic()
                    fh.flush()
                    os.fsync(fh.fileno())
                    timing["write"] += time.monotonic() - f0
                tmp.replace(dest)
            except Cancelled:
                tmp.unlink(missing_ok=True)
                t["bytes_done"] -= got
                t["current"] = None
                push(force=True)
                raise
            except (SyncError, OSError, urllib.error.URLError) as exc:
                tmp.unlink(missing_ok=True)
                t["bytes_done"] -= got
                t["bytes_total"] = max(t["bytes_total"] - d["size"], t["bytes_done"], 1)
                t["failed"] += 1
                result["failed"] += 1
                self.log(f"Failed: {d['label']} ({exc})", "error")
                continue
            # Planned sizes come from Navidrome; correct the total to what arrived.
            t["bytes_total"] = max(t["bytes_total"] + got - d["size"], t["bytes_done"], 1)
            t["files_done"] += 1
            result["downloaded"] += 1
            result["downloaded_bytes"] += got
            manifest["songs"][d["id"]] = {"path": d["path"], "size": got, "origin": "sync"}
            manifest["history"][d["path"]] = d["id"]
            save_manifest(self.device, manifest)
            self.log(f"Copied {d['label']} · {fmt_bytes(got)}", "ok")
            push(force=True)
        t["current"] = None
        push(force=True)

    def _sweep_parts(self) -> None:
        """Remove partial files a killed run left behind."""
        for rel in (self.cfg["music_dir"], *LEGACY_DIRS):
            root = self.device / rel
            if root.is_dir():
                for f in root.rglob("*"):
                    if f.name.endswith(PART_SUFFIXES):
                        f.unlink(missing_ok=True)

    def _covers(self, p: dict, result: dict) -> None:
        if not p["covers"]:
            return
        self.update(phase="covers", step="Adding album covers")
        for c in p["covers"]:
            self.check_cancel()
            target_dir = self.device / c["dir"]
            if not target_dir.is_dir():
                continue  # album not on the device, e.g. its download failed
            try:
                with self.api.open_stream("getCoverArt", [("id", c["cover"]), ("size", "500")]) as resp:
                    data = resp.read()
            except (SyncError, OSError, urllib.error.URLError):
                continue
            if data[:3] == b"\xff\xd8\xff":  # Rockbox shows JPEG album art
                (target_dir / "cover.jpg").write_bytes(data)
                result["covers"] += 1
        if result["covers"]:
            self.log(f"Added {result['covers']} album covers", "ok")

    def _playlists(self, p: dict, manifest: dict, result: dict) -> None:
        if not p["playlists"]:
            return
        self.update(phase="playlists", step="Writing playlists")
        (self.device / self.cfg["playlist_dir"]).mkdir(parents=True, exist_ok=True)
        for pl in p["playlists"]:
            lines, present = ["#EXTM3U"], 0
            for sid in p["playlist_tracks"][pl["name"]]:
                entry = manifest["songs"].get(sid)
                if not entry or not (self.device / entry["path"]).exists():
                    continue
                song = p["wanted"].get(sid, {})
                lines.append(f"#EXTINF:{int(song.get('duration') or -1)},{song_label(song)}")
                lines.append("/" + entry["path"])
                present += 1
            (self.device / pl["file"]).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            manifest["playlists"][pl["name"]] = pl["file"]
            result["playlists"] += 1
            missing = pl["tracks"] - present
            self.log(f"Wrote {pl['file']} · {present} tracks" + (f" ({missing} missing)" if missing else ""),
                     "warn" if missing else "ok")

    def _cleanup(self, p: dict, manifest: dict, result: dict) -> None:
        self.update(phase="cleanup", step="Cleaning up")
        for s in p["stale_playlists"]:
            (self.device / s["file"]).unlink(missing_ok=True)
            manifest["playlists"].pop(s["name"], None)
            self.log(f"Removed playlist {s['file']}", "ok")
        for d in p["deletions"]:
            (self.device / d["path"]).unlink(missing_ok=True)
            manifest["songs"].pop(d["id"], None)
            result["deleted"] += 1
        for d in p["duplicates"]:
            keep = manifest["songs"].get(d["id"])
            # Only remove a duplicate once the copy being kept is confirmed in place.
            if keep and fold(keep["path"]) != fold(d["path"]) and (self.device / keep["path"]).exists():
                dup = self.device / d["path"]
                dup.unlink(missing_ok=True)
                result["deleted"] += 1
                move_companions(dup.parent, (self.device / keep["path"]).parent)
        if result["deleted"]:
            self.log(f"Deleted {result['deleted']} files no longer needed", "ok")
        roots = {self.cfg["music_dir"], *LEGACY_DIRS}
        roots |= {Path(m["from"]).parts[0] for m in p["moves"] if len(Path(m["from"]).parts) > 1}
        prune_empty_dirs(self.device, roots, keep={self.cfg["music_dir"]})


def start_execution(run: Run) -> threading.Thread:
    def target():
        try:
            run.execute()
        except Exception as exc:
            run.update(error=str(exc), phase="error", step="Failed")
            run.log(f"Error: {exc}", "error")
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def plan_worker(run: Run) -> None:
    try:
        run.build_plan()
    except Exception as exc:
        run.update(error=str(exc), phase="error", step="Failed")


# ================================================================ terminal front end


class Terminal:
    """Log lines scroll; on a TTY a status block is redrawn under them."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.live = 0

    def _clear(self):
        if self.live:
            sys.stdout.write("\x1b[1A\x1b[2K" * (self.live - 1) + "\r\x1b[2K")
            self.live = 0

    def line(self, text: str) -> None:
        if self.tty:
            self._clear()
        print(text, flush=True)

    def status(self, state: dict | None) -> None:
        if not self.tty:
            return
        self._clear()
        if state is None:
            sys.stdout.flush()
            return
        width = shutil.get_terminal_size((100, 20)).columns - 1
        t = state.get("transfer")
        if state["phase"] == "transfer" and t:
            frac = t["bytes_done"] / t["bytes_total"]
            filled = int(frac * 24)
            lines = [f"  {'█' * filled}{'░' * (24 - filled)} {frac * 100:3.0f}%  "
                     f"{t['files_done'] + t['failed']}/{t['files_total']} tracks  "
                     f"{fmt_bytes(t['bytes_done'])} / {fmt_bytes(t['bytes_total'])}  "
                     f"{fmt_bytes(t['rate'])}/s  ETA {fmt_duration(t['eta_s'])}  "
                     f"elapsed {fmt_duration(t['elapsed_s'])}"]
            if t.get("bottleneck"):
                lines.append(f"  network {fmt_bytes(t['net_bps'])}/s · device writes "
                             f"{fmt_bytes(t['write_bps'])}/s · limited by the {t['bottleneck']}")
            c = t.get("current")
            if c:
                cf = c["done"] / c["size"] if c["size"] else 0
                lines.append(f"  {'▮' * int(cf * 10)}{'▯' * (10 - int(cf * 10))}  "
                             f"{fmt_bytes(c['size'])}  {c['label']}")
        else:
            lines = [f"  {state['step']}..."]
        sys.stdout.write("\n".join(l[:width] for l in lines))
        sys.stdout.flush()
        self.live = len(lines)


def print_plan(state: dict) -> None:
    plan, dev = state["plan"], state["device"]
    link = dev.get("link", {})
    w = dev.get("write_bps")
    s = plan["scrobbles"]
    rows = [
        ("Device", f"{dev['path']} · {fmt_bytes(dev.get('free', 0))} free of {fmt_bytes(dev.get('total', 0))}"),
        ("Link", (f"{link['mbps']:.0f} Mb/s · " if link.get("mbps") else "") + link.get("label", "unknown")
         + ("  << plug straight into the computer, try another cable" if link.get("slow") else "")),
        ("Card", f"writes at {fmt_bytes(w)}/s" if w else "write speed unknown"),
        ("Plays", f"{s['pending']} to submit · {s['already']} already sent · {s['unknown']} unidentified · {s['short']} too short"
         if s["logging"] else "no playback log (turn on Settings > Playback > Logging)"),
        ("On device", f"{plan['kept_files']} library files kept · {plan['synced_files']} synced files"
         + (f" · {plan['unmatched_files']} not on the server (left as is)" if plan["unmatched_files"] else "")),
    ]
    if plan["moves"]:
        rows.append(("Organize", f"{plan['moves']} files move into /Music/<Album Artist>/<Album> (<Year>)/"))
    rows.append(("Playlists", f"{len(plan['playlists'])} · {plan['tracks_total']} tracks · {plan['on_device']} already on the device"))
    rows.append(("Copy", f"{plan['downloads']} tracks · {fmt_bytes(plan['download_bytes'])}"
                 + (f" · about {fmt_duration(plan['estimate_s'])}" if plan["estimate_s"] else "")))
    if plan["deletions"] or plan["duplicates"] or plan["stale_playlists"]:
        rows.append(("Remove", f"{plan['deletions']} unused synced files · {plan['duplicates']} duplicate copies"
                     f" · {len(plan['stale_playlists'])} old playlists"))
    if plan["covers"]:
        rows.append(("Covers", f"{plan['covers']} albums get cover.jpg"))
    if not plan["fits"]:
        rows.append(("WARNING", "not enough free space on the device"))
    print()
    for k, v in rows:
        print(f"  {k:<10} {v}")
    print()


def run_terminal(cfg: dict, api: Subsonic, assume_yes: bool, plan_only: bool) -> int:
    run, term = Run(cfg, api), Terminal()
    run.listeners.append(lambda e: term.line(f"  {e['text']}"))
    planner = threading.Thread(target=plan_worker, args=(run,), daemon=True)
    planner.start()
    while planner.is_alive():
        planner.join(0.3)
        term.status(run.snapshot())
    term.status(None)
    state = run.snapshot()
    if state["phase"] != "ready":
        print(f"error: {state['error']}")
        return 1
    print_plan(state)
    if plan_only:
        return 0
    if state["plan"]["nothing_to_do"]:
        print("Everything is already in sync.")
        return 0
    if not state["plan"]["fits"]:
        return 1
    if not assume_yes and input("Start sync? [y/N] ").strip().lower() not in ("y", "yes"):
        return 0
    worker = start_execution(run)
    try:
        while worker.is_alive():
            worker.join(0.3)
            term.status(run.snapshot())
    except KeyboardInterrupt:
        run.cancel_event.set()
        term.line("  Cancelling after the current file...")
        worker.join()
    term.status(None)
    state = run.snapshot()
    r = state.get("result") or {}
    if state["error"]:
        print(f"error: {state['error']}")
    else:
        print(f"\n{'Cancelled' if r.get('cancelled') else 'Done'} in {fmt_duration(r.get('elapsed_s'))}: "
              f"{r.get('scrobbled', 0)} plays submitted · {r.get('moved', 0)} moved · "
              f"{r.get('downloaded', 0)} copied ({fmt_bytes(r.get('downloaded_bytes', 0))}) · "
              f"{r.get('failed', 0)} failed · {r.get('deleted', 0)} deleted")
    return 1 if state["error"] or r.get("failed") else 0


# ================================================================ main


def default_device(settings: Settings, wanted: str | None) -> Path | None:
    """--device, else the last device used, else the volume H2Sync runs from,
    else the first Rockbox player found."""
    if wanted:
        return Path(wanted).expanduser()
    last = settings.data.get("last_device")
    if last and Path(last).is_dir():
        return Path(last)
    vols = list_volumes()
    home_root = volume_root(settings.home)
    for v in vols:
        if v["path"] == home_root:
            return v["path"]
    return vols[0]["path"] if vols and vols[0]["rockbox"] else None


def acquire_lock():
    """One H2Sync per computer user: a second window on the same card would race."""
    path = Path(tempfile.gettempdir()) / f"h2sync-{getpass.getuser()}.lock"
    fh = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", default="ui", choices=["ui", "sync", "plan", "list"])
    parser.add_argument("--home", help="the portable H2Sync folder (settings live in <home>/settings)")
    parser.add_argument("--device", help="device or folder to sync")
    parser.add_argument("--server", help="server name from settings")
    parser.add_argument("--yes", action="store_true", help="sync: start without asking")
    args = parser.parse_args()

    if sys.stdout is None:  # pythonw on Windows: keep a log for troubleshooting
        sys.stdout = sys.stderr = open(Path(tempfile.gettempdir()) / "h2sync.log", "a", encoding="utf-8")

    settings = Settings(Path(args.home).expanduser() if args.home else Path(__file__).resolve().parent)
    lock = acquire_lock()
    if lock is None:
        print("H2Sync is already running")
        return 1
    try:
        if args.command == "ui":
            import ui_tk  # tkinter is only needed for the window
            return ui_tk.run(settings, default_device(settings, args.device), args.server)

        server = settings.server(args.server or settings.data.get("last_server")) or (
            settings.servers[0] if settings.servers else None)
        if not server:
            raise SyncError("no server configured: add one in the window first")
        if not server.get("token"):
            server["salt"], server["token"] = login_token(getpass.getpass(f"Password for {server['user']}: "))
            settings.put_server(server)
        api = Subsonic(server)
        device = default_device(settings, args.device)
        if args.command == "list":
            sel = playlist_selection(load_device_settings(device), server) if device else {"names": [], "patterns": []}
            for p in sorted(api.playlists(), key=lambda p: p["name"].lower()):
                print(f"{'*' if selected(p['name'], sel) else ' '} {p['name']}  ({p.get('songCount', 0)} songs)")
            print(f"\n* = chosen for {device}")
            return 0
        if not device:
            raise SyncError("no device: pass --device")
        settings.remember(device, server)
        return run_terminal(make_cfg(device, server), api, args.yes, args.command == "plan")
    except SyncError as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
