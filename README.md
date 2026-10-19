# Transcript Grabber (YTG)

**Paste a YouTube link and get its transcript saved as a text file.** It works for one video, a whole playlist, or an entire channel. Each video becomes a clean, readable file on your computer that you can search, study, take notes from, or feed into other tools.

Everything runs on your own computer through a simple web page in your browser. There's no account and no cloud service, and you never have to touch the command line unless you want to.

![Transcript Grabber while a playlist is downloading](docs/screenshot.png)

## Features

- **Any link:** a single video, a playlist, or a channel. Playlists and channels are expanded automatically.
- **Formats:** TXT, Markdown (timestamps link back to that moment in the video), or JSON.
- **Layout:** timestamps on or off, and one file per video or everything combined into one.
- **Language:** choose the transcript language; when only another language exists, it falls back to YouTube's auto-translation.
- **Live progress:** see where the run is, preview each transcript as it arrives, and retry anything that failed.
- **Cancel anytime:** use the **Cancel** button or press **Esc**. Nothing already saved is lost.
- **Resume:** run the same playlist again and it skips what you already have.

## Start it

**Windows:** double-click `Start Transcript Grabber.bat`.
**macOS / Linux:** run `./start.sh`.

The first run creates a private Python environment (`.venv`) and installs the two packages it needs, which takes about a minute. After that it opens **http://127.0.0.1:8765** in your browser. Keep the console window open while you use it; close it to quit.

You'll need Python 3.9 or newer (python.org). On Windows, tick "Add python.exe to PATH" during install.

## Use it

1. Paste a video, playlist, or channel URL.
2. Choose the language, timestamps on or off, one file per video or one combined file, and the format.
3. Click **Get transcripts**. Click any row to preview it. The **⋮** menu can open the file, open the video, or retry a failed one.
4. Changed your mind? Click **Cancel** (or press **Esc**). It stops right away, and every transcript saved so far stays saved.

Files go to `~/Transcript Grabber` by default (you can change this in the app). A playlist gets its own subfolder, with files numbered in playlist order: `001 - Title.txt`.

## Command line

```
python transcript_grabber.py "PLAYLIST_OR_VIDEO_URL"
python transcript_grabber.py URL --format md --no-timestamps --out "D:\Transcripts"
python transcript_grabber.py URL --combine --lang es
python transcript_grabber.py --urls-file links.txt
```

Run `python transcript_grabber.py --help` to see every option. Press Ctrl+C to stop.

## Good to know

- **Captions required:** it only works on videos that have captions (uploaded or auto-generated). Videos without captions show **No captions**.
- **Throttling:** YouTube slows down bulk requests. The app waits about 1.5 seconds between videos and backs off when it's blocked. If you see "Blocked by YouTube", wait a while and use **Retry**. It works best from a home connection; VPNs and cloud servers get blocked much more often.
- **Privacy:** the app only listens on your own computer (127.0.0.1) and only opens files it created.
- **Updates:** if YouTube changes something and lookups start failing, re-running the launcher updates both packages automatically.
- **Personal use:** transcripts belong to their creators. Keep them for your own study and notes, and don't republish them.

## How it's built

The short version: there's a Python core that does the work, a tiny local web server that drives it, and a single-page UI that polls the server for progress. The command line and the web UI share the same core.

[**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) is the full engineering write-up: components, data flow, how cancellation works, the design decisions and their trade-offs, and how it's tested.

## Project layout

| File | What it is |
|---|---|
| `tg_core.py` | Core logic: playlist expansion, fetching, formatting, saving, cancellation |
| `app.py` | Local web server and job controller for the UI |
| `static/index.html` | The UI (HTML, CSS, and vanilla JavaScript in one file) |
| `transcript_grabber.py` | Command-line version |
| `test_core.py` | Offline tests: `python -m unittest test_core` |
| `Start Transcript Grabber.bat`, `start.sh` | One-click launchers that set up a virtual environment |
| `docs/` | Architecture write-up and screenshot |
