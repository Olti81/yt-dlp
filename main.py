#!/usr/bin/env python3
"""
yt-dlp Downloader 2.1
=====================

A tkinter front-end for yt-dlp with automatic media inspection, a persistent
download queue, live progress, a settings panel, and a scanner that queues the
video and audio tabs open in your browsers in one go.

Everything the app writes lives in %LOCALAPPDATA%\\yt-dlp-gui (config, queue,
download archive, thumbnail cache and an updatable copy of yt-dlp.exe).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field, asdict
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import tabscan

APP_NAME = "yt-dlp Downloader"
APP_VERSION = "2.1"
IS_WINDOWS = platform.system() == "Windows"

# Sentinels used to pick our own machine-readable lines out of yt-dlp's output.
P_TAG = "@@P@@"
F_TAG = "@@FILE@@"
T_TAG = "@@TITLE@@"
PROGRESS_TEMPLATE = (
    "download:" + P_TAG +
    "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|"
    "%(progress.eta)s|%(progress.status)s"
)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
# URLs anywhere in a block of text: a pasted list, a chat message, an e-mail.
URL_FIND_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)

VIDEO_KINDS = ["MP4 Video", "MKV Video", "WEBM Video"]
AUDIO_KINDS = ["MP3 Audio", "M4A Audio", "WAV Audio", "FLAC Audio", "Opus Audio"]
ALL_KINDS = VIDEO_KINDS + AUDIO_KINDS

COOKIE_BROWSERS = ["", "chrome", "brave", "edge", "firefox", "opera", "vivaldi",
                   "chromium", "safari", "whale"]

BEST = "Best available"
QUALITY_LADDER = [2160, 1440, 1080, 720, 480, 360, 240, 144]
GENERIC_QUALITIES = [BEST] + [f"{h}p" for h in QUALITY_LADDER]
AUDIO_QUALITIES = ["Best available", "320 kbps", "256 kbps", "192 kbps", "128 kbps", "96 kbps"]

# Queue columns. Widths are shared out by weight on every resize so the table
# always fits the window, whatever the display scaling is.
TREE_COLUMNS = ("act", "title", "kind", "quality", "status", "progress", "size", "speed", "eta")
COLUMN_SPEC = {
    "act":      {"text": "",         "weight": 0.04, "min": 34,  "anchor": tk.CENTER},
    "title":    {"text": "Title",    "weight": 0.27, "min": 140, "anchor": tk.W},
    "kind":     {"text": "Format",   "weight": 0.08, "min": 60,  "anchor": tk.W},
    "quality":  {"text": "Quality",  "weight": 0.11, "min": 70,  "anchor": tk.W},
    "status":   {"text": "Status",   "weight": 0.11, "min": 70,  "anchor": tk.W},
    "progress": {"text": "Progress", "weight": 0.17, "min": 120, "anchor": tk.W},
    "size":     {"text": "Size",     "weight": 0.08, "min": 60,  "anchor": tk.E},
    "speed":    {"text": "Speed",    "weight": 0.08, "min": 60,  "anchor": tk.E},
    "eta":      {"text": "ETA",      "weight": 0.06, "min": 50,  "anchor": tk.E},
}
ACT_COLUMN = f"#{TREE_COLUMNS.index('act') + 1}"

GLYPH_PLAY = "▶"    # black right-pointing triangle
GLYPH_PAUSE = "‖"   # double vertical line -- present in far more fonts than U+23F8

STATUS_QUEUED = "Queued"
STATUS_RUNNING = "Downloading"
STATUS_PAUSED = "Paused"
STATUS_DONE = "Done"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"
STATUS_SKIPPED = "Already have"
FINISHED_STATES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED)
# Statuses whose play button starts (or restarts) the download.
STARTABLE_STATES = (STATUS_QUEUED, STATUS_PAUSED, STATUS_FAILED, STATUS_CANCELLED)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def bundle_dir() -> Path:
    """Directory that holds the bundled `resources` folder."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return bundle_dir().joinpath("resources", *parts)


def asset_path(*parts: str) -> Path:
    return bundle_dir().joinpath("assets", *parts)


APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "yt-dlp-gui"
CONFIG_FILE = APP_DATA / "config.json"
QUEUE_FILE = APP_DATA / "queue.json"
ARCHIVE_FILE = APP_DATA / "archive.txt"
BIN_DIR = APP_DATA / "bin"
BIN_STAMP = BIN_DIR / "source.json"
THUMB_DIR = APP_DATA / "thumbs"


def default_download_dir() -> str:
    for candidate in (Path.home() / "Downloads", Path.home(), Path.cwd()):
        if candidate.is_dir():
            return str(candidate)
    return str(Path.cwd())


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def startup_info():
    if IS_WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return si
    return None


CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def kill_process_tree(proc: subprocess.Popen):
    """
    Stop yt-dlp for real.

    yt-dlp.exe is itself a PyInstaller one-file bundle: the process we spawn is
    only a bootloader that unpacks and re-launches the actual downloader as a
    child. Terminating the parent leaves that child running -- still downloading
    and still holding our stdout pipe open -- so the whole tree has to go.
    """
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30,
                           startupinfo=startup_info(), creationflags=CREATE_NO_WINDOW)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


def human_bytes(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_eta(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def to_number(text):
    """yt-dlp writes 'NA' for unknown values in progress templates."""
    if text in (None, "", "NA", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def bar_text(percent: float, width: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(percent / 100 * width))
    return "█" * filled + "░" * (width - filled) + f" {percent:5.1f}%"


def looks_like_url(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and "\n" not in text and bool(URL_RE.match(text))


def split_urls(text: str) -> list[str]:
    """Every URL in `text`, in order and without repeats."""
    found = []
    for match in URL_FIND_RE.findall(text or ""):
        url = match.rstrip(".,;:!?)]}>")
        if url.lower().startswith("www."):
            url = "https://" + url
        found.append(url)
    return list(dict.fromkeys(found))


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def open_in_explorer(path: str, select: bool = False):
    p = Path(path)
    try:
        if IS_WINDOWS:
            if select and p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p if p.is_dir() else p.parent))  # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(p if p.is_dir() else p.parent)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    except Exception:
        pass


# --------------------------------------------------------------------------
# Executable discovery
# --------------------------------------------------------------------------

def _runnable(path: str) -> bool:
    try:
        subprocess.run([path, "--version"], capture_output=True, timeout=30,
                       startupinfo=startup_info(), creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def resolve_ytdlp() -> tuple[str | None, list[str]]:
    """
    Return (path, notes).

    The bundled yt-dlp.exe is mirrored into %LOCALAPPDATA% so that yt-dlp's own
    `-U` updater can replace it -- inside a PyInstaller one-file bundle the
    original lives in a temp folder that is wiped on exit.
    """
    notes: list[str] = []
    bundled = resource_path("yt-dlp.exe" if IS_WINDOWS else "yt-dlp")
    local = BIN_DIR / bundled.name

    if bundled.is_file():
        try:
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            stamp = {}
            if BIN_STAMP.is_file():
                stamp = json.loads(BIN_STAMP.read_text(encoding="utf-8"))
            if not local.is_file() or stamp.get("size") != bundled.stat().st_size:
                shutil.copy2(bundled, local)
                BIN_STAMP.write_text(json.dumps({"size": bundled.stat().st_size}),
                                     encoding="utf-8")
                notes.append(f"Installed bundled yt-dlp to {local}")
            return str(local), notes
        except Exception as exc:
            notes.append(f"Could not use updatable copy ({exc}); running bundled binary.")
            return str(bundled), notes

    if local.is_file():
        return str(local), notes

    name = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    found = shutil.which(name) or shutil.which("yt-dlp")
    if found:
        notes.append(f"Using yt-dlp from PATH: {found}")
        return found, notes
    return None, notes


def resolve_ffmpeg() -> str | None:
    bundled = resource_path("ffmpeg.exe" if IS_WINDOWS else "ffmpeg")
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg")


def has_js_runtime() -> bool:
    """yt-dlp needs Deno (or another JS runtime) for full YouTube extraction."""
    for name in ("deno", "node", "bun", "quickjs"):
        if shutil.which(name):
            return True
    return (resource_path("deno.exe").is_file() or resource_path("deno").is_file())


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "download_dir": "",
    "kind": "MP4 Video",
    "quality": BEST,
    "auto_probe": True,
    "auto_start": True,
    "max_concurrent": 1,
    "clipboard_watch": True,
    "notify_done": True,
    "scan_show_all": False,
    "scan_include_closed": False,
    "subtitles": False,
    "sub_langs": "en",
    "auto_subs": False,
    "embed_subs": True,
    "embed_thumbnail": False,
    "embed_metadata": True,
    "sponsorblock": False,
    "playlist_subfolder": True,
    "filename_template": "%(title)s.%(ext)s",
    "restrict_filenames": False,
    "use_archive": False,
    "impersonate": True,
    "limit_rate": "",
    "concurrent_fragments": 4,
    "retries": 10,
    "cookies_browser": "",
    "theme": "Light",
    "geometry": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.is_file():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not cfg.get("download_dir") or not Path(cfg["download_dir"]).is_dir():
        cfg["download_dir"] = default_download_dir()
    return cfg


def save_config(cfg: dict):
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Job model
# --------------------------------------------------------------------------

@dataclass
class Job:
    url: str
    title: str = ""
    uploader: str = ""
    duration: str = ""
    kind: str = "MP4 Video"
    quality: str = BEST
    dest_dir: str = ""
    whole_playlist: bool = False
    opts: dict = field(default_factory=dict)

    status: str = STATUS_QUEUED
    percent: float = 0.0
    speed: str = ""
    speed_bytes: float = 0.0
    eta: str = ""
    part: str = ""
    size: str = ""
    filepath: str = ""
    error: str = ""
    jid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def display_title(self) -> str:
        return self.title or self.url

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# --------------------------------------------------------------------------
# Themes
# --------------------------------------------------------------------------

THEMES = {
    "Light": {
        "base": "vista" if IS_WINDOWS else "clam",
        "bg": "#f2f4f7", "panel": "#ffffff", "fg": "#1c2430", "muted": "#5c6b7f",
        "field": "#ffffff", "field_fg": "#1c2430", "border": "#c9d2dd",
        "accent": "#2563eb", "accent_fg": "#ffffff",
        "log_bg": "#ffffff", "log_fg": "#1c2430",
        "row_alt": "#f6f8fb", "sel": "#dbe7ff",
        "ok": "#127a3d", "err": "#c0392b", "warn": "#b7791f", "run": "#1d4ed8",
    },
    "Dark": {
        "base": "clam",
        "bg": "#1b1f27", "panel": "#232833", "fg": "#e6e9ef", "muted": "#98a4b6",
        "field": "#2b313d", "field_fg": "#e6e9ef", "border": "#3a4252",
        "accent": "#3b82f6", "accent_fg": "#ffffff",
        "log_bg": "#171b22", "log_fg": "#d7dce6",
        "row_alt": "#262c38", "sel": "#33415c",
        "ok": "#4ade80", "err": "#f87171", "warn": "#fbbf24", "run": "#60a5fa",
    },
}


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class YtdlpGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()

        self.jobs: list[Job] = []
        self.jobs_lock = threading.RLock()
        self.wake = threading.Event()
        self.shutdown = False
        self.paused = not self.cfg["auto_start"]

        # Downloads run concurrently, so every piece of per-download state is
        # keyed by job id rather than held as a single "current" value.
        self.run_lock = threading.RLock()
        self.active: set[str] = set()                  # jobs claimed by a thread
        self.procs: dict[str, subprocess.Popen] = {}   # live yt-dlp processes
        self.cancel_flags: set[str] = set()
        self.pause_flags: set[str] = set()
        self.remove_when_stopped: set[str] = set()
        self.batch_active = False                      # for the "all finished" notice

        self.probe_seq = 0
        self.probe_after_id = None
        self.last_probed_url = ""
        self.last_clipboard = ""
        self.probe_info: dict | None = None
        self.thumb_image: tk.PhotoImage | None = None
        self.save_after_id = None
        self.scan_dialog: TabScanDialog | None = None
        self.batch_dialog: BatchDialog | None = None

        self.ytdlp, notes = resolve_ytdlp()
        self.ffmpeg = resolve_ffmpeg()
        self.startup_notes = notes

        self._build_vars()
        self._build_menu()
        self._build_ui()
        self.apply_theme(self.cfg["theme"])

        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self._set_window_icon()
        self._size_window()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._report_environment()
        self._restore_queue()
        self._refresh_overall()

        self.worker = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.worker.start()
        threading.Thread(target=self._check_version, daemon=True).start()

        self.root.after(1200, self._poll_clipboard)
        self.url_entry.focus_set()

    # -- variables ---------------------------------------------------------

    def _build_vars(self):
        c = self.cfg
        self.url_var = tk.StringVar()
        self.kind_var = tk.StringVar(value=c["kind"])
        self.quality_var = tk.StringVar(value=c["quality"])
        self.dir_var = tk.StringVar(value=c["download_dir"])
        self.probe_status_var = tk.StringVar(value="Paste or type a URL - media info is fetched automatically.")
        self.media_title_var = tk.StringVar(value="No media loaded")
        self.media_meta_var = tk.StringVar(value="")
        self.current_label_var = tk.StringVar(value="Idle")
        self.overall_label_var = tk.StringVar(value="Queue empty")
        self.status_var = tk.StringVar(value="Ready")
        self.version_var = tk.StringVar(value="yt-dlp: checking...")

        self.bool_vars: dict[str, tk.BooleanVar] = {}
        for key in ("auto_probe", "auto_start", "clipboard_watch", "notify_done", "subtitles",
                    "auto_subs", "embed_subs", "embed_thumbnail", "embed_metadata",
                    "sponsorblock", "playlist_subfolder", "restrict_filenames",
                    "use_archive", "impersonate"):
            var = tk.BooleanVar(value=bool(c[key]))
            var.trace_add("write", lambda *_, k=key: self._on_setting(k))
            self.bool_vars[key] = var

        self.str_vars: dict[str, tk.StringVar] = {}
        for key in ("sub_langs", "filename_template", "limit_rate", "cookies_browser",
                    "concurrent_fragments", "retries", "max_concurrent"):
            var = tk.StringVar(value=str(c[key]))
            var.trace_add("write", lambda *_, k=key: self._on_setting(k))
            self.str_vars[key] = var

        self.theme_var = tk.StringVar(value=c["theme"])
        self.url_var.trace_add("write", self._on_url_changed)

    def _on_setting(self, key: str):
        if key in self.bool_vars:
            self.cfg[key] = bool(self.bool_vars[key].get())
            if key == "auto_start":
                self.paused = not self.cfg["auto_start"]
                self._refresh_start_button()
                self.wake.set()
        else:
            value = self.str_vars[key].get()
            if key in ("concurrent_fragments", "retries", "max_concurrent"):
                try:
                    value = max(1, int(value))
                except ValueError:
                    return
            self.cfg[key] = value
        save_config(self.cfg)

    # -- menu --------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Paste URL\tCtrl+V", command=self.paste_url)
        m_file.add_command(label="Fetch media info\tEnter", command=self.probe_now)
        m_file.add_command(label="Add to queue\tCtrl+Enter", command=self.add_to_queue)
        m_file.add_command(label="Add many URLs...\tCtrl+M", command=self.open_batch_dialog)
        m_file.add_command(label="Scan browser tabs...\tCtrl+T", command=self.open_scan_dialog)
        m_file.add_separator()
        m_file.add_command(label="Choose download folder...\tCtrl+O", command=self.browse_dir)
        m_file.add_command(label="Open download folder", command=lambda: open_in_explorer(self.dir_var.get()))
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=m_file)

        m_queue = tk.Menu(menubar, tearoff=0)
        m_queue.add_command(label="Start / pause queue", command=self.toggle_queue)
        m_queue.add_command(label="Start / resume selected", command=self.start_selected)
        m_queue.add_command(label="Pause selected\tSpace", command=self.pause_selected)
        m_queue.add_command(label="Pause all running", command=self.pause_running)
        m_queue.add_command(label="Cancel all running", command=self.cancel_running)
        m_queue.add_separator()
        m_queue.add_command(label="Move up", command=lambda: self.move_selected(-1))
        m_queue.add_command(label="Move down", command=lambda: self.move_selected(1))
        m_queue.add_command(label="Move to top", command=lambda: self.move_selected_to_edge(True))
        m_queue.add_command(label="Move to bottom", command=lambda: self.move_selected_to_edge(False))
        m_queue.add_command(label="Select all\tCtrl+A", command=self.select_all)
        m_queue.add_command(label="Remove selected\tDel", command=self.remove_selected)
        m_queue.add_separator()
        m_queue.add_command(label="Retry failed\tF5", command=self.retry_failed)
        m_queue.add_command(label="Clear finished", command=self.clear_finished)
        menubar.add_cascade(label="Queue", menu=m_queue)

        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="Update yt-dlp now", command=self.update_ytdlp)
        m_tools.add_command(label="Open app data folder", command=lambda: open_in_explorer(str(APP_DATA)))
        m_tools.add_separator()
        m_tools.add_command(label="Clear download archive", command=self.clear_archive)
        m_tools.add_command(label="Clear log\tCtrl+L", command=self.clear_log)
        menubar.add_cascade(label="Tools", menu=m_tools)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        m_help.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=m_help)

        self.root.config(menu=menubar)

        self.root.bind("<Control-v>", self._on_ctrl_v)
        self.root.bind("<Control-t>", lambda e: self.open_scan_dialog())
        self.root.bind("<Control-m>", lambda e: (self.open_batch_dialog(), "break")[1])
        self.root.bind("<Control-o>", lambda e: self.browse_dir())
        self.root.bind("<Control-l>", lambda e: self.clear_log())
        self.root.bind("<Control-Return>", lambda e: self.add_to_queue())
        self.root.bind("<F5>", lambda e: self.retry_failed())

    # -- layout ------------------------------------------------------------

    def _build_ui(self):
        # The status bar is packed before the expanding frame: pack gives space
        # to earlier children first, so doing it the other way round lets a
        # short window squeeze the status bar down to nothing.
        self._build_statusbar()

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        self.outer = outer

        self._build_source(outer)
        self._build_media(outer)

        # Bottom-anchored before the notebook, for the same reason as the status
        # bar: the notebook expands, and would otherwise eat this row.
        self._build_progress(outer)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(10, 8))
        self._build_queue_tab(self.notebook)
        self._build_log_tab(self.notebook)
        self._build_settings_tab(self.notebook)

    def _build_source(self, parent):
        frame = ttk.LabelFrame(parent, text=" 1 - Source ", padding=10)
        frame.pack(fill=tk.X)
        frame.columnconfigure(0, weight=1)

        row = ttk.Frame(frame)
        row.grid(row=0, column=0, columnspan=3, sticky=tk.EW)
        row.columnconfigure(0, weight=1)

        self.url_entry = ttk.Entry(row, textvariable=self.url_var, font=("Segoe UI", 10))
        self.url_entry.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6), ipady=3)
        self.url_entry.bind("<Return>", lambda e: self.probe_now())

        ttk.Button(row, text="Paste", width=8, command=self.paste_url).grid(row=0, column=1, padx=2)
        ttk.Button(row, text="Clear", width=8, command=self.clear_url).grid(row=0, column=2, padx=2)
        self.fetch_button = ttk.Button(row, text="Fetch info", width=12, command=self.probe_now)
        self.fetch_button.grid(row=0, column=3, padx=(2, 0))
        ttk.Separator(row, orient=tk.VERTICAL).grid(row=0, column=4, sticky=tk.NS, padx=10)
        self.batch_button = ttk.Button(row, text="Add many...", width=12,
                                       command=self.open_batch_dialog)
        self.batch_button.grid(row=0, column=5, padx=2)
        self.scan_button = ttk.Button(row, text="Scan browser tabs", style="Accent.TButton",
                                      command=self.open_scan_dialog)
        self.scan_button.grid(row=0, column=6, padx=(2, 0))

        status_row = ttk.Frame(frame)
        status_row.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        status_row.columnconfigure(0, weight=1)
        self.probe_status = ttk.Label(status_row, textvariable=self.probe_status_var,
                                      style="Muted.TLabel")
        self.probe_status.grid(row=0, column=0, sticky=tk.W)
        self.probe_bar = ttk.Progressbar(status_row, mode="indeterminate", length=120)

    def _build_media(self, parent):
        frame = ttk.LabelFrame(parent, text=" 2 - Media and quality ", padding=10)
        frame.pack(fill=tk.X, pady=(10, 0))
        frame.columnconfigure(1, weight=1)

        self.thumb_label = tk.Label(frame, width=26, height=7, bd=1, relief="solid",
                                    text="no preview", anchor=tk.CENTER)
        self.thumb_label.grid(row=0, column=0, rowspan=3, padx=(0, 12), sticky=tk.N)

        self.title_label = ttk.Label(frame, textvariable=self.media_title_var,
                                     style="Title.TLabel", wraplength=620, justify=tk.LEFT)
        self.title_label.grid(row=0, column=1, columnspan=4, sticky=tk.W)
        self.meta_label = ttk.Label(frame, textvariable=self.media_meta_var,
                                    style="Muted.TLabel", wraplength=620, justify=tk.LEFT)
        self.meta_label.grid(row=1, column=1, columnspan=4, sticky=tk.W, pady=(2, 8))
        frame.bind("<Configure>", self._fit_media_text)

        opts = ttk.Frame(frame)
        opts.grid(row=2, column=1, columnspan=4, sticky=tk.EW)

        ttk.Label(opts, text="Format:").grid(row=0, column=0, sticky=tk.W)
        self.kind_combo = ttk.Combobox(opts, textvariable=self.kind_var, values=ALL_KINDS,
                                       state="readonly", width=14)
        self.kind_combo.grid(row=0, column=1, padx=(6, 16))
        self.kind_combo.bind("<<ComboboxSelected>>", self._on_kind_changed)

        ttk.Label(opts, text="Quality:").grid(row=0, column=2, sticky=tk.W)
        self.quality_combo = ttk.Combobox(opts, textvariable=self.quality_var,
                                          values=GENERIC_QUALITIES, state="readonly", width=26)
        self.quality_combo.grid(row=0, column=3, padx=(6, 16))
        self.quality_combo.bind("<<ComboboxSelected>>", lambda e: self._remember_choice())

        self.add_button = ttk.Button(opts, text="Add to queue", style="Accent.TButton",
                                     command=self.add_to_queue)
        self.add_button.grid(row=0, column=4, padx=(0, 6))

        extras = ttk.Frame(frame)
        extras.grid(row=3, column=1, columnspan=4, sticky=tk.W, pady=(10, 0))
        checks = [
            ("Subtitles", "subtitles"),
            ("Embed thumbnail", "embed_thumbnail"),
            ("Embed metadata", "embed_metadata"),
            ("Skip sponsor segments", "sponsorblock"),
        ]
        for i, (text, key) in enumerate(checks):
            ttk.Checkbutton(extras, text=text, variable=self.bool_vars[key]).grid(
                row=0, column=i, sticky=tk.W, padx=(0, 16))

        # Destination
        dest = ttk.Frame(frame)
        dest.grid(row=4, column=0, columnspan=5, sticky=tk.EW, pady=(12, 0))
        dest.columnconfigure(1, weight=1)
        ttk.Label(dest, text="Save to:").grid(row=0, column=0, sticky=tk.W)
        self.dir_entry = ttk.Entry(dest, textvariable=self.dir_var, state="readonly")
        self.dir_entry.grid(row=0, column=1, sticky=tk.EW, padx=6, ipady=2)
        ttk.Button(dest, text="Browse...", width=10, command=self.browse_dir).grid(row=0, column=2, padx=2)
        ttk.Button(dest, text="Open", width=8,
                   command=lambda: open_in_explorer(self.dir_var.get())).grid(row=0, column=3, padx=2)

    def _fit_media_text(self, event):
        """Keep the title/metadata text inside the panel next to the thumbnail."""
        width = max(240, event.width - self.thumb_label.winfo_width() - 60)
        self.title_label.configure(wraplength=width)
        self.meta_label.configure(wraplength=width)

    def _build_queue_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Queue  ")
        self.queue_tab = tab

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, pady=(0, 8))
        self.start_button = ttk.Button(bar, text="Hold queue", width=13, command=self.toggle_queue)
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(bar, text="Pause all", width=10,
                   command=self.pause_running).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Cancel all", width=11,
                   command=self.cancel_running).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="Up", width=5, command=lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Down", width=6, command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Remove", width=9, command=self.remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="Retry failed", width=12, command=self.retry_failed).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Clear finished", width=13, command=self.clear_finished).pack(side=tk.LEFT, padx=2)

        wrap = ttk.Frame(tab)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(wrap, columns=TREE_COLUMNS, show="headings",
                                 selectmode="extended")
        for col in TREE_COLUMNS:
            spec = COLUMN_SPEC[col]
            self.tree.heading(col, text=spec["text"], anchor=spec["anchor"])
            # Widths are recomputed on resize, so nothing here may stretch itself.
            self.tree.column(col, anchor=spec["anchor"], stretch=False,
                             minwidth=spec["min"], width=spec["min"])
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Configure>", self._fit_columns)

        # Shown over the empty table, so a first-time user knows where to start.
        self.empty_label = tk.Label(
            self.tree, justify=tk.CENTER, font=("Segoe UI", 10),
            text="The queue is empty.\n\nPaste a link above, use 'Add many...' for a list "
                 "of links,\nor 'Scan browser tabs' to queue every video and audio tab "
                 "you have open.")

        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<space>", lambda e: (self.toggle_selected(), "break")[1])
        self.tree.bind("<Motion>", self._on_tree_motion)

        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Start / resume", command=self.start_selected)
        self.ctx.add_command(label="Pause", command=self.pause_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Open file", command=self.open_selected_file)
        self.ctx.add_command(label="Show in folder", command=self.reveal_selected_file)
        self.ctx.add_separator()
        self.ctx.add_command(label="Copy URL", command=self.copy_selected_url)
        self.ctx.add_command(label="Open URL in browser", command=self.open_selected_url)
        self.ctx.add_command(label="Load URL into source box", command=self.load_selected_url)
        self.ctx.add_separator()
        self.ctx.add_command(label="Use the current format and quality",
                             command=self.apply_format_to_selected)
        self.ctx.add_command(label="Retry", command=self.retry_selected)
        self.ctx.add_command(label="Move up", command=lambda: self.move_selected(-1))
        self.ctx.add_command(label="Move down", command=lambda: self.move_selected(1))
        self.ctx.add_command(label="Move to top", command=lambda: self.move_selected_to_edge(True))
        self.ctx.add_command(label="Move to bottom", command=lambda: self.move_selected_to_edge(False))
        self.ctx.add_command(label="Remove", command=self.remove_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Show error", command=self.show_selected_error)

    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Log  ")

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(bar, text="Copy log", width=11, command=self.copy_log).pack(side=tk.LEFT)
        ttk.Button(bar, text="Clear", width=8, command=self.clear_log).pack(side=tk.LEFT, padx=4)
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll_var).pack(side=tk.LEFT, padx=10)

        wrap = ttk.Frame(tab)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(wrap, wrap=tk.WORD, state=tk.DISABLED, relief="flat",
                                font=("Consolas", 9), height=10, borderwidth=0,
                                padx=6, pady=4)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # Continuation lines line up under the timestamp instead of the margin.
        self.log_text.tag_configure("body", lmargin2=80)

    def _build_settings_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Settings  ")

        left = ttk.Frame(tab)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        right = ttk.Frame(tab)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        g1 = ttk.LabelFrame(left, text=" Behaviour ", padding=10)
        g1.pack(fill=tk.X)
        for text, key in (("Fetch media info automatically while typing", "auto_probe"),
                          ("Start downloading automatically when items are queued", "auto_start"),
                          ("Watch the clipboard for URLs", "clipboard_watch"),
                          ("Beep and flash the taskbar when the queue is finished", "notify_done"),
                          ("Skip files already downloaded (download archive)", "use_archive")):
            ttk.Checkbutton(g1, text=text, variable=self.bool_vars[key]).pack(anchor=tk.W, pady=1)
        row = ttk.Frame(g1)
        row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row, text="Downloads at the same time:").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=1, to=8, width=4,
                    textvariable=self.str_vars["max_concurrent"]).pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text="play on any row starts one extra",
                  style="Muted.TLabel").pack(side=tk.LEFT)

        g2 = ttk.LabelFrame(left, text=" Subtitles ", padding=10)
        g2.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(g2, text="Download subtitles", variable=self.bool_vars["subtitles"]).pack(anchor=tk.W)
        ttk.Checkbutton(g2, text="Include auto-generated subtitles", variable=self.bool_vars["auto_subs"]).pack(anchor=tk.W)
        ttk.Checkbutton(g2, text="Embed into the video file", variable=self.bool_vars["embed_subs"]).pack(anchor=tk.W)
        row = ttk.Frame(g2)
        row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row, text="Languages:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.str_vars["sub_langs"], width=18).pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text="e.g. en,no or all", style="Muted.TLabel").pack(side=tk.LEFT)

        g3 = ttk.LabelFrame(left, text=" Files and naming ", padding=10)
        g3.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(g3, text="Put playlists in their own subfolder",
                        variable=self.bool_vars["playlist_subfolder"]).pack(anchor=tk.W)
        ttk.Checkbutton(g3, text="Restrict filenames to plain ASCII",
                        variable=self.bool_vars["restrict_filenames"]).pack(anchor=tk.W)
        row = ttk.Frame(g3)
        row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row, text="Filename template:").pack(anchor=tk.W)
        ttk.Entry(row, textvariable=self.str_vars["filename_template"]).pack(fill=tk.X, pady=(2, 0))
        ttk.Label(g3, text="yt-dlp output template, e.g. %(uploader)s - %(title)s.%(ext)s",
                  style="Muted.TLabel", wraplength=320).pack(anchor=tk.W, pady=(2, 0))

        g4 = ttk.LabelFrame(right, text=" Network ", padding=10)
        g4.pack(fill=tk.X)
        ttk.Checkbutton(g4, text="Impersonate Chrome (helps with blocked sites)",
                        variable=self.bool_vars["impersonate"]).pack(anchor=tk.W)
        grid = ttk.Frame(g4)
        grid.pack(fill=tk.X, pady=(6, 0))
        grid.columnconfigure(1, weight=1)
        fields = [("Speed limit", "limit_rate", "2M / 500K - blank = off"),
                  ("Parallel fragments", "concurrent_fragments", "1-16"),
                  ("Retries", "retries", ""),
                  ("Cookies from browser", "cookies_browser", "for sites that need a login")]
        for i, (label, key, hint) in enumerate(fields):
            ttk.Label(grid, text=label + ":").grid(row=i, column=0, sticky=tk.W, pady=2)
            if key == "cookies_browser":
                widget = ttk.Combobox(grid, textvariable=self.str_vars[key], width=14,
                                      values=COOKIE_BROWSERS)
            else:
                widget = ttk.Entry(grid, textvariable=self.str_vars[key], width=16)
            widget.grid(row=i, column=1, sticky=tk.W, padx=6)
            if hint:
                ttk.Label(grid, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky=tk.W)

        g5 = ttk.LabelFrame(right, text=" Appearance ", padding=10)
        g5.pack(fill=tk.X, pady=(10, 0))
        row = ttk.Frame(g5)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Theme:").pack(side=tk.LEFT)
        theme_combo = ttk.Combobox(row, textvariable=self.theme_var, values=list(THEMES),
                                   state="readonly", width=10)
        theme_combo.pack(side=tk.LEFT, padx=6)
        theme_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_theme(self.theme_var.get()))

        g6 = ttk.LabelFrame(right, text=" Tools ", padding=10)
        g6.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(g6, textvariable=self.version_var).pack(anchor=tk.W)
        row = ttk.Frame(g6)
        row.pack(fill=tk.X, pady=(6, 0))
        self.update_button = ttk.Button(row, text="Update yt-dlp", width=14, command=self.update_ytdlp)
        self.update_button.pack(side=tk.LEFT)
        ttk.Button(row, text="App data folder", width=16,
                   command=lambda: open_in_explorer(str(APP_DATA))).pack(side=tk.LEFT, padx=6)
        ttk.Label(g6, text=f"{APP_NAME} {APP_VERSION}", style="Muted.TLabel").pack(anchor=tk.W, pady=(8, 0))

    def _build_progress(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Current:", width=9).grid(row=0, column=0, sticky=tk.W)
        self.current_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.current_bar.grid(row=0, column=1, sticky=tk.EW, padx=6)
        ttk.Label(frame, textvariable=self.current_label_var, width=44, anchor=tk.W).grid(row=0, column=2, sticky=tk.W)

        ttk.Label(frame, text="Queue:", width=9).grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.overall_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.overall_bar.grid(row=1, column=1, sticky=tk.EW, padx=6, pady=(6, 0))
        ttk.Label(frame, textvariable=self.overall_label_var, width=44, anchor=tk.W).grid(row=1, column=2, sticky=tk.W, pady=(6, 0))

    def _build_statusbar(self):
        self.statusbar = ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W,
                                   style="Status.TLabel", padding=(10, 4))
        self.statusbar.pack(fill=tk.X, side=tk.BOTTOM)

    def _set_window_icon(self):
        """
        Title bar and taskbar icon.

        iconbitmap takes the .ico on Windows and gives the crispest result;
        iconphoto with the PNG covers everything else. Losing the icon is never
        worth failing to start over, so every step is best-effort.
        """
        ico, png = asset_path("icon.ico"), asset_path("icon.png")
        if IS_WINDOWS and ico.is_file():
            try:
                self.root.iconbitmap(default=str(ico))
                return
            except tk.TclError:
                pass
        if png.is_file():
            try:
                self._icon_image = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_image)
            except tk.TclError:
                pass

    def _size_window(self):
        """
        Size the window from what the widgets actually ask for.

        Fixed pixel sizes do not survive Windows display scaling: at 150% the
        fonts grow but hard-coded numbers do not, so a nominally 1180px window
        clips its own contents. Asking Tk for the required size sidesteps that
        entirely, and a stored geometry that is too small gets discarded.
        """
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        # 1.333 px/point is the normal Windows value; anything above it means the
        # fonts are being scaled up, so the window has to grow by the same factor.
        try:
            scale = max(1.0, float(self.root.tk.call("tk", "scaling")) / 1.3333)
        except (tk.TclError, ValueError):
            scale = 1.0

        min_w = min(int(screen_w * 0.95), max(960, self.root.winfo_reqwidth() + 24))
        min_h = min(int(screen_h * 0.90), max(720, self.root.winfo_reqheight() + 24))
        self.root.minsize(min_w, min_h)

        width = max(min_w, min(int(1240 * scale), int(screen_w * 0.95)))
        height = max(min_h, min(int(940 * scale), int(screen_h * 0.90)))
        saved = re.match(r"(\d+)x(\d+)", self.cfg.get("geometry") or "")
        if saved:
            width = max(min_w, min(int(saved.group(1)), screen_w))
            height = max(min_h, min(int(saved.group(2)), screen_h))
        self.root.geometry(f"{width}x{height}")

    # -- theming -----------------------------------------------------------

    def apply_theme(self, name: str):
        theme = THEMES.get(name, THEMES["Light"])
        self.theme = theme
        self.cfg["theme"] = name
        save_config(self.cfg)

        style = ttk.Style(self.root)
        try:
            style.theme_use(theme["base"])
        except tk.TclError:
            style.theme_use("clam")

        bg, panel, fg, muted = theme["bg"], theme["panel"], theme["fg"], theme["muted"]
        field, border, accent = theme["field"], theme["border"], theme["accent"]

        self.root.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                        bordercolor=border, font=("Segoe UI", 9))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Title.TLabel", background=bg, foreground=fg, font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", background=panel, foreground=muted)
        style.configure("TLabelframe", background=bg, bordercolor=border)
        style.configure("TLabelframe.Label", background=bg, foreground=accent,
                        font=("Segoe UI", 9, "bold"))
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", muted)])
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", padding=(12, 4), font=("Segoe UI", 9, "bold"))
        style.configure("TEntry", fieldbackground=field, foreground=theme["field_fg"],
                        insertcolor=theme["field_fg"], bordercolor=border)
        style.map("TEntry", fieldbackground=[("readonly", field)],
                  foreground=[("readonly", muted)])
        style.configure("TCombobox", fieldbackground=field, background=field,
                        foreground=theme["field_fg"], arrowcolor=fg, bordercolor=border)
        style.map("TCombobox",
                  fieldbackground=[("readonly", field)],
                  foreground=[("readonly", theme["field_fg"])],
                  selectbackground=[("readonly", field)],
                  selectforeground=[("readonly", theme["field_fg"])])
        self.root.option_add("*TCombobox*Listbox.background", field)
        self.root.option_add("*TCombobox*Listbox.foreground", theme["field_fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", theme["accent_fg"])
        style.configure("TNotebook", background=bg, bordercolor=border)
        style.configure("TNotebook.Tab", background=bg, foreground=muted, padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", panel)], foreground=[("selected", fg)])
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg,
                        rowheight=24, bordercolor=border)
        style.configure("Treeview.Heading", background=bg, foreground=muted,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", theme["sel"])],
                  foreground=[("selected", fg)])
        style.configure("TProgressbar", background=accent, troughcolor=field, bordercolor=border,
                        lightcolor=accent, darkcolor=accent)
        style.configure("TSeparator", background=border)

        self.log_text.configure(bg=theme["log_bg"], fg=theme["log_fg"],
                                insertbackground=theme["log_fg"],
                                selectbackground=theme["sel"])
        for tag, colour in (("error", theme["err"]), ("warning", theme["warn"]),
                            ("ok", theme["ok"]), ("cmd", muted)):
            self.log_text.tag_config(tag, foreground=colour)

        self.thumb_label.configure(bg=panel, fg=muted, highlightbackground=border)
        self.empty_label.configure(bg=panel, fg=muted)
        for dialog in (self.scan_dialog, self.batch_dialog):
            if dialog is not None:
                try:
                    dialog.apply_theme()
                except tk.TclError:
                    pass
        for tag, colour in (("queued", muted), ("running", theme["run"]),
                            ("done", theme["ok"]), ("failed", theme["err"]),
                            ("cancelled", theme["warn"]), ("skipped", muted),
                            ("paused", theme["warn"])):
            self.tree.tag_configure(tag, foreground=colour)

    # -- logging -----------------------------------------------------------

    def _after(self, func, *args):
        """Schedule a UI callback from a worker thread, tolerating a closing window."""
        if self.shutdown:
            return
        try:
            self.root.after(0, func, *args)
        except (tk.TclError, RuntimeError):
            # The window is being torn down, or Tk has already left its loop.
            pass

    def log(self, message: str, level: str = "info"):
        self._after(self._log_ui, message, level)

    def _log_ui(self, message: str, level: str):
        try:
            self.log_text.config(state=tk.NORMAL)
            stamp = time.strftime("%H:%M:%S")
            tags = ["body"]
            if level in ("error", "warning", "ok", "cmd"):
                tags.append(level)
            self.log_text.insert(tk.END, f"[{stamp}] {message}\n", tuple(tags))
            if self.autoscroll_var.get():
                self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", tk.END))
        self.set_status("Log copied to clipboard.")

    def set_status(self, text: str):
        self._after(self.status_var.set, text)

    def _report_environment(self):
        for note in self.startup_notes:
            self.log(note)
        if not self.ytdlp:
            self.log("yt-dlp was not found. Downloads are disabled.", "error")
            self.add_button.config(state=tk.DISABLED)
            self.fetch_button.config(state=tk.DISABLED)
        else:
            self.log(f"yt-dlp: {self.ytdlp}")
        if self.ffmpeg:
            self.log(f"ffmpeg: {self.ffmpeg}")
        else:
            self.log("ffmpeg not found - merging and audio conversion will fail.", "warning")
        if not has_js_runtime():
            self.log("No JavaScript runtime (Deno) found. Some YouTube formats may be "
                     "unavailable; install Deno and put deno.exe on PATH to fix.", "warning")
        self.log(f"Downloads go to: {self.dir_var.get()}")

    # -- URL handling / probing -------------------------------------------

    def _on_ctrl_v(self, event):
        """
        Ctrl+V anywhere loads the clipboard as the source URL -- except in other
        text fields (settings, dialogs), where it must stay an ordinary paste.
        """
        widget = event.widget
        if (widget is not self.url_entry and widget is not self.log_text
                and isinstance(widget, (tk.Entry, ttk.Entry, tk.Text, tk.Spinbox))):
            return None
        self.paste_url()
        return "break"

    def paste_url(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.set_status("Clipboard is empty or does not contain text.")
            return
        if len(split_urls(text)) > 1:
            # A single-line box is no place for a list: hand it to the batch window.
            self.last_clipboard = text.strip()
            self.open_batch_dialog(text)
            return
        self.url_var.set(text.strip())
        self.last_clipboard = text.strip()
        self.probe_now()

    def clear_url(self):
        self.url_var.set("")
        self._reset_media_panel()

    def _on_url_changed(self, *_):
        if self.probe_after_id:
            self.root.after_cancel(self.probe_after_id)
            self.probe_after_id = None
        text = self.url_var.get().strip()
        if not text:
            self._reset_media_panel()
            return
        if not self.cfg["auto_probe"]:
            return
        urls = split_urls(text)
        if len(urls) > 1:
            self.probe_status_var.set(f"{len(urls)} URLs detected - press 'Add to queue' to add them all.")
            return
        if looks_like_url(text) and text != self.last_probed_url:
            self.probe_after_id = self.root.after(900, self.probe_now)

    def _poll_clipboard(self):
        if self.cfg["clipboard_watch"]:
            try:
                text = self.root.clipboard_get().strip()
            except tk.TclError:
                text = ""
            if (text and text != self.last_clipboard and looks_like_url(text)
                    and text != self.url_var.get().strip()):
                self.last_clipboard = text
                if not self.url_var.get().strip():
                    self.url_var.set(text)
                    self.set_status("URL picked up from clipboard.")
                else:
                    self.probe_status_var.set(f"Clipboard: {text[:70]} - press Ctrl+V to load it.")
        self.root.after(1000, self._poll_clipboard)

    def probe_now(self):
        if self.probe_after_id:
            self.root.after_cancel(self.probe_after_id)
            self.probe_after_id = None
        text = self.url_var.get().strip()
        urls = split_urls(text)
        if len(urls) > 1:
            self.probe_status_var.set(f"{len(urls)} URLs detected - press 'Add to queue' to add them all.")
            return
        if not looks_like_url(text):
            self.probe_status_var.set("That does not look like a URL.")
            return
        if not self.ytdlp:
            return

        self.last_probed_url = text
        self.probe_seq += 1
        seq = self.probe_seq
        self._set_probing(True, "Fetching media information...")
        threading.Thread(target=self._probe_worker, args=(text, seq), daemon=True).start()

    def _set_probing(self, active: bool, message: str = ""):
        if message:
            self.probe_status_var.set(message)
        if active:
            self.probe_bar.grid(row=0, column=1, sticky=tk.E)
            self.probe_bar.start(12)
            self.fetch_button.config(state=tk.DISABLED)
        else:
            self.probe_bar.stop()
            self.probe_bar.grid_remove()
            if self.ytdlp:
                self.fetch_button.config(state=tk.NORMAL)

    def _probe_worker(self, url: str, seq: int):
        cmd = [self.ytdlp, "--no-update", "--no-warnings", "--ignore-no-formats-error",
               "-J", "--flat-playlist"]
        if self.cfg["impersonate"]:
            cmd += ["--impersonate", "chrome"]
        if self.cfg["cookies_browser"]:
            cmd += ["--cookies-from-browser", self.cfg["cookies_browser"]]
        cmd.append(url)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=180,
                                  startupinfo=startup_info(), creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            self._after(self._probe_failed, seq, "Timed out while fetching info.")
            return
        except Exception as exc:
            self._after(self._probe_failed, seq, str(exc))
            return

        if proc.returncode != 0 or not proc.stdout.strip():
            reason = (proc.stderr or "").strip().splitlines()
            message = reason[-1] if reason else f"yt-dlp exited with code {proc.returncode}"
            self._after(self._probe_failed, seq, message)
            return

        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self._after(self._probe_failed, seq, "Could not parse the response from yt-dlp.")
            return

        self._after(self._probe_done, seq, info)

    def _probe_failed(self, seq: int, message: str):
        if seq != self.probe_seq:
            return
        self._set_probing(False, f"Could not read that URL: {message}")
        self.log(f"Info fetch failed: {message}", "error")
        self.probe_info = None
        self.media_title_var.set("No media loaded")
        self.media_meta_var.set("You can still queue the URL - quality will be chosen at download time.")
        self.quality_combo.config(values=GENERIC_QUALITIES)

    def _probe_done(self, seq: int, info: dict):
        if seq != self.probe_seq:
            return
        self._set_probing(False, "Media information loaded.")
        self.probe_info = info

        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            total = info.get("playlist_count") or len(entries)
            # Very large playlists come back partially listed, so say what will
            # actually be queued rather than what the playlist claims to hold.
            count = (f"{len(entries)} of {total} items listed" if total > len(entries)
                     else f"{total} items")
            self.media_title_var.set(info.get("title") or "Playlist")
            self.media_meta_var.set(
                f"Playlist - {count} - {info.get('uploader') or info.get('channel') or 'unknown channel'}\n"
                "'Add to queue' adds every listed item as its own queue entry.")
            self.quality_combo.config(values=self._quality_values_for_kind(None))
            self._set_thumbnail(None)
            self.log(f"Playlist detected: {info.get('title')} ({total} items)")
        else:
            title = info.get("title") or "Untitled"
            self.media_title_var.set(title)
            bits = [b for b in (info.get("uploader") or info.get("channel"),
                                human_duration(info.get("duration")),
                                info.get("extractor_key")) if b]
            heights = self._available_heights(info)
            if heights:
                bits.append("up to " + f"{max(heights)}p")
            self.media_meta_var.set("  -  ".join(bits))
            self.quality_combo.config(values=self._quality_values_for_kind(info))
            self._sync_quality_selection()
            threading.Thread(target=self._load_thumbnail, args=(info, seq), daemon=True).start()
            self.log(f"Loaded: {title}")

    def _reset_media_panel(self):
        self.probe_info = None
        self.last_probed_url = ""
        self.media_title_var.set("No media loaded")
        self.media_meta_var.set("")
        self.probe_status_var.set("Paste or type a URL - media info is fetched automatically.")
        self._set_thumbnail(None)
        self.quality_combo.config(values=self._quality_values_for_kind(None))

    # -- quality options ---------------------------------------------------

    @staticmethod
    def _available_heights(info: dict | None) -> list[int]:
        if not info:
            return []
        heights = set()
        for fmt in info.get("formats") or []:
            h = fmt.get("height")
            if isinstance(h, (int, float)) and h >= 100:
                heights.add(int(h))
        return sorted(heights, reverse=True)

    def _quality_values_for_kind(self, info: dict | None) -> list[str]:
        """Quality choices for the current format. `info` is None for 'no media loaded'."""
        if self.kind_var.get() in AUDIO_KINDS:
            return AUDIO_QUALITIES
        heights = self._available_heights(info)
        if not heights:
            return GENERIC_QUALITIES
        return [BEST] + [f"{h}p{self._size_note(info, h)}" for h in heights if h >= 144]

    def _size_note(self, info: dict | None, height: int) -> str:
        if not info:
            return ""
        best_video = 0
        best_audio = 0
        for fmt in info.get("formats") or []:
            size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if not size:
                continue
            if fmt.get("height") == height and fmt.get("vcodec") not in (None, "none"):
                best_video = max(best_video, size)
            if fmt.get("vcodec") == "none" and fmt.get("acodec") not in (None, "none"):
                best_audio = max(best_audio, size)
        total = best_video + (best_audio if best_video else 0)
        return f"   (~{human_bytes(total)})" if total else ""

    def _on_kind_changed(self, _event=None):
        values = self._quality_values_for_kind(self.probe_info)
        self.quality_combo.config(values=values)
        if self.quality_var.get() not in values:
            self.quality_var.set(values[0])
        self._remember_choice()

    def _sync_quality_selection(self):
        values = list(self.quality_combo.cget("values"))
        current = self.quality_var.get()
        if current in values:
            return
        wanted = parse_height(current)
        if wanted:
            for value in values:
                if parse_height(value) and parse_height(value) <= wanted:
                    self.quality_var.set(value)
                    return
        self.quality_var.set(values[0] if values else BEST)

    def _remember_choice(self):
        self.cfg["kind"] = self.kind_var.get()
        self.cfg["quality"] = strip_size_note(self.quality_var.get())
        save_config(self.cfg)

    # -- thumbnail ---------------------------------------------------------

    def _load_thumbnail(self, info: dict, seq: int):
        url = pick_thumbnail(info)
        if not url or not self.ffmpeg:
            self._after(self._set_thumbnail, None)
            return
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            raw = THUMB_DIR / f"{info.get('id', 'thumb')}.raw"
            png = THUMB_DIR / f"{info.get('id', 'thumb')}.png"
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                raw.write_bytes(response.read())
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "quiet", "-i", str(raw),
                            "-vf", "scale=208:-2", str(png)],
                           timeout=30, startupinfo=startup_info(),
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raw.unlink(missing_ok=True)
            if png.is_file():
                self._after(self._set_thumbnail, str(png), seq)
                return
        except Exception:
            pass
        self._after(self._set_thumbnail, None)

    def _set_thumbnail(self, path: str | None, seq: int | None = None):
        if seq is not None and seq != self.probe_seq:
            return
        if not path:
            self.thumb_image = None
            self.thumb_label.configure(image="", text="no preview", width=26, height=7)
            return
        try:
            self.thumb_image = tk.PhotoImage(file=path)
            self.thumb_label.configure(image=self.thumb_image, text="", width=0, height=0)
        except tk.TclError:
            self.thumb_image = None
            self.thumb_label.configure(image="", text="no preview", width=26, height=7)

    # -- queue management --------------------------------------------------

    def browse_dir(self):
        directory = filedialog.askdirectory(initialdir=self.dir_var.get(),
                                            title="Select download folder")
        if directory:
            self.dir_var.set(directory)
            self.cfg["download_dir"] = directory
            save_config(self.cfg)
            self.log(f"Download folder set to: {directory}")
            self.wake.set()

    def add_to_queue(self):
        if not self.ytdlp:
            messagebox.showerror(APP_NAME, "yt-dlp was not found, so nothing can be downloaded.")
            return
        dest = self._check_destination()
        if not dest:
            return

        text = self.url_var.get().strip()
        urls = split_urls(text)
        if not urls and looks_like_url(text):
            urls = [text]
        if not urls:
            messagebox.showwarning(APP_NAME, "Enter one or more URLs first.")
            return

        kind = self.kind_var.get()
        quality = strip_size_note(self.quality_var.get())
        opts = self._snapshot_options()
        added: list[Job] = []

        info = self.probe_info
        single = len(urls) == 1 and info is not None and urls[0] == self.last_probed_url

        if len(urls) == 1:
            twin = self._find_duplicate(urls[0])
            if twin is not None and not messagebox.askyesno(
                    APP_NAME, f"This is already in the queue ({twin.status.lower()}):\n\n"
                              f"{twin.display_title}\n\nAdd it again?"):
                return
        else:
            known = self._queued_keys()
            fresh = [u for u in urls if tabscan.url_key(u) not in known]
            if len(fresh) < len(urls):
                self.log(f"Skipped {len(urls) - len(fresh)} URL(s) already in the queue.")
            urls = fresh
            if not urls:
                self.set_status("Every one of those URLs is already in the queue.")
                return

        if single and info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e and (e.get("url") or e.get("id"))]
            choice = messagebox.askyesnocancel(
                APP_NAME,
                f"'{info.get('title') or 'Playlist'}' contains {len(entries)} items.\n\n"
                "Yes  -  add every item as a separate queue entry\n"
                "No   -  add the playlist as one queue entry\n"
                "Cancel  -  do nothing")
            if choice is None:
                return
            if choice:
                for entry in entries:
                    added.append(Job(
                        url=entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        title=entry.get("title") or "",
                        uploader=entry.get("uploader") or info.get("uploader") or "",
                        duration=human_duration(entry.get("duration")),
                        kind=kind, quality=quality, dest_dir=dest, opts=opts))
            else:
                added.append(Job(url=urls[0], title=info.get("title") or "", kind=kind,
                                 quality=quality, dest_dir=dest, whole_playlist=True, opts=opts))
        else:
            for url in urls:
                title = uploader = duration = ""
                if single and info:
                    title = info.get("title") or ""
                    uploader = info.get("uploader") or info.get("channel") or ""
                    duration = human_duration(info.get("duration"))
                added.append(Job(url=url, title=title, uploader=uploader, duration=duration,
                                 kind=kind, quality=quality, dest_dir=dest, opts=opts))

        self._remember_choice()
        self._enqueue(added)
        self.url_var.set("")
        self._reset_media_panel()
        self.url_entry.focus_set()

    def _enqueue(self, added: list[Job], source: str = ""):
        """Append jobs to the queue and get the scheduler going."""
        if not added:
            return
        with self.jobs_lock:
            self.jobs.extend(added)
        for job in added:
            self._insert_row(job)
        origin = f" from {source}" if source else ""
        self.log(f"Queued {len(added)} item(s){origin}.", "ok")
        self.set_status(f"Added {len(added)} item(s) to the queue{origin}.")
        self._refresh_overall()
        self._schedule_save()
        self.notebook.select(self.queue_tab)

        if self.cfg["auto_start"]:
            self.paused = False
            self._refresh_start_button()
        self.wake.set()

    def _check_destination(self) -> str | None:
        dest = self.dir_var.get().strip()
        if not dest or not Path(dest).is_dir():
            messagebox.showwarning(APP_NAME, "Choose a valid download folder first.")
            self.browse_dir()
            return None
        return dest

    def _queued_keys(self) -> set[str]:
        with self.jobs_lock:
            return {tabscan.url_key(j.url) for j in self.jobs}

    def _find_duplicate(self, url: str) -> Job | None:
        key = tabscan.url_key(url)
        with self.jobs_lock:
            for job in self.jobs:
                if tabscan.url_key(job.url) == key:
                    return job
        return None

    def queue_entries(self, entries: list[tuple[str, str, bool]], kind: str, quality: str,
                      source: str = "") -> tuple[int, int]:
        """
        Bulk-add (url, title, whole_playlist) entries from the batch and tab-scan
        windows. URLs that are already queued are skipped. Returns (added, skipped).
        """
        if not self.ytdlp:
            messagebox.showerror(APP_NAME, "yt-dlp was not found, so nothing can be downloaded.")
            return 0, 0
        dest = self._check_destination()
        if not dest:
            return 0, 0
        known = self._queued_keys()
        opts = self._snapshot_options()
        jobs: list[Job] = []
        skipped = 0
        for url, title, playlist in entries:
            key = tabscan.url_key(url)
            if key in known:
                skipped += 1
                continue
            known.add(key)
            jobs.append(Job(url=url, title=title, kind=kind, quality=quality, dest_dir=dest,
                            whole_playlist=playlist, opts=opts))
        self._enqueue(jobs, source)
        if skipped:
            self.log(f"Skipped {skipped} item(s) already in the queue.")
        return len(jobs), skipped

    def open_scan_dialog(self):
        if self.scan_dialog is not None:
            self.scan_dialog.top.deiconify()
            self.scan_dialog.top.lift()
            self.scan_dialog.top.focus_set()
            self.scan_dialog.rescan()
            return
        self.scan_dialog = TabScanDialog(self)

    def open_batch_dialog(self, text: str = ""):
        if self.batch_dialog is not None:
            self.batch_dialog.top.deiconify()
            self.batch_dialog.top.lift()
            if text:
                self.batch_dialog.append_text(text)
            self.batch_dialog.text.focus_set()
            return
        self.batch_dialog = BatchDialog(self, text)

    def _snapshot_options(self) -> dict:
        keys = ("subtitles", "auto_subs", "embed_subs", "sub_langs", "embed_thumbnail",
                "embed_metadata", "sponsorblock", "playlist_subfolder", "filename_template",
                "restrict_filenames", "use_archive", "impersonate", "limit_rate",
                "concurrent_fragments", "retries", "cookies_browser")
        return {k: self.cfg[k] for k in keys}

    def _insert_row(self, job: Job):
        self.tree.insert("", tk.END, iid=job.jid, values=self._row_values(job),
                         tags=(status_tag(job.status),))

    @staticmethod
    def _row_values(job: Job) -> tuple:
        return (action_glyph(job.status), job.display_title,
                job.kind.replace(" Video", "").replace(" Audio", ""),
                job.quality, job.status, bar_text(job.percent), job.size, job.speed, job.eta)

    def _update_row(self, job: Job):
        try:
            if self.tree.exists(job.jid):
                self.tree.item(job.jid, values=self._row_values(job),
                               tags=(status_tag(job.status),))
        except tk.TclError:
            pass

    def _fit_columns(self, _event=None):
        """Share the visible width out between the columns by weight."""
        available = self.tree.winfo_width() - 4
        if available < 120:
            return
        floor = sum(COLUMN_SPEC[c]["min"] for c in TREE_COLUMNS)
        if available <= floor:
            for col in TREE_COLUMNS:
                self.tree.column(col, width=COLUMN_SPEC[col]["min"])
            return
        used = 0
        for col in TREE_COLUMNS[:-1]:
            spec = COLUMN_SPEC[col]
            width = max(spec["min"], int(available * spec["weight"]))
            self.tree.column(col, width=width)
            used += width
        last = TREE_COLUMNS[-1]
        self.tree.column(last, width=max(COLUMN_SPEC[last]["min"], available - used))

    def _selected_jobs(self) -> list[Job]:
        ids = set(self.tree.selection())
        with self.jobs_lock:
            return [j for j in self.jobs if j.jid in ids]

    def _is_act_cell(self, event) -> bool:
        return (self.tree.identify_region(event.x, event.y) == "cell"
                and self.tree.identify_column(event.x) == ACT_COLUMN)

    def _on_tree_click(self, event):
        """Clicking the first column acts as that row's play/pause button."""
        if not self._is_act_cell(event):
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        self.toggle_job(row)
        return "break"          # do not also change the selection

    def _on_double_click(self, event):
        if self._is_act_cell(event):
            return "break"      # the single click already handled it
        self.open_selected_file()
        return None

    def _on_tree_motion(self, event):
        self.tree.configure(cursor="hand2" if self._is_act_cell(event) else "")

    def start_selected(self):
        for job in self._selected_jobs():
            if job.status in STARTABLE_STATES:
                self.start_job(job)

    def pause_selected(self):
        for job in self._selected_jobs():
            if job.status == STATUS_RUNNING:
                self.pause_job(job)

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.ctx.tk_popup(event.x_root, event.y_root)

    def remove_selected(self):
        jobs = self._selected_jobs()
        if not jobs:
            return
        removed = stopping = 0
        for job in jobs:
            with self.run_lock:
                busy = job.jid in self.active
            if busy:
                # Stop it first; it leaves the queue once its thread has exited.
                self.remove_when_stopped.add(job.jid)
                self.cancel_job(job)
                stopping += 1
                continue
            self._drop_job(job)
            removed += 1
        if stopping:
            self.set_status(f"Removed {removed} item(s); {stopping} running download(s) "
                            "will be removed as soon as they stop.")
        elif removed:
            self.set_status(f"Removed {removed} item(s).")
        self._refresh_overall()
        self._schedule_save()

    def _drop_job(self, job: Job):
        with self.jobs_lock:
            if job in self.jobs:
                self.jobs.remove(job)
        if self.tree.exists(job.jid):
            self.tree.delete(job.jid)

    def _drop_if_pending(self, jid: str):
        """Finish a removal that had to wait for the download to stop."""
        if jid not in self.remove_when_stopped:
            return
        self.remove_when_stopped.discard(jid)
        job = self._job_by_id(jid)
        if job is not None:
            self._drop_job(job)
            self._refresh_overall()
            self._schedule_save()

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def move_selected_to_edge(self, top: bool):
        jobs = self._selected_jobs()
        if not jobs:
            return
        with self.jobs_lock:
            rest = [j for j in self.jobs if j not in jobs]
            self.jobs = jobs + rest if top else rest + jobs
            order = [j.jid for j in self.jobs]
        for position, jid in enumerate(order):
            if self.tree.exists(jid):
                self.tree.move(jid, "", position)
        self.tree.see(jobs[0].jid if top else jobs[-1].jid)
        self._schedule_save()

    def apply_format_to_selected(self):
        """Re-target queued items, e.g. switch a batch of tab scans to MP3."""
        kind = self.kind_var.get()
        quality = strip_size_note(self.quality_var.get())
        changed = 0
        for job in self._selected_jobs():
            if job.status in (STATUS_RUNNING, STATUS_DONE, STATUS_SKIPPED):
                continue
            if job.kind != kind:
                job.percent = 0.0  # a partial file in the old format is no use
            job.kind, job.quality = kind, quality
            self._update_row(job)
            changed += 1
        self.set_status(f"Set {changed} item(s) to {kind}, {quality}." if changed
                        else "Only items that are not running or finished can be changed.")
        if changed:
            self._schedule_save()

    def move_selected(self, delta: int):
        jobs = self._selected_jobs()
        if not jobs:
            return
        with self.jobs_lock:
            indices = sorted((self.jobs.index(j) for j in jobs), reverse=delta > 0)
            for index in indices:
                target = index + delta
                if 0 <= target < len(self.jobs):
                    self.jobs[index], self.jobs[target] = self.jobs[target], self.jobs[index]
            order = [j.jid for j in self.jobs]
        for position, jid in enumerate(order):
            if self.tree.exists(jid):
                self.tree.move(jid, "", position)
        self._schedule_save()

    def retry_selected(self):
        count = 0
        for job in self._selected_jobs():
            if job.status in (STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED, STATUS_DONE):
                self._reset_job(job)
                count += 1
        if count:
            self.log(f"Re-queued {count} item(s).")
            self.wake.set()
            self._refresh_overall()
            self._schedule_save()

    def retry_failed(self):
        with self.jobs_lock:
            targets = [j for j in self.jobs if j.status in (STATUS_FAILED, STATUS_CANCELLED)]
        for job in targets:
            self._reset_job(job)
        if targets:
            self.log(f"Re-queued {len(targets)} failed item(s).")
            self.wake.set()
            self._refresh_overall()
            self._schedule_save()
        else:
            self.set_status("Nothing to retry.")

    def _reset_job(self, job: Job):
        job.status = STATUS_QUEUED
        job.percent = 0.0
        job.speed = job.eta = job.error = job.part = ""
        job.speed_bytes = 0.0
        self._update_row(job)

    def clear_finished(self):
        with self.jobs_lock:
            keep, drop = [], []
            for job in self.jobs:
                (drop if job.status in FINISHED_STATES else keep).append(job)
            self.jobs = keep
        for job in drop:
            if self.tree.exists(job.jid):
                self.tree.delete(job.jid)
        self.set_status(f"Cleared {len(drop)} finished item(s).")
        self._refresh_overall()
        self._schedule_save()

    def open_selected_file(self):
        for job in self._selected_jobs():
            if job.filepath and Path(job.filepath).is_file():
                try:
                    if IS_WINDOWS:
                        os.startfile(job.filepath)  # noqa: S606
                    else:
                        open_in_explorer(job.filepath)
                except Exception as exc:
                    self.log(f"Could not open file: {exc}", "error")
            else:
                self.set_status("No downloaded file recorded for that item yet.")
            break

    def reveal_selected_file(self):
        for job in self._selected_jobs():
            open_in_explorer(job.filepath or job.dest_dir, select=bool(job.filepath))
            break

    def copy_selected_url(self):
        jobs = self._selected_jobs()
        if jobs:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(j.url for j in jobs))
            self.set_status("URL copied.")

    def open_selected_url(self):
        for job in self._selected_jobs()[:10]:
            webbrowser.open(job.url)

    def load_selected_url(self):
        jobs = self._selected_jobs()
        if jobs:
            self.url_var.set(jobs[0].url)
            self.probe_now()

    def show_selected_error(self):
        for job in self._selected_jobs():
            messagebox.showinfo(APP_NAME, job.error or "No error recorded for this item.")
            break

    def clear_archive(self):
        if not ARCHIVE_FILE.is_file():
            self.set_status("No download archive to clear.")
            return
        if messagebox.askyesno(APP_NAME, "Forget every previously downloaded item?\n\n"
                                         "Files on disk are not touched."):
            try:
                ARCHIVE_FILE.unlink()
                self.log("Download archive cleared.")
            except Exception as exc:
                self.log(f"Could not clear archive: {exc}", "error")

    # -- queue persistence -------------------------------------------------

    def _schedule_save(self):
        if self.save_after_id:
            self.root.after_cancel(self.save_after_id)
        self.save_after_id = self.root.after(600, self._save_queue)

    def _save_queue(self):
        self.save_after_id = None
        try:
            APP_DATA.mkdir(parents=True, exist_ok=True)
            with self.jobs_lock:
                data = []
                for job in self.jobs:
                    item = job.to_dict()
                    if item["status"] == STATUS_RUNNING:
                        item["status"] = STATUS_QUEUED
                        item["percent"] = 0.0
                    data.append(item)
            QUEUE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _restore_queue(self):
        if not QUEUE_FILE.is_file():
            return
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        restored = 0
        for item in data:
            try:
                job = Job.from_dict(item)
            except Exception:
                continue
            if job.status == STATUS_RUNNING:
                job.status = STATUS_QUEUED
                job.percent = 0.0
            with self.jobs_lock:
                self.jobs.append(job)
            self._insert_row(job)
            restored += 1
        if restored:
            self.log(f"Restored {restored} item(s) from the previous session.")
            self._refresh_overall()

    # -- queue worker ------------------------------------------------------

    def toggle_queue(self):
        self.paused = not self.paused
        self._refresh_start_button()
        if not self.paused:
            self.wake.set()
        self.set_status("Queue held - running downloads continue, nothing new starts."
                        if self.paused else "Queue running.")
        self._refresh_overall()

    def _refresh_start_button(self):
        self.start_button.config(text="Start queue" if self.paused else "Hold queue")

    @property
    def max_concurrent(self) -> int:
        try:
            return max(1, int(self.cfg.get("max_concurrent", 1)))
        except (TypeError, ValueError):
            return 1

    def _running_jobs(self) -> list[Job]:
        with self.jobs_lock:
            return [j for j in self.jobs if j.status == STATUS_RUNNING]

    def _job_by_id(self, jid: str) -> Job | None:
        with self.jobs_lock:
            for job in self.jobs:
                if job.jid == jid:
                    return job
        return None

    # -- per-item play / pause --------------------------------------------

    def toggle_job(self, jid: str):
        """The play/pause control in the first column of each row."""
        job = self._job_by_id(jid)
        if job is None:
            return
        if job.status == STATUS_RUNNING:
            self.pause_job(job)
        elif job.status in STARTABLE_STATES:
            self.start_job(job)
        else:
            self.set_status(f"'{job.display_title}' has already finished.")

    def toggle_selected(self):
        for job in self._selected_jobs():
            self.toggle_job(job.jid)

    def start_job(self, job: Job):
        """Start (or resume) one item now, regardless of the concurrency limit."""
        if not self.ytdlp:
            return
        if not job.dest_dir or not Path(job.dest_dir).is_dir():
            messagebox.showwarning(APP_NAME,
                                   f"The download folder for this item no longer exists:\n"
                                   f"{job.dest_dir}")
            return
        with self.run_lock:
            if job.jid in self.active:
                self.set_status(f"'{job.display_title}' is already running.")
                return
        if job.status in (STATUS_FAILED, STATUS_CANCELLED):
            job.percent = 0.0          # a retry restarts the accounting
        job.status = STATUS_QUEUED
        job.error = ""
        self._update_row(job)
        if self._launch(job):
            self.set_status(f"Started: {job.display_title}")

    def pause_job(self, job: Job):
        """
        Stop yt-dlp but keep the partial file, so starting again continues from
        where it stopped rather than from the beginning.
        """
        with self.run_lock:
            if job.jid in self.pause_flags:
                self.set_status("Already pausing - waiting for yt-dlp to stop.")
                return
            if job.jid not in self.active:
                self.set_status("That item is not downloading.")
                return
            self.pause_flags.add(job.jid)
            proc = self.procs.get(job.jid)
        self.log(f"Pausing: {job.display_title}", "warning")
        self.set_status(f"Pausing {job.display_title}...")
        if proc is not None:
            threading.Thread(target=kill_process_tree, args=(proc,), daemon=True).start()

    def cancel_job(self, job: Job):
        """Stop a download and discard it -- unlike pause, this does not resume."""
        with self.run_lock:
            if job.jid in self.cancel_flags or job.jid not in self.active:
                return
            self.cancel_flags.add(job.jid)
            proc = self.procs.get(job.jid)
        self.log(f"Cancelling: {job.display_title}", "warning")
        if proc is not None:
            threading.Thread(target=kill_process_tree, args=(proc,), daemon=True).start()

    def cancel_running(self):
        running = self._running_jobs()
        if not running:
            self.set_status("Nothing is downloading.")
            return
        for job in running:
            self.cancel_job(job)
        self.set_status(f"Cancelling {len(running)} download(s)...")

    def pause_running(self):
        running = self._running_jobs()
        if not running:
            self.set_status("Nothing is downloading.")
            return
        for job in running:
            self.pause_job(job)

    # -- scheduling --------------------------------------------------------

    def _next_queued(self) -> Job | None:
        with self.jobs_lock, self.run_lock:
            for job in self.jobs:
                if job.status == STATUS_QUEUED and job.jid not in self.active:
                    return job
        return None

    def _launch(self, job: Job) -> bool:
        """Claim a job and run it on its own thread. False if already claimed."""
        with self.run_lock:
            if job.jid in self.active:
                return False
            self.active.add(job.jid)
            self.cancel_flags.discard(job.jid)
            self.pause_flags.discard(job.jid)
        threading.Thread(target=self._job_thread, args=(job,), daemon=True).start()
        return True

    def _scheduler_loop(self):
        """Keep up to `max_concurrent` downloads running while the queue is live."""
        while not self.shutdown:
            self.wake.wait(0.4)
            self.wake.clear()
            if self.shutdown:
                return
            if self.paused:
                continue
            while not self.shutdown:
                with self.run_lock:
                    if len(self.active) >= self.max_concurrent:
                        break
                job = self._next_queued()
                if job is None or not self._launch(job):
                    break

    def _job_thread(self, job: Job):
        try:
            self._run_job(job)
        except Exception as exc:
            job.status = STATUS_FAILED
            job.error = str(exc)
            self._after(self._update_row, job)
            self.log(f"Unexpected error: {exc}", "error")
        finally:
            with self.run_lock:
                self.active.discard(job.jid)
                self.procs.pop(job.jid, None)
                self.cancel_flags.discard(job.jid)
                self.pause_flags.discard(job.jid)
            self._after(self._drop_if_pending, job.jid)
            self._after(self._refresh_overall)
            self._after(self._refresh_running_bar)
            self._after(self._schedule_save)
            self._after(self._check_queue_finished)
            self.wake.set()

    # -- command building --------------------------------------------------

    def _build_command(self, job: Job, impersonate: bool) -> list[str]:
        opts = job.opts or self._snapshot_options()
        cmd = [self.ytdlp, "--no-update", "--newline", "--progress", "--no-simulate",
               "--progress-delta", "0.3",
               "--progress-template", PROGRESS_TEMPLATE,
               "--print", f"after_move:{F_TAG}%(filepath)s",
               "--print", f"before_dl:{T_TAG}%(title)s",
               "--retries", str(opts.get("retries", 10)),
               "--fragment-retries", str(opts.get("retries", 10)),
               "--concurrent-fragments", str(opts.get("concurrent_fragments", 4))]

        if IS_WINDOWS:
            cmd += ["--windows-filenames", "--trim-filenames", "200"]
        if opts.get("restrict_filenames"):
            cmd.append("--restrict-filenames")
        if impersonate:
            cmd += ["--impersonate", "chrome"]
        if opts.get("limit_rate"):
            cmd += ["--limit-rate", str(opts["limit_rate"])]
        if opts.get("cookies_browser"):
            cmd += ["--cookies-from-browser", str(opts["cookies_browser"])]
        if opts.get("use_archive"):
            cmd += ["--download-archive", str(ARCHIVE_FILE)]
        if self.ffmpeg:
            cmd += ["--ffmpeg-location", str(Path(self.ffmpeg).parent)]

        cmd.append("--yes-playlist" if job.whole_playlist else "--no-playlist")

        cmd += self._format_args(job)

        if opts.get("embed_metadata"):
            cmd += ["--embed-metadata"]
        if opts.get("embed_thumbnail"):
            cmd += ["--embed-thumbnail"]
        if opts.get("subtitles"):
            cmd += ["--write-subs", "--sub-langs", str(opts.get("sub_langs") or "en"),
                    "--convert-subs", "srt"]
            if opts.get("auto_subs"):
                cmd.append("--write-auto-subs")
            if opts.get("embed_subs"):
                cmd.append("--embed-subs")
        if opts.get("sponsorblock"):
            cmd += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]

        template = str(opts.get("filename_template") or "%(title)s.%(ext)s")
        if opts.get("playlist_subfolder"):
            # The '/' here is a path separator for yt-dlp, so join as plain text --
            # pathlib would rewrite it and break the conditional template.
            template = "%(playlist_title&{}/|)s" + template
        cmd += ["-o", os.path.join(job.dest_dir, template)]

        cmd.append(job.url)
        return cmd

    def _format_args(self, job: Job) -> list[str]:
        kind = job.kind
        if kind in AUDIO_KINDS:
            codec = kind.split()[0].lower()
            quality = audio_quality_arg(job.quality)
            return ["-f", "bestaudio/best", "-x", "--audio-format", codec,
                    "--audio-quality", quality]

        container = {"MP4 Video": "mp4", "MKV Video": "mkv", "WEBM Video": "webm"}[kind]
        height = parse_height(job.quality)
        limit = f"[height<={height}]" if height else ""

        # Preferred container first, then any codec at the wanted height, then
        # anything at all so a download never fails purely on format choice.
        preferred = {"mp4": ("[ext=mp4]", "[ext=m4a]"),
                     "webm": ("[ext=webm]", "[ext=webm]")}.get(container)
        candidates = []
        if preferred:
            candidates.append(f"bv*{limit}{preferred[0]}+ba{preferred[1]}")
        candidates += [f"bv*{limit}+ba", f"b{limit}", "b"]
        selector = "/".join(dict.fromkeys(candidates))
        return ["-f", selector, "--merge-output-format", container]

    # -- running a job -----------------------------------------------------

    def _stopping(self, job: Job) -> bool:
        with self.run_lock:
            return job.jid in self.cancel_flags or job.jid in self.pause_flags

    def _run_job(self, job: Job) -> None:
        self.batch_active = True
        resuming = job.percent > 0
        job.status = STATUS_RUNNING
        job.error = ""
        self._after(self._update_row, job)
        self._after(self._refresh_overall)
        self.log(f"{'Resuming' if resuming else 'Starting'}: {job.display_title}", "ok")

        impersonate = bool((job.opts or {}).get("impersonate", self.cfg["impersonate"]))
        code, output = self._spawn(job, impersonate)

        if (code != 0 and impersonate and not self._stopping(job)
                and re.search(r"impersonat|curl_cffi", output, re.IGNORECASE)):
            self.log("Retrying without browser impersonation...", "warning")
            code, output = self._spawn(job, False)

        with self.run_lock:
            was_paused = job.jid in self.pause_flags
            was_cancelled = job.jid in self.cancel_flags

        if was_paused:
            job.status = STATUS_PAUSED
            job.speed = job.eta = ""
            job.speed_bytes = 0.0
            self.log(f"Paused: {job.display_title} - press play to continue", "warning")
        elif was_cancelled:
            job.status = STATUS_CANCELLED
            job.speed = job.eta = ""
            job.speed_bytes = 0.0
            self.log(f"Cancelled: {job.display_title}", "warning")
        elif code == 0:
            if job.status != STATUS_SKIPPED:
                job.status = STATUS_DONE
                job.percent = 100.0
                self.log(f"Finished: {job.display_title}", "ok")
            else:
                self.log(f"Already downloaded: {job.display_title}")
            job.speed = job.eta = ""
            job.speed_bytes = 0.0
        else:
            job.status = STATUS_FAILED
            errors = [ln for ln in output.splitlines() if ln.startswith("ERROR")]
            job.error = errors[-1] if errors else f"yt-dlp exited with code {code}"
            job.speed = job.eta = ""
            job.speed_bytes = 0.0
            self.log(f"Failed: {job.display_title} - {job.error}", "error")

        self._after(self._update_row, job)
        self._after(self._refresh_running_bar)

    def _spawn(self, job: Job, impersonate: bool) -> tuple[int, str]:
        cmd = self._build_command(job, impersonate)
        self.log("> " + " ".join(quote(a) for a in cmd), "cmd")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, startupinfo=startup_info(),
                                    creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            return 1, "ERROR: yt-dlp executable not found."

        with self.run_lock:
            self.procs[job.jid] = proc
            stop_now = job.jid in self.cancel_flags or job.jid in self.pause_flags
        if stop_now:
            # Pause or cancel arrived while the process was still starting up.
            kill_process_tree(proc)

        expected_streams = 1 if job.kind in AUDIO_KINDS else 2
        finished_streams = 0
        collected: list[str] = []

        for raw in iter(proc.stdout.readline, ""):
            line = ANSI_RE.sub("", raw.rstrip("\r\n"))
            if not line:
                continue

            if line.startswith(P_TAG):
                finished_streams = self._handle_progress(
                    job, line[len(P_TAG):], expected_streams, finished_streams)
                continue

            if line.startswith(F_TAG):
                job.filepath = line[len(F_TAG):].strip()
                continue

            if line.startswith(T_TAG):
                # Fills in the title of items queued without fetching their info
                # first, such as a batch of links or a tab scan.
                title = line[len(T_TAG):].strip()
                if title and title != "NA" and not job.whole_playlist and not job.title:
                    job.title = title
                    self._after(self._update_row, job)
                continue

            collected.append(line)
            # Two different skips: the file is already on disk, or the download
            # archive says we have fetched this id before.
            if ("has already been downloaded" in line
                    or "has already been recorded in the archive" in line):
                job.status = STATUS_SKIPPED
                job.percent = 100.0
                self._after(self._update_row, job)
            level = "error" if line.startswith("ERROR") else (
                "warning" if line.startswith("WARNING") else "info")
            self.log(line, level)
            if line.startswith(("[Merger]", "[ExtractAudio]", "[Fixup", "[EmbedSubtitle",
                                "[Metadata]", "[SponsorBlock]", "[ThumbnailsConvertor]")):
                job.speed = job.eta = ""
                job.speed_bytes = 0.0
                self._after(self._update_row, job)

        try:
            proc.stdout.close()
        except Exception:
            pass
        code = proc.wait()
        with self.run_lock:
            self.procs.pop(job.jid, None)
        return code, "\n".join(collected)

    def _handle_progress(self, job: Job, payload: str, expected: int, finished: int) -> int:
        parts = payload.split("|")
        if len(parts) < 6:
            return finished
        downloaded = to_number(parts[0])
        total = to_number(parts[1]) or to_number(parts[2])
        speed = to_number(parts[3])
        eta = parts[4]
        state = parts[5]

        if state == "finished":
            finished += 1
            fraction = 1.0
        elif total and downloaded is not None:
            fraction = min(1.0, downloaded / total)
        else:
            fraction = 0.0

        streams = max(expected, finished + (0 if state == "finished" else 1))
        completed = finished - (1 if state == "finished" else 0)
        job.percent = min(100.0, (completed + fraction) / streams * 100)
        job.size = human_bytes(total) if total else ""
        job.speed = (human_bytes(speed) + "/s") if speed else ""
        job.speed_bytes = float(speed or 0.0)
        job.eta = human_eta(eta) if state != "finished" else ""
        job.part = f"{min(finished + 1, streams)}/{streams}" if streams > 1 else ""

        self._after(self._push_progress, job)
        return finished

    def _push_progress(self, job: Job):
        self._update_row(job)
        self._refresh_running_bar()
        self._refresh_overall()

    def _refresh_running_bar(self):
        """The 'Current' bar covers every running download, not just one."""
        running = self._running_jobs()
        if not running:
            self.current_bar["value"] = 0
            self.current_label_var.set("Idle")
            self.root.title(f"{APP_NAME} {APP_VERSION}")
            return

        average = sum(j.percent for j in running) / len(running)
        self.current_bar["value"] = average
        if len(running) == 1:
            job = running[0]
            bits = [f"{job.percent:.1f}%", job.speed,
                    f"ETA {job.eta}" if job.eta else "",
                    f"part {job.part}" if job.part else ""]
        else:
            total_speed = sum(j.speed_bytes for j in running)
            bits = [f"{len(running)} downloading", f"{average:.1f}% average",
                    f"{human_bytes(total_speed)}/s" if total_speed else ""]
        self.current_label_var.set("  ".join(b for b in bits if b))
        self.root.title(f"[{average:.0f}%] {APP_NAME} {APP_VERSION}")

    def _refresh_overall(self):
        with self.jobs_lock:
            total = len(self.jobs)
            done = sum(1 for j in self.jobs if j.status in FINISHED_STATES)
            running = [j for j in self.jobs if j.status == STATUS_RUNNING]
            waiting = sum(1 for j in self.jobs if j.status == STATUS_QUEUED)
            held = sum(1 for j in self.jobs if j.status == STATUS_PAUSED)
        self.notebook.tab(self.queue_tab, text=f"  Queue ({total})  " if total else "  Queue  ")
        if total == 0:
            self.empty_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        else:
            self.empty_label.place_forget()
        if total == 0:
            self.overall_bar["value"] = 0
            self.overall_label_var.set("Queue empty")
            return
        fraction = sum(j.percent for j in running) / 100
        percent = min(100.0, (done + fraction) / total * 100)
        self.overall_bar["value"] = percent

        if running:
            state = f"{len(running)} running"
        elif self.paused:
            state = "queue held"
        else:
            state = "idle"
        parts = [f"{done}/{total} done", f"{waiting} waiting"]
        if held:
            parts.append(f"{held} paused")
        parts.append(state)
        self.overall_label_var.set(" - ".join(parts))

    def _check_queue_finished(self):
        """Say so once when the last download of a run has finished."""
        if not self.batch_active:
            return
        with self.run_lock:
            if self.active:
                return
        with self.jobs_lock:
            if any(j.status == STATUS_PAUSED for j in self.jobs):
                return  # paused by hand, so the run is not over
            waiting = any(j.status == STATUS_QUEUED for j in self.jobs)
            done = sum(1 for j in self.jobs if j.status in (STATUS_DONE, STATUS_SKIPPED))
            failed = sum(1 for j in self.jobs if j.status == STATUS_FAILED)
        if waiting and not self.paused:
            return  # the scheduler is about to start the next one
        self.batch_active = False
        message = f"Queue finished - {done} downloaded"
        if failed:
            message += f", {failed} failed (F5 retries them)"
        self.log(message, "warning" if failed else "ok")
        self.set_status(message)
        if self.cfg.get("notify_done"):
            self._notify()

    def _notify(self):
        try:
            self.root.bell()
        except tk.TclError:
            pass
        if IS_WINDOWS and self.root.focus_displayof() is None:
            try:
                import ctypes
                hwnd = int(self.root.wm_frame(), 16)
                ctypes.windll.user32.FlashWindow(hwnd, True)
            except Exception:
                pass

    # -- yt-dlp maintenance ------------------------------------------------

    def _check_version(self):
        if not self.ytdlp:
            self._after(self.version_var.set, "yt-dlp: not found")
            return
        try:
            proc = subprocess.run([self.ytdlp, "--version"], capture_output=True, text=True,
                                  timeout=60, startupinfo=startup_info(),
                                  creationflags=CREATE_NO_WINDOW)
            version = (proc.stdout or "").strip() or "unknown"
        except Exception:
            version = "unknown"
        self._after(self.version_var.set, f"yt-dlp version: {version}")
        age = version_age_days(version)
        if age is not None and age > 90:
            self.log(f"yt-dlp is {age} days old - use Tools > Update yt-dlp if sites start failing.",
                     "warning")

    def update_ytdlp(self):
        if not self.ytdlp:
            return
        self.update_button.config(state=tk.DISABLED)
        self.notebook.select(1)
        self.log("Updating yt-dlp...")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            proc = subprocess.run([self.ytdlp, "-U"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600,
                                  startupinfo=startup_info(), creationflags=CREATE_NO_WINDOW)
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.strip():
                    self.log(line.strip())
            if proc.returncode == 0:
                self.log("yt-dlp update finished.", "ok")
                try:
                    if BIN_STAMP.is_file():
                        BIN_STAMP.write_text(json.dumps({"size": Path(self.ytdlp).stat().st_size}),
                                             encoding="utf-8")
                except Exception:
                    pass
            else:
                self.log(f"Update failed with exit code {proc.returncode}.", "error")
        except Exception as exc:
            self.log(f"Update failed: {exc}", "error")
        finally:
            self._after(lambda: self.update_button.config(state=tk.NORMAL))
            threading.Thread(target=self._check_version, daemon=True).start()

    # -- misc dialogs ------------------------------------------------------

    def show_shortcuts(self):
        messagebox.showinfo(
            "Keyboard shortcuts",
            "Ctrl+V\tPaste a URL and fetch its info\n"
            "Enter\tFetch info for the URL in the box\n"
            "Ctrl+Enter\tAdd to the queue\n"
            "Ctrl+M\tAdd many URLs at once\n"
            "Ctrl+T\tScan browser tabs for video and audio\n"
            "Ctrl+A\tSelect every queue item\n"
            "Ctrl+O\tChoose the download folder\n"
            "Delete\tRemove the selected queue items\n"
            "F5\tRetry every failed item\n"
            "Ctrl+L\tClear the log\n"
            "Space\tPlay / pause the selected row\n"
            "First column\tClick to play or pause that download\n"
            "Double-click\tOpen a finished file\n"
            "Right-click\tQueue item actions")

    def show_about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "A tkinter front-end for yt-dlp with automatic media inspection,\n"
            "a persistent download queue, live progress and browser tab scanning.\n\n"
            f"yt-dlp: {self.ytdlp or 'not found'}\n"
            f"ffmpeg: {self.ffmpeg or 'not found'}\n"
            f"App data: {APP_DATA}")

    # -- shutdown ----------------------------------------------------------

    def on_close(self):
        with self.run_lock:
            live = list(self.procs.values())
            count = len(self.active)
        if count and not messagebox.askokcancel(
                APP_NAME, f"{count} download(s) still running. Quit anyway?\n\n"
                          "Partly downloaded files are kept, so they can be resumed."):
            return
        self.shutdown = True
        self.paused = True
        self.wake.set()
        for proc in live:
            kill_process_tree(proc)
        try:
            self.cfg["geometry"] = self.root.geometry()
        except Exception:
            pass
        save_config(self.cfg)
        self._save_queue()
        self.root.destroy()


# --------------------------------------------------------------------------
# Bulk-add windows
# --------------------------------------------------------------------------

CHECK_ON = "☑"    # ballot box with check
CHECK_OFF = "☐"   # ballot box
CHECK_NA = "–"    # en dash: already queued, nothing to tick


class _BulkDialog:
    """Shared frame for the windows that add many items at once."""

    def __init__(self, app: YtdlpGui, title: str, size: tuple[int, int]):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title(title)
        self.top.transient(app.root)
        self.top.minsize(560, 360)
        try:
            scale = max(1.0, float(app.root.tk.call("tk", "scaling")) / 1.3333)
        except (tk.TclError, ValueError):
            scale = 1.0
        width, height = int(size[0] * scale), int(size[1] * scale)
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - width) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - height) // 3)
        self.top.geometry(f"{width}x{height}+{x}+{y}")
        self.top.bind("<Escape>", lambda e: self.close())
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        self.body = ttk.Frame(self.top, padding=12)
        self.body.pack(fill=tk.BOTH, expand=True)

    def _build_format_row(self, parent) -> ttk.Frame:
        """Format and quality for everything this window adds, preset from the main window."""
        row = ttk.Frame(parent)
        kind = self.app.kind_var.get()
        self.kind_var = tk.StringVar(value=kind)
        self.quality_var = tk.StringVar()
        ttk.Label(row, text="Format:").pack(side=tk.LEFT)
        kind_combo = ttk.Combobox(row, textvariable=self.kind_var, values=ALL_KINDS,
                                  state="readonly", width=12)
        kind_combo.pack(side=tk.LEFT, padx=(6, 14))
        ttk.Label(row, text="Quality:").pack(side=tk.LEFT)
        self.quality_combo = ttk.Combobox(row, textvariable=self.quality_var,
                                          state="readonly", width=15)
        self.quality_combo.pack(side=tk.LEFT, padx=(6, 0))
        kind_combo.bind("<<ComboboxSelected>>", lambda e: self._sync_qualities())
        self._sync_qualities(strip_size_note(self.app.quality_var.get()))
        return row

    def _sync_qualities(self, wanted: str | None = None):
        values = AUDIO_QUALITIES if self.kind_var.get() in AUDIO_KINDS else GENERIC_QUALITIES
        self.quality_combo.config(values=values)
        wanted = wanted or self.quality_var.get()
        self.quality_var.set(wanted if wanted in values else values[0])

    def apply_theme(self):
        try:
            self.top.configure(bg=self.app.theme["bg"])
        except tk.TclError:
            pass

    def close(self):
        for attr in ("scan_dialog", "batch_dialog"):
            if getattr(self.app, attr) is self:
                setattr(self.app, attr, None)
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class TabScanDialog(_BulkDialog):
    """
    Lists the video and audio pages open in the user's browsers, all ticked,
    so the whole lot can be queued with one click.
    """

    TITLE_LIMIT = 70

    def __init__(self, app: YtdlpGui):
        super().__init__(app, "Scan browser tabs", (900, 600))
        self.rows: dict[str, dict] = {}
        self.results: list[tabscan.BrowserResult] = []
        self.scan_seq = 0

        cfg = app.cfg
        self.show_all_var = tk.BooleanVar(value=bool(cfg.get("scan_show_all")))
        self.include_closed_var = tk.BooleanVar(value=bool(cfg.get("scan_include_closed")))
        self.summary_var = tk.StringVar(value="Scanning your browsers...")
        self.detail_var = tk.StringVar(value="")

        head = ttk.Frame(self.body)
        head.pack(fill=tk.X)
        head.columnconfigure(0, weight=1)
        ttk.Label(head, textvariable=self.summary_var, style="Title.TLabel").grid(
            row=0, column=0, sticky=tk.W)
        self.progress = ttk.Progressbar(head, mode="indeterminate", length=140)
        self.progress.grid(row=0, column=1, sticky=tk.E)
        self.detail_label = ttk.Label(head, textvariable=self.detail_var, style="Muted.TLabel",
                                      justify=tk.LEFT, wraplength=820)
        self.detail_label.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))
        head.bind("<Configure>", lambda e: self.detail_label.configure(
            wraplength=max(300, e.width - 10)))

        opts = ttk.Frame(self.body)
        opts.pack(fill=tk.X, pady=(10, 6))
        ttk.Checkbutton(opts, text="Show every tab, not only video and audio",
                        variable=self.show_all_var, command=self._on_show_all).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Include closed browsers (tabs from their last session)",
                        variable=self.include_closed_var,
                        command=self._on_include_closed).pack(side=tk.LEFT, padx=(16, 0))

        bottom = ttk.Frame(self.body)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self._build_format_row(bottom).pack(side=tk.LEFT)
        self.add_button = ttk.Button(bottom, text="Add to queue", style="Accent.TButton",
                                     command=self.add_checked)
        self.add_button.pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Close", width=9, command=self.close).pack(side=tk.RIGHT, padx=6)
        self.rescan_button = ttk.Button(bottom, text="Rescan", width=9, command=self.rescan)
        self.rescan_button.pack(side=tk.RIGHT)

        picks = ttk.Frame(self.body)
        picks.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(picks, text="Tick all", width=9,
                   command=lambda: self._set_all(True)).pack(side=tk.LEFT)
        ttk.Button(picks, text="Tick none", width=10,
                   command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=4)
        ttk.Label(picks, text="Click a row to tick or untick it. Tabs opened in the last "
                              "few seconds may be missing - press Rescan.",
                  style="Muted.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        wrap = ttk.Frame(self.body)
        wrap.pack(fill=tk.BOTH, expand=True)
        columns = ("check", "title", "site", "browser")
        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        for col, text, width, stretch, anchor in (
                ("check", CHECK_ON, 36, False, tk.CENTER), ("title", "Title", 420, True, tk.W),
                ("site", "Site", 150, False, tk.W), ("browser", "Browser", 170, False, tk.W)):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=width, minwidth=width if not stretch else 160,
                             stretch=stretch, anchor=anchor)
        self.tree.heading("check", command=self._toggle_all)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: (self._toggle_selection(), "break")[1])
        self.tree.bind("<Double-1>", lambda e: "break")
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(
            self.tree.get_children()), "break")[1])
        self.top.bind("<Return>", lambda e: self.add_checked())
        self.top.bind("<F5>", lambda e: self.rescan())

        self.apply_theme()
        self.rescan()

    # -- scanning ----------------------------------------------------------

    def rescan(self):
        self.scan_seq += 1
        seq = self.scan_seq
        self.summary_var.set("Scanning your browsers...")
        self.detail_var.set("Reading the session files of Chrome, Brave, Edge, Vivaldi, "
                            "Opera, Firefox and others.")
        self.progress.grid()
        self.progress.start(12)
        self.rescan_button.config(state=tk.DISABLED)
        include_closed = self.include_closed_var.get()

        def work():
            try:
                results = tabscan.scan_browsers(include_closed=include_closed)
                error = ""
            except Exception as exc:  # never leave the window spinning
                results, error = [], str(exc)
            self.app._after(self._scan_done, seq, results, error)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, seq: int, results: list, error: str):
        if seq != self.scan_seq or not self.top.winfo_exists():
            return
        self.progress.stop()
        self.progress.grid_remove()
        self.rescan_button.config(state=tk.NORMAL)
        self.results = results
        if error:
            self.app.log(f"Tab scan failed: {error}", "error")
        self._populate()

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        known = self.app._queued_keys()
        seen: set[str] = set()
        media_rows, other_rows = [], []
        for result in self.results:
            for tab in result.tabs:
                key = tabscan.url_key(tab.url)
                if key in seen:
                    continue  # the same page open twice, or in two browsers
                seen.add(key)
                media = tabscan.is_media_url(tab.url)
                row = {"tab": tab, "media": media, "queued": key in known, "checked": False}
                row["checked"] = media and not row["queued"] and tab.running
                (media_rows if media else other_rows).append(row)

        rows = media_rows + (other_rows if self.show_all_var.get() else [])
        for index, row in enumerate(rows):
            iid = f"r{index}"
            self.rows[iid] = row
            self.tree.insert("", tk.END, iid=iid, values=self._values(row), tags=self._tags(row))
        self._update_summary(len(media_rows), len(media_rows) + len(other_rows))
        self._update_add_button()

    def _values(self, row: dict) -> tuple:
        tab = row["tab"]
        title = tabscan.clean_title(tab.title) or tab.url
        browser = tab.source if tab.running else f"{tab.source} - closed"
        if row["queued"]:
            check, browser = CHECK_NA, "already in the queue"
        else:
            check = CHECK_ON if row["checked"] else CHECK_OFF
        return check, truncate(title, self.TITLE_LIMIT), tabscan.site_name(tab.url), browser

    @staticmethod
    def _tags(row: dict) -> tuple:
        if row["queued"]:
            return ("queued",)
        return ("media",) if row["media"] else ("other",)

    def _update_summary(self, media: int, total: int):
        readable = [r for r in self.results if not r.error]
        browsers = list(dict.fromkeys(r.browser for r in readable))
        if not self.results:
            self.summary_var.set("No open browser tabs were found")
            names = ", ".join(n for n, *_ in tabscan.CHROMIUM_BROWSERS + tabscan.FIREFOX_BROWSERS)
            closed = ("" if self.include_closed_var.get() else
                      " Tick 'Include closed browsers' to read the tabs a browser had open "
                      "when it was last closed.")
            self.detail_var.set(f"Supported browsers: {names}.{closed}")
            return
        where = (", ".join(browsers[:-1]) + " and " + browsers[-1]) if len(browsers) > 1 \
            else (browsers[0] if browsers else "your browsers")
        queued = sum(1 for r in self.rows.values() if r["queued"])
        text = f"{media} of {total} open tab{'s' if total != 1 else ''} in {where} " \
               f"{'has' if media == 1 else 'have'} video or audio"
        if queued:
            text += f" ({queued} already queued)"
        self.summary_var.set(text)

        parts = []
        for r in self.results:
            if r.error:
                parts.append(f"{r.source}: could not be read ({r.error})")
                continue
            count = sum(1 for t in r.tabs if tabscan.is_media_url(t.url))
            state = "" if r.running else ", closed"
            parts.append(f"{r.source}: {len(r.tabs)} tabs, {count} media{state}")
        self.detail_var.set("   ·   ".join(parts))

    # -- ticking -------------------------------------------------------------

    def _refresh_row(self, iid: str):
        row = self.rows[iid]
        self.tree.item(iid, values=self._values(row), tags=self._tags(row))

    def _toggle(self, iid: str, value: bool | None = None):
        row = self.rows.get(iid)
        if row is None or row["queued"]:
            return
        row["checked"] = (not row["checked"]) if value is None else value
        self._refresh_row(iid)

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self._toggle(iid)
        self._update_add_button()
        self.tree.focus(iid)
        return "break"

    def _toggle_selection(self):
        for iid in self.tree.selection() or ((self.tree.focus(),) if self.tree.focus() else ()):
            self._toggle(iid)
        self._update_add_button()

    def _set_all(self, value: bool):
        for iid in self.rows:
            self._toggle(iid, value)
        self._update_add_button()

    def _toggle_all(self):
        selectable = [r for r in self.rows.values() if not r["queued"]]
        self._set_all(not all(r["checked"] for r in selectable) if selectable else False)

    def _checked(self) -> list[dict]:
        return [self.rows[iid] for iid in self.tree.get_children()
                if self.rows[iid]["checked"] and not self.rows[iid]["queued"]]

    def _update_add_button(self):
        count = len(self._checked())
        self.add_button.config(text=f"Add {count} to queue" if count else "Add to queue",
                               state=tk.NORMAL if count else tk.DISABLED)

    def _on_show_all(self):
        self.app.cfg["scan_show_all"] = bool(self.show_all_var.get())
        save_config(self.app.cfg)
        self._populate()

    def _on_include_closed(self):
        self.app.cfg["scan_include_closed"] = bool(self.include_closed_var.get())
        save_config(self.app.cfg)
        self.rescan()

    def apply_theme(self):
        super().apply_theme()
        theme = self.app.theme
        self.tree.tag_configure("queued", foreground=theme["muted"])
        self.tree.tag_configure("other", foreground=theme["muted"])
        self.tree.tag_configure("media", foreground=theme["fg"])

    # -- adding --------------------------------------------------------------

    def add_checked(self):
        rows = self._checked()
        if not rows:
            return
        entries = []
        for row in rows:
            tab = row["tab"]
            entries.append((tabscan.canonical_url(tab.url), tabscan.clean_title(tab.title),
                            tabscan.is_playlist_url(tab.url)))
        added, skipped = self.app.queue_entries(
            entries, self.kind_var.get(), self.quality_var.get(), source="browser tabs")
        if added or skipped:
            self.close()


class BatchDialog(_BulkDialog):
    """A text box for a whole list of links, or any text that contains them."""

    def __init__(self, app: YtdlpGui, text: str = ""):
        super().__init__(app, "Add many URLs", (720, 480))
        self.count_var = tk.StringVar(value="")

        ttk.Label(self.body, text="Paste links below - one per line, or any text that "
                                  "contains them. Duplicates and links already in the "
                                  "queue are skipped.",
                  style="Muted.TLabel", wraplength=660, justify=tk.LEFT).pack(anchor=tk.W)

        bottom = ttk.Frame(self.body)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self._build_format_row(bottom).pack(side=tk.LEFT)
        self.add_button = ttk.Button(bottom, text="Add to queue", style="Accent.TButton",
                                     command=self.add)
        self.add_button.pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Close", width=9, command=self.close).pack(side=tk.RIGHT, padx=6)

        info = ttk.Frame(self.body)
        info.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Label(info, textvariable=self.count_var).pack(side=tk.LEFT)
        ttk.Button(info, text="Paste", width=8, command=self.paste).pack(side=tk.RIGHT)
        ttk.Button(info, text="Clear", width=8,
                   command=lambda: self.text.delete("1.0", tk.END)).pack(side=tk.RIGHT, padx=4)

        wrap = ttk.Frame(self.body)
        wrap.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.text = tk.Text(wrap, wrap=tk.NONE, undo=True, font=("Consolas", 10),
                            relief="flat", borderwidth=0, padx=6, pady=4)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Control-Return>", lambda e: (self.add(), "break")[1])

        self.apply_theme()
        if text:
            self.append_text(text)
        self._update_count()
        self.text.focus_set()

    def append_text(self, text: str):
        current = self.text.get("1.0", tk.END).strip()
        self.text.insert(tk.END, ("\n" if current else "") + text.strip() + "\n")
        self.text.see(tk.END)

    def paste(self):
        try:
            self.append_text(self.app.root.clipboard_get())
        except tk.TclError:
            pass

    def _urls(self) -> list[str]:
        return split_urls(self.text.get("1.0", tk.END))

    def _on_modified(self, _event=None):
        self.text.edit_modified(False)
        self._update_count()

    def _update_count(self):
        urls = self._urls()
        known = self.app._queued_keys()
        fresh = len({tabscan.url_key(u) for u in urls} - known)
        if not urls:
            self.count_var.set("No links found yet.")
        elif fresh == len(urls):
            self.count_var.set(f"{len(urls)} link{'s' if len(urls) != 1 else ''} found.")
        else:
            self.count_var.set(f"{len(urls)} links found, {len(urls) - fresh} already in the queue.")
        self.add_button.config(text=f"Add {fresh} to queue" if fresh else "Add to queue",
                               state=tk.NORMAL if fresh else tk.DISABLED)

    def apply_theme(self):
        super().apply_theme()
        theme = self.app.theme
        self.text.configure(bg=theme["field"], fg=theme["field_fg"],
                            insertbackground=theme["field_fg"], selectbackground=theme["sel"])

    def add(self):
        urls = self._urls()
        if not urls:
            return
        entries = [(tabscan.canonical_url(u), "", tabscan.is_playlist_url(u)) for u in urls]
        added, skipped = self.app.queue_entries(
            entries, self.kind_var.get(), self.quality_var.get(), source="the list")
        if added or skipped:
            self.close()


# --------------------------------------------------------------------------
# Module level helpers used by the GUI
# --------------------------------------------------------------------------

def status_tag(status: str) -> str:
    return {
        STATUS_QUEUED: "queued", STATUS_RUNNING: "running", STATUS_DONE: "done",
        STATUS_FAILED: "failed", STATUS_CANCELLED: "cancelled", STATUS_SKIPPED: "skipped",
        STATUS_PAUSED: "paused",
    }.get(status, "queued")


def action_glyph(status: str) -> str:
    """The play/pause control shown in each queue row."""
    if status == STATUS_RUNNING:
        return GLYPH_PAUSE
    if status in STARTABLE_STATES:
        return GLYPH_PLAY
    return ""


def strip_size_note(label: str) -> str:
    """'1080p   (~120.5 MB)' -> '1080p'."""
    return re.sub(r"\s{2,}\(~.*\)$", "", label or "").strip()


def parse_height(label: str) -> int | None:
    match = re.match(r"\s*(\d{3,4})p", label or "")
    return int(match.group(1)) if match else None


def audio_quality_arg(label: str) -> str:
    match = re.match(r"\s*(\d+)\s*kbps", label or "", re.IGNORECASE)
    return f"{match.group(1)}K" if match else "0"


def quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg and not arg.startswith('"') else arg


def pick_thumbnail(info: dict) -> str | None:
    best = None
    best_width = -1
    for thumb in info.get("thumbnails") or []:
        url = thumb.get("url")
        if not url:
            continue
        width = thumb.get("width") or 0
        if 120 <= width <= 800 and width > best_width:
            best, best_width = url, width
    return best or info.get("thumbnail")


def version_age_days(version: str) -> int | None:
    match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version or "")
    if not match:
        return None
    try:
        released = time.mktime((int(match.group(1)), int(match.group(2)),
                                int(match.group(3)), 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return None
    return int((time.time() - released) / 86400)


def main():
    APP_DATA.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    YtdlpGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
