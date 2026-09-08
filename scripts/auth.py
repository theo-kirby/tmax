"""Password-approved OpenSSH masters with a fixed, boot-bound lifetime.

Passwords are read only by OpenSSH from the terminal. A lease is a local
convenience boundary, not protection against compromise of the local account.
"""
import contextlib
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

LIFETIME = 24 * 60 * 60


class Locked(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def boot_id():
    if sys.platform == "darwin":
        return subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True).strip()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def identity(host, cfg):
    return hashlib.sha256((host + json.dumps(cfg, sort_keys=True)).encode()).hexdigest()[:16]


def lease_path(runtime, host, cfg):
    return runtime / (identity(host, cfg) + ".lease")


@contextlib.contextmanager
def mutex(runtime, host, cfg):
    with lease_path(runtime, host, cfg).with_suffix(".auth-lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_lease(runtime, host, cfg):
    try:
        return json.loads(lease_path(runtime, host, cfg).read_text())
    except (OSError, ValueError):
        return {}


def live(lease):
    try:
        return (lease["boot"] == boot_id()
                and lease["started"] <= time.time() < lease["expires"]
                and time.monotonic() < lease["deadline"])
    except (KeyError, TypeError):
        return False


def control(lease, operation):
    return subprocess.run(
        ["ssh", "-S", lease["socket"], "-O", operation,
         "-o", "ControlMaster=no", "-o", "ProxyCommand=false", lease["destination"]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=5)


def revoke(runtime, host, cfg, token=None):
    with mutex(runtime, host, cfg):
        lease = read_lease(runtime, host, cfg)
        if token is not None and lease.get("token") != token:
            return
        lease_path(runtime, host, cfg).unlink(missing_ok=True)
        if lease.get("socket") and lease.get("destination"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                control(lease, "exit")
            Path(lease["socket"]).unlink(missing_ok=True)


def connection(runtime, host, cfg):
    lease = read_lease(runtime, host, cfg)
    if not live(lease):
        if lease:
            revoke(runtime, host, cfg, lease.get("token"))
        raise Locked(host + " is locked; select its host heading in the popup to unlock")
    try:
        alive = control(lease, "check").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        alive = False
    if not alive:
        revoke(runtime, host, cfg, lease["token"])
        raise Locked(host + " disconnected; unlock it again in the popup")
    return lease


def command(runtime, host, cfg):
    lease = connection(runtime, host, cfg)
    # ssh otherwise falls back to a fresh connection when multiplexing fails.
    return ["ssh", "-T", "-S", lease["socket"],
            "-o", "ControlMaster=no", "-o", "ProxyCommand=false",
            "-o", "BatchMode=yes", "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no", "-o", "HostbasedAuthentication=no",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            cfg["destination"]]


def unlock(runtime, host, cfg, guard_command):
    with mutex(runtime, host, cfg):
        old = read_lease(runtime, host, cfg)
        if live(old):
            try:
                if control(old, "check").returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        if old.get("socket"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                control(old, "exit")
        lease_path(runtime, host, cfg).unlink(missing_ok=True)
        token = uuid.uuid4().hex
        socket_path = str(runtime / (token[:16] + ".auth"))
        lease = {"token": token, "socket": socket_path, "destination": cfg["destination"]}
        fd, log_name = tempfile.mkstemp(prefix="auth-", dir=runtime)
        os.close(fd)
        approved = False
        try:
            print("Unlock " + host + " (" + cfg["destination"] + ") for 24 hours.", flush=True)
            # -f forks only after authentication. Verbose output confirms that a
            # password was actually used; a key-only server must never grant a lease.
            argv = ["ssh", "-v", "-E", log_name, "-f", "-M", "-N", "-S", socket_path,
                    "-o", "ControlMaster=yes", "-o", "ControlPersist=no",
                    "-o", "BatchMode=no", "-o", "PreferredAuthentications=publickey,password",
                    "-o", "PubkeyAuthentication=yes", "-o", "PasswordAuthentication=yes",
                    "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=3",
                    "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
                    "-o", "StrictHostKeyChecking=ask", "-o", "ConnectTimeout=10",
                    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                    cfg["destination"]]
            result = subprocess.run(argv, env=dict(os.environ, LC_ALL="C", SSH_ASKPASS_REQUIRE="never"))
            log = Path(log_name).read_text(errors="replace")
            password_used = re.search(r'^Authenticated to .+ using "password"\.$', log, re.M)
            key_used = 'Authenticated using "publickey" with partial success.' in log
            if result.returncode or not password_used or not key_used:
                print("Unlock failed." if result.returncode else
                      "Host did not require both key and account password. Run setup-host.py for this host first.")
                return False
            now = time.time()
            lease.update(boot=boot_id(), started=now, expires=now + LIFETIME,
                         deadline=time.monotonic() + LIFETIME)
            path = lease_path(runtime, host, cfg)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(lease))
            temporary.chmod(0o600)
            temporary.replace(path)
            with (runtime / "remote.log").open("ab") as stream:
                subprocess.Popen(guard_command + [host, token], stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=stream, start_new_session=True)
            approved = True
            return True
        finally:
            Path(log_name).unlink(missing_ok=True)
            if not approved:
                lease_path(runtime, host, cfg).unlink(missing_ok=True)
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    control(lease, "exit")
                Path(socket_path).unlink(missing_ok=True)


def guard(runtime, host, cfg, token):
    while True:
        lease = read_lease(runtime, host, cfg)
        if lease.get("token") != token:
            return
        if not live(lease):
            revoke(runtime, host, cfg, token)
            return
        try:
            if control(lease, "check").returncode:
                revoke(runtime, host, cfg, token)
                return
        except (OSError, subprocess.TimeoutExpired):
            revoke(runtime, host, cfg, token)
            return
        time.sleep(min(1, max(0, lease["expires"] - time.time())))
