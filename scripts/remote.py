#!/usr/bin/env python3
"""SSH discovery and control-mode proxy panes. Requires Python 3.9+ locally."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import termios
import time
import tty

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("TMAX_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tmax"))
CONFIG = Path(os.environ.get("TMAX_REMOTES_FILE", ROOT / "remotes.json"))
IDENTITY = hashlib.sha256((str(STATE) + ",".join(os.environ.get("TMUX", "").split(",")[:2])).encode()).hexdigest()[:12]
# macOS TMPDIR paths leave too little room for OpenSSH's temporary socket suffix.
RUNTIME = Path("/tmp") / ("tmax-" + str(os.getuid()) + "-" + IDENTITY)
SELF = [sys.executable, str(Path(__file__).resolve())]


def setup():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe runtime directory")
    RUNTIME.chmod(0o700)


def hosts():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def local(*args, check=True):
    result = subprocess.run(["tmux", *args], capture_output=True, timeout=10)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout.decode(errors="replace").rstrip("\n")


def ssh(host, *args):
    cfg = hosts()[host]
    master = cfg.get("control_path")
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    if master:
        command += ["-S", os.path.expanduser(master), "-o", "ProxyCommand=false"]
    else:
        command += ["-o", "ControlMaster=auto", "-o", "ControlPersist=120",
                    "-o", "ControlPath=" + str(RUNTIME / (key(host) + ".ssh"))]
    remote = [cfg.get("tmux", "tmux")]
    if cfg.get("socket"):
        remote += ["-L", cfg["socket"]]
    return command + [cfg["destination"], shlex.join(remote + list(args))]


def fetch(host, *args):
    result = subprocess.run(ssh(host, *args), capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout.decode(errors="replace").rstrip("\n")


def spawn(*args):
    with (RUNTIME / "remote.log").open("ab") as log:
        subprocess.Popen(SELF + list(args), stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True)


def clean(value):
    return "".join(c if c.isprintable() else " " for c in value)


def missing_session(message):
    return any(part in message for part in ["no server running", "no sessions", "can't find session"]) or (
        "error connecting to " in message and "No such file or directory" in message)


def control_quote(value):
    # tmux has its own lexer: shell-safe tokens such as %0:pause still need quoting.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def refresh(host):
    path = RUNTIME / (key(host) + ".json")
    with (RUNTIME / (key(host) + ".lock")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        data = {"time": time.time(), "status": "online", "sessions": []}
        try:
            # A host that runs tmax itself has proxy sessions for its own remotes
            # (possibly this machine). Skip them so sessions are not listed twice.
            listing = fetch(host, "list-sessions", "-F",
                            "#{session_id}\t#{session_windows}\t#{@tmax-remote-host}\t#{session_name}")
            for line in listing.splitlines():
                sid, count, proxied, name = line.split("\t", 3)
                if proxied:
                    continue
                if re.fullmatch(r"\$\d+", sid):
                    data["sessions"].append([sid, clean(name), int(count)])
        except Exception as exc:
            message = str(exc)
            if missing_session(message):
                pass
            else:
                data["status"] = "auth required" if any(s in message for s in ["Permission denied", "Host key verification", "ProxyCommand"]) else "offline"
                data["error"] = message
                if path.exists():
                    with contextlib.suppress(ValueError, OSError):
                        data["sessions"] = json.loads(path.read_text())["sessions"]
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data))
        temp.replace(path)


def rows():
    for host in hosts():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", host):
            continue
        path = RUNTIME / (key(host) + ".json")
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {"time": 0, "status": "connecting", "sessions": []}
        if time.time() - data["time"] > (10 if data["status"] == "online" else 30):
            spawn("refresh", host)
        print("H\t" + host + "\t" + data["status"] + "\t")
        for sid, name, count in data["sessions"]:
            print("R\t" + name + "\t" + str(count) + "w\t" + sid)


def proxy_key(host, sid):
    """Stable identifier for lock files. The local session is named host/title."""
    return "tmax-" + host + "-" + sid.lstrip("$")


def display_name(host, title):
    # tmux itself replaces ':' and '.' in session names.
    return host + "/" + re.sub(r"[:.]", "_", title)


def sessions():
    """Local sessions as (name, remote host, remote session ID); the last two are empty for local ones."""
    listing = local("list-sessions", "-F", "#{session_name}\t#{@tmax-remote-host}\t#{@tmax-remote-session}", check=False)
    return [tuple(line.split("\t", 2)) for line in listing.splitlines()]


def proxy_session(host, sid):
    """Name of the local session representing a remote session, or None."""
    return next((name for name, owner, remote in sessions() if owner == host and remote == sid), None)


def prepare(host, sid, epoch, title):
    """Register a cheap native-tree entry without connecting any remote panes."""
    if not re.fullmatch(r"\$\d+", sid):
        raise ValueError("invalid session ID")
    wanted = display_name(host, title)
    with (RUNTIME / (key(proxy_key(host, sid)) + ".attach")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = sessions()
        name = next((n for n, owner, remote in existing if owner == host and remote == sid), None)
        taken = any(n == wanted for n, _, _ in existing)
        if name is None:
            if taken:
                raise RuntimeError("local session " + wanted + " already exists")
            name = wanted
            placeholder = local("new-session", "-d", "-s", name, "-n", "connect", "-P", "-F", "#{window_id}",
                                "printf 'Select this session to connect to remote tmux.\\n'; sleep 86400")
            local("move-window", "-s", placeholder, "-t", name + ":999999")
            local("set-option", "-w", "-t", placeholder, "@tmax-connecting", "1")
            local("set-option", "-t", name, "@tmax-remote-host", host)
            local("set-option", "-t", name, "@tmax-remote-session", sid)
            local("set-option", "-t", name, "@tmax-remote-epoch", epoch)
            local("set-option", "-t", name, "@tmax-sidebar-overview", "off")
            local("set-option", "-t", name, "renumber-windows", "off")
        elif local("show-options", "-qv", "-t", name, "@tmax-remote-epoch") != epoch:
            raise RuntimeError("remote session was replaced; close its old local proxy first")
        elif name != wanted and not taken:
            # The remote session was renamed; follow it.
            local("rename-session", "-t", name, wanted)
            name = wanted
    return name


def attach(host, sid, quiet=False):
    epoch, title = fetch(host, "display-message", "-p", "-t", sid, "#{pid}:#{session_created}\t#{session_name}").split("\t", 1)
    name = prepare(host, sid, epoch, clean(title))
    with (RUNTIME / (key(proxy_key(host, sid)) + ".sync")).open("w") as sync_lock:
        fcntl.flock(sync_lock, fcntl.LOCK_EX)
        sync(host, sid)
    spawn("watch", host, sid)
    if not quiet:
        print(name)


def activate(session):
    fields = local("display-message", "-p", "-t", session,
                   "#{@tmax-remote-host}\t#{@tmax-remote-session}", check=False).split("\t")
    if len(fields) == 2 and fields[0]:
        try:
            attach(*fields, quiet=True)
        except Exception as exc:
            local("display-message", "tmax: " + clean(str(exc)), check=False)


def tree(client, pane):
    from concurrent.futures import ThreadPoolExecutor
    local("display-message", "-c", client, "Refreshing sessions…", check=False)
    configured = hosts()
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(configured)))) as pool:
        list(pool.map(refresh, configured))
    errors = []
    for host in configured:
        data = json.loads((RUNTIME / (key(host) + ".json")).read_text())
        if data["status"] != "online":
            errors.append(host + ": " + data["status"])
            continue
        for sid, title, count in data["sessions"]:
            try:
                name = proxy_session(host, sid)
                epoch = local("show-options", "-qv", "-t", name, "@tmax-remote-epoch", check=False) if name else ""
                if not epoch:
                    epoch = fetch(host, "display-message", "-p", "-t", sid, "#{pid}:#{session_created}")
                name = prepare(host, sid, epoch, title)
                local("set-option", "-t", name, "@tmax-remote-name", title)
                local("set-option", "-t", name, "@tmax-remote-windows", str(count))
            except Exception as exc:
                errors.append(host + ": " + clean(str(exc)))
        current_ids = {row[0] for row in data["sessions"]}
        for line in local("list-sessions", "-F", "#{session_name}\t#{@tmax-remote-host}\t#{@tmax-remote-session}").splitlines():
            name, owner, sid = line.split("\t")
            if owner == host and sid not in current_ids:
                forget_proxy(name)
    # Do not open a chooser over unrelated work if its invoking client moved meanwhile.
    clients = dict(line.split("\t", 1) for line in local("list-clients", "-F", "#{client_name}\t#{session_id}", check=False).splitlines())
    current = local("display-message", "-p", "-t", clients[client], "#{pane_id}", check=False) if client in clients else None
    if current == pane:
        # Session lines are named host/name already; keep the text short and stock-like.
        local("choose-tree", "-Zs", "-t", pane, "-F",
              "#{?pane_format,#{pane_current_command},#{?window_format,#{window_name}#{window_flags},"
              "#{?@tmax-remote-host,#{@tmax-remote-windows},#{session_windows}} windows#{?session_attached, (attached),}}}")
    if errors:
        local("display-message", "-c", client, "tmax: " + "; ".join(errors), check=False)


def layout_translate(layout, mapping):
    # Leaves are WIDTHxHEIGHT,X,Y,PANE_ID; internal cells end with '[' or '{'.
    body = layout.split(",", 1)[1]
    body = re.sub(r"(\d+x\d+,\d+,\d+,)(\d+)(?=[,}\]]|$)",
                  lambda m: m[1] + mapping["%" + m[2]].lstrip("%"), body)
    checksum = 0
    for byte in body.encode():
        checksum = ((checksum >> 1) | ((checksum & 1) << 15))
        checksum = (checksum + byte) & 0xffff
    return f"{checksum:04x}," + body


def sync(host, sid):
    name = proxy_session(host, sid)
    if name is None:
        raise RuntimeError("no local session for " + host + " " + sid)
    side = local("show-options", "-gqv", "@tmax-sidebar-pane")
    focused_side = bool(side and local("display-message", "-p", "-t", name, "#{pane_id}") == side)
    fmt = "#{window_id}\t#{window_index}\t#{window_name}\t#{window_layout}\t#{pane_id}\t#{pane_active}\t#{window_active}\t#{pid}:#{session_created}\t#{window_zoomed_flag}\t#{session_name}"
    records = [line.split("\t") for line in fetch(host, "list-panes", "-s", "-t", sid, "-F", fmt).splitlines()]
    if records and records[0][7] != local("show-options", "-qv", "-t", name, "@tmax-remote-epoch"):
        raise RuntimeError("remote session was replaced; refusing to reconnect to reused IDs")
    if records:
        wanted = display_name(host, clean(records[0][9]))
        if name != wanted and not any(n == wanted for n, _, _ in sessions()):
            local("rename-session", "-t", name, wanted)
            name = wanted
    windows = {}
    for wid, index, title, layout, pid, active, window_active, epoch, zoomed, _ in records:
        data = windows.setdefault(wid, {"index": index, "title": clean(title), "layout": layout, "panes": [], "active": window_active, "zoomed": zoomed})
        data["panes"].append(pid)
        if active == "1":
            data["active_pane"] = pid
    existing = local("list-panes", "-s", "-t", name, "-F", "#{pane_id}\t#{window_id}\t#{@tmax-remote-pane}\t#{@tmax-remote-window}")
    panes, wins = {}, {}
    for line in existing.splitlines():
        lp, lw, rp, rw = line.split("\t")
        if rp:
            panes[rp] = lp
        if rw:
            wins[rw] = lw
    for rw, data in windows.items():
        lw = wins.get(rw)
        mapping = {}
        changed = False
        for rp in data["panes"]:
            lp = panes.get(rp)
            if lp is None:
                command = shlex.join(SELF + ["view", host, sid, rp])
                if lw is None:
                    result = local("new-window", "-d", "-t", name + ":" + data["index"], "-P", "-F", "#{window_id}\t#{pane_id}", "-n", data["title"], command)
                    lw, lp = result.split("\t")
                    local("set-option", "-w", "-t", lw, "@tmax-remote-window", rw)
                    local("set-option", "-w", "-t", lw, "automatic-rename", "off")
                    local("set-option", "-w", "-t", lw, "allow-rename", "off")
                else:
                    lp = local("split-window", "-d", "-t", lw, "-P", "-F", "#{pane_id}", command)
                local("set-option", "-p", "-t", lp, "@tmax-remote-pane", rp)
                changed = True
            mapping[rp] = lp
        # Remove deleted remote panes before applying a layout with fewer leaves.
        for old_rp, old_lp in list(panes.items()):
            if old_rp not in {record[4] for record in records}:
                local("kill-pane", "-t", old_lp, check=False)
                del panes[old_rp]
        previous = local("show-options", "-wqv", "-t", lw, "@tmax-remote-layout")
        if changed or previous != data["layout"]:
            # Sidebar is a local-only pane, so defer remote layout adoption while open.
            side = local("show-options", "-gqv", "@tmax-sidebar-pane")
            in_window = local("list-panes", "-t", lw, "-F", "#{pane_id}").splitlines()
            if side not in in_window:
                local("select-layout", "-t", lw, layout_translate(data["layout"], mapping))
                local("set-option", "-w", "-t", lw, "@tmax-remote-layout", data["layout"])
        local("rename-window", "-t", lw, data["title"])
        if changed and data["active"] == "1":
            local("select-window", "-t", lw)
            if data.get("active_pane") in mapping:
                local("select-pane", "-t", mapping[data["active_pane"]])
        local_zoom = local("display-message", "-p", "-t", lw, "#{window_zoomed_flag}")
        if local_zoom != data["zoomed"]:
            local("resize-pane", "-Z", "-t", mapping[data["active_pane"]])
    remote_panes = {record[4] for record in records}
    for rp, lp in panes.items():
        if rp not in remote_panes:
            local("kill-pane", "-t", lp, check=False)
    # Remove only our initial placeholder after at least one proxy is ready.
    if windows:
        for line in local("list-windows", "-t", name, "-F", "#{window_id}\t#{@tmax-connecting}").splitlines():
            lw, connecting = line.split("\t")
            if connecting == "1":
                side = local("show-options", "-gqv", "@tmax-sidebar-pane")
                if side and side in local("list-panes", "-t", lw, "-F", "#{pane_id}").splitlines():
                    target = next(line for line in local("list-windows", "-t", name, "-F", "#{?@tmax-remote-window,#{window_id},}").splitlines() if line)
                    width = local("show-options", "-gqv", "@tmax-sidebar-width") or "28"
                    local("join-pane", "-d", "-fhb", "-l", width, "-s", side, "-t", target)
                local("kill-window", "-t", lw)
    if focused_side:
        local("select-pane", "-t", side, check=False)


def watch(host, sid):
    with (RUNTIME / (key(proxy_key(host, sid)) + ".watch")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            name = proxy_session(host, sid)
            if name is None:
                return
            try:
                with (RUNTIME / (key(proxy_key(host, sid)) + ".sync")).open("w") as sync_lock:
                    fcntl.flock(sync_lock, fcntl.LOCK_EX)
                    sync(host, sid)
                name = proxy_session(host, sid) or name
                local("set-option", "-t", name, "@tmax-remote-status", "online")
            except Exception as exc:
                if missing_session(str(exc)):
                    forget_proxy(name)
                    return
                print(f"{host}: {exc}", flush=True)
                local("set-option", "-t", name, "@tmax-remote-status", "offline", check=False)
            attached = local("display-message", "-p", "-t", name, "#{session_attached}", check=False)
            time.sleep(5 if attached == "1" else 15)


class Control:
    def __init__(self, host, sid, output):
        self.proc = subprocess.Popen(ssh(host, "-C", "attach-session", "-E", "-f", "no-output,ignore-size", "-t", sid),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buffer = b""
        self.output = output
        self.topology_changed = False
        try:
            self.read_block()
        except BaseException:
            self.close()
            raise

    def line(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError("tmux control response timed out")
            if not select.select([self.proc.stdout], [], [], 0.1)[0]:
                continue
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                raise ConnectionError("SSH control connection closed")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def event(self, line):
        if line.startswith((b"%layout-change ", b"%window-add ")):
            self.topology_changed = True
        if line.startswith(b"%output "):
            _, pane, value = line.split(b" ", 2)
            self.output(pane.decode(), re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), value))
        if line.startswith(b"%exit"):
            raise ConnectionError("remote session ended")

    def read_block(self):
        lines, started = [], False
        while True:
            line = self.line()
            if line.startswith(b"%begin "):
                started = True
            elif line.startswith(b"%end ") and started:
                return b"\n".join(lines)
            elif line.startswith(b"%error ") and started:
                raise RuntimeError(b"\n".join(lines).decode(errors="replace"))
            elif started:
                lines.append(line)
            else:
                self.event(line)

    def call(self, *args):
        self.proc.stdin.write(" ".join(control_quote(arg) for arg in args).encode() + b"\n")
        self.proc.stdin.flush()
        try:
            return self.read_block()
        except RuntimeError as exc:
            raise RuntimeError(shlex.join(list(args)) + ": " + str(exc)) from exc

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def repaint(control, rp):
    screen = control.call("capture-pane", "-p", "-e", "-t", rp)
    values = control.call("display-message", "-p", "-t", rp,
                          "#{cursor_x} #{cursor_y} #{cursor_flag} #{alternate_on} #{pane_height} #{scroll_region_upper} #{scroll_region_lower}").decode().split()
    x, y, cursor, alternate, height, upper, lower = map(int, values)
    os.write(1, b"\033[0m\033[?1049l" + (b"\033[?1049h" if alternate else b"") + b"\033[r\033[H\033[2J")
    os.write(1, b"\r\n".join(screen.split(b"\n")[:height]))
    os.write(1, f"\033[{upper+1};{lower+1}r\033[{y+1};{x+1}H\033[?25{'h' if cursor else 'l'}".encode())
    flags = ["keypad_cursor_flag", "origin_flag", "wrap_flag", "mouse_standard_flag", "mouse_button_flag", "mouse_all_flag", "mouse_utf8_flag", "mouse_sgr_flag"]
    states = control.call("display-message", "-p", "-t", rp, " ".join("#{" + flag + "}" for flag in flags)).decode().split()
    for mode, enabled in zip([1, 6, 7, 1000, 1002, 1003, 1005, 1006], states):
        os.write(1, f"\033[?{mode}{'h' if enabled == '1' else 'l'}".encode())


def pane_socket(lp):
    return RUNTIME / ("pane-" + lp.lstrip("%") + ".sock")


def view(host, sid, rp):
    lp = os.environ["TMUX_PANE"]
    saved = termios.tcgetattr(0)
    control = None
    path = pane_socket(lp)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    try:
        tty.setraw(0)
        while True:
            try:
                control = Control(host, sid, lambda pane, data: os.write(1, data) if pane == rp else None)
                epoch = control.call("display-message", "-p", "-t", sid, "#{pid}:#{session_created}").decode()
                if epoch != local("show-options", "-qv", "-t", lp, "@tmax-remote-epoch"):
                    raise RuntimeError("remote session was replaced; close this proxy and reopen")
                # Pause discards output without blocking remote applications, unlike 'off'.
                for pane in control.call("list-panes", "-s", "-t", sid, "-F", "#{pane_id}").decode().splitlines():
                    control.call("refresh-client", "-A", pane + ":pause")
                control.call("refresh-client", "-f", "!no-output")
                visible, size, last_check = False, None, 0
                while True:
                    if control.topology_changed:
                        control.topology_changed = False
                        for pane in control.call("list-panes", "-s", "-t", sid, "-F", "#{pane_id}").decode().splitlines():
                            if pane != rp:
                                control.call("refresh-client", "-A", pane + ":pause")
                    now = time.monotonic()
                    if now - last_check > 1:
                        last_check = now
                        info = local("display-message", "-p", "-t", lp,
                                     "#{session_attached} #{window_active} #{window_width} #{window_height} #{window_zoomed_flag} #{pane_active}").split()
                        attached, active, width, height, zoomed, pane_active = map(int, info)
                        extras = local("list-panes", "-t", lp, "-F", "#{?@tmax-remote-pane,,#{pane_width}}").splitlines()
                        width -= sum(int(extra) + 1 for extra in extras if extra.isdigit())
                        width = max(1, width)
                        showing = bool(attached and active and (not zoomed or pane_active))
                        if showing != visible:
                            control.call("refresh-client", "-A", rp + (":continue" if showing else ":pause"))
                            if showing:
                                repaint(control, rp)
                            visible = showing
                        # Normal tmux client sizing; other attached clients retain their say.
                        if visible and (width, height) != size:
                            control.call("refresh-client", "-f", "!ignore-size")
                            control.call("refresh-client", "-C", f"{width}x{height}")
                            size = (width, height)
                        elif not visible and size is not None:
                            control.call("refresh-client", "-f", "ignore-size")
                            size = None
                    if b"\n" in control.buffer:
                        control.event(control.line())
                        continue
                    ready, _, _ = select.select([0, control.proc.stdout, server], [], [], 0.2)
                    if control.proc.stdout in ready:
                        control.event(control.line())
                    if 0 in ready:
                        data = os.read(0, 1024)
                        if not data:
                            return
                        control.call("send-keys", "-H", "-t", rp, *[f"{b:02x}" for b in data])
                    if server in ready:
                        conn, _ = server.accept()
                        with conn:
                            conn.settimeout(2)
                            payload = b""
                            while not payload.endswith(b"\n") and len(payload) < 65536:
                                part = conn.recv(4096)
                                if not part:
                                    break
                                payload += part
                            args = json.loads(payload)
                            try:
                                target = sid + ":" if args[0] == "new-window" else rp
                                control.call(args[0], "-t", target, *args[1:])
                                conn.sendall(b"ok\n")
                            except RuntimeError as exc:
                                conn.sendall(str(exc).encode() + b"\n")
            except (ConnectionError, TimeoutError, RuntimeError, OSError) as exc:
                os.write(1, ("\033[0m\033[?25h\r\n[tmax: " + clean(str(exc)) + "; reconnecting in 5s]\r\n").encode())
                # Input typed while disconnected is deliberately discarded.
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    if select.select([0], [], [], 0.2)[0]:
                        if not os.read(0, 4096):
                            return
            finally:
                if control:
                    control.close()
                    control = None
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        server.close()
        path.unlink(missing_ok=True)


def action(lp, args):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect(str(pane_socket(lp)))
        conn.sendall(json.dumps(args).encode() + b"\n")
        response = conn.recv(65536).decode().strip()
    if response != "ok":
        raise RuntimeError(response)
    fields = local("display-message", "-p", "-t", lp, "#{@tmax-remote-host}\t#{@tmax-remote-session}", check=False).split("\t")
    if len(fields) == 2 and fields[0]:
        host, sid = fields
        with (RUNTIME / (key(proxy_key(host, sid)) + ".sync")).open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            sync(host, sid)


def install():
    """Wrap recognized bindings, preserving their exact local command branch."""
    direct = {"new-window", "split-window", "next-layout", "select-layout", "swap-pane", "rotate-window", "resize-pane"}
    for line in local("list-keys", "-T", "prefix").splitlines():
        match = re.match(r"bind-key\s+(-r\s+)?-T\s+prefix\s+((?:\\.|[^\s])+)\s+(.*)", line)
        if not match:
            continue
        repeat, key_text, original = match.groups()
        binding_key = shlex.split(key_text)[0]
        try:
            tokens = shlex.split(original)
        except ValueError:
            continue
        remote = None
        if tokens and tokens[0] in direct and all(token not in tokens for token in [";", "{", "}"]):
            remote = "run-shell -b " + control_quote(shlex.join(SELF + ["action", "#{pane_id}", *tokens]))
        elif original.startswith("confirm-before") and tokens[-1:] in [["kill-pane"], ["kill-window"]]:
            run = "run-shell -b " + control_quote(shlex.join(SELF + ["action", "#{pane_id}", tokens[-1]]))
            remote = "confirm-before -p 'Kill remote " + ("pane" if tokens[-1] == "kill-pane" else "window") + "? (y/n)' " + control_quote(run)
        elif binding_key == "," and "rename-window" in original:
            remote = "display-popup -E -w 60 -h 5 " + control_quote(shlex.join(SELF + ["prompt", "#{pane_id}", "rename-window"]))
        if remote:
            options = ["-r"] if repeat else []
            local("bind-key", *options, "-T", "prefix", binding_key, "if-shell", "-F",
                  "#{@tmax-remote-host}", remote, original)


def prompt(lp, operation):
    name = input("Remote window name: ").strip()
    if name:
        action(lp, [operation, name])


def create(host, name):
    sid = fetch(host, "new-session", "-d", "-s", name, "-P", "-F", "#{session_id}")
    refresh(host)
    attach(host, sid)


def rename(host, sid, name):
    fetch(host, "rename-session", "-t", sid, name)
    current = proxy_session(host, sid)
    if current:
        local("rename-session", "-t", current, display_name(host, clean(name)), check=False)
    refresh(host)


def remove(host, sid):
    fetch(host, "kill-session", "-t", sid)
    current = proxy_session(host, sid)
    if current:
        forget_proxy(current)
    refresh(host)


def forget_proxy(name):
    sessions = local("list-sessions", "-F", "#{?@tmax-remote-host,,#{session_name}}", check=False).splitlines()
    fallback = next((session for session in sessions if session and session != name), None)
    if fallback:
        side = local("show-options", "-gqv", "@tmax-sidebar-pane", check=False)
        if side and local("display-message", "-p", "-t", side, "#{session_name}", check=False) == name:
            width = local("show-options", "-gqv", "@tmax-sidebar-width") or "28"
            local("join-pane", "-d", "-fhb", "-l", width, "-s", side, "-t", fallback + ":", check=False)
        for line in local("list-clients", "-F", "#{session_name}\t#{client_name}", check=False).splitlines():
            session, client = line.split("\t", 1)
            if session == name:
                local("switch-client", "-c", client, "-t", fallback, check=False)
    local("kill-session", "-t", name, check=False)


def main():
    setup()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["rows", "refresh", "attach", "watch", "view", "action", "install", "prompt", "create", "rename", "remove", "tree", "activate"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "action":
        try:
            action(args.args[0], args.args[1:])
        except Exception as exc:
            local("display-message", "tmax: " + str(exc), check=False)
    else:
        globals()[args.command](*args.args)


if __name__ == "__main__":
    main()
