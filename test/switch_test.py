# Integration test for the fzf session switcher (prefix + Space).
# Runs a throwaway tmux server (socket "tmaxswitch") with a fake terminal,
# opens the popup, types into fzf, and checks which session the client is on.
# Does not touch your real tmux server.  Run:  python3 test/switch_test.py

import os, pty, time, subprocess, sys, fcntl, termios, struct, select, tempfile, shutil, re

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
t("new-session", "-d", "-s", "beta", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "gamma", "-x", "120", "-y", "40")
t("new-window", "-d", "-t", "beta:")
t("set-environment", "-g", "TMAX_STATE_DIR", STATE)
config = os.path.join(STATE, "remotes.json")
with open(config, "w") as f: f.write("{}")
t("set-environment", "-g", "TMAX_REMOTES_FILE", config)
t("set-option", "-g", "@tmax-sidebar", "off")
t("run-shell", os.path.join(HERE, "..", "tmax.tmux"))
binding = t("list-keys", "-T", "prefix", "Space")
expect("Space opens a popup", "display-popup" in binding and "switch" in binding, True)

# The list itself, without a terminal.
env = dict(os.environ, TMUX=t("display-message", "-p", "#{socket_path}") + ",0,0")
rows = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "remote.py"), "switch-list"],
                      capture_output=True, text=True, env=env).stdout.splitlines()
expect("switch-list names", [r.split("\t")[1] for r in rows], ["alpha", "beta", "gamma"])
expect("switch-list beta label", rows[1].split("\t")[2:], ["beta  ", "2 windows"])

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp("tmux", ["tmux", "-L", SOCK, "attach", "-t", "alpha"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

def drain():
    while True:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r: break
        try: os.read(fd, 65536)
        except OSError: break

def send(s, wait=0.6):
    os.write(fd, s.encode()); time.sleep(wait)
    if os.environ.get("TMAX_TEST_DEBUG"):
        out = b""
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r: break
            try: out += os.read(fd, 65536)
            except OSError: break
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B|\x1b\[[0-9]*X", "", out.decode(errors="replace"))
        print("   screen after %r: %s" % (s, re.sub(r"[\s\u2500-\u257f]+", " ", text)[-300:]))
    drain()

def session(): return t("display-message", "-p", "#{client_session}")

time.sleep(0.8); drain()
expect("start on alpha", session(), "alpha")

send("\x02 ", 1.5)              # C-b Space: open the popup, give fzf time to start
send("j", 0.4); send("j", 0.4); send("\r", 1.5)
expect("normal mode: j j Enter switches to gamma", session(), "gamma")

send("\x02 ", 1.5)
send("gam", 0.6); send("\r", 1.5)
expect("normal mode ignores typed letters (stays on first item)", session(), "alpha")

send("\x02 ", 1.5)
send("i", 0.4); send("gam", 1.0); send("\r", 1.5)
expect("i then gam + Enter switches to gamma", session(), "gamma")

send("\x02 ", 1.5)
send("i", 0.4); send("delta", 1.0); send("\r", 1.5)
expect("no match + Enter creates delta", session(), "delta")
expect("delta exists", "delta" in t("list-sessions", "-F", "#{session_name}").split(), True)

send("\x02 ", 1.5)
send("i", 0.4); send("a", 0.6); send("\x1b", 0.8); send("j", 0.4); send("\r", 1.5)
expect("insert a, Esc back to normal, j Enter -> second a-match (gamma)", session(), "gamma")

send("\x02 ", 1.5)
send("\x1b", 1.5)               # Esc in normal mode cancels
expect("Esc keeps the session", session(), "gamma")

send("\x02 ", 1.5)
send("q", 1.5)
expect("q keeps the session", session(), "gamma")

send("\x02 ", 1.5)
send("i", 0.4); send("alp", 1.0); send("\r", 1.5)
expect("back to alpha", session(), "alpha")

os.kill(pid, 15)
subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
shutil.rmtree(STATE, ignore_errors=True)
print("\n%d failure(s)" % len(failures) if failures else "\nall passed")
sys.exit(1 if failures else 0)
