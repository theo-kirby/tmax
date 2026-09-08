# Integration test for the fzf session switcher (prefix + Space).
# Runs a throwaway tmux server (socket "tmaxswitch") with a fake terminal,
# opens the popup, types into fzf, and checks which session the client is on.
# Does not touch your real tmux server.  Run:  python3 test/switch_test.py

import os, pty, time, subprocess, sys, fcntl, termios, struct, select, tempfile, shutil, re, json, shlex
from pathlib import Path

SOCK = "tmaxswitch"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = tempfile.mkdtemp(prefix="tmaxswitch-state-")
failures = []

def t(*args, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        print("tmux err:", args, r.stderr.strip())
    return r.stdout.strip()

def expect(label, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL"), label, "->", repr(got), "" if ok else "(want %r)" % (want,))
    if not ok: failures.append(label)

if not shutil.which("fzf"):
    print("fzf is not installed; skipping"); sys.exit(0)

subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
t("-f", "/dev/null", "new-session", "-d", "-s", "alpha", "-x", "120", "-y", "40")
t("set-option", "-s", "escape-time", "10")
t("new-session", "-d", "-s", "beta", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "gamma", "-x", "120", "-y", "40")
t("new-window", "-d", "-t", "beta:")
t("set-environment", "-g", "TMAX_STATE_DIR", STATE)
config = os.path.join(STATE, "remotes.json")
with open(config, "w") as f: f.write('{"local": {"label": "this box"}, "srv": {"destination": "nowhere.invalid", "label": "big server"}}')
t("set-environment", "-g", "TMAX_REMOTES_FILE", config)
t("run-shell", os.path.join(HERE, "..", "tmax.tmux"))
expect("s opens the stock local tree", t("list-keys", "-T", "prefix", "s").split(" s ", 1)[-1], "choose-tree -Zs")
binding = t("list-keys", "-T", "prefix", "Space")
expect("Space opens a popup", "display-popup" in binding and "switch" in binding, True)
expect("popup border follows status-style", "-S 'fg=#{?#{m/r:bg=,#{status-style}}" in binding, True)
expect("popup title is white", "-T '#[fg=white] sessions '" in binding, True)
expect("popup size", "-w '75%'" in binding and "-h '65%'" in binding, True)
rename_binding = t("list-keys", "-T", "prefix", ",")
t("run-shell", os.path.join(HERE, "..", "tmax.tmux"))
expect("plugin reload does not rewrap bindings", t("list-keys", "-T", "prefix", ","), rename_binding)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
os.environ["TMAX_REMOTES_FILE"] = config   # read when the module loads
os.environ["TMAX_STATE_DIR"] = STATE
import remote
expect("preview clips rows and follows bottom", remote.fit_switch_preview("top\n0123456789ABCD\ncurrent\nlast\n\n", 8, 3),
       "01234567\ncurrent\nlast")
expect("preview clips wide characters by display width", remote.fit_switch_preview("abcdef界z", 8, 1), "abcdef界")
expect("preview toggle key", remote.SWITCH_NORMAL_KEYS.get("p"), "toggle-preview")
expect("host name colours", [remote.tint("x", c).split("m")[0] for c in ["blue", "yellow", "brightred", "colour201", "#ff8800", "12"]],
       ["\x1b[34", "\x1b[33", "\x1b[91", "\x1b[38;5;201", "\x1b[38;2;255;136;0", "\x1b[38;5;12"])
expect("labels: host entries and the local entry", [list(remote.hosts()), remote.label("local"), remote.label("srv"), remote.label("other")],
       [["srv"], "this box", "big server", "other"])
expect("fzf colour names", [remote.fzf_color(c) for c in ["green", "colour235", "color7", "brightred", "#ff8800", "default"]],
       ["green", "235", "7", "bright-red", "#ff8800", None])

# The list itself, without a terminal.
env = dict(os.environ, TMUX=t("display-message", "-p", "#{socket_path}") + ",0,0")
status = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-status"],
                        capture_output=True, text=True, env=dict(env, FZF_INFO="3/3")).stdout
expect("locked hosts start without unattended SSH", remote.auth.read_lease(remote.RUNTIME, "srv", remote.hosts()["srv"]), {})
expect("top-right host status", ["big server" in status, "\x1b[33m●" in status, status.rstrip().endswith("3/3")], [True, True, True])
rows = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-list"],
                      capture_output=True, text=True, env=env).stdout.splitlines()
session_rows = [r for r in rows if r.split("\t")[5] == "session"]
expect("switch-list names", [r.split("\t")[1] for r in session_rows], ["alpha", "beta", "gamma"])
fields = session_rows[1].split("\t")
expect("switch-list beta label", [fields[2].strip(), fields[3].strip(), "this box" in fields[4] and "\x1b[" in fields[4]], ["beta", remote.activity_dots(["plain", "plain"]), True])
heading = rows[0].split("\t")[2].strip()
heading_badge = rows[0].split("\t")[4]
expect("local host heading", [rows[0].split("\t")[5], heading, "this box" in heading_badge and "\x1b[" in heading_badge],
       ["group", "▾ this box", True])
preview = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"),
                          "switch-preview", session_rows[0].split("\t")[0], "session", "--once"],
                         capture_output=True, text=True, env=env).stdout
expect("selected session preview", ["alpha" in preview, "0:" in preview, "\x1b[2J" in preview], [True, True, True])
group_preview = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"),
                                "switch-preview", "host:local", "group", "--once"],
                               capture_output=True, text=True, env=env).stdout
expect("host rows show a preview hint", "Select a session" in group_preview, True)

# Preview navigation is isolated from the session's active window.
preview_env = dict(env, TMAX_SWITCH_SNAPSHOT=os.path.join(STATE, "preview-test.txt"))
beta_sid = session_rows[1].split("\t")[0]
def preview_command(command, *args):
    return subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), command, *args],
                          capture_output=True, text=True, env=preview_env, check=True).stdout

def beta_preview():
    return preview_command("switch-preview", beta_sid, "session", "--once")

preview_command("switch-preview-cycle", beta_sid, "session", "1")
expect("preview cycles right", "1:" in beta_preview(), True)
preview_command("switch-preview-cycle", beta_sid, "session", "1")
expect("preview wraps right", "0:" in beta_preview(), True)
preview_command("switch-preview-cycle", beta_sid, "session", "-1")
expect("preview wraps left", "1:" in beta_preview(), True)
expect("cycling leaves active window unchanged", t("display-message", "-p", "-t", "beta:", "#{window_index}"), "0")
preview_command("switch-preview-cycle", "host:local", "group", "1")
expect("group cycling leaves preview unchanged", "1:" in beta_preview(), True)
t("kill-window", "-t", "beta:1")
expect("closed preview window falls back to active", "0:" in beta_preview(), True)
t("new-window", "-d", "-t", "beta:1")
expect("help hides", preview_command("switch-help"), "")
expect("help restores legend", preview_command("switch-help").strip(), remote.SWITCH_LEGEND)

unlock_action = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"),
                                "switch-enter", "group", "srv"], capture_output=True, text=True, env=env).stdout
expect("locked host Enter requests interactive unlock", "execute(" in unlock_action and "unlock srv" in unlock_action, True)
# A cached remote proxy lets us exercise host visibility without contacting SSH.
t("new-session", "-d", "-s", "srv/omega", "-x", "120", "-y", "40")
t("set-option", "-t", "srv/omega", "@tmax-remote-host", "srv")
t("set-option", "-t", "srv/omega", "@tmax-remote-name", "omega")
t("set-option", "-t", "srv/omega", "@tmax-remote-windows", "3")
def switch_names():
    result = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-list"],
                            capture_output=True, text=True, env=env)
    return [row.split("\t")[1] for row in result.stdout.splitlines() if row.split("\t")[5] == "session"]
expect("hosts are shown by default", switch_names(), ["alpha", "beta", "gamma", "srv/omega"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-host", "collapse", "srv"], env=env, check=True)
expect("one host collapses", switch_names(), ["alpha", "beta", "gamma"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-host", "expand", "srv"], env=env, check=True)
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-favorite", "star", "session", "remote:srv:omega"], env=env, check=True)
expect("favorite pins to top", switch_names(), ["srv/omega", "alpha", "beta", "gamma"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-host", "collapse", "srv"], env=env, check=True)
expect("favorite survives collapsed host", switch_names(), ["srv/omega", "alpha", "beta", "gamma"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-host", "expand", "srv"], env=env, check=True)
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-favorite", "unstar", "session", "remote:srv:omega"], env=env, check=True)
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-hosts", "hide"], env=env, check=True)
expect("switch-hosts hide", [switch_names(), t("show-option", "-gqv", "@tmax-switch-hosts")],
       [["alpha", "beta", "gamma"], "off"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-hosts", "show"], env=env, check=True)
expect("switch-hosts show", [switch_names(), t("show-option", "-gqv", "@tmax-switch-hosts")],
       [["alpha", "beta", "gamma", "srv/omega"], "on"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-hosts", "toggle"], env=env, check=True)
expect("switch-hosts toggle", [switch_names(), t("show-option", "-gqv", "@tmax-switch-hosts")],
       [["alpha", "beta", "gamma"], "off"])
subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-hosts", "toggle"], env=env, check=True)

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp("tmux", ["tmux", "-L", SOCK, "attach", "-t", "alpha"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

def drain():
    out = b""
    while True:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r: break
        try: out += os.read(fd, 65536)
        except OSError: break
    return out

def send(s, wait=0.6):
    os.write(fd, s.encode()); time.sleep(wait)
    out = drain()
    if os.environ.get("TMAX_TEST_DEBUG"):
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B|\x1b\[[0-9]*X", "", out.decode(errors="replace"))
        print("   screen after %r: %s" % (s, re.sub(r"[\s\u2500-\u257f]+", " ", text)[-300:]))
    return out

def session(): return t("display-message", "-p", "#{client_session}")

time.sleep(0.8); drain()
expect("start on alpha", session(), "alpha")
attached_rows = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-list"],
                               capture_output=True, text=True, env=env).stdout.splitlines()
attached_alpha = next(row.split("\t")[3].strip() for row in attached_rows if row.split("\t")[1] == "alpha")
expect("attached session has one window dot", attached_alpha, remote.activity_dots(["plain"]))

send("\x02 ", 1.5)
send("\r", 0.8)
expect("Enter collapses a host heading", switch_names(), ["srv/omega"])
send("\r", 0.8)
expect("Enter expands a host heading", switch_names(), ["alpha", "beta", "gamma", "srv/omega"])
send("c", 0.5); send("\r", 0.5)
expect("empty create prompt cancels", switch_names(), ["alpha", "beta", "gamma", "srv/omega"])
send("x", 0.5)
expect("x ignores host headings", switch_names(), ["alpha", "beta", "gamma", "srv/omega"])
send("c", 0.5); send("a-new\r", 0.8)
expect("c creates on the selected local host", switch_names(), ["a-new", "alpha", "beta", "gamma", "srv/omega"])
send("j", 0.3)
prompt_output = send("x", 0.5)
expect("x asks before closing", b"Are you sure?" in prompt_output, True)
send("n\r", 0.5)
expect("declining close preserves session", "a-new" in switch_names(), True)
send("x", 0.5); send("y\r", 0.8)
expect("confirmed x closes selected session", switch_names(), ["alpha", "beta", "gamma", "srv/omega"])
send("g", 0.3)
send("H", 0.8)
expect("normal mode H hides all remote hosts", t("show-option", "-gqv", "@tmax-switch-hosts"), "off")
send("H", 0.8)
expect("normal mode H shows all remote hosts", t("show-option", "-gqv", "@tmax-switch-hosts"), "on")
send("j", 0.3); send("f", 0.8)
expect("normal mode f stars a session", "local:alpha" in remote.switch_state()["favorites"], True)
send("f", 0.8)
expect("normal mode f unstars a session", remote.switch_state()["favorites"], [])
send("q", 1.0)

# Exercise the actual modal bindings in fzf.
runtime = subprocess.check_output([sys.executable, "-c",
    "import sys; sys.path.insert(0, " + repr(os.path.join(HERE, "..", "scripts")) + "); import remote; print(remote.RUNTIME)"],
    env=dict(env, TMUX=t("display-message", "-p", "#{socket_path},#{pid},0")), text=True).strip()
probe = Path(STATE) / "fzf-env"
t("set-environment", "-g", "FZF_DEFAULT_OPTS", shlex.join([
    "--bind", "ctrl-t:execute-silent(env > " + shlex.quote(str(probe)) + ")"]))
def preview_size():
    send("\x14", 0.3)
    values = dict(line.split("=", 1) for line in probe.read_text().splitlines() if "=" in line)
    return int(values.get("FZF_PREVIEW_COLUMNS", "0")), int(values.get("FZF_PREVIEW_LINES", "0"))

send("\x02 ", 1.5)
group_size = preview_size()
expect("preview is visible by default", group_size[0] > 0 and group_size[1] > 0, True)
send("s", 0.5)
expect("s ignores host headings", preview_size(), group_size)
send("p", 0.4)
send("i", 0.4); send("beta", 1.0); send("\x1b", 0.8)
send("s", 0.5)
view_path = next(Path(runtime).glob("switch-*.view"))
expect("s leaves hidden preview hidden", json.loads(view_path.read_text()), {"visible": False})
t("send-keys", "-t", "beta:0", "printf '%070dFULLWIDTH\\n' 0", "Enter")
send("p", 0.5)
split_size = preview_size()
full_output = send("s", 0.8)
expect("full preview renders beyond split boundary", b"FULLWIDTH" in full_output, True)
send("l", 0.5)
preview_paths = list(Path(runtime).glob("switch-*.preview"))
expect("normal mode l selects next preview window", bool(preview_paths) and
       json.loads(preview_paths[0].read_text()).get(beta_sid) == t("display-message", "-p", "-t", "beta:1", "#{window_id}"), True)
send("h", 0.5)
expect("normal mode h selects previous preview window", bool(preview_paths) and
       json.loads(preview_paths[0].read_text()).get(beta_sid) == t("display-message", "-p", "-t", "beta:0", "#{window_id}"), True)
send("s", 0.5)
expect("s restores split view", preview_size(), split_size)
send("?", 0.5)
expect("question mark hides legend", bool(list(Path(runtime).glob("switch-*.help"))), True)
send("?", 0.5)
expect("question mark restores legend", list(Path(runtime).glob("switch-*.help")), [])
send("s", 0.5); send("p", 0.5)
expect("p exits full view and hides preview", json.loads(view_path.read_text()), {"visible": False})
send("p", 0.5); send("s", 0.5); send("q", 0.8)
expect("preview state cleaned up", list(Path(runtime).glob("switch-*.preview")), [])

send("\x02 ", 1.5)              # C-b Space: open the popup, give fzf time to start
send("j", 0.4); send("j", 0.4); send("j", 0.4); send("\r", 1.5)
expect("normal mode: navigation + Enter switches to gamma", session(), "gamma")

send("\x02 ", 1.5)
send("gam", 0.6); send("j", 0.3); send("\r", 1.5)
expect("normal mode ignores typed letters (stays on first item)", session(), "alpha")

send("\x02 ", 1.5)
send("i", 0.4); send("gam", 1.0); send("\r", 1.5)
expect("i then gam + Enter switches to gamma", session(), "gamma")

send("\x02 ", 1.5)
send("i", 0.4); send("gamx", 0.6); send("\x7f", 0.6); send("\r", 1.5)
expect("insert mode: backspace removes the last character", session(), "gamma")

send("\x02 ", 1.5)
send("i", 0.4); send("delta", 1.0); send("\r", 1.5)
expect("no match + Enter creates delta", session(), "delta")
expect("delta exists", "delta" in t("list-sessions", "-F", "#{session_name}").split(), True)

send("\x02 ", 1.5)
send("i", 0.4); send("delt", 0.8); send("\x1b", 0.8); send("j", 0.4); send("\r", 1.5)
expect("insert delt, Esc keeps the filter, j Enter in normal mode -> delta", session(), "delta")

send("\x02 ", 1.5)
send("\x1b", 1.5)               # Esc in normal mode cancels
expect("Esc keeps the session", session(), "delta")

send("\x02 ", 1.5)
send("q", 1.5)
expect("q keeps the session", session(), "delta")

send("\x02 ", 1.5)
send("i", 0.4); send("alp", 1.0); send("\r", 1.5)
expect("back to alpha", session(), "alpha")

os.kill(pid, 15)
subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
shutil.rmtree(STATE, ignore_errors=True)
print("\n%d failure(s)" % len(failures) if failures else "\nall passed")
sys.exit(1 if failures else 0)
