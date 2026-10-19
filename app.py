#!/usr/bin/env python3
"""
Transcript Grabber - local web UI.

Run:  python app.py        (opens http://127.0.0.1:8765 in your browser)
      python app.py --port 9000 --no-browser

The server only listens on your own computer (127.0.0.1); nothing is exposed
to your network. Uses only the Python standard library plus the two packages
in requirements.txt.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tg_core import DEFAULT_OUTPUT, FORMATS, Options, Runner, list_videos

STATIC = Path(__file__).resolve().parent / "static"


class Job:
    """One run at a time; the UI polls /api/status.

    Each run gets its own stop Event and a generation number. Cancelling sets
    the Event (the Runner checks it between videos, during waits, and after
    every request) and bumps the generation, so a thread that is still
    finishing a slow request can never overwrite the state of a newer run.
    """

    ACTIVE = ("listing", "running", "cancelling")

    def __init__(self):
        self.lock = threading.Lock()
        self.gen = 0
        self._clear()

    def _clear(self):
        self.stage = "idle"     # idle | listing | running | cancelling | done | cancelled | error
        self.error = ""
        self.collection = None
        self.runner: Runner | None = None
        self.result = {}
        self.stop_event = threading.Event()

    def busy(self) -> bool:
        return self.stage in self.ACTIVE

    def start(self, url: str, opts: Options):
        with self.lock:
            self.gen += 1
            self._clear()
            self.stage = "listing"
            gen, stop = self.gen, self.stop_event
        threading.Thread(target=self._run, args=(gen, stop, url, opts), daemon=True).start()

    def _current(self, gen) -> bool:
        return gen == self.gen

    def _run(self, gen, stop, url, opts):
        try:
            col = list_videos(url)
            if stop.is_set() or not self._current(gen):
                return                                  # cancelled while reading the playlist
            if not col.videos:
                raise ValueError("No videos found at that URL.")
            runner = Runner(col, opts, should_stop=stop.is_set)
            with self.lock:
                if not self._current(gen):
                    return
                self.collection, self.runner, self.stage = col, runner, "running"
            result = runner.run()
            with self.lock:
                if self._current(gen):
                    self.result = result
                    self.stage = "cancelled" if stop.is_set() else "done"
        except Exception as e:
            with self.lock:
                if self._current(gen) and not stop.is_set():
                    self.error, self.stage = _friendly(e), "error"

    def cancel(self):
        with self.lock:
            if not self.busy():
                return
            self.stop_event.set()
            if self.stage == "listing":
                # Nothing fetched yet: finish right away and ignore the lookup when it returns.
                self.gen += 1
                self.stage = "cancelled"
            else:
                self.stage = "cancelling"   # the runner stops within a moment

    def retry(self, index: int):
        v = next((v for v in self.collection.videos if v.index == index), None) if self.collection else None
        if not v or not self.runner:
            raise ValueError("Unknown video")
        with self.lock:
            self.stop_event = threading.Event()
            self.runner.should_stop = self.stop_event.is_set
            self.stage = "running"
            gen, stop = self.gen, self.stop_event

        def work():
            self.runner.process(v)
            combined = self.runner._write_combined() if self.runner.opts.combine else self.result.get("combined_file", "")
            with self.lock:
                if self._current(gen):
                    self.result = self.runner.summary(combined)
                    self.stage = "cancelled" if stop.is_set() else "done"

        threading.Thread(target=work, daemon=True).start()

    def status(self) -> dict:
        with self.lock:
            vids = [v.to_dict() for v in self.collection.videos] if self.collection else []
            finished = sum(v["status"] not in ("queued", "fetching", "cancelled") for v in vids)
            return {
                "stage": self.stage, "error": self.error,
                "title": self.collection.title if self.collection else "",
                "folder": str(self.runner.folder) if self.runner else "",
                "combined_file": self.result.get("combined_file", ""),
                "total": len(vids), "finished": finished, "videos": vids,
            }


def _friendly(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if "Unsupported URL" in msg or "is not a valid URL" in msg:
        return "That doesn't look like a video, playlist, or channel URL."
    return msg[:300]


JOB = Job()


def open_path(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Handler(BaseHTTPRequestHandler):
    server_version = "TranscriptGrabber/1.0"

    def log_message(self, *args):   # keep the console quiet
        pass

    # ---- helpers
    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self) -> bool:
        # Block other websites from driving this local server from your browser.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/config":
            self._json({"output_dir": DEFAULT_OUTPUT, "formats": list(FORMATS)})
        elif u.path == "/api/status":
            self._json(JOB.status())
        elif u.path == "/api/preview":
            idx = int(parse_qs(u.query).get("index", ["0"])[0])
            self._json({"text": self._preview(idx)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        path = urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/start":
                if JOB.busy():
                    return self._json({"error": "A job is already running."}, 409)
                url = (data.get("url") or "").strip()
                if not url:
                    return self._json({"error": "Paste a video or playlist URL first."}, 400)
                fmt = data.get("format", "txt")
                opts = Options(language=data.get("language", "en"),
                               timestamps=bool(data.get("timestamps", True)),
                               combine=bool(data.get("combine", False)),
                               fmt=fmt if fmt in FORMATS else "txt",
                               output_dir=(data.get("output_dir") or DEFAULT_OUTPUT).strip(),
                               overwrite=bool(data.get("overwrite", False)))
                JOB.start(url, opts)
                return self._json({"ok": True})
            if path in ("/api/cancel", "/api/stop"):
                JOB.cancel()
                return self._json({"ok": True, "stage": JOB.stage})
            if path == "/api/retry":
                if JOB.busy():
                    return self._json({"error": "Wait for the current run to finish."}, 409)
                JOB.retry(int(data["index"]))
                return self._json({"ok": True})
            if path == "/api/open":
                return self._open(data)
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": _friendly(e)}, 400)

    def _preview(self, idx: int) -> str:
        if not JOB.collection or not JOB.runner:
            return ""
        v = next((v for v in JOB.collection.videos if v.index == idx), None)
        if not v:
            return ""
        text = JOB.runner.rendered.get(v.video_id)
        if text is None and v.file and Path(v.file).exists():
            text = Path(v.file).read_text(encoding="utf-8", errors="replace")
        return (text or "")[:20000]

    def _open(self, data):
        """Open the output folder or a saved transcript - only paths this app created."""
        target = data.get("path") or ""
        allowed = set()
        if JOB.runner:
            allowed.add(str(JOB.runner.folder))
            allowed.update(v.file for v in JOB.collection.videos if v.file)
        if not target:
            folder = Path(os.path.expanduser(data.get("folder") or DEFAULT_OUTPUT))
            folder.mkdir(parents=True, exist_ok=True)
            target = str(folder)
            allowed.add(target)
        if target not in allowed or not Path(target).exists():
            return self._json({"error": "Nothing to open yet."}, 400)
        open_path(target)
        self._json({"ok": True})


def main():
    p = argparse.ArgumentParser(description="Transcript Grabber local web UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Transcript Grabber is running at {url}\nClose this window (or press Ctrl+C) to quit.")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
