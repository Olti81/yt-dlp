"""Tests for the listener that receives tabs from the companion browser extension."""

import json
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tabbridge  # noqa: E402
import tabscan  # noqa: E402

EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def report(browser="Brave", instance="one", tabs=None) -> bytes:
    return json.dumps({"version": 1, "browser": browser, "instance": instance,
                       "tabs": tabs if tabs is not None else [
                           {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "A"},
                           {"url": "https://example.com/", "title": "B"}]}).encode()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = tabbridge.TabBridge(free_port())
        self.assertTrue(self.bridge.start(), self.bridge.error)
        self.addCleanup(self.bridge.stop)

    def post(self, body: bytes, origin: str | None = EXTENSION,
             content_type="application/json", host=None) -> int:
        url = f"http://127.0.0.1:{self.bridge.port}/tabs"
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        if origin:
            request.add_header("Origin", origin)
        if host:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_extension_report_is_stored(self):
        self.assertEqual(self.post(report()), 204)
        (snap,) = self.bridge.snapshots()
        self.assertEqual((snap.browser, snap.instance), ("Brave", "one"))
        self.assertEqual(snap.tabs[0], ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "A"))

    def test_newer_report_replaces_older(self):
        self.post(report(tabs=[{"url": "https://a.example/", "title": ""}]))
        self.post(report(tabs=[{"url": "https://b.example/", "title": ""}]))
        self.post(report(browser="Chrome"))
        snaps = {s.browser: s for s in self.bridge.snapshots()}
        self.assertEqual(snaps["Brave"].tabs, [("https://b.example/", "")])
        self.assertEqual(set(snaps), {"Brave", "Chrome"})

    def test_web_pages_are_refused(self):
        self.assertEqual(self.post(report(), origin="https://evil.example"), 403)
        self.assertEqual(self.post(report(), origin=None), 403)
        self.assertEqual(self.post(report(), content_type="text/plain"), 415)
        self.assertEqual(self.post(report(), host="evil.example"), 403)
        self.assertEqual(self.post(b"{not json"), 400)
        self.assertEqual(self.post(json.dumps({"browser": "x"}).encode()), 400)
        self.assertEqual(self.bridge.snapshots(), [])

    def test_stale_reports_are_dropped(self):
        self.post(report())
        self.assertEqual(self.bridge.snapshots(max_age=-1), [])

    def test_wait_for(self):
        start = time.monotonic()
        self.assertEqual(self.bridge.wait_for({"Brave"}, start + 0.3), {"Brave"})
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        self.post(report())
        self.assertEqual(self.bridge.wait_for({"Brave"}, time.monotonic() + 5), set())

    def test_second_listener_on_same_port_fails_cleanly(self):
        other = tabbridge.TabBridge(self.bridge.port)
        self.assertFalse(other.start())
        self.assertTrue(other.error)


class LiveScanTests(unittest.TestCase):
    def test_extension_tabs_replace_locked_session(self):
        snap = tabbridge.Snapshot("Brave", "one", [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Video - YouTube"),
            ("chrome://newtab/", "New Tab")], time.monotonic())
        with tempfile.TemporaryDirectory() as tmp:
            browsers = [("Brave", ("brave",), [Path(tmp)])]
            with mock.patch.object(tabscan, "running_process_names", return_value={"brave"}), \
                    mock.patch.object(tabscan, "read_shared",
                                      side_effect=AssertionError("must not read files")):
                results = tabscan.scan_browsers(chromium=browsers, firefox=[], live=[snap])
        (result,) = results
        self.assertTrue(result.via_extension)
        self.assertEqual([t.url for t in result.tabs],
                         ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])

    def test_report_from_closed_browser_is_ignored(self):
        snap = tabbridge.Snapshot("Brave", "one", [("https://youtu.be/dQw4w9WgXcQ", "")],
                                  time.monotonic())
        with mock.patch.object(tabscan, "running_process_names", return_value=set()):
            results = tabscan.scan_browsers(chromium=[("Brave", ("brave",), [])],
                                            firefox=[], live=[snap])
        self.assertEqual(results, [])

    def test_two_profiles(self):
        snaps = [tabbridge.Snapshot("Chrome", i, [("https://youtu.be/dQw4w9WgXcQ", "")],
                                    time.monotonic()) for i in ("a", "b")]
        with mock.patch.object(tabscan, "running_process_names", return_value={"chrome"}):
            results = tabscan.scan_browsers(chromium=[("Chrome", ("chrome",), [])],
                                            firefox=[], live=snaps)
        self.assertEqual([r.source for r in results],
                         ["Chrome (profile 1)", "Chrome (profile 2)"])


if __name__ == "__main__":
    unittest.main()
