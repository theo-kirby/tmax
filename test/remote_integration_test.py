"""Exercise an explicitly configured SSH host using isolated tmux servers."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import tempfile
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="SSH destination or alias for the test host")
    parser.add_argument("--master", default="", help="optional existing SSH control socket")
    parser.add_argument("--tmux", default="tmux", help="tmux executable on the test host")
    parser.add_argument("--label", default="remote", help="host label used in the test UI")
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    proxy = "tmax-" + args.label + "-0"
    second_proxy = "tmax-" + args.label + "-1"
    socket = "tmax-integration-" + uuid.uuid4().hex[:10]
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if args.master:
        ssh += ["-S", args.master, "-o", "ProxyCommand=false"]
    ssh += [args.host]
    remote_base = [args.tmux, "-L", socket]
    fd = None
    child = None
    with tempfile.TemporaryDirectory(prefix="tmax-integration-") as directory:
        config = Path(directory) / "hosts.json"
        config.write_text(json.dumps({args.label: {"destination": args.host, "control_path": args.master,
                                               "tmux": remote_base[0], "socket": socket}}))

        def remote(*cmd):
            return subprocess.check_output(ssh + [shlex.join(remote_base + list(cmd))], timeout=15).decode().strip()

        def tmux(*cmd):
            return subprocess.check_output(["tmux", "-L", socket, *cmd], timeout=15).decode().strip()

        terminal_output = bytearray()

        def active_session():
            return tmux("list-clients", "-F", "#{session_id}").splitlines()[0]

        def drain():
            while select.select([fd], [], [], 0)[0]:
                terminal_output.extend(os.read(fd, 65536))

        def wait_for(predicate, label):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                drain()
                if predicate():
                    print("PASS:", label, flush=True)
                    return
                time.sleep(0.2)
            raise AssertionError(label)

        def send(data):
            os.write(fd, data)

        try:
            remote("-f", "/dev/null", "new-session", "-d", "-s", "remote-test")
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "local-test", "-x", "120", "-y", "40")
            tmux("set-environment", "-g", "TMAX_STATE_DIR", directory)
            tmux("set-environment", "-g", "TMAX_REMOTES_FILE", str(config))
            tmux("set-option", "-g", "prefix", "C-a")
            tmux("set-option", "-g", "@tmax-sidebar-overview", "off")
            if args.native:
                tmux("set-option", "-g", "@tmax-sidebar", "off")
                tmux("set-hook", "-g", "client-session-changed[0]", "set-option -g @unrelated-hook preserved")
            tmux("run-shell", str(ROOT / "tmax.tmux"))
            child, fd = pty.fork()
            if child == 0:
                os.environ["TERM"] = "xterm-256color"
                os.execvp("tmux", ["tmux", "-L", socket, "attach", "-t", "local-test"])
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            time.sleep(0.5)
            send(b"\x01s")
            def side():
                return tmux("show-options", "-gqv", "@tmax-sidebar-pane")
            if args.native:
                wait_for(lambda: "tree-mode" in tmux("display-message", "-p", "-t", active_session(), "#{pane_mode}"), "native session tree opens")
                assert not side()
                wait_for(lambda: b"remote-test" in terminal_output, "native tree renders remote session name")
                assert tmux("show-options", "-gqv", "prefix") == "C-a"
                send(b"\x1b[B\r")
            else:
                wait_for(lambda: bool(side()), "local prefix opens sidebar")
                wait_for(lambda: "remote-test" in tmux("capture-pane", "-p", "-t", side()), "host dropdown lists remote session")
                assert "local" in tmux("capture-pane", "-p", "-t", side())
                send(b"j ")
                wait_for(lambda: "remote-test" not in tmux("capture-pane", "-p", "-t", side()), "host heading folds")
                send(b" ")
                wait_for(lambda: "remote-test" in tmux("capture-pane", "-p", "-t", side()), "host heading unfolds")
                send(b"j\r")
            wait_for(lambda: proxy in tmux("list-clients", "-F", "#{session_name}"), "sidebar selects remote session")
            wait_for(lambda: "%0" in tmux("list-panes", "-s", "-t", proxy, "-F", "#{@tmax-remote-pane}"), "remote pane mirrored locally")
            time.sleep(2)
            send(b"printf 'BRIDGE_%s\\n' READY\r")
            wait_for(lambda: "BRIDGE_READY" in remote("capture-pane", "-p", "-t", "%0"), "typed input executes on the remote host")
            wait_for(lambda: "BRIDGE_READY" in tmux("capture-pane", "-p", "-t", proxy), "remote output renders locally")
            send(b"\x01c")
            wait_for(lambda: len(remote("list-windows").splitlines()) == 2, "local prefix+c creates window on the remote host")
            wait_for(lambda: len(tmux("list-windows", "-t", proxy).splitlines()) == 2, "new remote window mirrored")
            send(b"\x01%")
            wait_for(lambda: len(remote("list-panes", "-s").splitlines()) == 3, "local prefix+% splits on the remote host")
            wait_for(lambda: len(tmux("list-panes", "-s", "-t", proxy).splitlines()) == 3, "remote split mirrored")
            send(b"\x01z")
            wait_for(lambda: remote("display-message", "-p", "-t", "%2", "#{window_zoomed_flag}") == "1", "zoom applies remotely")
            wait_for(lambda: tmux("display-message", "-p", "-t", proxy, "#{window_zoomed_flag}") == "1", "zoom mirrored locally")
            send(b"\x01z")
            wait_for(lambda: remote("display-message", "-p", "-t", "%2", "#{window_zoomed_flag}") == "0", "remote unzoom")
            wait_for(lambda: tmux("display-message", "-p", "-t", proxy, "#{window_zoomed_flag}") == "0", "local unzoom")
            if args.native:
                send(b"\x01s")
                wait_for(lambda: "tree-mode" in tmux("display-message", "-p", "-t", active_session(), "#{pane_mode}"), "native tree opens from remote work")
                assert not side()
                send(b"q")
                wait_for(lambda: not tmux("display-message", "-p", "-t", active_session(), "#{pane_mode}"), "native tree closes")
            else:
                send(b"\x01s")
                wait_for(lambda: bool(side()), "local sidebar opens from remote pane without nested prefix")
                send(b"q")
                wait_for(lambda: not side(), "sidebar closes without closing remote work")
                assert len(remote("list-panes", "-s").splitlines()) == 3
            time.sleep(1)
            send(b"printf 'SPLIT_%s\\n' READY\r")
            wait_for(lambda: "SPLIT_READY" in remote("capture-pane", "-p", "-t", "%2"), "new split accepts input on the remote host")
            # Paused output must not block a hidden remote application.
            tmux("switch-client", "-t", "local-test")
            time.sleep(2)
            remote("send-keys", "-t", "%2", "printf 'HIDDEN_%s\\n' DONE", "Enter")
            wait_for(lambda: "HIDDEN_DONE" in remote("capture-pane", "-p", "-t", "%2"), "hidden remote application keeps running")
            lp = next(line.split()[0] for line in tmux("list-panes", "-s", "-t", proxy, "-F", "#{pane_id} #{@tmax-remote-pane}").splitlines() if line.endswith(" %2"))
            assert "HIDDEN_DONE" not in tmux("capture-pane", "-p", "-t", lp)
            tmux("switch-client", "-t", proxy)
            wait_for(lambda: "HIDDEN_DONE" in tmux("capture-pane", "-p", "-t", lp), "returning to remote restores current screen")
            # Detach only clients of the isolated remote test server.
            for client in remote("list-clients", "-F", "#{client_name}").splitlines():
                remote("detach-client", "-t", client)
            wait_for(lambda: "reconnecting" in tmux("capture-pane", "-p", "-t", lp), "disconnect is visible")
            time.sleep(6)
            send(b"printf 'RECONNECTED_%s\\n' OK\r")
            wait_for(lambda: "RECONNECTED_OK" in remote("capture-pane", "-p", "-t", "%2"), "bridge reconnects and accepts input")
            send(b"\x01x")
            time.sleep(0.3)
            send(b"n")
            time.sleep(0.3)
            assert len(remote("list-panes", "-s").splitlines()) == 3
            send(b"\x01x")
            time.sleep(0.3)
            send(b"y")
            wait_for(lambda: len(remote("list-panes", "-s").splitlines()) == 2, "confirmed kill applies on the remote host")
            wait_for(lambda: len(tmux("list-panes", "-s", "-t", proxy).splitlines()) == 2, "deleted pane removed locally")
            # Exercise an alternate-screen editor, including hiding and restoring it.
            send(b"vim -Nu NONE -n\r")
            wait_for(lambda: remote("display-message", "-p", "-t", "%1", "#{alternate_on}") == "1", "Vim enters alternate screen")
            send(b"iEDITOR_BRIDGE\x1b")
            wait_for(lambda: "EDITOR_BRIDGE" in remote("capture-pane", "-p", "-t", "%1"), "editor accepts input")
            tmux("switch-client", "-t", "local-test")
            time.sleep(2)
            tmux("switch-client", "-t", proxy)
            wait_for(lambda: "EDITOR_BRIDGE" in tmux("capture-pane", "-p", "-t", proxy), "editor screen survives hiding and restoring")
            send(b":q!\r")
            wait_for(lambda: remote("display-message", "-p", "-t", "%1", "#{alternate_on}") == "0", "editor exits back to shell")
            if args.native:
                assert tmux("show-options", "-gqv", "@unrelated-hook") == "preserved"
                tmux("run-shell", str(ROOT / "tmax.tmux"))
                assert not side()
                assert tmux("show-options", "-gqv", "prefix") == "C-a"
                remote("kill-session", "-t", "$0")
                wait_for(lambda: proxy not in tmux("list-sessions", "-F", "#{session_name}"), "native mode cleans up ended remote session")
                print("PASS: native mode preserves prefix, unrelated hooks, and reload behavior", flush=True)
                return
            # Sidebar session operations use subprocess arguments, including unusual names.
            send(b"\x01s")
            wait_for(lambda: bool(side()), "sidebar reopens for session actions")
            send(b"r")
            time.sleep(0.3)
            send(b"renamed remote\r")
            wait_for(lambda: remote("display-message", "-p", "-t", "$0", "#{session_name}") == "renamed remote", "sidebar renames remote session")
            send(b"n")
            time.sleep(0.3)
            send(b"new-remote\r")
            wait_for(lambda: len(remote("list-sessions").splitlines()) == 2, "sidebar creates remote session")
            wait_for(lambda: second_proxy in tmux("list-clients", "-F", "#{session_name}"), "new remote session selected")
            send(b"\x01s")
            wait_for(lambda: bool(side()) and tmux("display-message", "-p", "#{pane_id}") == side(), "sidebar remains focused during new-session setup")
            send(b"d")
            time.sleep(0.3)
            send(b"y\r")
            wait_for(lambda: len(remote("list-sessions").splitlines()) == 1, "sidebar confirms and kills remote session")
            wait_for(lambda: "local-test" in tmux("list-clients", "-F", "#{session_name}"), "killing current remote session returns to local")
            remote("kill-session", "-t", "$0")
            wait_for(lambda: proxy not in tmux("list-sessions", "-F", "#{session_name}"), "externally ended remote session removes its local proxies")
        except Exception:
            if args.native: print("TERMINAL:", bytes(terminal_output[-4000:]))
            print("BIND C:", tmux("list-keys", "-T", "prefix", "c"))
            print("MESSAGES:", "\n".join(line for line in tmux("show-messages").splitlines() if any(word in line for word in ["error", "tmax:", "run-shell -b"]))[-4000:])
            error = Path(directory) / "remote-attach-error"
            if error.exists():
                print("ATTACH ERROR:", error.read_text())
            print("LOCAL PANES:", tmux("list-panes", "-a", "-F", "#{pane_id} #{session_name} #{window_name} #{@tmax-remote-pane}"))
            for pane in tmux("list-panes", "-a", "-F", "#{pane_id}").splitlines():
                print(pane, tmux("capture-pane", "-p", "-t", pane))
            raise
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
            subprocess.run(ssh + [shlex.join(remote_base + ["kill-server"])], capture_output=True, timeout=15)
            if fd is not None:
                os.close(fd)
            if child:
                os.waitpid(child, 0)


if __name__ == "__main__":
    main()
