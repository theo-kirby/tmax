"""Session lifecycle routing and confirmation; no network or real sessions."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import remote


class SessionActionsTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(remote, "hosts", return_value={"box": {}}))
        self.stack.enter_context(patch.object(remote, "label", side_effect=lambda host: host))
        self.stack.enter_context(patch.object(remote.auth, "live", return_value=True))
        self.stack.enter_context(patch.object(remote.auth, "read_lease", return_value={}))
        self.local = self.stack.enter_context(patch.object(remote, "local"))
        self.fetch = self.stack.enter_context(patch.object(remote, "fetch"))
        self.prepare = self.stack.enter_context(patch.object(remote, "prepare", return_value="box/new"))
        self.forget = self.stack.enter_context(patch.object(remote, "forget_proxy"))
        self.refresh = self.stack.enter_context(patch.object(remote, "refresh"))
        self.expand = self.stack.enter_context(patch.object(remote, "switch_host"))
        self.input = self.stack.enter_context(patch("builtins.input"))
        self.metadata = "box/work\t1:2\tbox\t$7\t3:4"
        self.local.return_value = self.metadata

    def test_create_routes_name_to_remote_and_registers_proxy(self):
        name = "work space; $(literal)"
        self.input.return_value = name
        self.fetch.return_value = "$9\t3:5\t" + name
        remote.switch_session("create", "group", "box", "host:box")
        self.assertEqual(self.fetch.call_args.args[:6], ("box", "new-session", "-d", "-s", name, "-P"))
        self.prepare.assert_called_once_with("box", "$9", "3:5", name)
        self.expand.assert_called_once_with("expand", "box")

    def test_create_from_session_uses_its_host(self):
        self.input.return_value = "new"
        remote.switch_session("create", "session", "local", "$2")
        self.local.assert_called_once_with("new-session", "-d", "-s", "new")
        self.fetch.assert_not_called()

    def test_cancel_does_not_create_or_unlock(self):
        self.input.return_value = ""
        remote.switch_session("create", "group", "box", "host:box")
        self.fetch.assert_not_called()
        self.local.assert_not_called()

    def test_confirmed_close_targets_remote_id_then_removes_proxy(self):
        self.input.return_value = "yes"
        self.fetch.return_value = "3:4"
        remote.switch_session("close", "session", "box", "$2")
        self.fetch.assert_any_call("box", "kill-session", "-t", "$7")
        self.forget.assert_called_once_with("box/work")
        self.assertFalse(any(call.args[0] == "kill-session" for call in self.local.call_args_list))

    def test_cancel_close_never_contacts_remote(self):
        self.input.return_value = "n"
        remote.switch_session("close", "session", "box", "$2")
        self.fetch.assert_not_called()
        self.forget.assert_not_called()

    def test_replaced_remote_session_is_not_killed(self):
        self.input.side_effect = ["y", ""]
        self.fetch.return_value = "different:epoch"
        remote.switch_session("close", "session", "box", "$2")
        self.assertFalse(any(call.args[1] == "kill-session" for call in self.fetch.call_args_list))
        self.forget.assert_not_called()

    def test_changed_proxy_during_confirmation_is_not_killed(self):
        self.input.side_effect = ["y", ""]
        self.local.side_effect = [self.metadata, self.metadata.replace("$7", "$8")]
        remote.switch_session("close", "session", "box", "$2")
        self.fetch.assert_not_called()

    def test_host_heading_cannot_be_closed(self):
        remote.switch_session("close", "group", "box", "host:box")
        self.input.assert_not_called()
        self.local.assert_not_called()

    def test_failed_unlock_does_not_create(self):
        self.input.return_value = "new"
        with patch.object(remote.auth, "live", return_value=False), patch.object(remote.auth, "unlock", return_value=False):
            remote.switch_session("create", "group", "box", "host:box")
        self.fetch.assert_not_called()
        self.prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
