"""
Receive the open tabs from the companion browser extension.

Chrome, Brave and Edge hold their live session file exclusively, so on Windows
no other program can read it while the browser runs (not even as
administrator). The extension in `browser_extension/` asks the browser for its
tabs instead and posts them here, to a listener on 127.0.0.1 that only this
computer can reach. Each post is a full snapshot of one browser profile; it is
sent on every tab change and every 30 seconds.

Only posts that come from a browser extension are accepted: a web page cannot
fake the Origin header, and its JSON post would need a CORS preflight this
listener never grants. That stops a web site from planting URLs in the queue.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 47813
MAX_BODY = 8 * 1024 * 1024
# The extension reports every 30 seconds; older than this, it has stopped (the
# profile was closed, or the extension removed).
FRESH_SECONDS = 75
EXTENSION_ORIGINS = ("chrome-extension://",)


@dataclass
class Snapshot:
    browser: str                     # "Chrome", "Brave", ...
    instance: str                    # random id of one browser profile
    tabs: list[tuple[str, str]]      # (url, title) in window and tab order
    received: float                  # time.monotonic()


def parse_report(body: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """(browser, instance, tabs) from the extension's JSON; ValueError if malformed."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("not an object")
    browser, instance, tabs = data.get("browser"), data.get("instance"), data.get("tabs")
    if not isinstance(browser, str) or not browser or len(browser) > 40:
        raise ValueError("bad browser")
    if not isinstance(instance, str) or not instance or len(instance) > 100:
        raise ValueError("bad instance")
    if not isinstance(tabs, list):
        raise ValueError("bad tabs")
    out = []
    for tab in tabs[:5000]:
        if not isinstance(tab, dict):
            continue
        url, title = tab.get("url"), tab.get("title")
        if isinstance(url, str) and url:
            out.append((url[:8000], title[:1000] if isinstance(title, str) else ""))
    return browser, instance, out


class _Server(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second program bind
    # the same port and take reports meant for this one. Insist on the port alone.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class TabBridge:
    def __init__(self, port: int = PORT):
        self.port = port
        self.error = ""
        self.started = time.monotonic()
        self._snapshots: dict[tuple[str, str], Snapshot] = {}
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._server: ThreadingHTTPServer | None = None

    # -- listener ------------------------------------------------------------

    def start(self) -> bool:
        """Listen in a background thread. False (and `error` set) if the port is taken."""
        bridge = self
        hosts = (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

        class Handler(BaseHTTPRequestHandler):
            server_version = "yt-dlp-gui"

            def log_message(self, *args):
                pass

            def _origin_ok(self) -> bool:
                return (self.headers.get("Origin") or "").startswith(EXTENSION_ORIGINS)

            def _reply(self, code: int, body: bytes = b""):
                self.send_response(code)
                if self._origin_ok():
                    self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                    self.send_header("Access-Control-Allow-Methods", "POST")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                if self.path == "/ping":
                    self._reply(200, b"yt-dlp-gui")
                else:
                    self._reply(404)

            def do_OPTIONS(self):
                self._reply(204 if self._origin_ok() else 403)

            def do_POST(self):
                if self.path != "/tabs":
                    return self._reply(404)
                # Host: guards against DNS rebinding; Origin: only an extension.
                if self.headers.get("Host") not in hosts or not self._origin_ok():
                    return self._reply(403)
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._reply(415)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(411)
                if not 0 < length <= MAX_BODY:
                    return self._reply(413)
                try:
                    browser, instance, tabs = parse_report(self.rfile.read(length))
                except ValueError:
                    return self._reply(400)
                bridge.store(browser, instance, tabs)
                self._reply(204)

        try:
            self._server = _Server(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            self.error = (f"port {self.port} is in use - is the app already open?"
                          if getattr(exc, "winerror", None) == 10048 or exc.errno in (98, 48, 10048)
                          else (exc.strerror or str(exc)))
            return False
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    # -- snapshots -------------------------------------------------------------

    def store(self, browser: str, instance: str, tabs: list[tuple[str, str]]):
        with self._arrived:
            self._snapshots[(browser, instance)] = Snapshot(browser, instance, tabs,
                                                            time.monotonic())
            self._arrived.notify_all()

    def snapshots(self, max_age: float = FRESH_SECONDS) -> list[Snapshot]:
        now = time.monotonic()
        with self._lock:
            return [s for s in self._snapshots.values() if now - s.received <= max_age]

    def browsers(self) -> set[str]:
        return {s.browser for s in self.snapshots()}

    def wait_for(self, browsers: set[str], until: float) -> set[str]:
        """
        Wait until each of `browsers` has reported, or time.monotonic() passes
        `until`. Returns the browsers still missing.
        """
        with self._arrived:
            while True:
                now = time.monotonic()
                missing = browsers - {s.browser for s in self._snapshots.values()
                                      if now - s.received <= FRESH_SECONDS}
                if not missing or now >= until:
                    return missing
                self._arrived.wait(min(1.0, until - now))
