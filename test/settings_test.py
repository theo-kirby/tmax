import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "tmax.conf"
        self.patch = patch.object(settings, "PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(settings.get("switcher", "key"), "Space")
        self.assertTrue(settings.flag("dial", "seconds"))
        self.assertEqual(settings.number("dial", "fps"), 12)
        self.assertIsNone(settings.hosts())
        self.assertEqual(settings.agents(), {})

    def test_values_hosts_and_reporter_in_their_old_shapes(self):
        self.path.write_text("[dial]\nlabels = names\nfps = nope\n\n[host local]\nlabel = air\n\n"
                             "[host box]\ndestination = me@box\ncolour = #ff8800\n\n"
                             "[agents]\nhost = air\ntoken_file = ~/t\nsockets = /a, /b\n")
        self.assertEqual(settings.get("dial", "labels"), "names")
        self.assertEqual(settings.number("dial", "fps"), 12)  # unreadable: default
        self.assertEqual(settings.hosts(), {"local": {"label": "air"}, "box": {"destination": "me@box", "colour": "#ff8800"}})
        self.assertEqual(settings.agents(), {"host": "air", "tokenFile": "~/t", "sockets": ["/a", "/b"]})

    def test_init_round_trips_the_current_setup(self):
        remotes = self.root / "remotes.json"
        remotes.write_text(json.dumps({"local": {"label": "air"}, "box": {"destination": "box", "tmux": "tmux"}}))
        agents = self.root / "agents.json"
        agents.write_text(json.dumps({"host": "air", "endpoint": "https://lab/ingest", "ouroboros": []}))
        with patch.dict(os.environ, {"TMAX_REMOTES_FILE": str(remotes), "TMAX_AGENT_CONFIG": str(agents)}), \
                patch.object(settings, "tmux_option", return_value=""):
            settings.init()
            with self.assertRaises(SystemExit):
                settings.init()
        self.assertEqual(settings.hosts(), {"local": {"label": "air"}, "box": {"destination": "box", "tmux": "tmux"}})
        self.assertEqual(settings.agents(), {"host": "air", "endpoint": "https://lab/ingest"})
        self.assertEqual(settings.get("switcher", "preview"), "agents")


if __name__ == "__main__":
    unittest.main()
