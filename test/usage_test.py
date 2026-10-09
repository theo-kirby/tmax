import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage

ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(usage, "STATE", self.root), patch.object(usage, "CACHE", self.root / "usage.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_codex_reads_the_weekly_window(self):
        auth = self.root / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "t", "account_id": "a"}}))
        reply = {"rate_limit": {"primary_window": {"used_percent": 61, "limit_window_seconds": 18000, "reset_at": 1},
                                "secondary_window": {"used_percent": 26, "limit_window_seconds": 604800, "reset_at": 2}}}
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}), patch.object(usage, "request", return_value=reply) as request:
            self.assertEqual(usage.codex(), {"percent": 26.0, "resets": 2})
        self.assertEqual(request.call_args[0][1]["ChatGPT-Account-Id"], "a")

    def test_claude_reads_seven_day_and_never_uses_an_expired_token(self):
        login = {"claudeAiOauth": {"accessToken": "t", "expiresAt": (time.time() + 3600) * 1000}}
        (self.root / ".credentials.json").write_text(json.dumps(login))
        reply = {"seven_day": {"utilization": 24.0, "resets_at": "2026-10-09T13:59:59+00:00"}}
        with patch.object(usage.sys, "platform", "linux"), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}), \
                patch.object(usage, "request", return_value=reply):
            self.assertEqual(usage.claude()["percent"], 24.0)
            login["claudeAiOauth"]["expiresAt"] = 1000
            (self.root / ".credentials.json").write_text(json.dumps(login))
            with self.assertRaisesRegex(usage.Unavailable, "expired"):
                usage.claude()

    def test_refresh_caches_and_keeps_readings_through_an_outage(self):
        with patch.dict(usage.PROVIDERS, {"codex": lambda: {"percent": 40.0, "resets": None}}):
            self.assertEqual(usage.refresh(["codex"])["codex"]["percent"], 40.0)
        def offline():
            raise usage.Unavailable("offline")
        with patch.dict(usage.PROVIDERS, {"codex": offline}):
            self.assertEqual(usage.refresh(["codex"])["codex"]["percent"], 40.0)  # still fresh: not fetched
            data = usage.refresh(["codex"], max_age=0)
        self.assertEqual((data["codex"]["percent"], data["codex"]["error"]), (40.0, "offline"))

    def test_meters_fill_in_proportion_and_report_errors(self):
        text = usage.meters(["codex", "claude"], 30, {"codex": {"percent": 50.0}, "claude": {"error": "not signed in"}})
        codex, claude = ESCAPE.sub("", text).split("\n")
        self.assertEqual(len(codex), 30)
        self.assertTrue(codex.startswith("codex  ■") and codex.endswith(" 50%"))
        self.assertEqual(claude, "claude not signed in")
        self.assertEqual(text.split("\n")[0].count("\x1b[38;2;58;61;60m"), 1)  # the empty half is one dark run
        self.assertEqual(usage.colour(0), usage.GRADIENT[0])
        self.assertEqual(usage.colour(1), usage.GRADIENT[2])


if __name__ == "__main__":
    unittest.main()
