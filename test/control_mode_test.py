"""Isolated feasibility probe: python3 test/control_mode_test.py.

Uses a fresh tmux socket, never the user's server. This tests the protocol,
not a completed terminal bridge or sidebar integration.
"""
import argparse
import os
import select
import shlex
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="SSH destination; omit to test locally")
    parser.add_argument("--control-path", help="existing authenticated SSH master socket")
    parser.add_argument("--tmux", default="tmux", help="tmux executable on the target")
    args = parser.parse_args()
    if args.control_path and not args.host:
        parser.error("--control-path requires --host")
    socket = "tmax-probe-" + uuid.uuid4().hex

    def invocation(*command_args):
        command = [args.tmux, "-L", socket, *command_args]
        if not args.host:
            return command
        ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if args.control_path:
            # If the master is gone, fail instead of starting another connection.
            ssh += ["-S", args.control_path, "-o", "ProxyCommand=false"]
        return ssh + [args.host, shlex.join(command)]
    client = None
    pending = b""

    def until(predicate):
        nonlocal pending
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if b"\n" not in pending:
                if not select.select([client.stdout], [], [], 0.1)[0]:
                    continue
                chunk = os.read(client.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError("control client exited early")
                pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.startswith(b"%error"):
                    raise AssertionError(line)
                if predicate(line):
                    return line
        raise AssertionError("timed out waiting for control response")

    def command(text):
        client.stdin.write(text.encode() + b"\n")
        client.stdin.flush()

    try:
        subprocess.run(invocation("-f", "/dev/null", "new-session", "-d",
                                  "-s", "probe", "stty raw -echo; printf READY; exec cat"),
                       check=True, timeout=15)
        pane = subprocess.check_output(invocation("list-panes", "-F", "#{pane_id}"), timeout=15).decode().strip()
        # Wait for raw mode before sending the byte under test.
        deadline = time.monotonic() + 5
        while b"READY" not in subprocess.check_output(invocation("capture-pane", "-p"), timeout=15):
            assert time.monotonic() < deadline, "test pane did not start"
            time.sleep(0.05)
        client = subprocess.Popen(invocation("-C", "attach-session", "-t", "probe"),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        until(lambda line: line.startswith(b"%session-changed"))
        command("send-keys -H -t " + pane + " 02 54 4d 41 58")
        output = b""
        def echoed(line):
            nonlocal output
            if line.startswith(b"%output " + pane.encode() + b" "):
                output += line.split(b" ", 2)[2]
            return b"\\002TMAX" in output
        until(echoed)
        print("PASS: Ctrl-B reached the application as a byte; no nested prefix interception")
        command("split-window -d -t " + pane + " 'sleep 30'")
        until(lambda line: line.startswith(b"%end"))
        deadline = time.monotonic() + 5
        while len(subprocess.check_output(invocation("list-panes", "-F", "#{pane_id}"), timeout=15).splitlines()) != 2:
            assert time.monotonic() < deadline, "split was not applied"
            time.sleep(0.05)
        print("PASS: control command created a second pane")
        command("detach-client")
        client.wait(timeout=5)
        subprocess.run(invocation("has-session", "-t", "probe"), check=True, timeout=15)
        print("PASS: session survived control-client disconnect")
    finally:
        if client is not None and client.poll() is None:
            client.terminate()
            client.wait(timeout=5)
        cleanup = subprocess.run(invocation("kill-server"), capture_output=True, timeout=15)
        if cleanup.returncode:
            print("Cleanup failed for isolated socket " + socket + ": " + cleanup.stderr.decode(errors="replace"))


if __name__ == "__main__":
    main()
