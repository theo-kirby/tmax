"""Lifecycle, fallback detection, aggregation, and real tmux activity tests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import agent_status as activity
import remote


class ActivityTests(unittest.TestCase):
    def test_event_transitions(self):
        for event in ["UserPromptSubmit", "PostToolUse", "agent_start", "ui_prompt_end"]:
            self.assertEqual(activity.event_state(event, {}), "working")
        for event in ["Stop", "Interrupt", "PermissionRequest", "agent_settled", "ui_prompt_start"]:
            self.assertEqual(activity.event_state(event, {}), "waiting")
        self.assertEqual(activity.event_state("PreToolUse", {"tool_name": "functions.request_user_input"}), "waiting")
        self.assertEqual(activity.event_state("PreToolUse", {"tool_name": "AskUserQuestion"}), "waiting")
        self.assertEqual(activity.event_state("SessionEnd", {}), "plain")
        self.assertIsNone(activity.event_state("Notification", {"notification_type": "unrelated"}))

    def test_terminal_activity_distinguishes_work_idle_errors_and_unknown(self):
        self.assertEqual(activity.screen_activity("• Working (4m 13s • esc to interrupt)"),("working",""))
        self.assertEqual(activity.screen_activity("esc to interrupt\nDo you want to proceed?"),("waiting","approval"))
        self.assertEqual(activity.screen_activity("● API Error: Unable to connect to API: SSL certificate verification failed\n❯\nshift+tab to cycle"),("waiting","connection error"))
        self.assertEqual(activity.screen_activity("❯\n? for shortcuts"),("idle",""))
        self.assertEqual(activity.screen_activity("✻ Germinating… (15m 16s · ↓ 71.4k tokens)\n❯\nshift+tab to cycle"),("working",""))
        self.assertEqual(activity.screen_activity("✻ Crunched for 1m 5s · done 6:00 AM\n❯\nshift+tab to cycle"),("idle",""))
        self.assertEqual(activity.screen_activity("some output without UI indicators"),("unknown",""))
    def test_background_work_is_active_until_live_footer_clears(self):
        footer = "❯\n⏵⏵ bypass permissions on · 2 shells · ← for agents"
        self.assertEqual(activity.screen_activity(footer), ("working", "background work"))
        self.assertEqual(activity.screen_activity(footer.replace("2 shells", "1 task")), ("working", "background work"))
        self.assertEqual(activity.screen_activity(footer.replace("2 shells", "0 shells")), ("idle", ""))
        self.assertEqual(activity.screen_activity("1 shell still running\n❯\n⏵⏵ bypass permissions on"), ("idle", ""))
        self.assertEqual(activity.screen_activity(footer + "\nDo you want to proceed?"), ("waiting", "approval"))
        self.assertEqual(activity.screen_activity("✻ Working… (3s · ↓ 10 tokens)\n" + footer), ("working", ""))

    def test_interactive_client_does_not_count_its_backend_helpers(self):
        table={1:(0,"shell","zsh"),2:(1,"client","codex"),3:(2,"backend","/opt/codex app-server --listen unix://"),4:(2,"loop","/opt/codex app-server daemon pid-update-loop")}
        self.assertEqual(activity.interactive_agents(1,table),{2})
    def test_backend_socket_link_requires_an_actual_unique_peer(self):
        output="codex 10 theo 18u unix 0xclient 0t0 ->0xserver\ncodex 20 theo 44u unix 0xserver 0t0 /tmp/socket\n"
        self.assertEqual(activity.unix_peer_links(output,{10},{20}),{20:10})
        self.assertEqual(activity.unix_peer_links(output+"codex 11 theo 19u unix 0xother 0t0 ->0xserver\n",{10,11},{20}),{})
        self.assertEqual(activity.unix_peer_links("codex 10 theo 18u unix 0xclient 0t0 /tmp/socket",{10},{20}),{})

    def test_process_arguments_are_not_shell_escaped(self):
        self.assertEqual(activity.agent_name("claude --dangerously-skip-permissions Check the project's status"),"claude")
        self.assertEqual(activity.agent_name('codex a prompt with an unmatched "quote'),"codex")
        self.assertFalse(activity.is_codex_backend("codex explain app-server"))
        self.assertTrue(activity.is_codex_backend("/opt/codex app-server --listen unix://"))

    def test_priority(self):
        self.assertEqual(activity.strongest(["plain", "working", "waiting"]), "waiting")
        self.assertEqual(activity.strongest(["plain", "working"]), "working")
        self.assertEqual(activity.strongest([]), "plain")

    def test_process_detection_does_not_match_prose(self):
        self.assertIsNone(activity.agent_name("bash -c 'echo codex working'"))
        self.assertEqual(activity.agent_name("node /opt/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"), "pi")
        self.assertEqual(activity.agent_name("/Users/me/.local/share/claude/versions/2.1.263"), "claude")

    def test_stale_pid_record_and_ordinary_terminal(self):
        table = {10: (1, "new", "codex")}
        stale = json.dumps({"pid": 10, "started": "old", "state": "working"})
        self.assertEqual(activity.pane_state(10, stale, table, lambda: "ready"), "waiting")
        self.assertEqual(activity.pane_state(10, stale, {10: (1, "new", "bash")}, lambda: "esc to interrupt"), "plain")

    def test_fallback_waiting_overrides_busy_hint(self):
        self.assertEqual(activity.screen_state("Working (esc to interrupt)"), "working")
        self.assertEqual(activity.screen_state("esc to interrupt\nDo you want to proceed?"), "waiting")
        self.assertEqual(activity.screen_state("You’ve hit your limit"), "waiting")

    def test_dot_colours_and_visible_alignment(self):
        dots = remote.activity_dots(["plain", "working", "waiting"])
        self.assertEqual(remote.visible_width(dots), 5)
        self.assertIn("\x1b[97m", dots)
        self.assertIn("\x1b[92m", dots)
        self.assertIn("\x1b[93m", dots)
        self.assertEqual(remote.visible_width(remote.pad_visible(dots, 9)), 9)

    def test_host_session_and_remote_aggregation(self):
        listing = "$0\tlocal\t\t\t3\t\n$1\tbox/remote\tbox\tremote\t2\t$9"
        with patch.object(remote, "local", return_value=listing), \
             patch.object(remote, "hosts", return_value={"box": {"destination": "box"}}), \
             patch.object(remote, "switch_hosts_focus", return_value=None), \
             patch.object(remote, "switch_state", return_value={"favorites": [], "collapsed": []}), \
             patch.object(remote, "activity_snapshot", return_value={"$0": ["plain", "working", "waiting"]}), \
             patch.object(remote, "remote_activity", return_value={"box": {"$9": ["plain", "working"]}}), \
             patch.object(remote, "host_colour", return_value="blue"), \
             patch.object(remote.auth, "live", return_value=True), \
             patch.object(remote.auth, "read_lease", return_value={}):
            rows, _ = remote.switch_rows()
        fields = {row.split("\t")[0]: row.split("\t") for row in rows}
        self.assertEqual(fields["$0"][3].strip(), remote.activity_dots(["plain", "working", "waiting"]))
        self.assertEqual(fields["host:local"][3].strip(), remote.activity_dots(["waiting"]))
        self.assertEqual(fields["$1"][3].strip(), remote.activity_dots(["plain", "working"]))
        self.assertEqual(fields["host:box"][3].strip(), remote.activity_dots(["working"]))

    def test_installer_preserves_other_hooks_and_is_idempotent(self):
        spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install-agent-status.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"other": True, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-existing-hook"}]}]}}))
            command = 'python3 "$HOME/.local/share/tmax/agent_status.py" hook claude'
            installer.merge(path, ["Stop"], command)
            first = path.read_text()
            installer.merge(path, ["Stop"], command)
            self.assertEqual(path.read_text(), first)
            self.assertIn("my-existing-hook", first)
            self.assertTrue(json.loads(first)["other"])


class TmuxTests(unittest.TestCase):
    def test_live_hooks_splits_exit_and_window_order(self):
        socket = "tmax-activity-" + uuid.uuid4().hex[:8]
        def tmux(*args):
            return subprocess.check_output(["tmux", "-L", socket, *args], text=True).strip()
        fixture = tempfile.TemporaryDirectory(prefix="tmax-fake-agent-")
        executable = Path(fixture.name) / "codex"
        executable.symlink_to("/bin/sleep")
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "test", "-x", "100", "-y", "30")
            env = dict(os.environ, TMUX=tmux("display-message", "-p", "#{socket_path},#{pid},0"))
            agent_command = shlex.join([str(executable), "90"])
            first = tmux("new-window", "-d", "-t", "test:1", "-P", "-F", "#{pane_id}", agent_command)
            second = tmux("new-window", "-d", "-t", "test:2", "-P", "-F", "#{pane_id}", agent_command)
            import time
            time.sleep(0.3)
            def event(pane, name):
                pid = tmux("display-message", "-p", "-t", pane, "#{pane_pid}")
                subprocess.run([sys.executable, str(ROOT / "scripts/agent_status.py"), "hook", "codex", name],
                               env=dict(env, TMUX_PANE=pane, TMAX_AGENT_PID=pid), check=True)
            event(first, "UserPromptSubmit")
            event(second, "Stop")
            sid = tmux("display-message", "-p", "-t", "test", "#{session_id}")
            self.assertEqual(activity.snapshot(socket=socket)[sid], ["plain", "working", "waiting"])
            split = tmux("split-window", "-d", "-t", first, "-P", "-F", "#{pane_id}", agent_command)
            time.sleep(0.3)
            event(split, "Stop")
            self.assertEqual(activity.snapshot(socket=socket)[sid], ["plain", "waiting", "waiting"])
            tmux("kill-pane", "-t", split)
            tmux("respawn-pane", "-k", "-t", second, "sleep 90")
            time.sleep(0.2)
            self.assertEqual(activity.snapshot(socket=socket)[sid], ["plain", "working", "plain"])
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
