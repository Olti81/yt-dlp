<img src="assets/icon.png" width="72" align="right" alt="">

# yt-dlp Downloader

A Windows desktop front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp): paste a URL, see
what the video actually offers, pick a quality, and let a queue download everything one item
at a time — or press **Scan browser tabs** and queue every video you have open in one click. Built with tkinter, packaged as a single `.exe` with yt-dlp and ffmpeg bundled
inside — nothing to install.

![The app in light mode](docs/screenshot-light.png)

<details>
<summary><b>Dark mode</b> — switchable in Settings</summary>

![The app in dark mode](docs/screenshot-dark.png)

</details>

## What it does

**Scan your browser tabs.** Got a dozen videos open in tabs? Press **Scan browser tabs**
(Ctrl+T) instead of copying links one by one. The app reads the open tabs of Chrome, Brave,
Edge, Vivaldi, Opera, Chromium, Firefox, LibreWolf, Waterfox, Zen and Floorp — every profile
— and lists the ones with video or audio: a truncated title, the site and the browser, all
ticked. One click on **Add N to queue** queues the lot, in whatever format and quality you
pick in that window. Tabs already in the queue are marked and skipped, the same page open in
two browsers is listed once, and *Show every tab* lets you tick pages the app does not
recognise so yt-dlp can try them anyway. No browser extension and no setup: browsers keep
their open tabs on disk for crash recovery, and that is what is read. A tab opened in the
last few seconds may not have been written yet — press **Rescan**.

**Inspect before you download.** Paste a URL and the media info is fetched automatically:
title, uploader, duration, thumbnail, and the resolutions that genuinely exist for that video
— each with an estimated file size. A 4K upload offers 2160p down to 144p; a 720p one only
offers what it has, instead of a fixed list that silently falls back.

**A queue that keeps working.** Add items while a download is running and they start
automatically, in order, as soon as the current one finishes. Reorder, remove, retry failed
items, or cancel the running download. The queue survives restarts.

**Play/pause on every row.** One download runs at a time by default, but the control in the
first column of each row overrides that: press play on a queued item to run it alongside the
current one, or pause a running one. Pausing stops yt-dlp and keeps the partial file, so
pressing play again continues from where it stopped instead of starting over. Raise
*Downloads at the same time* in Settings if you would rather several always run at once.

**Playlists and batches.** A playlist URL can be expanded into one queue entry per video, or
kept as a single entry. **Add many...** (Ctrl+M) opens a box for a whole list of links — one
per line, or any text that contains them, such as a chat message — and pasting several links
anywhere opens it for you. Duplicates and links already in the queue are skipped; titles of
items queued this way are filled in as soon as their download starts.

**Live progress.** Per-item and whole-queue progress bars with real speed, ETA and size,
read from yt-dlp's structured progress output rather than scraped from its console text.
With several downloads at once the top bar shows their average and combined throughput. The
window title shows the current percentage, so it is readable from the taskbar.

**Formats.** MP4 / MKV / WEBM video, or MP3 / M4A / WAV / FLAC / Opus audio with a selectable
bitrate. Optional subtitles (embedded or as `.srt`), embedded thumbnails and metadata, and
SponsorBlock segment removal.

**Quality-of-life.** Light and dark themes, clipboard watching, keyboard shortcuts, a
right-click menu on the queue (move to top or bottom, open the page in your browser, or switch
selected items to the current format and quality — handy for turning a batch into MP3s),
duplicate detection, a beep and taskbar flash when the queue is finished, filename templates,
per-playlist subfolders, a speed limit, a download archive to skip files you already have,
and cookie import from your browser for sites that need a login. Removing a running item
stops it and takes it off the queue in one go.

## Getting started

Run `yt-dlp-gui-2.1.exe`. There is no installer and no dependency to set up.

On first launch it copies its bundled `yt-dlp.exe` into `%LOCALAPPDATA%\yt-dlp-gui\bin` and
runs it from there. That is deliberate: it means **Tools → Update yt-dlp** can replace the
binary in place, so you can fix a broken site without rebuilding the app.

Everything the app writes lives in `%LOCALAPPDATA%\yt-dlp-gui`:

| File | Purpose |
| --- | --- |
| `config.json` | Settings and window geometry |
| `queue.json` | The queue, restored on next launch |
| `archive.txt` | Download archive (only when enabled) |
| `bin\yt-dlp.exe` | The updatable yt-dlp copy |
| `thumbs\` | Cached preview thumbnails |

## Running from source

Requires Python 3.13. The app itself uses only the standard library — tkinter, `subprocess`,
`json` — so there is nothing to `pip install` to run it.

```bash
python main.py
```

`resources/` is **not** in this repository (it holds ~190 MB of binaries). To run or build
from source, create it and add:

- `yt-dlp.exe` — from [yt-dlp releases](https://github.com/yt-dlp/yt-dlp/releases)
- `ffmpeg.exe`, `ffprobe.exe` and their `av*.dll` / `sw*.dll` companions — from any Windows
  ffmpeg build

If `resources/` is missing, the app falls back to `yt-dlp` and `ffmpeg` on your `PATH`.

The browser tab scanner lives in `tabscan.py` and has tests, which use recorded Chromium and
Firefox session files from `tests/data`:

```bash
python -m unittest discover tests
```

## Building the .exe

On Windows, double-click **`build.cmd`**. It pulls the latest version with `git pull`, checks
that `resources\` is in place, sets up PyInstaller in a private `.venv`, runs the tests and
builds. To build local changes without pulling, run `set YTDLP_NO_PULL=1` first. Or do it by
hand:

```bash
pip install pyinstaller
pyinstaller main.spec --noconfirm
```

The result is `dist/yt-dlp-gui-2.1.exe`, a single file of roughly 150 MB — most of which is
the bundled ffmpeg.

The icon is generated rather than drawn by hand, so there is no binary to edit: the skull is
defined as a handful of superellipses in `tools/make_icon.py`, which rasterises it by
supersampling and writes `assets/icon.ico` (eight sizes, 16–256 px) plus `assets/icon.png`.
Adjust the shape constants and re-run it:

```bash
python tools/make_icon.py
```

## Notes

**How the tab scan works.** Chromium-based browsers keep an append-only log of tab changes in
`<profile>\Sessions\Session_*` (the "SNSS" format); replaying it gives the tabs that are open
now. Chromium writes a tab's title there some time after the page loads, so missing titles are
looked up in a copy of the profile's history. Firefox and its forks write
`sessionstore-backups\recovery.jsonlz4`, LZ4-compressed JSON. Both are read with the standard
library only. Browsers that are not running are left out unless you tick *Include closed
browsers*, since their files describe the tabs from the last time they ran. If a running
browser keeps its session file locked, the scan lists the video pages visited in that
browser over the last 24 hours instead, unticked and marked *history*.

**Keep yt-dlp current.** Sites change constantly and a stale yt-dlp is the most common cause
of a download that suddenly stops working. The app shows the version and its age in
**Settings**, and warns in the log once it is over 90 days old.

**YouTube wants a JavaScript runtime.** yt-dlp has deprecated YouTube extraction without one,
and some formats may be missing until you install [Deno](https://deno.com/) and put it on
your `PATH`. The app checks at startup and warns if none is found.

**Impersonation.** Requests are sent as Chrome by default, which helps with sites that block
plain HTTP clients. If that is rejected, the download is retried once without it
automatically. It can be turned off in **Settings**.

**How pause really works.** There is no way to freeze a running download and hold the
connection open, so pause stops yt-dlp and leaves the `.part` file in place; play restarts
yt-dlp, which continues from that file. For ordinary progressive downloads this resumes to
the byte. For fragmented streams (HLS/DASH) it resumes at the last completed fragment, so a
little of the current fragment is re-fetched. A few sites issue single-use URLs and will
restart the file instead — rare, but worth knowing.
