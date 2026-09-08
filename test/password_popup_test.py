"""Exercise the real fzf terminal flow with a fake SSH endpoint and test-only input."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
SOCKET = "tmax-password-" + uuid.uuid4().hex[:10]


def main():
    if not shutil.which("fzf"):
        raise RuntimeError("fzf required")
    fd = child = None
    with tempfile.TemporaryDirectory(prefix="tmax-password-") as directory:
        directory = Path(directory)
        fake = directory / "ssh"
        fake.write_text("#!" + sys.executable + '''
import os, pathlib, sys, termios
args = sys.argv[1:]
socket = pathlib.Path(args[args.index("-S") + 1])
if "-O" in args:
    if args[args.index("-O") + 1] == "exit":
        socket.unlink(missing_ok=True)
        sys.exit(0)
    sys.exit(0 if socket.exists() else 1)
if "-E" in args:
    terminal = os.open("/dev/tty", os.O_RDWR)
    try:
        saved = termios.tcgetattr(terminal)
        hidden = termios.tcgetattr(terminal)
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(terminal, termios.TCSANOW, hidden)
        try:
            os.write(terminal, b"Fixture account password: ")
            password = os.read(terminal, 1024).decode().strip()
        except KeyboardInterrupt:
            sys.exit(130)
        finally:
            termios.tcsetattr(terminal, termios.TCSANOW, saved)
    finally:
        os.close(terminal)
    if password != "test-only-input":
        sys.exit(255)
    pathlib.Path(args[args.index("-E") + 1]).write_text(
        'Authenticated using "publickey" with partial success.\\n'
        'Authenticated to fixture ([127.0.0.1]:22) using "password".\\n')
    socket.touch()
    sys.exit(0)
# Discovery on the fake host finds no remote tmux sessions.
sys.stderr.write("no sessions\\n")
sys.exit(1)
''')
        fake.chmod(0o700)
        config = directory / "hosts.json"
        config.write_text('{"fixture":{"destination":"fixture","label":"fixture host"}}')
        env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                   TMAX_STATE_DIR=str(directory), TMAX_REMOTES_FILE=str(config))
        output = bytearray()

        def tmux(*args):
            return subprocess.check_output(["tmux", "-L", SOCKET, *args], env=env, text=True).strip()

        def wait_for(predicate, label):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                while select.select([fd], [], [], 0)[0]:
                    output.extend(os.read(fd, 65536))
                if predicate():
                    print("PASS:", label, flush=True)
                    return
                time.sleep(0.1)
            raise AssertionError(label + ": " + repr(bytes(output[-1000:])))

        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "local", "-x", "120", "-y", "40")
            for name in ["PATH", "TMAX_STATE_DIR", "TMAX_REMOTES_FILE"]:
                tmux("set-environment", "-g", name, env[name])
            tmux("run-shell", str(ROOT / "tmax.tmux"))
            env["TMUX"] = tmux("display-message", "-p", "#{socket_path},#{pid},0")
            child, fd = pty.fork()
            if child == 0:
                os.environ.update(env, TERM="xterm-256color")
                os.execvp("tmux", ["tmux", "-L", SOCKET, "attach", "-t", "local"])
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            time.sleep(0.5)
            os.write(fd, b"\x02 ")
            wait_for(lambda: b"locked" in output, "locked host appears in popup")
            os.write(fd, b"jj\r")
            wait_for(lambda: b"Fixture account password:" in output, "Enter opens interactive password prompt")
            redraws = output.count(b"normal>")
            os.write(fd, b"test-only-input\r")
            # Read the lease using the same environment as the popup.
            lease_code = ("import sys;sys.path.insert(0," + repr(str(ROOT / "scripts")) +
                          ");import remote;print(remote.auth.lease_path(remote.RUNTIME,'fixture',remote.hosts()['fixture']))")
            lease = Path(subprocess.check_output([sys.executable, "-c", lease_code], env=env, text=True).strip())
            wait_for(lease.exists, "password creates an approved lease")
            wait_for(lambda: output.count(b"normal>") > redraws, "unlocked host returns to popup")
            time.sleep(0.5)  # let the session reload complete before sending a key
            assert b"test-only-input" not in output, "password must not echo"
            os.write(fd, b"L")
            wait_for(lambda: not lease.exists(), "L revokes the host connection")
            time.sleep(0.5)  # let fzf finish the lock command and reload
            count = output.count(b"Fixture account password:")
            os.write(fd, b"G\r")
            wait_for(lambda: output.count(b"Fixture account password:") > count, "reopening a locked host asks again")
            redraws = output.count(b"normal>")
            os.write(fd, b"\x03")
            wait_for(lambda: output.count(b"normal>") > redraws, "cancelled password returns to popup")
            assert not lease.exists()
            os.write(fd, b"q")
            assert tmux("list-sessions", "-F", "#{session_name}") == "local"
            print("PASS: cancellation leaves host locked and local work intact", flush=True)
        finally:
            if "TMUX" in env:
                subprocess.run([sys.executable, str(ROOT / "scripts/remote.py"), "lock", "fixture"], env=env, capture_output=True)
            subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)
            if fd is not None:
                os.close(fd)
            if child:
                os.waitpid(child, 0)


if __name__ == "__main__":
    main()
