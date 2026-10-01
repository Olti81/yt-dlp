"""Tests for the browser tab scanner. Run with:  python -m unittest discover tests"""

import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tabscan  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def snss(*commands: tuple[int, bytes]) -> bytes:
    out = b"SNSS" + struct.pack("<i", 1)
    for cmd, payload in commands:
        out += struct.pack("<H", len(payload) + 1) + bytes([cmd]) + payload
    return out


def pad(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def nav(tab_id: int, index: int, url: str, title: str) -> tuple[int, bytes]:
    body = struct.pack("<ii", tab_id, index)
    body += struct.pack("<i", len(url)) + pad(url.encode())
    body += struct.pack("<i", len(title)) + pad(title.encode("utf-16-le"))
    return 6, struct.pack("<I", len(body)) + body


def ints(cmd: int, *values: int) -> tuple[int, bytes]:
    return cmd, struct.pack(f"<{len(values)}i", *values)


class ChromiumSessionTests(unittest.TestCase):
    def test_real_session_file(self):
        # Recorded from Chromium: five tabs opened, one closed again, one
        # navigated onwards to a second page in the same tab.
        tabs = tabscan.parse_snss((DATA / "chromium_session.snss").read_bytes())
        urls = [u for u, _ in tabs]
        self.assertEqual(urls, ["http://127.0.0.1:8765/first",
                                "http://127.0.0.1:8765/watch?v=one",
                                "http://127.0.0.1:8765/second-b",
                                "http://127.0.0.1:8765/fourth"])

    def test_order_selection_and_closing(self):
        data = snss(
            ints(0, 1, 10), ints(2, 10, 1), nav(10, 0, "https://a.example/", "A"),
            ints(0, 1, 11), ints(2, 11, 0), nav(11, 0, "https://b.example/", "B"),
            nav(11, 1, "https://b.example/2", "B2"), ints(7, 11, 0),
            ints(0, 2, 12), nav(12, 0, "https://gone.example/", "closed window"),
            ints(0, 1, 13), nav(13, 0, "https://gone.example/tab", "closed tab"),
            (16, struct.pack("<iiq", 13, 0, 0)),
            (17, struct.pack("<iiq", 2, 0, 0)),
        )
        self.assertEqual(tabscan.parse_snss(data),
                         [("https://b.example/", "B"), ("https://a.example/", "A")])

    def test_pruned_navigation(self):
        data = snss(ints(0, 1, 5), nav(5, 0, "https://x/0", ""), nav(5, 1, "https://x/1", ""),
                    nav(5, 2, "https://x/2", ""), ints(7, 5, 2), ints(11, 5, 1))
        # Pruning one entry from the front moves the selected index with it.
        self.assertEqual(tabscan.parse_snss(data), [("https://x/2", "")])

    def test_truncated_file_keeps_what_was_complete(self):
        data = snss(ints(0, 1, 1), nav(1, 0, "https://ok.example/", "ok"))
        self.assertEqual(tabscan.parse_snss(data + b"\x40\x00\x06abc"),
                         [("https://ok.example/", "ok")])

    def test_rejects_other_files(self):
        with self.assertRaises(ValueError):
            tabscan.parse_snss(b"not a session")

    def test_scan_reads_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "User Data" / "Profile 2" / "Sessions"
            sessions.mkdir(parents=True)
            shutil.copy(DATA / "chromium_session.snss", sessions / "Session_1")
            (Path(tmp) / "User Data" / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Profile 2": {"name": "Work"}}}}))
            results = tabscan.scan_browsers(
                include_closed=True, firefox=[],
                chromium=[("Brave", ("no-such-process",), [Path(tmp) / "User Data"])])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].browser, "Brave")
        self.assertEqual(len(results[0].tabs), 4)

    def test_closed_browsers_are_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "Default" / "Sessions"
            sessions.mkdir(parents=True)
            shutil.copy(DATA / "chromium_session.snss", sessions / "Session_1")
            results = tabscan.scan_browsers(
                firefox=[], chromium=[("Edge", ("no-such-process",), [Path(tmp)])])
        if tabscan.running_process_names() is not None:
            self.assertEqual(results, [])


class FirefoxSessionTests(unittest.TestCase):
    def test_lz4_session(self):
        raw = tabscan.read_mozlz4((DATA / "firefox_recovery.jsonlz4").read_bytes())
        tabs = tabscan.parse_firefox_session(json.loads(raw))
        self.assertEqual([u for u, _ in tabs], [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4",
            "about:newtab",
            "https://soundcloud.com/artist/track-name",
            "https://vimeo.com/76979871",
        ])

    def test_lz4_overlapping_match(self):
        # literal 'ab', a match of length 8 at offset 2, then a final literal '!'
        block = bytes([0x24]) + b"ab" + struct.pack("<H", 2) + bytes([0x10]) + b"!"
        self.assertEqual(tabscan.lz4_block_decompress(block), b"ababababab!")

    def test_scan_firefox_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            backups = Path(tmp) / "Profiles" / "abcd.default-release" / "sessionstore-backups"
            backups.mkdir(parents=True)
            shutil.copy(DATA / "firefox_recovery.jsonlz4", backups / "recovery.jsonlz4")
            results = tabscan.scan_browsers(
                include_closed=True, chromium=[],
                firefox=[("Firefox", ("no-such-process",), [Path(tmp) / "Profiles"])])
        self.assertEqual(len(results), 1)
        # about: pages are dropped, the rest are kept
        self.assertEqual(len(results[0].tabs), 3)


class MediaDetectionTests(unittest.TestCase):
    def test_media_pages(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ",
                    "https://m.youtube.com/shorts/dQw4w9WgXcQ",
                    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/playlist?list=PL123",
                    "https://vimeo.com/76979871",
                    "https://soundcloud.com/artist/track-name",
                    "https://artist.bandcamp.com/album/name",
                    "https://www.tiktok.com/@user/video/123",
                    "https://www.twitch.tv/videos/123456",
                    "https://tv.nrk.no/serie/some-show",
                    "https://example.com/files/clip.mp4"):
            self.assertTrue(tabscan.is_media_url(url), url)

    def test_other_pages(self):
        for url in ("https://www.youtube.com/",
                    "https://www.youtube.com/feed/subscriptions",
                    "https://soundcloud.com/discover/sets",
                    "https://vimeo.com/",
                    "https://github.com/yt-dlp/yt-dlp",
                    "https://mail.google.com/mail/u/0/",
                    "about:blank"):
            self.assertFalse(tabscan.is_media_url(url), url)

    def test_canonical_url_and_key(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4&t=30s"
        self.assertEqual(tabscan.canonical_url(url), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(tabscan.url_key(url), tabscan.url_key("https://youtu.be/dQw4w9WgXcQ"))
        self.assertEqual(tabscan.url_key("https://vimeo.com/1/"), tabscan.url_key("https://vimeo.com/1"))

    def test_clean_title(self):
        self.assertEqual(tabscan.clean_title("(12) Some video - YouTube"), "Some video")
        self.assertEqual(tabscan.clean_title("Stream Track by Artist | Listen online for free on SoundCloud"),
                         "Stream Track by Artist")
        self.assertEqual(tabscan.clean_title("The New Vimeo Player on Vimeo"), "The New Vimeo Player")


if __name__ == "__main__":
    unittest.main()
