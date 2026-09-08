"""Authentication lifecycle tests; no network, accounts, or real passwords."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.cfg = {"destination": "example"}
        self.boot = patch.object(auth, "boot_id", return_value="boot-one")
        self.boot.start()
        self.addCleanup(self.boot.stop)

    def lease(self, **changes):
        data = dict(token="one", socket=str(self.runtime / "master"), destination="example",
                    boot="boot-one", started=time.time() - 1, expires=time.time() + 60,
                    deadline=time.monotonic() + 60)
        data.update(changes)
        auth.lease_path(self.runtime, "box", self.cfg).write_text(json.dumps(data))
        return data

    def test_locked_never_starts_ssh(self):
        with patch.object(auth.subprocess, "run") as run:
            with self.assertRaises(auth.Locked):
                auth.command(self.runtime, "box", self.cfg)
            run.assert_not_called()

    def test_expiry_reboot_and_clock_rollback_revoke(self):
        for changes in [dict(expires=time.time() - 1), dict(boot="old-boot"),
                        dict(deadline=time.monotonic() - 1), dict(started=time.time() + 60)]:
            with self.subTest(changes=changes):
                self.lease(**changes)
                with patch.object(auth, "control") as control:
                    with self.assertRaises(auth.Locked):
                        auth.connection(self.runtime, "box", self.cfg)
                    self.assertEqual(control.call_args.args[1], "exit")
                self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))

    def test_connection_loss_requires_unlock(self):
        self.lease()
        with patch.object(auth, "control", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(auth.Locked):
                auth.connection(self.runtime, "box", self.cfg)
        self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))

    def test_no_fallback_and_no_forwarding(self):
        self.lease()
        with patch.object(auth, "control", return_value=subprocess.CompletedProcess([], 0)):
            argv = auth.command(self.runtime, "box", self.cfg)
        for option in ["ProxyCommand=false", "PubkeyAuthentication=no", "PasswordAuthentication=no",
                       "KbdInteractiveAuthentication=no", "ForwardAgent=no", "ControlMaster=no"]:
            self.assertIn(option, argv)

    def test_changed_destination_cannot_reuse_lease(self):
        self.lease()
        with self.assertRaises(auth.Locked):
            auth.connection(self.runtime, "box", {"destination": "different"})

    def test_old_guard_cannot_revoke_new_unlock(self):
        self.lease(token="new")
        with patch.object(auth, "control") as control:
            auth.revoke(self.runtime, "box", self.cfg, "old")
            control.assert_not_called()
        self.assertEqual(auth.read_lease(self.runtime, "box", self.cfg)["token"], "new")

    def test_guard_closes_active_connection_at_deadline(self):
        self.lease(expires=time.time() - 1)
        with patch.object(auth, "control") as control:
            auth.guard(self.runtime, "box", self.cfg, "one")
            self.assertEqual(control.call_args.args[1], "exit")

    def unlock_as(self, method, status=0, spawn_error=False, partial_key=True):
        def ssh(argv, **kwargs):
            self.assertIn("BatchMode=no", argv)
            self.assertIn("StrictHostKeyChecking=ask", argv)
            Path(argv[argv.index("-E") + 1]).write_text(
                ('Authenticated using "publickey" with partial success.\n' if method == "password" and partial_key else '') +
                'Authenticated to example ([127.0.0.1]:22) using "' + method + '".\n')
            return subprocess.CompletedProcess(argv, status)
        with patch.object(auth.subprocess, "run", side_effect=ssh), \
             patch.object(auth, "control") as control, \
             patch.object(auth.subprocess, "Popen", side_effect=OSError("spawn failed") if spawn_error else None), \
             contextlib.redirect_stdout(io.StringIO()):
            if spawn_error:
                with self.assertRaises(OSError):
                    auth.unlock(self.runtime, "box", self.cfg, ["guard"])
                return control
            self.result = auth.unlock(self.runtime, "box", self.cfg, ["guard"])
            return control

    def test_password_login_gets_24_hours_without_storing_password(self):
        self.unlock_as("password")
        self.assertTrue(self.result)
        lease = auth.read_lease(self.runtime, "box", self.cfg)
        self.assertEqual(lease["expires"] - lease["started"], 86400)
        self.assertFalse(list(self.runtime.glob("auth-*")))
        self.assertEqual(auth.lease_path(self.runtime, "box", self.cfg).stat().st_mode & 0o777, 0o600)

    def test_key_only_login_rejected_and_master_closed(self):
        control = self.unlock_as("publickey")
        self.assertFalse(self.result)
        self.assertEqual(control.call_args.args[1], "exit")
        self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))

    def test_password_without_key_rejected(self):
        self.unlock_as("password", partial_key=False)
        self.assertFalse(self.result)
        self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))

    def test_failed_login_leaves_no_lease(self):
        self.unlock_as("password", status=255)
        self.assertFalse(self.result)
        self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))

    def test_guard_start_failure_revokes_master(self):
        control = self.unlock_as("password", spawn_error=True)
        self.assertEqual(control.call_args.args[1], "exit")
        self.assertFalse(auth.read_lease(self.runtime, "box", self.cfg))


if __name__ == "__main__":
    unittest.main()
