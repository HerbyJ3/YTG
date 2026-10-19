#!/usr/bin/env python3
"""
Transcript Grabber - command line.

Examples
  python transcript_grabber.py "https://www.youtube.com/watch?v=VIDEO_ID"
  python transcript_grabber.py "https://www.youtube.com/playlist?list=PLAYLIST_ID" --format md
  python transcript_grabber.py URL --no-timestamps --combine --out "D:/Transcripts"
  python transcript_grabber.py --urls-file links.txt

Run `python app.py` instead for the point-and-click version.
"""
import argparse
import sys

from tg_core import DEFAULT_LOG_DIR, DEFAULT_OUTPUT, Options, Runner, list_videos

ICONS = {"saved": "OK  ", "skipped": "SKIP", "no_captions": "NONE", "error": "FAIL", "fetching": "... "}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Save transcripts from YouTube videos, playlists, or channels.")
    p.add_argument("urls", nargs="*", help="Video, playlist, or channel URL(s)")
    p.add_argument("--urls-file", help="Text file with one URL per line")
    p.add_argument("--out", default=DEFAULT_OUTPUT, help=f"Output folder (default: {DEFAULT_OUTPUT})")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"Where failure logs go (default: {DEFAULT_LOG_DIR})")
    p.add_argument("--format", choices=["txt", "md", "json"], default="txt")
    p.add_argument("--lang", default="en", help='Language code, e.g. en, es, fr, or "any" (default: en)')
    p.add_argument("--no-timestamps", action="store_true", help="Write flowing paragraphs instead of timestamped lines")
    p.add_argument("--combine", action="store_true", help="Put a playlist's transcripts into one file")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between videos (default: 1.5)")
    p.add_argument("--overwrite", action="store_true", help="Re-download transcripts that already exist")
    a = p.parse_args(argv)

    urls = list(a.urls)
    if a.urls_file:
        with open(a.urls_file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        p.print_help()
        return 1

    opts = Options(language=a.lang, timestamps=not a.no_timestamps, combine=a.combine,
                   fmt=a.format, output_dir=a.out, delay=a.delay, overwrite=a.overwrite,
                   log_dir=a.log_dir)
    failures = 0
    for url in urls:
        print(f"\nLooking up {url} ...")
        try:
            col = list_videos(url)
        except Exception as e:
            print(f"  Could not read that URL: {e}")
            failures += 1
            continue
        total = len(col.videos)
        print(f"  {col.title}: {total} video(s)")

        def show(v):
            if v.status == "fetching":
                return
            line = f"  [{ICONS.get(v.status, v.status)}] {v.index:>3}/{total}  {v.title[:70]}"
            if v.status in ("no_captions", "error"):
                line += f"  ({v.message})"
            print(line, flush=True)

        result = Runner(col, opts, on_update=show, source_url=url).run()
        c = result["counts"]
        failures += c.get("error", 0)
        print(f"\n  Saved {c.get('saved', 0)}, skipped {c.get('skipped', 0)}, "
              f"no captions {c.get('no_captions', 0)}, failed {c.get('error', 0)}")
        print(f"  Folder: {result['folder']}")
        if result["combined_file"]:
            print(f"  Combined file: {result['combined_file']}")
        if result["log_file"]:
            print(f"  Failure log: {result['log_file']}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
