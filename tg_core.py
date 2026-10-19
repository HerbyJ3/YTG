"""
Transcript Grabber - core logic.

Expands a YouTube video, playlist, or channel URL into a list of videos (yt-dlp),
fetches each video's transcript (youtube-transcript-api), and saves it as
TXT, Markdown, or JSON. Used by both the command line (transcript_grabber.py)
and the local web UI (app.py).
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

DEFAULT_OUTPUT = str(Path.home() / "Transcript Grabber")
DEFAULT_LOG_DIR = str(Path.home() / "Transcript Grabber" / "Failure Logs")
APP_VERSION = "1.1.0"
FORMATS = {"txt": ".txt", "md": ".md", "json": ".json"}

# ----------------------------------------------------------------- data types


@dataclass
class Video:
    index: int
    video_id: str
    title: str
    duration: Optional[int] = None          # seconds
    status: str = "queued"                  # queued | fetching | saved | skipped | no_captions | error | cancelled
    message: str = ""
    file: str = ""
    language: str = ""
    detail: str = ""                        # full error text, for the failure log
    attempts: int = 0

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def thumbnail(self) -> str:
        return f"https://i.ytimg.com/vi/{self.video_id}/mqdefault.jpg"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(url=self.url, thumbnail=self.thumbnail, duration_text=fmt_duration(self.duration))
        return d


@dataclass
class Options:
    language: str = "en"            # language code, or "any"
    timestamps: bool = True
    combine: bool = False
    fmt: str = "txt"                # txt | md | json
    output_dir: str = DEFAULT_OUTPUT
    delay: float = 1.5              # seconds between videos (be gentle with YouTube)
    retries: int = 3
    overwrite: bool = False
    log_dir: str = DEFAULT_LOG_DIR  # where failure logs go


@dataclass
class Segment:
    start: float
    duration: float
    text: str


@dataclass
class Collection:
    title: str
    videos: list = field(default_factory=list)
    is_playlist: bool = False


# ----------------------------------------------------------------- helpers

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def extract_video_id(url: str) -> Optional[str]:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def is_playlist_url(url: str) -> bool:
    return ("list=" in url) or ("/playlist" in url) or bool(re.search(r"/(@[^/]+|channel/|c/|user/)", url))


def safe_name(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return (name[:max_len].rstrip() or "untitled")


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def clean_text(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ----------------------------------------------------------------- listing videos


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def list_videos(url: str) -> Collection:
    """Turn any URL into a Collection of Video objects. Playlists/channels use yt-dlp."""
    vid = extract_video_id(url)
    if vid and not is_playlist_url(url):
        return _single_video(vid)

    import yt_dlp  # imported lazily so the module loads without it for tests

    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True, "skip_download": True}
    # A watch URL that also has &list= : prefer the playlist
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = list(_flatten(info.get("entries") or []))
    if not entries and info.get("id") and info.get("_type") != "playlist":
        return Collection(title=info.get("title") or info["id"],
                          videos=[Video(1, info["id"], info.get("title") or info["id"], info.get("duration"))])

    videos = []
    for e in entries:
        if not e or not e.get("id") or len(e["id"]) != 11:
            continue
        title = e.get("title") or e["id"]
        if title in ("[Private video]", "[Deleted video]"):
            continue
        videos.append(Video(len(videos) + 1, e["id"], title, e.get("duration")))
    return Collection(title=info.get("title") or "Playlist", videos=videos, is_playlist=True)


def _flatten(entries):
    """Channel URLs nest tabs (Videos, Shorts...) as sub-playlists; flatten them."""
    for e in entries:
        if e and e.get("_type") == "playlist" and e.get("entries"):
            yield from _flatten(e["entries"])
        else:
            yield e


def _single_video(vid: str) -> Collection:
    title, duration = vid, None
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "logger": _QuietLogger()}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
            title, duration = info.get("title") or vid, info.get("duration")
    except Exception:
        pass  # title lookup is cosmetic; the transcript fetch is what matters
    return Collection(title=title, videos=[Video(1, vid, title, duration)])


# ----------------------------------------------------------------- fetching transcripts


class NoCaptions(Exception):
    pass


class Blocked(Exception):
    pass


def fetch_transcript(video_id: str, language: str = "en") -> tuple[list[Segment], str]:
    """Return (segments, language_code). Raises NoCaptions or Blocked."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as E

    api = YouTubeTranscriptApi()
    blocked = tuple(getattr(E, n) for n in ("RequestBlocked", "IpBlocked", "TooManyRequests") if hasattr(E, n))
    none = tuple(getattr(E, n) for n in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable",
                                          "VideoUnplayable", "AgeRestricted", "InvalidVideoId",
                                          "NotTranslatable", "TranslationLanguageNotAvailable") if hasattr(E, n))
    try:
        tlist = api.list(video_id)
        if language == "any":
            transcript = next(iter(tlist))
        else:
            try:
                transcript = tlist.find_transcript([language])
            except E.NoTranscriptFound:
                # Fall back: translate any available transcript into the requested language
                first = next(iter(tlist))
                if first.is_translatable:
                    transcript = first.translate(language)
                else:
                    raise
        fetched = transcript.fetch()
    except blocked as e:
        raise Blocked(str(e).splitlines()[0] if str(e) else "YouTube is blocking requests") from e
    except none as e:
        raise NoCaptions(type(e).__name__) from e
    except StopIteration as e:
        raise NoCaptions("No transcripts available") from e

    segs = [Segment(float(s.start), float(s.duration), clean_text(s.text)) for s in fetched]
    segs = [s for s in segs if s.text]
    if not segs:
        raise NoCaptions("Transcript is empty")
    return segs, transcript.language_code


# ----------------------------------------------------------------- formatting


def render(video: Video, segs: list[Segment], opts: Options, heading_level: int = 1) -> str:
    if opts.fmt == "json":
        return json.dumps({
            "title": video.title, "video_id": video.video_id, "url": video.url,
            "language": video.language,
            "segments": [{"start": round(s.start, 2), "duration": round(s.duration, 2), "text": s.text} for s in segs],
        }, ensure_ascii=False, indent=2)

    if opts.fmt == "md":
        head = f"{'#' * heading_level} {video.title}\n\n<{video.url}>\n\n"
        if opts.timestamps:
            body = "\n".join(
                f"[`{fmt_ts(s.start)}`]({video.url}&t={int(s.start)}s) {s.text}  " for s in segs)
        else:
            body = paragraphs(segs)
        return head + body + "\n"

    head = f"{video.title}\n{video.url}\n\n"
    if opts.timestamps:
        body = "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in segs)
    else:
        body = paragraphs(segs)
    return head + body + "\n"


def paragraphs(segs: list[Segment], gap: float = 2.5, max_chars: int = 700) -> str:
    """Join segments into readable paragraphs, breaking on pauses or length."""
    out, cur, last_end = [], [], None
    for s in segs:
        if cur and ((last_end is not None and s.start - last_end > gap) or sum(len(c) for c in cur) > max_chars):
            out.append(" ".join(cur))
            cur = []
        cur.append(s.text)
        last_end = s.start + s.duration
    if cur:
        out.append(" ".join(cur))
    return "\n\n".join(out)


def _describe(e: BaseException) -> str:
    """Full, single-line error description, including the underlying cause."""
    parts, seen = [], set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        msg = " ".join(str(e).split())
        parts.append(f"{type(e).__name__}: {msg}" if msg else type(e).__name__)
        e = e.__cause__ or e.__context__
    return " <- caused by ".join(parts)[:2000]


def _lib_versions() -> str:
    out = []
    for mod, name in (("yt_dlp.version", "yt-dlp"), ("youtube_transcript_api", "youtube-transcript-api")):
        try:
            from importlib.metadata import version
            out.append(f"{name} {version(name)}")
        except Exception:
            out.append(f"{name} ?")
    return ", ".join(out)


# ----------------------------------------------------------------- the runner


class Runner:
    """Processes a Collection, reporting progress through a callback.

    on_update(video) is called whenever a video's status changes.
    should_stop() is checked between videos, during waits, and after each
    request, so a cancel takes effect almost immediately.
    """

    def __init__(self, collection: Collection, opts: Options,
                 on_update: Callable[[Video], None] = lambda v: None,
                 should_stop: Callable[[], bool] = lambda: False,
                 fetcher: Callable = fetch_transcript,
                 sleeper: Callable[[float], None] = time.sleep,
                 source_url: str = ""):
        self.c, self.opts = collection, opts
        self.on_update, self.should_stop = on_update, should_stop
        self.fetcher, self.sleep = fetcher, sleeper
        self.ext = FORMATS[opts.fmt]
        base = Path(os.path.expanduser(opts.output_dir))
        self.folder = base / safe_name(collection.title) if collection.is_playlist else base
        self.combined: list[tuple[Video, list[Segment]]] = []
        self.rendered: dict[str, str] = {}   # video_id -> text, for UI previews
        self.source_url = source_url
        self.started = time.localtime()
        self.log_file = ""                   # set once a failure log has been written

    def file_for(self, v: Video) -> Path:
        prefix = f"{v.index:03d} - " if self.c.is_playlist else ""
        return self.folder / f"{prefix}{safe_name(v.title)}{self.ext}"

    def run(self) -> dict:
        self.folder.mkdir(parents=True, exist_ok=True)
        first = True
        for v in self.c.videos:
            if self.should_stop():
                break
            target = self.file_for(v)
            if target.exists() and not self.opts.overwrite and not self.opts.combine:
                self._set(v, "skipped", "Already saved", str(target))
                continue
            if not first:
                self.wait(self.opts.delay + random.uniform(0, 0.75))
                if self.should_stop():
                    break
            first = False
            self.process(v, target)
        if self.should_stop():
            for v in self.c.videos:
                if v.status in ("queued", "fetching"):
                    self._set(v, "cancelled", "Cancelled")
        # On cancel, the combined file still gets whatever was fetched.
        combined_file = self._write_combined() if self.opts.combine else ""
        self.write_failure_log()
        return self.summary(combined_file)

    def wait(self, seconds: float, step: float = 0.2) -> None:
        """Sleep in small steps so a cancel takes effect within ~0.2s."""
        remaining = seconds
        while remaining > 0 and not self.should_stop():
            chunk = min(step, remaining)
            self.sleep(chunk)
            remaining -= chunk

    def process(self, v: Video, target: Optional[Path] = None) -> None:
        target = target or self.file_for(v)
        v.detail, v.attempts = "", 0
        self._set(v, "fetching")
        for attempt in range(1, self.opts.retries + 1):
            if self.should_stop():
                self._set(v, "cancelled", "Cancelled")
                return
            try:
                segs, lang = self.fetcher(v.video_id, self.opts.language)
                if self.should_stop():            # cancelled while the request was in flight
                    self._set(v, "cancelled", "Cancelled")
                    return
                segs = [Segment(s.start, s.duration, clean_text(s.text)) for s in segs]
                segs = [s for s in segs if s.text]
                v.language = lang
                self.rendered[v.video_id] = render(v, segs, self.opts)
                if self.opts.combine:
                    self.combined.append((v, segs))
                    self._set(v, "saved", f"{len(segs)} lines")
                else:
                    target.write_text(self.rendered[v.video_id], encoding="utf-8")
                    self._set(v, "saved", f"{len(segs)} lines", str(target))
                return
            except NoCaptions as e:
                v.attempts, v.detail = attempt, _describe(e)
                self._set(v, "no_captions", str(e))
                return
            except Blocked as e:
                v.attempts, v.detail = attempt, _describe(e)
                if attempt == self.opts.retries:
                    self._set(v, "error", f"Blocked by YouTube: {e}. Wait a while and retry.")
                    return
                self.wait(15 * attempt)           # back off hard when YouTube pushes back
            except Exception as e:                # network hiccups etc.
                v.attempts, v.detail = attempt, _describe(e)
                if attempt == self.opts.retries:
                    self._set(v, "error", f"{type(e).__name__}: {e}"[:300])
                    return
                self.wait(3 * attempt)
        if self.should_stop() and v.status == "fetching":
            self._set(v, "cancelled", "Cancelled")

    def _write_combined(self) -> str:
        if not self.combined:
            return ""
        order = {v.video_id: i for i, v in enumerate(self.c.videos)}
        items = sorted(self.combined, key=lambda p: order[p[0].video_id])
        path = self.folder / f"{safe_name(self.c.title)} (combined){self.ext}"
        if self.opts.fmt == "json":
            data = {"title": self.c.title, "videos": [json.loads(render(v, s, self.opts)) for v, s in items]}
            text = json.dumps(data, ensure_ascii=False, indent=2)
        elif self.opts.fmt == "md":
            text = f"# {self.c.title}\n\n" + "\n---\n\n".join(render(v, s, self.opts, 2) for v, s in items)
        else:
            sep = "\n" + "=" * 70 + "\n\n"
            text = sep.join(f"{v.index}. " + render(v, s, self.opts) for v, s in items)
        path.write_text(text, encoding="utf-8")
        for v, _ in items:
            v.file = str(path)
        return str(path)

    def _set(self, v: Video, status: str, message: str = "", file: str = "") -> None:
        v.status, v.message = status, message
        if file:
            v.file = file
        self.on_update(v)

    def summary(self, combined_file: str = "") -> dict:
        counts = {}
        for v in self.c.videos:
            counts[v.status] = counts.get(v.status, 0) + 1
        return {"folder": str(self.folder), "combined_file": combined_file, "counts": counts,
                "log_file": self.log_file}

    # ------------------------------------------------------------- failure log

    def failures(self) -> list:
        return [v for v in self.c.videos if v.status in ("error", "no_captions")]

    def write_failure_log(self) -> str:
        """Write (or rewrite) this run's failure log. Returns its path, or "" if nothing failed.

        One file per run, named by start time, so a retry updates the same log
        instead of creating a new one. If a retry fixes everything, the log is removed.
        """
        failed = self.failures()
        path = Path(os.path.expanduser(self.opts.log_dir)) / (
            time.strftime("%Y-%m-%d %H%M%S", self.started) + f" - {safe_name(self.c.title, 80)}.log")
        if not failed:
            if self.log_file and Path(self.log_file).exists():
                Path(self.log_file).unlink()
            self.log_file = ""
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_failure_log(failed), encoding="utf-8")
        self.log_file = str(path)
        return self.log_file

    def render_failure_log(self, failed: list) -> str:
        import platform
        counts = self.summary_counts()
        o = self.opts
        lines = [
            "TRANSCRIPT GRABBER - FAILURE LOG",
            "=" * 48,
            f"Run started : {time.strftime('%Y-%m-%d %H:%M:%S', self.started)}",
            f"Log written : {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Source      : {self.source_url or '(not recorded)'}",
            f"Collection  : {self.c.title} ({len(self.c.videos)} video{'s' if len(self.c.videos) != 1 else ''})",
            f"Output      : {self.folder}",
            f"Options     : language={o.language}, format={o.fmt}, timestamps={o.timestamps}, "
            f"combine={o.combine}, retries={o.retries}, delay={o.delay}s",
            f"Environment : Transcript Grabber {APP_VERSION}, Python {platform.python_version()}, "
            f"{platform.system()} {platform.release()}, {_lib_versions()}",
            "",
            "SUMMARY",
            "-" * 48,
            "  " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(counts.items())),
            "",
        ]
        groups = [("FAILED (can be retried)", [v for v in failed if v.status == "error"]),
                  ("NO CAPTIONS (nothing to retry)", [v for v in failed if v.status == "no_captions"])]
        for heading, items in groups:
            if not items:
                continue
            lines += [f"{heading}: {len(items)}", "-" * 48]
            for v in items:
                lines += [
                    f"#{v.index}  {v.title}",
                    f"    URL      : {v.url}",
                    f"    Reason   : {v.message or '-'}",
                    f"    Attempts : {v.attempts}",
                    f"    Detail   : {v.detail or '-'}",
                    "",
                ]
        retry = [v.url for v in failed if v.status == "error"]
        if retry:
            lines += ["RETRY LIST (save as links.txt and run: python transcript_grabber.py --urls-file links.txt)",
                      "-" * 48, *retry, ""]
        lines += ["HINTS",
                  "-" * 48,
                  "  Blocked / 429 / RequestBlocked : YouTube is throttling this IP. Wait 30-60 minutes, then retry.",
                  "  Network or proxy errors        : check the internet connection or VPN, then retry.",
                  "  Anything else                  : update the libraries (re-run the launcher, or",
                  "                                   pip install -U yt-dlp youtube-transcript-api) and retry.",
                  ""]
        return "\n".join(lines)

    def summary_counts(self) -> dict:
        counts = {}
        for v in self.c.videos:
            counts[v.status] = counts.get(v.status, 0) + 1
        return counts
