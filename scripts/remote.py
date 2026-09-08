#!/usr/bin/env python3
"""SSH discovery and control-mode proxy panes. Requires Python 3.9+ locally."""
import argparse
import auth
import agent_status
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
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("TMAX_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tmax"))
CONFIG = Path(os.environ.get("TMAX_REMOTES_FILE", ROOT / "remotes.json"))
IDENTITY = hashlib.sha256((str(STATE) + ",".join(os.environ.get("TMUX", "").split(",")[:2])).encode()).hexdigest()[:12]
# macOS TMPDIR paths leave too little room for OpenSSH's temporary socket suffix.
RUNTIME = Path("/tmp") / ("tmax-" + str(os.getuid()) + "-" + IDENTITY)
SELF = [sys.executable, str(Path(__file__).resolve())]
SWITCH_STATE = STATE / "switcher.json"


def setup():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe runtime directory")
    RUNTIME.chmod(0o700)


def config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def hosts():
    """Remote hosts: every entry with an SSH destination. A "local" entry may carry only a label."""
    return {host: entry for host, entry in config().items() if "destination" in entry}


def label(host):
    """Pill text for a host key, or for "local": the entry's label, else the key itself."""
    return config().get(host, {}).get("label") or host


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def local(*args, check=True):
    result = subprocess.run(["tmux", *args], capture_output=True, timeout=10)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout.decode(errors="replace").rstrip("\n")


def ssh(host, *args):
    cfg = hosts()[host]
    command = auth.command(RUNTIME, host, cfg)
    remote = [cfg.get("tmux", "tmux")]
    if cfg.get("socket"):
        remote += ["-L", cfg["socket"]]
    return command + [shlex.join(remote + list(args))]


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
            if data["sessions"]:
                cfg = hosts()[host]
                args = ["snapshot", "--tmux", cfg.get("tmux", "tmux")]
                if cfg.get("socket"):
                    args += ["--socket", cfg["socket"]]
                command = 'python3 "$HOME/.local/share/tmax/agent_status.py" ' + shlex.join(args)
                result = subprocess.run(auth.command(RUNTIME, host, cfg) + [command],
                                        capture_output=True, text=True, timeout=12)
                if result.returncode == 0:
                    data["activity"] = json.loads(result.stdout)
        except Exception as exc:
            message = str(exc)
            if missing_session(message):
                pass
            else:
                data["status"] = "locked" if isinstance(exc, auth.Locked) else "offline"
                data["error"] = message
                if path.exists():
                    with contextlib.suppress(ValueError, OSError):
                        data["sessions"] = json.loads(path.read_text())["sessions"]
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data))
        temp.replace(path)


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


def discover():
    """Refresh every configured host and register a placeholder for each remote session. Returns error strings."""
    from concurrent.futures import ThreadPoolExecutor
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
    return errors


# Host name colours: terminal colours, local first, then remote hosts in remotes.json order. A "colour" per entry overrides.
HOST_COLOURS = ["blue", "magenta", "red", "yellow"]
ANSI = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}


def tint(text, colour):
    """Text in a foreground colour; colour is a tmux-style name, colourN, or #rrggbb."""
    name = colour.lower()
    match = re.fullmatch(r"colou?r(\d+)|(\d+)", name)
    if match:
        code = "38;5;" + (match.group(1) or match.group(2))
    elif re.fullmatch(r"#[0-9a-f]{6}", name):
        code = "38;2;" + ";".join(str(int(name[i:i + 2], 16)) for i in (1, 3, 5))
    elif name.startswith("bright") and name[6:] in ANSI:
        code = str(90 + ANSI[name[6:]])
    elif name in ANSI:
        code = str(30 + ANSI[name])
    else:
        code = "39"
    return "\x1b[" + code + "m" + text + "\x1b[0m"


def host_colour(host, position):
    """The entry's own "colour", else the palette colour for its position (0 = local)."""
    return config().get(host, {}).get("colour") or HOST_COLOURS[position % len(HOST_COLOURS)]


def switch_hosts_visible():
    """Whether remote-host sessions should be included in the switcher."""
    return local("show-options", "-gqv", "@tmax-switch-hosts", check=False) != "off"


def switch_hosts(mode="toggle"):
    """Show, hide, or toggle remote-host sessions in subsequent switcher lists."""
    if mode not in ("show", "hide", "toggle"):
        raise ValueError("switch-hosts expects show, hide, or toggle")
    visible = switch_hosts_visible()
    if mode == "toggle":
        visible = not visible
    else:
        visible = mode == "show"
    local("set-option", "-g", "@tmax-switch-hosts", "on" if visible else "off")


def switch_state():
    """Persistent collapsed hosts and favorite sessions for the switcher."""
    try:
        data = json.loads(SWITCH_STATE.read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "collapsed": list(data.get("collapsed", [])),
        "favorites": list(data.get("favorites", [])),
    }


def save_switch_state(data):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = SWITCH_STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temp.replace(SWITCH_STATE)


def switch_host(mode, host):
    """Collapse, expand, or toggle one host group."""
    if mode not in ("collapse", "expand", "toggle"):
        raise ValueError("switch-host expects collapse, expand, or toggle")
    if host != "local" and host not in hosts():
        return
    data = switch_state()
    collapsed = set(data["collapsed"])
    should_collapse = mode == "collapse" or (mode == "toggle" and host not in collapsed)
    if should_collapse:
        collapsed.add(host)
    else:
        collapsed.discard(host)
    data["collapsed"] = sorted(collapsed)
    save_switch_state(data)


def switch_favorite(mode, kind, identity):
    """Star, unstar, or toggle one session identity."""
    if kind != "session":
        return
    if mode not in ("star", "unstar", "toggle"):
        raise ValueError("switch-favorite expects star, unstar, or toggle")
    data = switch_state()
    favorites = set(data["favorites"])
    should_star = mode == "star" or (mode == "toggle" and identity not in favorites)
    if should_star:
        favorites.add(identity)
    else:
        favorites.discard(identity)
    data["favorites"] = sorted(favorites)
    save_switch_state(data)


def switch_enter(kind, host):
    """fzf transform action: group rows fold; session rows are accepted."""
    if host != "local" and host in hosts() and not auth.live(auth.read_lease(RUNTIME, host, hosts()[host])):
        print("execute(" + shlex.join(SELF + ["unlock", host]) + ")+reload-sync(" +
              shlex.join(SELF + ["switch-list", "--refresh"]) + ")")
    elif kind == "group":
        switch_host("toggle", host)
        print("reload-sync(" + shlex.join(SELF + ["switch-list"]) + ")")
    else:
        print("accept")


def host_statuses():
    """Configured remote hosts and their most recently refreshed status."""
    result = []
    for host in hosts():
        if not auth.live(auth.read_lease(RUNTIME, host, hosts()[host])):
            result.append((host, "locked"))
            continue
        try:
            status = json.loads((RUNTIME / (key(host) + ".json")).read_text()).get("status", "connecting")
        except (OSError, ValueError):
            status = "connecting"
        result.append((host, status))
    return result


def switch_status():
    """Fast fzf info-command showing configured host connectivity at top right."""
    statuses = host_statuses()
    marks = []
    for host, status in statuses:
        if status == "online":
            mark = tint("●", "green")
        elif status == "connecting":
            mark = tint("◌", "yellow")
        elif status == "locked":
            mark = tint("●", "yellow")
        else:
            mark = tint("○", "brightblack")
        marks.append(mark + " " + label(host))
    info = os.environ.get("FZF_INFO", "")
    print("  ".join(marks + ([info] if info else [])))


PREVIEW_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clip_switch_preview_line(line, columns):
    """Clip one ANSI-coloured terminal row without allowing it to reflow."""
    output, width, position = "", 0, 0
    while position < len(line):
        match = PREVIEW_ESCAPE.match(line, position)
        if match:
            output += match.group(0)
            position = match.end()
            continue
        char = line[position]
        char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if width + char_width > columns:
            break
        output += char
        width += char_width
        position += 1
    return output + ("\x1b[0m" if "\x1b" in output else "")


def fit_switch_preview(frame, columns, rows):
    """Keep the bottom of a terminal capture and clip it to the preview viewport."""
    source = frame.splitlines()
    while source and not PREVIEW_ESCAPE.sub("", source[-1]).strip():
        source.pop()
    return "\n".join(clip_switch_preview_line(line, columns) for line in source[-rows:])


def switch_preview(sid, kind, *flags):
    """Continuously render the selected session's active pane for fzf's preview area."""
    if kind != "session":
        print("Select a session to preview its active window.")
        return
    once = "--once" in flags
    while True:
        columns = max(1, int(os.environ.get("FZF_PREVIEW_COLUMNS", "80")))
        rows = max(1, int(os.environ.get("FZF_PREVIEW_LINES", "24")) - 2)
        info = local("display-message", "-p", "-t", sid + ":",
                     "#{session_name}\t#{window_index}\t#{window_name}\t#{window_panes}", check=False)
        if not info:
            title, frame = "Session is no longer available", ""
        else:
            session, index, window, panes = info.split("\t", 3)
            title = session + "  ·  " + index + ": " + window
            if panes != "1":
                title += "  ·  " + panes + " panes"
            frame = local("capture-pane", "-p", "-e", "-t", sid + ":", check=False)
            frame = fit_switch_preview(frame, columns, rows)
        title = clip_switch_preview_line(title, columns)
        # Home + clear makes the long-running preview update in place. fzf stops
        # this process whenever selection changes or the popup closes.
        sys.stdout.write("\x1b[H\x1b[2J\x1b[1m" + title + "\x1b[0m\n\n" + frame + "\n")
        sys.stdout.flush()
        if once:
            return
        time.sleep(1)


def host_status_signature():
    return json.dumps(host_statuses(), separators=(",", ":"))



DOT_COLOURS = {"plain": "brightwhite", "working": "brightgreen", "waiting": "brightyellow"}


def activity_dots(states):
    return " ".join(tint("●", DOT_COLOURS.get(state, "white")) for state in states)


def activity_snapshot():
    try:
        return agent_status.snapshot()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def remote_activity():
    result = {}
    for host, cfg in hosts().items():
        if not auth.live(auth.read_lease(RUNTIME, host, cfg)):
            continue
        try:
            data = json.loads((RUNTIME / (key(host) + ".json")).read_text())
            if data.get("status") == "online":
                result[host] = data.get("activity", {})
        except (OSError, ValueError):
            pass
    return result


def visible_width(text):
    text = PREVIEW_ESCAPE.sub("", text)
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad_visible(text, width):
    return text + " " * max(0, width - visible_width(text))


def switch_rows(refresh_hosts=False):
    """Lines for the grouped fzf switcher.

    Fields are ID, local name, shown name, detail, coloured host, row kind,
    host key and favorite identity. The final three fields are metadata hidden
    by --with-nth; the ID lets fzf track the cursor across reloads."""
    errors = discover() if refresh_hosts else []
    order = {host: index for index, host in enumerate(hosts())}
    show_hosts = switch_hosts_visible()
    state = switch_state()
    collapsed, favorites = set(state["collapsed"]), set(state["favorites"])
    sessions_by_host = {"local": []}
    local_activity, cached_activity = activity_snapshot(), remote_activity()
    session_states = {}
    for line in local("list-sessions", "-F", "#{session_id}\t#{session_name}\t#{@tmax-remote-host}\t#{@tmax-remote-name}\t"
                      "#{?@tmax-remote-host,#{@tmax-remote-windows},#{session_windows}}\t#{@tmax-remote-session}", check=False).splitlines():
        sid, name, host, title, count, remote_sid = line.split("\t", 5)
        if host and not show_hosts:
            continue
        host = host or "local"
        shown = title or (name[len(host) + 1:] if host != "local" and name.startswith(host + "/") else name)
        states = (local_activity.get(sid) if host == "local" else
                  cached_activity.get(host, {}).get(remote_sid))
        if states is None:
            states = ["plain"] * (int(count) if count.isdigit() else 1)
        states = [state if state in DOT_COLOURS else "plain" for state in states]
        session_states[sid] = agent_status.strongest(states)
        detail = activity_dots(states)
        badge_position = 0 if host == "local" else 1 + order.get(host, len(order))
        badge = tint(label(host), host_colour(host, badge_position))
        identity = ("local:" + name) if host == "local" else ("remote:" + host + ":" + shown)
        sessions_by_host.setdefault(host, []).append((shown.lower(), sid, name, shown, detail, badge, identity))
    for entries in sessions_by_host.values():
        entries.sort()

    host_order = ["local"] + (list(hosts()) if show_hosts else [])
    rows = []
    # Favorites are real session rows, moved out of their normal host group.
    for host in host_order:
        for _, sid, name, shown, detail, badge, identity in sessions_by_host.get(host, []):
            if identity in favorites:
                rows.append((sid, name, "★ " + shown, detail, badge, "session", host, identity))
    for host in host_order:
        entries = sessions_by_host.get(host, [])
        arrow = "▸" if host in collapsed else "▾"
        count = activity_dots([session_states[entry[1]] for entry in entries])
        if host != "local" and not auth.live(auth.read_lease(RUNTIME, host, hosts()[host])):
            count = "locked"
        badge_position = 0 if host == "local" else 1 + order.get(host, len(order))
        badge = tint(label(host), host_colour(host, badge_position))
        rows.append(("host:" + host, "-", arrow + " " + label(host), count, badge, "group", host, "-"))
        if host not in collapsed:
            for _, sid, name, shown, detail, badge, identity in entries:
                if identity not in favorites:
                    rows.append((sid, name, "  " + shown, detail, badge, "session", host, identity))
    name_width = max((visible_width(row[2]) for row in rows), default=0)
    detail_width = max((visible_width(row[3]) for row in rows), default=0)
    lines = ["\t".join((sid, name, pad_visible(shown, name_width) + " ", pad_visible(detail, detail_width) + " ", badge, kind, host, identity))
             for sid, name, shown, detail, badge, kind, host, identity in rows]
    return lines, errors


def switch_list(*flags):
    lines, _ = switch_rows("--refresh" in flags)
    text = "\n".join(lines) + "\n"
    snapshot = os.environ.get("TMAX_SWITCH_SNAPSHOT")
    if snapshot and Path(snapshot).exists():
        path = Path(snapshot)
        temporary = path.with_suffix("." + str(os.getpid()) + ".tmp")
        temporary.write_text(text)
        temporary.replace(path)
    print(text, end="")


def switch_refresh(snapshot, *flags):
    """Poll when the user pauses; never rearrange rows during active navigation."""
    if "--initial" not in flags and int(os.environ.get("FZF_IDLE_TIME_MS", "1000")) < 1000:
        return
    settings_before = (switch_state(), switch_hosts_visible())
    path = Path(snapshot)
    if not path.exists():
        return
    status_path = path.with_suffix(".status")
    lines, _ = switch_rows(True)
    text = "\n".join(lines) + "\n"
    if settings_before != (switch_state(), switch_hosts_visible()):
        return
    status = host_status_signature()
    rows_changed = path.exists() and path.read_text() != text
    status_changed = not status_path.exists() or status_path.read_text() != status
    if rows_changed:
        temp = path.with_suffix("." + str(os.getpid()) + ".tmp")
        temp.write_text(text)
        temp.replace(path)
    status_path.write_text(status)
    if path.exists():
        if rows_changed or status_changed:
            print("reload-sync(cat " + shlex.quote(str(path)) + ")")



# Keys that only type text: ignored in normal mode, released in insert mode.
SWITCH_TYPING_KEYS = [chr(c) for c in range(ord("a"), ord("z") + 1)] + [chr(c) for c in range(ord("A"), ord("Z") + 1)] \
    + [str(d) for d in range(10)] + ["-", "_", ".", "space"]
# Keys whose fzf default edits the query: unbound in normal mode, restored in insert mode.
SWITCH_EDIT_KEYS = ["backspace", "ctrl-h", "delete"]
SWITCH_NORMAL_KEYS = {"j": "down", "k": "up", "g": "first", "G": "last", "ctrl-d": "half-page-down", "ctrl-u": "half-page-up",
                      "q": "abort", "i": "enter-insert", "/": "enter-insert", "h": "toggle-host", "H": "toggle-hosts",
                      "f": "toggle-favorite", "p": "toggle-preview", "L": "lock-host"}


def fzf_color(value):
    """A tmux colour name as fzf spells it, or None for the terminal default."""
    value = value.lower()
    if value in ("", "default", "terminal"):
        return None
    match = re.fullmatch(r"colou?r(\d+)", value)
    if match:
        return match.group(1)
    if value.startswith("bright"):
        return "bright-" + value[6:]
    return value


def switch_colors():
    """fzf --color entries that paint the current line like the tmux status bar, in one colour."""
    style = dict(part.split("=", 1) for part in local("display-message", "-p", "#{status-style}", check=False).split(",") if "=" in part)
    bg = fzf_color(style.get("bg", ""))
    # Strip the selected host badge's ANSI colour. The name/dot fields
    # selected by --nth clear strip, preserving the activity colours.
    colors = ["fg+:black:regular:strip", "nth:regular"]
    if bg:
        colors += ["bg+:" + bg, "hl:" + bg, "pointer:" + bg, "prompt:" + bg, "hl+:black:underline"]
    return colors


def switch(client):
    """fzf popup that switches the client to the chosen session, or creates one when nothing matches.

    Starts in a vim-like normal mode (j/k move, Enter picks, q or Esc leaves); i or / enters insert
    mode for typing a filter and Esc returns. The list shows what tmux already knows immediately;
    hosts are refreshed in the background and the list is reloaded only when something changed."""
    import shutil
    fzf = shutil.which("fzf") or next((p for p in ["/opt/homebrew/bin/fzf", "/usr/local/bin/fzf"] if os.path.exists(p)), None)
    if not fzf:
        local("display-message", "-c", client, "tmax: fzf not found (brew install fzf)", check=False)
        return
    version = subprocess.check_output([fzf, "--version"], text=True).split()[0]
    if tuple(int(part) for part in version.split(".")[:3]) < (0, 74, 3):
        local("display-message", "-c", client, "tmax: update fzf to 0.74.3+ for live activity dots", check=False)
        return
    lines, _ = switch_rows()
    for stale in RUNTIME.glob("switch-*.txt"):
        # Left behind when a popup was killed instead of closed.
        with contextlib.suppress(ValueError, OSError):
            os.kill(int(stale.stem.split("-", 1)[1]), 0)
            continue
        stale.unlink(missing_ok=True)
    snapshot = RUNTIME / ("switch-" + str(os.getpid()) + ".txt")
    snapshot.write_text("\n".join(lines) + "\n")
    status_snapshot = snapshot.with_suffix(".status")
    status_snapshot.write_text(host_status_signature())
    refresh_cmd = shlex.join(SELF + ["switch-refresh", str(snapshot)])
    modal = sorted(set(SWITCH_TYPING_KEYS) | set(SWITCH_NORMAL_KEYS))
    edit = ",".join(SWITCH_EDIT_KEYS)
    to_insert = "change-prompt(insert> )+unbind(" + ",".join(modal) + ")+rebind(" + edit + ")"
    to_normal = "change-prompt(normal> )+rebind(" + ",".join(modal) + ")+unbind(" + edit + ")"
    reload_rows = "reload-sync(" + shlex.join(SELF + ["switch-list"]) + ")"
    toggle_host = "execute-silent(" + shlex.join(SELF + ["switch-host", "toggle"]) + " {7})+" + reload_rows
    toggle_hosts = "execute-silent(" + shlex.join(SELF + ["switch-hosts", "toggle"]) + ")+" + reload_rows
    toggle_favorite = "execute-silent(" + shlex.join(SELF + ["switch-favorite", "toggle"]) + " {6} {8})+" + reload_rows
    enter = "transform:[ \"$FZF_MATCH_COUNT\" = 0 ] && echo accept || " + shlex.join(SELF + ["switch-enter"]) + " {6} {7}"
    binds = ["start:unbind(" + edit + ")"]
    binds += [key + ":ignore" for key in SWITCH_TYPING_KEYS if key not in SWITCH_NORMAL_KEYS]
    lock_host = "execute-silent(" + shlex.join(SELF + ["lock"]) + " {7})+" + reload_rows
    special = {"enter-insert": to_insert, "toggle-host": toggle_host,
               "toggle-hosts": toggle_hosts, "toggle-favorite": toggle_favorite, "lock-host": lock_host}
    binds += [key + ":" + special.get(action, action)
              for key, action in SWITCH_NORMAL_KEYS.items()]
    binds.append("enter:" + enter)
    binds.append("esc:transform:[ \"$FZF_PROMPT\" = \"insert> \" ] && echo " + shlex.quote(to_normal) + " || echo abort")
    binds.append("load:bg-transform(" + refresh_cmd + " --initial)+unbind(load)")
    binds.append("every(2):bg-transform(" + refresh_cmd + ")")
    preview = shlex.join(SELF + ["switch-preview"]) + " {1} {6}"
    command = [fzf, "--ansi", "--reverse", "--no-multi", "--cycle", "--info=inline-right",
               "--info-command", shlex.join(SELF + ["switch-status"]), "--print-query", "--prompt", "normal> ",
               "--delimiter", "\t", "--with-nth", "3,4,5", "--nth", "1,2", "--tabstop", "1", "--track", "--id-nth", "1",
               "--preview", preview, "--preview-window", "right,50%,border-left,hidden,nowrap"]
    for bind in binds:
        command += ["--bind", bind]
    # No gutter bar: fzf would otherwise draw a bar in every row's first column in the highlight colour.
    command += ["--gutter", " ", "--color", ",".join(["gutter:-1"] + switch_colors())]
    popup_env = dict(os.environ, SHELL="/bin/sh", TMAX_SWITCH_SNAPSHOT=str(snapshot))
    # These colours convey state, including in terminals launched from a
    # non-colour automation environment.
    popup_env.pop("NO_COLOR", None)
    try:
        result = subprocess.run(command, input="\n".join(lines) + "\n", capture_output=True, text=True,
                                env=popup_env)
    finally:
        snapshot.unlink(missing_ok=True)
        status_snapshot.unlink(missing_ok=True)
    output = result.stdout.splitlines()
    query = output[0].strip() if output else ""
    if result.returncode == 0 and len(output) > 1:
        sid, name = output[1].split("\t", 2)[:2]
    elif result.returncode == 1 and query:
        try:
            sid = local("new-session", "-d", "-s", query, "-P", "-F", "#{session_id}")
        except RuntimeError as exc:
            local("display-message", "-c", client, "tmax: " + clean(str(exc)), check=False)
            return
        name = query
    else:
        return
    local("switch-client", "-c", client, "-t", sid, check=False)


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
                local("kill-window", "-t", lw)


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
    last_error = None
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
                # Keep this pane's terminal emulator synchronized even while its
                # local window is hidden. Repainting on every return clears the
                # local screen and corrupts its independently accumulated history.
                control.call("refresh-client", "-A", rp + ":continue")
                repaint(control, rp)
                last_error = None
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
                message = clean(str(exc))
                if not isinstance(exc, auth.Locked):
                    message += "; retrying in 5s"
                if message != last_error:
                    os.write(1, ("\033[0m\033[?25h\r\n[tmax: " + message + "]\r\n").encode())
                    last_error = message
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
        # Reloading tmax must not wrap one of its own conditional bindings
        # again. This is especially important for the rename binding because
        # its popup command still contains the word "rename-window".
        if str(Path(__file__).resolve()) in original:
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


def forget_proxy(name):
    sessions = local("list-sessions", "-F", "#{?@tmax-remote-host,,#{session_name}}", check=False).splitlines()
    fallback = next((session for session in sessions if session and session != name), None)
    if fallback:
        for line in local("list-clients", "-F", "#{session_name}\t#{client_name}", check=False).splitlines():
            session, client = line.split("\t", 1)
            if session == name:
                local("switch-client", "-c", client, "-t", fallback, check=False)
    local("kill-session", "-t", name, check=False)



def unlock(host):
    try:
        if auth.unlock(RUNTIME, host, hosts()[host], SELF + ["auth-guard"]):
            refresh(host)
            switch_host("expand", host)
        else:
            input("Press Enter to return to the popup.")
    except (KeyboardInterrupt, EOFError):
        pass


def lock(host):
    if host in hosts():
        auth.revoke(RUNTIME, host, hosts()[host])


def auth_guard(host, token):
    auth.guard(RUNTIME, host, hosts()[host], token)


def main():
    setup()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["unlock", "lock", "auth-guard", "refresh", "attach", "watch", "view", "action", "install", "prompt", "activate", "switch", "switch-list", "switch-refresh", "switch-hosts", "switch-host", "switch-favorite", "switch-enter", "switch-status", "switch-preview"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "action":
        try:
            action(args.args[0], args.args[1:])
        except Exception as exc:
            local("display-message", "tmax: " + str(exc), check=False)
    else:
        globals()[args.command.replace("-", "_")](*args.args)


if __name__ == "__main__":
    main()
