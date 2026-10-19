"""Offline tests: python -m unittest test_core  (no network needed)."""
import json
import tempfile
import unittest
from pathlib import Path

import tg_core as T

LOGS = tempfile.mkdtemp()   # keep test logs out of the real home folder


def fake_fetch(video_id, language="en"):
    if video_id == "nocaptions0":
        raise T.NoCaptions("TranscriptsDisabled")
    return ([T.Segment(1.0, 2.0, "Hello there."), T.Segment(3.5, 2.0, "Second  line\nwraps."),
             T.Segment(70.0, 2.0, "After a pause.")], language)


class Core(unittest.TestCase):
    def test_video_ids(self):
        self.assertEqual(T.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3"), "dQw4w9WgXcQ")
        self.assertEqual(T.extract_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(T.extract_video_id("https://youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertTrue(T.is_playlist_url("https://www.youtube.com/playlist?list=PL123"))
        self.assertTrue(T.is_playlist_url("https://www.youtube.com/@somechannel/videos"))
        self.assertFalse(T.is_playlist_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_safe_name(self):
        self.assertEqual(T.safe_name('a/b:c*d?"e<f>g|h. '), "abcdefgh")

    def _col(self):
        return T.Collection("My: List", [T.Video(1, "aaaaaaaaaaa", "First / One", 754),
                                         T.Video(2, "nocaptions0", "Second", 60),
                                         T.Video(3, "bbbbbbbbbbb", "Third", 3700)], is_playlist=True)

    def run_with(self, **kw):
        tmp = tempfile.mkdtemp()
        opts = T.Options(log_dir=LOGS, output_dir=tmp, delay=0, **kw)
        r = T.Runner(self._col(), opts, fetcher=fake_fetch, sleeper=lambda s: None)
        return r, r.run(), Path(tmp)

    def test_txt_playlist(self):
        r, res, tmp = self.run_with()
        folder = tmp / "My List"
        files = sorted(p.name for p in folder.iterdir())
        self.assertEqual(files, ["001 - First One.txt", "003 - Third.txt"])
        text = (folder / "001 - First One.txt").read_text()
        self.assertIn("[00:00:01] Hello there.", text)
        self.assertIn("[00:00:03] Second line wraps.", text)
        self.assertEqual(res["counts"], {"saved": 2, "no_captions": 1})

    def test_resume_skips_existing(self):
        r, _, tmp = self.run_with()
        r2 = T.Runner(self._col(), T.Options(log_dir=LOGS, output_dir=str(tmp), delay=0), fetcher=fake_fetch, sleeper=lambda s: None)
        self.assertEqual(r2.run()["counts"].get("skipped"), 2)

    def test_paragraphs_md(self):
        _, _, tmp = self.run_with(timestamps=False, fmt="md")
        text = (tmp / "My List" / "003 - Third.md").read_text()
        self.assertIn("Hello there. Second line wraps.\n\nAfter a pause.", text)
        self.assertTrue(text.startswith("# Third"))

    def test_md_timestamps_link(self):
        _, _, tmp = self.run_with(fmt="md")
        self.assertIn("&t=70s) After a pause.", (tmp / "My List" / "003 - Third.md").read_text())

    def test_combined_json(self):
        _, res, tmp = self.run_with(combine=True, fmt="json")
        data = json.loads(Path(res["combined_file"]).read_text())
        self.assertEqual([v["title"] for v in data["videos"]], ["First / One", "Third"])
        self.assertEqual(len(list((tmp / "My List").iterdir())), 1)

    def test_blocked_retries_then_fails(self):
        calls = []

        def blocked(vid, lang):
            calls.append(vid)
            raise T.Blocked("429")
        col = T.Collection("x", [T.Video(1, "aaaaaaaaaaa", "A")])
        r = T.Runner(col, T.Options(log_dir=LOGS, output_dir=tempfile.mkdtemp(), retries=3), fetcher=blocked, sleeper=lambda s: None)
        r.run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(col.videos[0].status, "error")

    def test_cancel_mid_run(self):
        stop = {"flag": False}

        def fetch(vid, lang):
            if vid == "aaaaaaaaaaa":
                stop["flag"] = True          # user hits Cancel while this request is in flight
            return fake_fetch(vid, lang)
        tmp = tempfile.mkdtemp()
        col = self._col()
        r = T.Runner(col, T.Options(log_dir=LOGS, output_dir=tmp), fetcher=fetch, sleeper=lambda s: None,
                     should_stop=lambda: stop["flag"])
        res = r.run()
        self.assertEqual([v.status for v in col.videos], ["cancelled"] * 3)
        self.assertEqual(list((Path(tmp) / "My List").iterdir()), [])   # in-flight result discarded

    def test_cancel_interrupts_wait(self):
        slept = []
        stop = {"flag": False}

        def sleeper(s):
            slept.append(s)
            if len(slept) == 3:
                stop["flag"] = True
        r = T.Runner(self._col(), T.Options(log_dir=LOGS, output_dir=tempfile.mkdtemp()), fetcher=fake_fetch,
                     sleeper=sleeper, should_stop=lambda: stop["flag"])
        r.wait(60)
        self.assertEqual(len(slept), 3)              # stopped after ~0.6s, not 60s

    def test_cancel_keeps_saved_and_resumes(self):
        stop = {"n": 0}

        def fetch(vid, lang):
            stop["n"] += 1
            return fake_fetch(vid, lang)
        tmp = tempfile.mkdtemp()
        col = self._col()
        T.Runner(col, T.Options(log_dir=LOGS, output_dir=tmp), fetcher=fetch, sleeper=lambda s: None,
                 should_stop=lambda: stop["n"] >= 2).run()
        self.assertEqual([v.status for v in col.videos], ["saved", "no_captions", "cancelled"])
        col2 = self._col()
        T.Runner(col2, T.Options(log_dir=LOGS, output_dir=tmp), fetcher=fake_fetch, sleeper=lambda s: None).run()
        self.assertEqual([v.status for v in col2.videos], ["skipped", "no_captions", "saved"])

    def test_failure_log(self):
        tmp = tempfile.mkdtemp()
        calls = {"n": 0}

        def fetch(vid, lang):
            if vid == "bbbbbbbbbbb":
                calls["n"] += 1
                raise ConnectionError("proxy said no") from OSError("tunnel 403")
            return fake_fetch(vid, lang)
        col = self._col()
        r = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs", retries=2), fetcher=fetch,
                     sleeper=lambda s: None, source_url="https://www.youtube.com/playlist?list=PLx")
        res = r.run()
        log = Path(res["log_file"])
        self.assertEqual(log.parent, Path(tmp) / "logs")
        text = log.read_text()
        self.assertIn("Source      : https://www.youtube.com/playlist?list=PLx", text)
        self.assertIn("FAILED (can be retried): 1", text)
        self.assertIn("NO CAPTIONS (nothing to retry): 1", text)
        self.assertIn("ConnectionError: proxy said no <- caused by OSError: tunnel 403", text)
        self.assertIn("Attempts : 2", text)
        self.assertIn("https://www.youtube.com/watch?v=bbbbbbbbbbb", text.split("RETRY LIST")[1])
        self.assertNotIn("nocaptions0", text.split("RETRY LIST")[1])   # no-caption videos aren't retryable

    def test_no_log_when_everything_works(self):
        tmp = tempfile.mkdtemp()
        col = T.Collection("ok", [T.Video(1, "aaaaaaaaaaa", "A")])
        res = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs"), fetcher=fake_fetch,
                       sleeper=lambda s: None).run()
        self.assertEqual(res["log_file"], "")
        self.assertFalse((Path(tmp) / "logs").exists())

    def test_retry_updates_then_removes_log(self):
        tmp = tempfile.mkdtemp()
        state = {"fail": True}

        def fetch(vid, lang):
            if state["fail"]:
                raise TimeoutError("slow")
            return fake_fetch(vid, lang)
        col = T.Collection("one", [T.Video(1, "aaaaaaaaaaa", "A")])
        r = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs", retries=1), fetcher=fetch,
                     sleeper=lambda s: None)
        log = r.run()["log_file"]
        self.assertTrue(Path(log).exists())
        state["fail"] = False
        r.process(col.videos[0])
        self.assertEqual(r.write_failure_log(), "")      # fixed by retry: log removed
        self.assertFalse(Path(log).exists())

    # ---- caption fallback (yt-dlp) -------------------------------------

    def test_parse_json3(self):
        raw = json.dumps({"events": [
            {"tStartMs": 1200, "dDurationMs": 2500, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
            {"tStartMs": 3700, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},   # newline-only event
            {"tStartMs": 4000},                                                              # window event, no text
            {"tStartMs": 65000, "dDurationMs": 2000, "segs": [{"utf8": "Second\nline"}]},
        ]})
        segs = T.parse_json3(raw)
        self.assertEqual([(s.start, s.text) for s in segs], [(1.2, "Hello world"), (65.0, "Second line")])

    def test_pick_track(self):
        j3 = [{"ext": "vtt", "url": "v"}, {"ext": "json3", "url": "j"}]
        info = {"subtitles": {"live_chat": j3, "es": j3},
                "automatic_captions": {"en-orig": j3, "en": j3, "fr": j3}}
        self.assertEqual(T._pick_track(info, "es")[1], "es")      # manual wins
        self.assertEqual(T._pick_track(info, "en")[1], "en")      # original-language auto captions
        self.assertEqual(T._pick_track(info, "fr")[1], "fr")      # auto-translated track
        self.assertEqual(T._pick_track(info, "de"), (None, ""))
        self.assertEqual(T._pick_track(info, "any")[1], "es")     # manual first, never live chat
        self.assertEqual(T._pick_track({"subtitles": {"en-US": j3}}, "en")[1], "en-US")
        self.assertEqual(T._pick_track({"automatic_captions": {"pt-orig": j3}}, "any")[1], "pt")

    def test_fallback_when_api_blocked_and_sticky(self):
        calls = []

        def api(vid, lang):
            calls.append(("api", vid))
            raise T.Blocked("IpBlocked")

        def ytdlp(vid, lang):
            calls.append(("ytdlp", vid))
            return fake_fetch(vid, lang)
        f = T.SmartFetcher("auto", api=api, ytdlp=ytdlp)
        tmp = tempfile.mkdtemp()
        col = T.Collection("x", [T.Video(1, "aaaaaaaaaaa", "A"), T.Video(2, "bbbbbbbbbbb", "B")], is_playlist=True)
        res = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs"), fetcher=f, sleeper=lambda s: None).run()
        self.assertEqual(res["counts"], {"saved": 2})
        # the blocked library is tried once, then skipped for the rest of the run
        self.assertEqual(calls, [("api", "aaaaaaaaaaa"), ("ytdlp", "aaaaaaaaaaa"), ("ytdlp", "bbbbbbbbbbb")])
        self.assertEqual(col.videos[0].source, "transcript-api blocked -> yt-dlp")
        self.assertTrue(col.videos[1].source.startswith("yt-dlp"))

    def test_api_no_captions_is_trusted(self):
        hit = []

        def api(vid, lang):
            raise T.NoCaptions("TranscriptsDisabled")
        f = T.SmartFetcher("auto", api=api, ytdlp=lambda v, l: hit.append(v))
        with self.assertRaises(T.NoCaptions):
            f("aaaaaaaaaaa")
        self.assertEqual(hit, [])

    def test_both_blocked_logs_method_and_clear_reason(self):
        def blocked(vid, lang):
            raise T.Blocked("Sign in to confirm you're not a bot")
        tmp = tempfile.mkdtemp()
        col = T.Collection("one", [T.Video(1, "aaaaaaaaaaa", "A")])
        r = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs", retries=2),
                     fetcher=T.SmartFetcher("auto", api=blocked, ytdlp=blocked), sleeper=lambda s: None)
        text = Path(r.run()["log_file"]).read_text()
        v = col.videos[0]
        self.assertEqual(v.status, "error")
        self.assertNotIn(": .", v.message)                 # the old "Blocked by YouTube: ." bug
        self.assertIn("Turning on cookies", v.message)
        self.assertIn("Method   : yt-dlp (transcript-api was blocked earlier this run)", text)
        self.assertIn("JS runtime:", text)

    # ---- rate limits (HTTP 429), bot checks, cookies --------------------

    def _rl_runner(self, fetch, n=1, **opt):
        tmp = tempfile.mkdtemp()
        slept = []
        col = T.Collection("rl", [T.Video(i + 1, f"vid{i:08d}", f"V{i}") for i in range(n)], is_playlist=True)
        r = T.Runner(col, T.Options(output_dir=tmp, log_dir=tmp + "/logs", delay=0, **opt), fetcher=fetch,
                     sleeper=slept.append)
        return r, col, slept

    def test_rate_limit_waits_patiently_then_fails(self):
        calls = []

        def fetch(vid, lang):
            calls.append(vid)
            raise T.RateLimited("YouTube rate-limited the caption download (HTTP 429)")
        r, col, slept = self._rl_runner(fetch, retries=3)
        r.run()
        self.assertEqual(len(calls), 2)                     # one patient retry, not three quick ones
        self.assertGreaterEqual(sum(slept), 75)             # waited >= 75s before it
        v = col.videos[0]
        self.assertEqual(v.status, "error")
        self.assertIn("at least an hour", v.message)
        self.assertIn("Turning on cookies", v.message)

    def test_rate_limit_cools_down_then_stops_run(self):
        calls = []

        def fetch(vid, lang):
            calls.append(vid)
            raise T.RateLimited("429")
        r, col, slept = self._rl_runner(fetch, n=5)
        res = r.run()
        # 2 videos x 2 attempts, then the run stops itself instead of hammering YouTube
        self.assertEqual(calls, ["vid00000000"] * 2 + ["vid00000001"] * 2)
        self.assertTrue(res["rate_limit_stop"])
        self.assertEqual([v.status for v in col.videos], ["error", "error", "cancelled", "cancelled", "cancelled"])
        self.assertIn("rate-limiting", col.videos[2].message)
        self.assertGreaterEqual(sum(slept), 75 + 120 + 75)  # retry wait, cooldown before video 2, its retry wait
        self.assertIn("STOPPED EARLY", Path(res["log_file"]).read_text())

    def test_success_resets_rate_limit_streak(self):
        seq = iter([T.RateLimited("429"), T.RateLimited("429"), "ok", T.RateLimited("429"), T.RateLimited("429"), "ok"])

        def fetch(vid, lang):
            x = next(seq)
            if isinstance(x, Exception):
                raise x
            return fake_fetch(vid, lang)
        r, col, _ = self._rl_runner(fetch, n=3)
        res = r.run()
        self.assertFalse(res["rate_limit_stop"])
        self.assertEqual([v.status for v in col.videos], ["error", "saved", "error"])

    def test_bot_check_and_cookie_problems_fail_fast(self):
        for exc, expect in ((T.BotCheck("bot"), "isn't a bot"), (T.CookieProblem("Couldn't read Chrome's cookies"), "Chrome")):
            calls = []

            def fetch(vid, lang, exc=exc):
                calls.append(vid)
                raise exc
            r, col, slept = self._rl_runner(fetch, retries=3)
            r.run()
            self.assertEqual(len(calls), 1, expect)         # no pointless retries
            self.assertEqual(slept, [])
            self.assertIn(expect, col.videos[0].message)

    def test_cookies_go_straight_to_ytdlp(self):
        calls = []
        f = T.SmartFetcher("auto", cookies_browser="firefox",
                           api=lambda v, l: calls.append("api"), ytdlp=lambda v, l: calls.append("ytdlp") or fake_fetch(v, l))
        f("aaaaaaaaaaa")
        self.assertEqual(calls, ["ytdlp"])
        self.assertIn("firefox cookies", f.last_source)

    def test_ytdlp_error_mapping(self):
        class E(Exception):
            pass
        cases = [
            (E("HTTP Error 429: Too Many Requests"), T.RateLimited),
            (E("ERROR: [youtube] x: Sign in to confirm you're not a bot"), T.BotCheck),
            (E("Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"), T.CookieProblem),
            (E("Failed to decrypt with DPAPI"), T.CookieProblem),
            (E("HTTP Error 403: Forbidden"), T.Blocked),
            (E("ERROR: [youtube] x: Private video"), T.NoCaptions),
        ]
        for err, want in cases:
            with self.assertRaises(want, msg=str(err)):
                T._raise_mapped(err, "caption download", "chrome")
        try:
            T._raise_mapped(E("HTTP Error 429"), "caption download")
        except T.RateLimited as e:
            self.assertIn("caption download", str(e))

    def test_first_line_skips_blank_lines(self):
        self.assertEqual(T._first_line(Exception("\n\n  Could not retrieve\nmore")), "Could not retrieve")

    def test_durations(self):
        self.assertEqual(T.fmt_duration(754), "12:34")
        self.assertEqual(T.fmt_duration(3700), "1:01:40")


if __name__ == "__main__":
    unittest.main()
