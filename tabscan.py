"""
Find the tabs that are open in the installed web browsers.

Browsers do not offer other programs a way to ask for their tabs, but every one
of them keeps its open tabs on disk so it can restore them after a crash, and
that is what is read here -- no extension, no remote-debugging port, nothing to
set up:

* Chromium-based browsers (Chrome, Brave, Edge, Vivaldi, Opera, Chromium) write
  an append-only command log, `Sessions/Session_<time>`, in the "SNSS" format.
  Replaying the commands gives the tabs that are open right now.
* Firefox and its forks write `sessionstore-backups/recovery.jsonlz4`: JSON
  compressed with a bare LZ4 block behind Mozilla's own 12-byte header.

Both are updated a few seconds after a tab changes, so a page opened a moment
ago can be missing; scanning again picks it up. Only the standard library is
used, so this works in the packaged .exe as it is.
"""

from __future__ import annotations

import configparser
import json
import os
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"


@dataclass
class BrowserTab:
    browser: str          # "Brave", "Firefox", ...
    profile: str          # profile display name, "" when the browser has only one
    url: str
    title: str
    running: bool         # False: read from the last session of a closed browser

    @property
    def source(self) -> str:
        return f"{self.browser} ({self.profile})" if self.profile else self.browser


@dataclass
class BrowserResult:
    """What one browser profile contributed to a scan, for the summary line."""
    browser: str
    profile: str
    running: bool
    tabs: list[BrowserTab]
    error: str = ""

    @property
    def source(self) -> str:
        return f"{self.browser} ({self.profile})" if self.profile else self.browser


# --------------------------------------------------------------------------
# Where browsers keep their profiles
# --------------------------------------------------------------------------

def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _home(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _platform_dirs(win: list[tuple[str, str]], mac: list[str], linux: list[str]) -> list[Path]:
    """`win` entries are (environment variable, relative path)."""
    if IS_WINDOWS:
        out = []
        for env, rel in win:
            base = _env_path(env)
            if base:
                out.append(base / rel)
        return out
    if IS_MAC:
        return [_home("Library", "Application Support", p) for p in mac]
    return [_home(p) for p in linux]


# (name, process names, user-data directories)
CHROMIUM_BROWSERS = [
    ("Chrome", ("chrome.exe", "chrome", "google chrome"), _platform_dirs(
        [("LOCALAPPDATA", "Google/Chrome/User Data")],
        ["Google/Chrome"], [".config/google-chrome"])),
    ("Brave", ("brave.exe", "brave", "brave browser"), _platform_dirs(
        [("LOCALAPPDATA", "BraveSoftware/Brave-Browser/User Data")],
        ["BraveSoftware/Brave-Browser"],
        [".config/BraveSoftware/Brave-Browser",
         ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"])),
    ("Edge", ("msedge.exe", "msedge", "microsoft edge"), _platform_dirs(
        [("LOCALAPPDATA", "Microsoft/Edge/User Data")],
        ["Microsoft Edge"], [".config/microsoft-edge"])),
    ("Vivaldi", ("vivaldi.exe", "vivaldi-bin", "vivaldi"), _platform_dirs(
        [("LOCALAPPDATA", "Vivaldi/User Data")], ["Vivaldi"], [".config/vivaldi"])),
    ("Opera", ("opera.exe", "opera"), _platform_dirs(
        [("APPDATA", "Opera Software/Opera Stable")],
        ["com.operasoftware.Opera"], [".config/opera"])),
    ("Opera GX", ("opera.exe", "opera"), _platform_dirs(
        [("APPDATA", "Opera Software/Opera GX Stable")],
        ["com.operasoftware.OperaGX"], [])),
    ("Chromium", ("chrome.exe", "chromium", "chromium-browser", "chrome"), _platform_dirs(
        [("LOCALAPPDATA", "Chromium/User Data")],
        ["Chromium"], [".config/chromium", "snap/chromium/common/chromium"])),
]

# (name, process names, directories that contain profile folders)
FIREFOX_BROWSERS = [
    ("Firefox", ("firefox.exe", "firefox", "firefox-bin"), _platform_dirs(
        [("APPDATA", "Mozilla/Firefox/Profiles")],
        ["Firefox/Profiles"],
        [".mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
         ".var/app/org.mozilla.firefox/.mozilla/firefox"])),
    ("LibreWolf", ("librewolf.exe", "librewolf"), _platform_dirs(
        [("APPDATA", "librewolf/Profiles")], ["librewolf/Profiles"], [".librewolf"])),
    ("Waterfox", ("waterfox.exe", "waterfox"), _platform_dirs(
        [("APPDATA", "Waterfox/Profiles")], ["Waterfox/Profiles"], [".waterfox"])),
    ("Zen", ("zen.exe", "zen"), _platform_dirs(
        [("APPDATA", "zen/Profiles")], ["zen/Profiles"], [".zen"])),
    ("Floorp", ("floorp.exe", "floorp"), _platform_dirs(
        [("APPDATA", "Floorp/Profiles")], ["Floorp/Profiles"], [".floorp"])),
]


# --------------------------------------------------------------------------
# Is the browser running?
# --------------------------------------------------------------------------

def running_process_names() -> set[str] | None:
    """Lower-cased executable names of every running process; None if unknown."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                errors="replace", timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return {line.split('","')[0].strip('"').lower()
                    for line in out.splitlines() if line.startswith('"')}
        out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True,
                             text=True, errors="replace", timeout=20).stdout
        return {os.path.basename(line.strip()).lower() for line in out.splitlines() if line.strip()}
    except Exception:
        return None


# --------------------------------------------------------------------------
# Chromium: SNSS session files
# --------------------------------------------------------------------------

# Command ids from chromium/src/components/sessions/core/session_service_commands.cc
CMD_SET_TAB_WINDOW = 0
CMD_SET_TAB_INDEX_IN_WINDOW = 2
CMD_NAV_PRUNED_FROM_BACK = 5
CMD_UPDATE_TAB_NAVIGATION = 6
CMD_SET_SELECTED_NAV_INDEX = 7
CMD_NAV_PRUNED_FROM_FRONT = 11
CMD_TAB_CLOSED = 16
CMD_WINDOW_CLOSED = 17
CMD_NAV_PRUNED = 24


class _Pickle:
    """Reader for Chromium's base::Pickle: 4-byte aligned, little-endian fields."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 4  # skip the payload-size header

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated pickle")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += (n + 3) & ~3
        return chunk

    def int(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def string(self) -> str:
        n = self.int()
        if n < 0:
            raise ValueError("bad length")
        return self._take(n).decode("utf-8", "replace")

    def string16(self) -> str:
        n = self.int()
        if n < 0:
            raise ValueError("bad length")
        return self._take(n * 2).decode("utf-16-le", "replace")


@dataclass
class _SnssTab:
    window: int = -1
    index: int = 0
    selected: int = -1


def parse_snss(data: bytes) -> list[tuple[str, str]]:
    """
    Replay a Chromium session file and return (url, title) for each open tab,
    in window and tab-strip order.
    """
    if len(data) < 8 or data[:4] != b"SNSS":
        raise ValueError("not a Chromium session file")
    version = struct.unpack("<i", data[4:8])[0]
    if version not in (1, 3):
        # 2 is the encrypted variant, which needs the browser's own key.
        raise ValueError(f"unsupported session file version {version}")

    tabs: dict[int, _SnssTab] = {}
    navs: dict[int, dict[int, tuple[str, str]]] = {}
    closed_windows: set[int] = set()

    def ints(payload: bytes, count: int) -> tuple[int, ...]:
        return struct.unpack_from(f"<{count}i", payload)

    pos = 8
    while pos + 2 <= len(data):
        size = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if size == 0 or pos + size > len(data):
            break  # an interrupted write at the end of the file
        cmd, payload = data[pos], data[pos + 1:pos + size]
        pos += size
        try:
            if cmd == CMD_UPDATE_TAB_NAVIGATION:
                p = _Pickle(payload)
                tab_id, index = p.int(), p.int()
                url = p.string()
                title = p.string16()
                navs.setdefault(tab_id, {})[index] = (url, title)
                tabs.setdefault(tab_id, _SnssTab())
            elif cmd == CMD_SET_SELECTED_NAV_INDEX:
                tab_id, index = ints(payload, 2)
                tabs.setdefault(tab_id, _SnssTab()).selected = index
            elif cmd == CMD_SET_TAB_WINDOW:
                window_id, tab_id = ints(payload, 2)
                tabs.setdefault(tab_id, _SnssTab()).window = window_id
            elif cmd == CMD_SET_TAB_INDEX_IN_WINDOW:
                tab_id, index = ints(payload, 2)
                tabs.setdefault(tab_id, _SnssTab()).index = index
            elif cmd == CMD_TAB_CLOSED:
                (tab_id,) = ints(payload, 1)
                tabs.pop(tab_id, None)
                navs.pop(tab_id, None)
            elif cmd == CMD_WINDOW_CLOSED:
                (window_id,) = ints(payload, 1)
                closed_windows.add(window_id)
            elif cmd == CMD_NAV_PRUNED_FROM_BACK:
                tab_id, count = ints(payload, 2)
                entries = navs.get(tab_id, {})
                for i in [i for i in entries if i >= count]:
                    del entries[i]
            elif cmd == CMD_NAV_PRUNED_FROM_FRONT:
                tab_id, count = ints(payload, 2)
                entries = navs.get(tab_id, {})
                navs[tab_id] = {i - count: v for i, v in entries.items() if i >= count}
                if tab_id in tabs:
                    tabs[tab_id].selected = max(-1, tabs[tab_id].selected - count)
            elif cmd == CMD_NAV_PRUNED:
                tab_id, index, count = ints(payload, 3)
                entries = navs.get(tab_id, {})
                navs[tab_id] = {(i - count if i >= index + count else i): v
                                for i, v in entries.items()
                                if not index <= i < index + count}
        except (struct.error, ValueError):
            continue  # one damaged command should not lose the rest

    result = []
    for tab_id, tab in sorted(tabs.items(), key=lambda kv: (kv[1].window, kv[1].index)):
        if tab.window in closed_windows:
            continue
        entries = navs.get(tab_id)
        if not entries:
            continue
        index = tab.selected if tab.selected in entries else max(entries)
        url, title = entries[index]
        result.append((url, title))
    return result


def latest_session_file(profile_dir: Path) -> Path | None:
    """Newest Session_* file, or the pre-2021 `Current Session`."""
    candidates = []
    sessions = profile_dir / "Sessions"
    if sessions.is_dir():
        candidates += [p for p in sessions.glob("Session_*") if p.is_file()]
    legacy = profile_dir / "Current Session"
    if legacy.is_file():
        candidates.append(legacy)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def history_titles(profile_dir: Path, urls: list[str]) -> dict[str, str]:
    """
    Page titles from the browser history, for tabs whose title the session file
    does not have yet (Chromium writes a tab's title to it some time after the
    page has loaded). The database is locked while the browser runs, so a copy
    is read instead.
    """
    source = profile_dir / "History"
    wanted = list(dict.fromkeys(u for u in urls if u))
    if not wanted or not source.is_file():
        return {}
    titles: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        try:
            shutil.copyfile(source, copy)
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            return {}
        try:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for url, title in conn.execute(
                        f"SELECT url, title FROM urls WHERE url IN ({marks})", chunk):
                    if title:
                        titles[url] = title
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return titles


def chromium_profiles(user_data: Path) -> list[tuple[str, Path]]:
    """(display name, directory) for every profile that has session data."""
    names: dict[str, str] = {}
    try:
        state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
        for key, info in (state.get("profile", {}).get("info_cache", {}) or {}).items():
            if isinstance(info, dict) and info.get("name"):
                names[key] = str(info["name"])
    except Exception:
        pass

    dirs = [user_data]  # Opera keeps its one profile in the user-data folder itself
    try:
        dirs += sorted(p for p in user_data.iterdir()
                       if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile ")))
    except OSError:
        pass

    found = [(names.get(d.name, d.name if d != user_data else ""), d)
             for d in dirs if latest_session_file(d)]
    if len(found) == 1:
        found = [("", found[0][1])]  # no need to name the only profile
    return found


# --------------------------------------------------------------------------
# Firefox: mozLz4 session store
# --------------------------------------------------------------------------

MOZLZ4_MAGIC = b"mozLz40\0"


def lz4_block_decompress(src: bytes, size_hint: int = 0) -> bytes:
    """Decompress one raw LZ4 block (the format has no frame around it)."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        length = token >> 4
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        dst += src[i:i + length]
        i += length
        if i >= n:
            break  # the last sequence is literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            raise ValueError("corrupt LZ4 data")
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(dst) - offset
        if offset >= length:
            dst += dst[start:start + length]
        else:
            # Overlapping copy: the match repeats the last `offset` bytes.
            pattern = bytes(dst[start:])
            dst += (pattern * (length // offset + 1))[:length]
    if size_hint and len(dst) != size_hint:
        raise ValueError("LZ4 size mismatch")
    return bytes(dst)


def read_mozlz4(data: bytes) -> bytes:
    if data[:8] != MOZLZ4_MAGIC:
        raise ValueError("not a mozLz4 file")
    size = struct.unpack("<I", data[8:12])[0]
    return lz4_block_decompress(data[12:], size)


def parse_firefox_session(session: dict) -> list[tuple[str, str]]:
    result = []
    for window in session.get("windows") or []:
        for tab in window.get("tabs") or []:
            entries = tab.get("entries") or []
            if not entries:
                continue
            # `index` is 1-based and points at the page currently shown.
            index = min(max(int(tab.get("index") or len(entries)), 1), len(entries)) - 1
            entry = entries[index]
            url = entry.get("url") or ""
            if url:
                result.append((url, entry.get("title") or ""))
    return result


def firefox_profiles(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    names: dict[str, str] = {}
    ini = root.parent / "profiles.ini" if root.name == "Profiles" else root / "profiles.ini"
    try:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini, encoding="utf-8")
        for section in parser.sections():
            path, name = parser.get(section, "Path", fallback=""), parser.get(section, "Name", fallback="")
            if path and name:
                names[Path(path).name] = name
    except Exception:
        pass
    found = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if (d / "sessionstore-backups" / "recovery.jsonlz4").is_file() or \
                (d / "sessionstore.jsonlz4").is_file():
            found.append((names.get(d.name) or d.name.split(".", 1)[-1], d))
    if len(found) == 1:
        found = [("", found[0][1])]
    return found


def firefox_session_file(profile_dir: Path, running: bool) -> Path | None:
    # recovery.jsonlz4 is the live copy; sessionstore.jsonlz4 is written on exit.
    live = profile_dir / "sessionstore-backups" / "recovery.jsonlz4"
    on_exit = profile_dir / "sessionstore.jsonlz4"
    order = (live, on_exit) if running else (on_exit, live)
    for path in order:
        if path.is_file():
            return path
    return None


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _is_running(process_names: set[str] | None, exe_names: tuple[str, ...]) -> bool:
    if process_names is None:
        return True  # cannot tell, so do not hide anything
    return any(name in process_names for name in exe_names)


def _usable(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def scan_browsers(include_closed: bool = False,
                  chromium: list | None = None,
                  firefox: list | None = None) -> list[BrowserResult]:
    """
    Read the open tabs of every browser profile found on this machine.

    Browsers that are not running only contribute when `include_closed` is set:
    their session files still describe the tabs from the last time they ran.
    """
    processes = running_process_names()
    results: list[BrowserResult] = []

    for name, exes, roots in (CHROMIUM_BROWSERS if chromium is None else chromium):
        running = _is_running(processes, exes)
        if not running and not include_closed:
            continue
        for root in roots:
            if not root.is_dir():
                continue
            for profile, directory in chromium_profiles(root):
                result = BrowserResult(name, profile, running, [])
                try:
                    session = latest_session_file(directory)
                    pages = [(u, t) for u, t in parse_snss(session.read_bytes()) if _usable(u)]
                    missing = [u for u, t in pages if not t]
                    known = history_titles(directory, missing) if missing else {}
                    for url, title in pages:
                        result.tabs.append(BrowserTab(name, profile, url,
                                                      title or known.get(url, ""), running))
                except Exception as exc:
                    result.error = str(exc) or exc.__class__.__name__
                results.append(result)

    for name, exes, roots in (FIREFOX_BROWSERS if firefox is None else firefox):
        running = _is_running(processes, exes)
        if not running and not include_closed:
            continue
        for root in roots:
            for profile, directory in firefox_profiles(root):
                result = BrowserResult(name, profile, running, [])
                try:
                    path = firefox_session_file(directory, running)
                    session = json.loads(read_mozlz4(path.read_bytes()))
                    for url, title in parse_firefox_session(session):
                        if _usable(url):
                            result.tabs.append(BrowserTab(name, profile, url, title, running))
                except Exception as exc:
                    result.error = str(exc) or exc.__class__.__name__
                results.append(result)

    return results


# --------------------------------------------------------------------------
# Which tabs hold video or audio?
# --------------------------------------------------------------------------

# host suffix -> path pattern (None: any path except the front page). These are
# pages yt-dlp has a dedicated extractor for; anything else can still be ticked
# by hand in the scan window, and yt-dlp's generic extractor will try it.
MEDIA_SITES: list[tuple[str, str | None]] = [
    ("youtube.com", r"^/(watch|shorts/|live/|embed/|playlist|v/)"),
    ("youtu.be", None),
    ("youtube-nocookie.com", r"^/embed/"),
    ("vimeo.com", r"^/(\d+|channels/[^/]+/\d+|showcase/|album/|video/)"),
    ("dailymotion.com", r"^/(video|playlist)/"),
    ("dai.ly", None),
    ("twitch.tv", r"^/(videos/\d+|[^/]+/clip/|[^/]+/v/)"),
    ("clips.twitch.tv", None),
    ("soundcloud.com", r"^/(?!(discover|feed|stream|search|you|upload|charts|settings|messages|notifications|people|pages|terms-of-use|imprint)(/|$))[^/]+/[^/]+"),
    ("bandcamp.com", r"^/(track|album)/"),
    ("mixcloud.com", r"^/(?!(discover|upload|settings|dashboard|search|select)(/|$))[^/]+/[^/]+"),
    ("tiktok.com", r"^/(@[^/]+/video/|t/|v/)"),
    ("vm.tiktok.com", None),
    ("instagram.com", r"^/(p|reel|reels|tv)/"),
    ("facebook.com", r"^/(watch|reel/|[^/]+/videos/|video\.php)"),
    ("fb.watch", None),
    ("bilibili.com", r"^/(video|bangumi)/"),
    ("b23.tv", None),
    ("nicovideo.jp", r"^/watch/"),
    ("rumble.com", r"^/v[0-9a-z]+-"),
    ("odysee.com", r"^/@[^/]+/[^/]+"),
    ("bitchute.com", r"^/video/"),
    ("streamable.com", r"^/[0-9a-z]+/?$"),
    ("kick.com", r"^/(video/|[^/]+/(clips|videos)/)"),
    ("archive.org", r"^/details/"),
    ("ted.com", r"^/talks/"),
    ("loom.com", r"^/share/"),
    ("podcasts.apple.com", r"/podcast/"),
    ("vk.com", r"^/(video|clip)"),
    ("ok.ru", r"^/video/"),
    ("tv.nrk.no", None),
    ("radio.nrk.no", None),
    ("svtplay.se", r"^/video/"),
    ("dr.dk", r"^/drtv/(se|episode|program)/"),
    ("tv2.no", r"/video/"),
    ("arte.tv", r"/videos/"),
    ("bbc.co.uk", r"^/(iplayer/episode/|sounds/play/|programmes/[^/]+$)"),
    ("9gag.com", r"^/gag/"),
    ("imgur.com", r"^/[a-zA-Z0-9]{5,}$"),
    ("v.redd.it", None),
]

MEDIA_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".mp3", ".m4a",
                    ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m3u8", ".mpd")


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def site_name(url: str) -> str:
    return _host(url) or url


def is_media_url(url: str) -> bool:
    if not _usable(url):
        return False
    parts = urlsplit(url)
    host, path = _host(url), parts.path or "/"
    if path.lower().endswith(MEDIA_EXTENSIONS):
        return True
    for suffix, pattern in MEDIA_SITES:
        if host == suffix or host.endswith("." + suffix):
            if pattern is None:
                return path not in ("", "/")
            # music.youtube.com etc. land here too, which is what we want.
            return re.search(pattern, path) is not None
    return False


_YT_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


def youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = _host(url)
    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
        return vid if _YT_ID.match(vid) else None
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if parts.path == "/watch":
            vid = (parse_qs(parts.query).get("v") or [""])[0]
            return vid if _YT_ID.match(vid) else None
        match = re.match(r"^/(shorts|live|embed|v)/([0-9A-Za-z_-]{11})", parts.path)
        if match:
            return match.group(2)
    return None


def is_playlist_url(url: str) -> bool:
    parts = urlsplit(url)
    return _host(url).endswith("youtube.com") and parts.path == "/playlist"


def canonical_url(url: str) -> str:
    """
    A cleaner URL to queue: YouTube watch pages lose their playlist, index and
    timestamp parameters, so a tab opened from inside a playlist downloads just
    that video.
    """
    vid = youtube_id(url)
    if vid and not _host(url).startswith("music."):
        return f"https://www.youtube.com/watch?v={vid}"
    return url.split("#", 1)[0]


def url_key(url: str) -> str:
    """Identity used to spot the same video queued twice under different URLs."""
    vid = youtube_id(url)
    if vid:
        return f"youtube:{vid}"
    parts = urlsplit(url.strip())
    host = _host(url)
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


_TITLE_COUNTER = re.compile(r"^\(\d+\+?\)\s*")
_TITLE_SUFFIX = re.compile(
    r"\s*[-|–—•·]\s*(YouTube Music|YouTube|Vimeo|Dailymotion|Twitch|SoundCloud|TikTok|"
    r"Instagram|Facebook|Rumble|Odysee|BitChute|Streamable|NRK TV|NRK Radio|bilibili.*|"
    r"Internet Archive.*|TED Talk|Loom)\s*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """'(3) Some video - YouTube' -> 'Some video'."""
    title = _TITLE_COUNTER.sub("", (title or "").strip())
    title = re.sub(r"\s*\|\s*Listen online for free on SoundCloud$", "", title)
    title = _TITLE_SUFFIX.sub("", title)
    title = re.sub(r"\s+on\s+(Vimeo|SoundCloud)$", "", title)
    return title.strip()
