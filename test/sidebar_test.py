# Integration test for the session sidebar.
# Runs a throwaway tmux server (socket "tmaxtest") with a fake terminal,
# presses keys, and prints the resulting tmux state. Does not touch your
# real tmux server.  Run:  python3 test/sidebar_test.py

import os, pty, time, subprocess, sys, fcntl, termios, struct, select

SOCK = "tmaxtest"
def t(*args, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        print("tmux err:", args, r.stderr.strip())
    return r.stdout.strip()

subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
t("-f", "/dev/null", "new-session", "-d", "-s", "alpha", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "beta", "-x", "120", "-y", "40")
t("new-session", "-d", "-s", "gamma", "-x", "120", "-y", "40")
t("run-shell", "/Users/theo/tmax/tmax.tmux")
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
# close with q
send("\x02s", 0.8); send("q", 1.0); state("after q (sidebar closed)")
print("\nsessions:", t("list-sessions", "-F", "#{session_name}"))
subprocess.run(["tmux", "-L", SOCK, "kill-server"], capture_output=True)
