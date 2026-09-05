# Integration test for the session sidebar.
# Runs a throwaway tmux server (socket "tmaxtest") with a fake terminal,
# presses keys, and prints the resulting tmux state. Does not touch your
# real tmux server.  Run:  python3 test/sidebar_test.py

import os, pty, time, subprocess, sys, fcntl, termios, struct, select, tempfile, shutil

SOCK = "tmaxtest"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = tempfile.mkdtemp(prefix="tmaxtest-state-")
def t(*args, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        print("tmux err:", args, r.stderr.strip())
    return r.stdout.strip()

subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
t("-f", "/dev/null", "new-session", "-d", "-s", "alpha", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "beta", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "gamma", "-x", "120", "-y", "40")
# groups / fold state go to a throwaway dir, not ~/.local/state/tmax
t("set-environment", "-g", "TMAX_STATE_DIR", STATE)
t("run-shell", os.path.join(HERE, "..", "tmax.tmux"))
print("bind s ->", t("list-keys", "-T", "prefix", "s"))
print("hook   ->", t("show-hooks", "-g"))

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
    os.write(fd, s.encode()); time.sleep(wait); drain()

def state(label):
    print(f"\n== {label}")
    print(" client session:", t("display-message", "-p", "#{client_session}"))
    print(" sidebar opt   :", t("show-option", "-gqv", "@tmax-sidebar-pane"))
    print(" panes:")
    for l in t("list-panes", "-a", "-F", "  #{session_name}:#{window_index} #{pane_id} w=#{pane_width} active=#{pane_active} cmd=#{pane_current_command}").splitlines():
        print(l)

def show_sidebar():
    p = t("show-option", "-gqv", "@tmax-sidebar-pane")
    if p:
        print(" --- sidebar screen ---")
        for l in t("capture-pane", "-p", "-t", p).splitlines():
            if l.strip(): print("  |" + l)

time.sleep(0.8); drain()
state("attached, no sidebar")

send("\x02s", 1.2); state("after prefix+s (open)"); show_sidebar()
send("j", 0.4); show_sidebar()
send("\t", 1.2); state("after j + Tab (should be beta, sidebar still focused)"); show_sidebar()
send("\x1b", 0.5); state("after Esc (focus on work pane, sidebar open)")
send("\x02s", 0.8); state("prefix+s again (should focus sidebar, not close)")
send("j", 0.3); send("\r", 1.2); state("j + Enter (should be gamma, sidebar closed)")
send("\x02s", 1.2); state("prefix+s (reopen in gamma)")
# external session switch -> hook should move the sidebar
send("\x1b", 0.5)   # esc -> focus work pane
t("switch-client", "-t", "alpha"); time.sleep(1.0)
state("after external switch-client to alpha (hook follow)")
# new session
send("\x02s", 0.8); send("n", 0.5); send("delta\r", 1.5); state("after n + 'delta' (should be in delta)"); show_sidebar()
# kill current (delta)
send("\x02s", 0.8); send("G", 0.3); show_sidebar()
send("d", 0.5); send("y\r", 1.5); state("after killing selected session")
show_sidebar()

# ---- groups ---------------------------------------------------------------
def files(label):
    print(f" --- state files ({label}) ---")
    for f in ("groups", "order", "collapsed"):
        path = os.path.join(STATE, f)
        body = open(path).read() if os.path.exists(path) else "(missing)"
        print(f"  {f}: {body!r}")

# sidebar is still open, client is in delta; sessions: alpha beta delta
send("g", 0.3); send("t", 0.5); send("work\r", 1.0)
print("\n== after tagging alpha -> work (expect: beta, ● delta, then '▾ work' / alpha)"); show_sidebar(); files("alpha tagged")
send("g", 0.3); send("j", 0.3); send("t", 0.5); send("personal\r", 1.0)
print("\n== after tagging delta -> personal (expect: beta, ▾ personal / ● delta, ▾ work / alpha)"); show_sidebar()
send("h", 0.8)
print("\n== after h on delta (expect: '▸ personal 1' in green, cursor on it)"); show_sidebar(); files("personal folded")
send("G", 0.3); send(" ", 0.8)
print("\n== after G + Space on alpha (expect: '▸ work 1', alpha hidden)"); show_sidebar()
send("k", 0.3); send("\t", 0.8)
print("\n== after k + Tab on personal (expect: personal unfolded, delta visible)"); show_sidebar()
send("\r", 0.8); state("Enter on group line (expect: personal folded again, sidebar still open, still delta)"); show_sidebar()
send("n", 0.5); send("eps\r", 1.5); state("n on personal line + 'eps' (expect: in eps, '▸ personal 2', eps hidden)"); show_sidebar(); files("eps created")
send("\x02s", 0.8); send("r", 0.5); send("home\r", 1.0)   # cursor sits on the personal line
print("\n== after renaming group personal -> home (expect: '▸ home 2', still folded)"); show_sidebar(); files("group renamed")
send("G", 0.3); send(" ", 0.8)
print("\n== after Space on work line (expect: work unfolded, alpha visible)"); show_sidebar()

# ---- reorder --------------------------------------------------------------
send("K", 0.8)
print("\n== after K on work line (expect: work above home)"); show_sidebar(); files("work moved up")
send("K", 0.8)
print("\n== after K again (expect: no change, work is already first)"); show_sidebar()
send("G", 0.3); send(" ", 0.8); send("G", 0.3); send("K", 0.8)
print("\n== after unfolding home, G, K (expect: eps above delta, cursor on eps)"); show_sidebar(); files("eps moved up")

# close with q
send("q", 1.0); state("after q (sidebar closed)")
print("\nsessions:", t("list-sessions", "-F", "#{session_name}"))
subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
shutil.rmtree(STATE, ignore_errors=True)
