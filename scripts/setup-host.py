#!/usr/bin/env python3
"""Install and verify key-plus-account-password policy in an interactive terminal."""
import argparse
import contextlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", choices=list(remote.hosts()))
    args = parser.parse_args()
    cfg = remote.hosts()[args.host]
    remote.setup()
    options = ["ssh", "-o", "ForwardAgent=no", "-o", "ControlMaster=no", "-S", "none"]
    effective = subprocess.check_output(options + ["-G", cfg["destination"]], text=True)
    user = next(line.split(" ", 1)[1] for line in effective.splitlines() if line.startswith("user "))
    script = Path(__file__).with_name("configure-ssh.sh").read_text()
    # Hold one administrative recovery connection until a new password-approved
    # connection has been verified. It is never offered to the data plane.
    with tempfile.TemporaryDirectory(prefix="tmax-setup-", dir="/tmp") as directory:
        master = str(Path(directory) / "ssh")
        bootstrap = ["ssh", "-S", master, "-o", "ForwardAgent=no",
                     "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=ask"]
        print("Configuring " + cfg["destination"] + " for key + account password.", flush=True)
        result = subprocess.call(bootstrap + ["-M", "-fN", "-o", "ControlPersist=no", cfg["destination"]])
        if result:
            return result
        def close_master():
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(bootstrap + ["-O", "exit", "-o", "ProxyCommand=false", cfg["destination"]],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
        timer = threading.Timer(600, close_master)
        timer.daemon = True
        timer.start()
        try:
            command = ('umask 077; task_script=$(mktemp /tmp/tmax-ssh-setup.XXXXXX) || exit; '
                       'trap \'rm -f "$task_script"\' EXIT; printf %s ' + shlex.quote(script) +
                       ' > "$task_script"; sudo bash "$task_script" ' + shlex.quote(user))
            print("Enter the remote sudo password when asked.", flush=True)
            result = subprocess.call(bootstrap + ["-tt", "-o", "ProxyCommand=false", cfg["destination"], command])
            if result:
                return result
            # Require fresh authentication, even when rerunning setup on an
            # already unlocked host.
            remote.lock(args.host)
            print("Now verify a fresh connection with the remote account password.", flush=True)
            if remote.auth.unlock(remote.RUNTIME, args.host, cfg, remote.SELF + ["auth-guard"]):
                remote.refresh(args.host)
                print("Verified. This host is unlocked for 24 hours.", flush=True)
                return 0
            print("Fresh login failed. Opening the held recovery connection.", flush=True)
            print("Use the rollback command printed above, then exit the recovery shell.", flush=True)
            subprocess.call(bootstrap + ["-tt", "-o", "ProxyCommand=false", cfg["destination"]])
            return 1
        except KeyboardInterrupt:
            print("\nSetup interrupted. If policy was installed, its backup path was printed above.", flush=True)
            return 130
        finally:
            timer.cancel()
            close_master()


if __name__ == "__main__":
    sys.exit(main())
