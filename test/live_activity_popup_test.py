"""Verify live activity dot changes in a real tmux/fzf popup."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
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
SOCKET = "tmax-live-dots-" + uuid.uuid4().hex[:8]
fd = child = None


def main():
    global fd, child
    with tempfile.TemporaryDirectory(prefix="tmax-live-dots-") as directory:
        directory = Path(directory)
        binary = directory / "codex"
        binary.symlink_to("/bin/sleep")
        config = directory / "hosts.json"
        config.write_text("{}")
        env = dict(os.environ, TMAX_STATE_DIR=str(directory), TMAX_REMOTES_FILE=str(config))

        def tmux(*args):
            return subprocess.check_output(["tmux", "-L", SOCKET, *args], env=env, text=True).strip()

        output = bytearray()

        def has_text_colour(data, text, foreground, background=None):
            fg = bg = None
            for part in re.split(rb"(\x1b\[[0-9;]*m)", bytes(data)):
                if part.startswith(b"\x1b[") and part.endswith(b"m"):
                    codes = [int(code or 0) for code in part[2:-1].split(b";")]
                    i = 0
                    while i < len(codes):
                        code = codes[i]
                        if code == 0:
                            fg = bg = None
                        elif 30 <= code <= 37 or 90 <= code <= 97:
                            fg = code - (30 if code < 90 else 82)
                        elif 40 <= code <= 47 or 100 <= code <= 107:
                            bg = code - (40 if code < 100 else 92)
                        elif code in (38, 48) and i + 2 < len(codes):
                            value = codes[i + 2] if codes[i + 1] == 5 else tuple(codes[i + 2:i + 5])
                            if code == 38:
                                fg = value
                            else:
                                bg = value
                            i += 2 if codes[i + 1] == 5 else 4
                        i += 1
                elif text in part and fg == foreground and (background is None or bg == background):
                    return True
            return False

        def wait_for(predicate, label):
            until = time.monotonic() + 12
            while time.monotonic() < until:
                while select.select([fd], [], [], 0)[0]:
                    output.extend(os.read(fd, 65536))
                if predicate():
                    print("PASS:", label, flush=True)
                    return
                time.sleep(0.1)
            code = "import sys;sys.path.insert(0," + repr(str(ROOT / "scripts")) + ");import remote;print(remote.RUNTIME)"
            runtime = Path(subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip())
            for snapshot in runtime.glob("switch-*.txt"):
                print("SNAPSHOT:", snapshot.read_text())
                result = subprocess.run([sys.executable, str(ROOT / "scripts/remote.py"), "switch-refresh", str(snapshot)], env=env, text=True, capture_output=True)
                print("REFRESH:", result.returncode, result.stdout, result.stderr)
            print("SCREEN:", re.sub(rb"\s+", b" ", re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output)))[-3000:])
            print("COLOURS:", sorted(set(re.findall(rb"\x1b\[[0-9;]+m", output))))
            print("ROWS:", subprocess.check_output([sys.executable, str(ROOT / "scripts/remote.py"), "switch-list"], env=env, text=True))
            raise AssertionError(label + " " + repr(bytes(output[-500:])))

        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "120", "-y", "40")
            tmux("set-option", "-as", "terminal-features", ",xterm-256color:RGB")
            for key in ["TMAX_STATE_DIR", "TMAX_REMOTES_FILE"]:
                tmux("set-environment", "-g", key, env[key])
            pane = tmux("new-window", "-d", "-t", "work:", "-P", "-F", "#{pane_id}", shlex.join([str(binary), "90"]))
            env["TMUX"] = tmux("display-message", "-p", "#{socket_path},#{pid},0")
            pid = tmux("display-message", "-p", "-t", pane, "#{pane_pid}")

            def event(name):
                subprocess.run([sys.executable, str(ROOT / "scripts/agent_status.py"), "hook", "codex", name],
                               env=dict(env, TMUX_PANE=pane, TMAX_AGENT_PID=pid), check=True)

            time.sleep(0.2)
            event("UserPromptSubmit")
            tmux("run-shell", str(ROOT / "tmax.tmux"))
            tmux("set-option", "-g", "status-style", "bg=green,fg=black")
            child, fd = pty.fork()
            if child == 0:
                os.environ.update(env, TERM="xterm-256color")
                os.execvp("tmux", ["tmux", "-L", SOCKET, "attach", "-t", "work"])
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            time.sleep(0.5)
            os.write(fd, b"\x02 ")
            wait_for(lambda: b"normal>" in output, "popup opens")
            wait_for(lambda: has_text_colour(output, b"local", 4), "unselected host badge retains its colour")
            mark = len(output)
            os.write(fd, b"j")
            wait_for(lambda: has_text_colour(output[mark:], b"local", 0, 2),
                     "selected session host badge is black on green")
            # tmux may render yellow as a basic or indexed ANSI colour.
            mark = len(output)
            event("Stop")
            wait_for(lambda: b"[93m" in output[mark:] or b"38;5;11m" in output[mark:],
                     "open popup updates waiting dots to yellow")
            # Allow an unchanged polling round, then change again.
            time.sleep(2)
            mark = len(output)
            event("UserPromptSubmit")
            wait_for(lambda: b"[92m" in output[mark:] or b"38;5;10m" in output[mark:],
                     "polling survives unchanged rounds and updates working dots")
            os.write(fd, b"q")
        finally:
            subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)
            if fd is not None:
                os.close(fd)
            if child:
                os.waitpid(child, 0)


if __name__ == "__main__":
    main()
