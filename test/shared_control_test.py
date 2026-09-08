"""Many mirrored windows share one SSH channel. Uses only isolated local servers."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent


def main():
    token = uuid.uuid4().hex[:8]
    local_socket, remote_socket = "tmax-share-local-" + token, "tmax-share-remote-" + token
    child = fd = None
    with tempfile.TemporaryDirectory(prefix="tmax-shared-control-") as directory:
        directory = Path(directory)
        fake = directory / "ssh"
        fake.write_text("#!" + sys.executable + "\n" + '''
import os, shlex, sys
if "-O" in sys.argv:
    sys.exit(0)
command = shlex.split(sys.argv[-1])
if "-C" in command:
    with open(os.environ["TMAX_TEST_SSH_LOG"], "a") as log:
        log.write("control\\n")
os.execvp(command[0], command)
''')
        fake.chmod(0o700)
        config = directory / "hosts.json"
        config.write_text(json.dumps({"fixture": {"destination": "fixture", "socket": remote_socket}}))
        os.environ.update(TMAX_STATE_DIR=str(directory), TMAX_REMOTES_FILE=str(config),
                          TMAX_TEST_SSH_LOG=str(directory / "channels"), PATH=str(directory) + os.pathsep + os.environ["PATH"])

        def tmux(socket, *args):
            return subprocess.check_output(["tmux", "-L", socket, *args], text=True).strip()

        def wait_for(predicate, label):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if fd is not None:
                    while select.select([fd], [], [], 0)[0]:
                        os.read(fd, 65536)
                if predicate():
                    print("PASS:", label, flush=True)
                    return
                time.sleep(0.1)
            raise AssertionError(label)

        try:
            tmux(local_socket, "-f", "/dev/null", "new-session", "-d", "-s", "local", "-x", "120", "-y", "40")
            tmux(remote_socket, "-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "120", "-y", "40")
            for _ in range(11):
                tmux(remote_socket, "new-window", "-d", "-t", "work:")
            os.environ["TMUX"] = tmux(local_socket, "display-message", "-p", "#{socket_path},#{pid},0")
            for name in ["TMAX_STATE_DIR", "TMAX_REMOTES_FILE", "TMAX_TEST_SSH_LOG", "PATH"]:
                tmux(local_socket, "set-environment", "-g", name, os.environ[name])
            sys.path.insert(0, str(ROOT / "scripts"))
            import remote
            remote.setup()
            lease = dict(boot=remote.auth.boot_id(), started=time.time(), expires=time.time() + 120,
                         deadline=time.monotonic() + 120, socket=str(directory / "master"), destination="fixture", token=token)
            remote.auth.lease_path(remote.RUNTIME, "fixture", remote.hosts()["fixture"]).write_text(json.dumps(lease))
            tmux(local_socket, "run-shell", str(ROOT / "tmax.tmux"))
            remote.attach("fixture", "$0", quiet=True)
            proxy = "fixture/work"
            child, fd = pty.fork()
            if child == 0:
                os.environ["TERM"] = "xterm-256color"
                os.execvp("tmux", ["tmux", "-L", local_socket, "attach", "-t", proxy])
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            wait_for(lambda: len(list(remote.RUNTIME.glob("pane-*.sock"))) == 12, "twelve mirrored panes start")
            wait_for(lambda: len(tmux(remote_socket, "list-clients").splitlines()) == 1, "twelve panes share one remote control client")
            # Let the view processes finish subscribing before checking the key.
            time.sleep(2)
            os.write(fd, b"\x02c")
            wait_for(lambda: len(tmux(remote_socket, "list-windows", "-t", "work").splitlines()) == 13,
                     "prefix+c creates a remote window beyond the old ten-channel limit")
            wait_for(lambda: len(tmux(local_socket, "list-windows", "-t", proxy).splitlines()) == 13,
                     "new remote window is mirrored")
            assert (directory / "channels").read_text().splitlines() == ["control"]
            wait_for(lambda: len(list(remote.RUNTIME.glob("pane-*.sock"))) == 13, "new pane uses shared connection")
            pane = tmux(remote_socket, "display-message", "-p", "-t", "work:0", "#{pane_id}")
            local_pane = next(line.split()[0] for line in tmux(local_socket, "list-panes", "-s", "-t", proxy,
                "-F", "#{pane_id} #{@tmax-remote-pane}").splitlines() if line.endswith(" " + pane))
            tmux(remote_socket, "send-keys", "-t", pane, "printf 'SHARED_%s\\n' OUTPUT", "Enter")
            wait_for(lambda: "SHARED_OUTPUT" in tmux(local_socket, "capture-pane", "-p", "-t", local_pane),
                     "hidden pane output stays synchronized")
            client = remote.Control("fixture", "$0", lambda *_: None)
            try:
                try:
                    client.call("select-window", "-t", "$0:987654")
                    raise AssertionError("invalid command should fail")
                except RuntimeError:
                    pass
                assert client.call("display-message", "-p", "#{session_name}") == b"work"
            finally:
                client.close()
            assert len(tmux(remote_socket, "list-clients").splitlines()) == 1
            print("PASS: command errors and a subscriber disconnect preserve the shared client", flush=True)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
            wait_for(lambda: tmux(remote_socket, "display-message", "-p", "-t", "work:", "#{window_width}") == "100",
                     "visible pane sizes the shared client while hidden panes stay connected")
            remote.auth.lease_path(remote.RUNTIME, "fixture", remote.hosts()["fixture"]).unlink()
            wait_for(lambda: not tmux(remote_socket, "list-clients"), "locking disconnects the shared client")
            assert len(tmux(remote_socket, "list-windows", "-t", "work").splitlines()) == 13
            print("PASS: remote windows survive locking", flush=True)
            print("all passed", flush=True)
        finally:
            if child:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, 15)
                os.waitpid(child, 0)
            if fd is not None:
                os.close(fd)
            for socket in [local_socket, remote_socket]:
                subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


if __name__ == "__main__":
    main()
