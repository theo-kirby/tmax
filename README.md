# tmax

Small additions on top of tmux. Built for one person, one feature at a time.

## Install

Add this line to `~/.tmux.conf`:

```tmux
run-shell ~/tmax/tmax.tmux
```

Then reload tmux:

```sh
tmux source-file ~/.tmux.conf
```

That is all. No plugin manager is needed. (It also works with TPM if you want
that later: TPM runs every `*.tmux` file in a plugin folder.)

## How a tmux plugin works

- A plugin is a folder with one `*.tmux` file at the top.
- That file is a normal shell script. tmux runs it once at start.
- The script calls `tmux bind-key`, `tmux set-option`, `tmux set-hook`.
- Key bindings point to helper scripts in `scripts/`.
- State is kept in tmux user options. They start with `@`.

## Session switcher

`prefix + Space` opens a small popup with an [fzf](https://github.com/junegunn/fzf)
list of every session, local and remote, like
[tmux-fzf](https://github.com/sainnhe/tmux-fzf). It works with the sidebar on
or off.

```
+---- sessions ------------------------------+
| normal>                             11/11  |
|▌ lab     2 windows (attached)   macbook    |
|  tmax    2 windows              macbook    |
|  ouro    2 windows              mac mini   |
|  0       2 windows              5090 box   |
+--------------------------------------------+
```

Each row is the session name, its window count, and the name of the
computer. Local sessions come first, then each host in the order of
`remotes.json`. Computer names are written in the terminal's own colours:
blue for local, then cyan, magenta and yellow for the hosts in order. On the
highlighted row the name takes the row's colour like the rest of the text.
The name is the entry's `label` in `remotes.json`, or the host key when
there is none, and an optional `colour` (a tmux colour name, `colourN`, or
`#rrggbb`) picks its colour. A `local` entry without a `destination` names
this machine:

```json
{
  "local": {"label": "macbook"},
  "mmini": {"destination": "mmini", "tmux": "/opt/homebrew/bin/tmux", "label": "mac mini"},
  "sb1x":  {"destination": "sb1x", "tmux": "tmux", "label": "5090 box", "colour": "yellow"}
}
```

The popup starts in a vim-like normal mode. Letters do nothing there except
the keys below, so `j` and `k` move without typing into the filter. Press
`i` (or `/`) for insert mode: type to filter on the name, `Esc` goes back to
normal mode with the filter kept.

| Mode   | Key             | Action                                       |
|--------|-----------------|----------------------------------------------|
| normal | `j` `k` arrows  | move                                         |
| normal | `g` `G`         | first / last                                 |
| normal | `Ctrl-d` `Ctrl-u` | half page down / up                        |
| normal | `i` `/`         | insert mode                                  |
| normal | `Enter`         | go to the session                            |
| normal | `q` `Esc`       | close                                        |
| insert | typing          | filter; `Ctrl-j` `Ctrl-k` still move         |
| insert | `Enter`         | go to the session; with no match, create a local session named after the text |
| insert | `Esc`           | back to normal mode                          |

Remote sessions connect when selected. (Their local proxies are still named
`host/session` in the native tmux tree.)

The popup border and the highlighted line use the colours of your status
bar (`status-style`); the title is white. A status bar without a background
colour leaves the border in the default colour.

The list appears at once with what tmux already knows (local sessions and
the remote ones seen before). The configured hosts are then refreshed in the
background, and the list is reloaded only if something changed, keeping the
cursor on the same session. Hosts that are offline keep their cached entries.

`fzf` 0.62 or newer must be installed (`brew install fzf`). Without it the key
shows a short message in the status line. tmux 3.3+ is needed for the popup.

This replaces tmux's default `prefix + Space` (`next-layout`); that command
is still available from the `:` prompt or by binding another key.

Options, in `~/.tmux.conf` before the `run-shell` line:

```tmux
set -g @tmax-switch-key    "Space"  # prefix + key
set -g @tmax-switch-width  "60%"    # popup size, columns or percent
set -g @tmax-switch-height "50%"
```

## Session sidebar

The sidebar is optional. For the native tmux session tree plus remote access,
put this **before** the `run-shell ~/tmax/tmax.tmux` line in `~/.tmux.conf`:

```tmux
set -g @tmax-sidebar off
```

Reload with `tmux source-file ~/.tmux.conf`. Your prefix and other appearance
settings stay as configured. `prefix + s` refreshes the configured hosts and
opens tmux's normal session tree. Remote entries are named `host/session`, for
example `mmini/ares`, while local sessions keep their own names; selecting
one connects its panes. Until selected, an entry is only a
lightweight local placeholder. Previously opened remote sessions also appear
in other native tmux session/window navigation commands.

The remote bridge and its command routing still need tmax loaded; the sidebar
does not. Native chooser deletion operates on the local representation, while
the routed pane/window kill keys act remotely. Arbitrary custom commands are
still not translated. Set `@tmax-sidebar on` and reload to restore the sidebar.

### Sidebar mode

`prefix + s` opens a sidebar on the left. Your work stays on the right.

```
+-----------+-----------------------------------+
| ● work    |                                   |
|   notes   |   your current session            |
|   scratch |                                   |
|           |                                   |
+-----------+-----------------------------------+
```

The sidebar shows only the session names. No header, no help text. The keys
are listed below. The bottom line is used for prompts and short messages.

`prefix + s` does one of three things:

| State                        | Action            |
|------------------------------|-------------------|
| no sidebar                   | open it, focus it |
| sidebar open, not focused    | focus it          |
| sidebar focused              | close it          |

Keys inside the sidebar:

| Key           | Action                                             |
|---------------|----------------------------------------------------|
| `j` `k` arrows| move                                               |
| `Tab`         | go to session, keep focus in the sidebar           |
| `Enter`       | go to session and close the sidebar                |
| `n`           | new session                                        |
| `r`           | rename session                                     |
| `d`           | kill session (asks `y`/`N`)                        |
| `Space` `h`   | fold / unfold the group the cursor is in           |
| `t`           | tag: put the session in a group (`-` = no group)   |
| `J` `K`       | move the group, or the session in its group, down/up |
| `Esc` or `l`  | focus work pane, keep sidebar open                 |
| `q`           | close sidebar                                      |

On a group line, `Tab` and `Enter` also fold / unfold. `n` creates the new
session inside the group the cursor is in. `r` on a group line renames the
group.

The sidebar follows you. If you change session another way (for example
`prefix + (`), the sidebar moves to the new session too.

### Overview

With `@tmax-sidebar-overview` set to `on`, the right side shows
every window of the session, tiled, like an overview. Each tile has a title
line (`1: logs *`, the `*` marks the current window) and the live text of that
window, refreshed once a second.

```
+-----------+-----------------+-----------------+
| ● work    | 0: editor *     | 1: logs         |
|   notes   | vim main.go     | [12:01] ready   |
|   scratch |                 |                 |
|           +-----------------+-----------------+
|           | 2: shell        |                 |
|           | $ make test     |                 |
+-----------+-----------------+-----------------+
```

Keys inside a tile:

| Key            | Action                                   |
|----------------|------------------------------------------|
| `j` `k` `h` `l` arrows | move between tiles               |
| `Enter`        | go to that window and leave sidebar mode |
| `s`            | focus the sidebar                        |
| `Esc` or `q`   | leave sidebar mode                       |

Leaving sidebar mode puts the session back on the window it was on. `Esc` in
the sidebar focuses the first tile. Tab to another session in the sidebar
builds its overview and drops the old one.

The overview is a real tmux window named `overview`. It shows up in the status
bar while sidebar mode is on and goes away when you leave. Set
`@tmax-sidebar-overview` to `off` to get a plain sidebar next to your window.

The stock tmux session tree is still there on `prefix + S`.

### Remote computers

The sidebar always has a `local` heading, followed by the computers in
`remotes.json`. Hosts are entirely user-configured; a fresh checkout makes no
remote connections. Copy `remotes.example.json` to `remotes.json` and edit it
with your SSH destinations. The local file is ignored by Git. Space or
Enter on a heading folds it; Enter on a session opens it. Existing groups
remain inside `local`. `n`, `r`, and confirmed `d` work on remote sessions.

Remote sessions use local proxy windows and panes backed by tmux control
mode over SSH. The local session is named `host/session` (the host key from
`remotes.json`, then the remote session name) and follows remote renames. There is one local status bar and one local prefix. The
remote machine keeps its own tmux configuration and running applications.
No software is installed remotely. Python 3.9+ is required locally and
tmux 3.4+ is required on the hosts.

Recognized bindings for new windows, splitting, pane/window deletion,
window renaming, and layouts are routed to the remote machine. Navigation,
copy mode, paste, and the sidebar stay local. Window rename uses a small
prompt popup. Custom bindings and commands typed directly at the tmux `:`
prompt are **not** generally translated; use the routed keys for remote
creation and deletion. The local copies are implementation details: deleting
a proxy through a direct local tmux command only removes that local copy,
and the synchronizer may recreate it.

Remote previews are disabled. Hidden panes pause their output subscriptions
without blocking the remote programs; returning to a window restores its
current screen. Metadata refreshes every 10 seconds in the sidebar, and
opened sessions reconcile their windows/panes every 5 seconds while attached
(15 seconds while detached). Connections retry after interruption; keystrokes
typed while disconnected are discarded. Ordinary client-size negotiation
applies when another terminal is attached on the remote computer.

This is an initial bridge: local copy-mode history starts when a pane is
opened; it does not import remote scrollback. Exact restoration of every
terminal mode, arbitrary custom bindings, and mixed local/remote layouts are
not guaranteed. Each opened remote pane currently uses a small Python process
and an SSH channel, sharing one SSH transport per host. RAM therefore scales
with the number of opened panes. Merely listing sessions does not create
these processes.

If a host runs tmax itself, its own remote proxy sessions (for example the
ones it holds for this machine) are skipped when listing, so nothing shows up
twice.

Host configuration example:

```json
{
  "laptop": {
    "destination": "user@laptop",
    "tmux": "/opt/homebrew/bin/tmux",
    "control_path": "~/.ssh/tmax-laptop.sock"
  },
  "server": {"destination": "server", "tmux": "tmux"}
}
```

`destination` uses normal SSH configuration. `tmux` is the executable path;
an optional `socket` selects a nondefault tmux server, and optional
`label` and `colour` fields set the computer name in the session switcher. An entry without a
`destination` (such as `local`) is not a host; it only supplies a label. Set `TMAX_REMOTES_FILE`
in the local tmux environment to use a different JSON file. An empty object
disables remote hosts while keeping the `local` heading.

For a host that uses password authentication, open a shared connection from a terminal:

```sh
ssh -M -S ~/.ssh/tmax-laptop.sock -o ControlPersist=30m -fnN user@laptop
```

The password stays in that terminal. A configured `control_path` must already
be connected; tmax won't prompt for a password in the sidebar. Hosts without
that field use unattended SSH authentication and reuse a private master socket.
Offline hosts remain in the list with cached session names. Diagnostic output
is in `remote.log` in the private `tmax-UID-HASH` directory under the system
temporary directory. Failed sidebar mutations are recorded at
`$TMAX_STATE_DIR/remote-attach-error` (the usual state directory by default).

### Groups

Sessions can be put in groups, like folders. Press `t` on a session and type
a group name. Sessions without a group stay at the top. Groups come after,
sorted by name.

```
+-----------+
|   scratch |
| ▸ work  3 |    folded: shows the session count
| ▾ personal|
|   ● notes |
|     blog  |
+-----------+
```

Press `Space` (or `h`) to fold or unfold the group the cursor is in. A folded
group is one line. If you are inside a folded group, its line is green.

Press `J` or `K` to move things. On a group line, the group moves down or up.
On a session line, the session moves inside its group. New groups and new
sessions go to the end, in name order, until you move them.

Groups and the session order are saved to `~/.local/state/tmax/groups`, the
group order to `~/.local/state/tmax/order`, and fold state to
`~/.local/state/tmax/collapsed`. All survive a tmux restart. A session name
remembers its group: if you kill `notes` and later create `notes` again, it is
back in `personal`. Set `TMAX_STATE_DIR` in the tmux environment to use a
different folder.

### Options

Put these in `~/.tmux.conf` **before** the `run-shell` line:

```tmux
set -g @tmax-sidebar-key    "s"    # prefix + key
set -g @tmax-sidebar-width  "28"   # columns
set -g @tmax-sidebar-follow "on"   # "off" = sidebar stays where it was opened
set -g @tmax-sidebar-overview "off" # default; "on" enables local overview previews
set -g @tmax-sidebar-hover "off"   # "on" = moving the cursor switches at once, no Tab needed
```

## Files

```
tmax.tmux              entry point: key bindings and hooks
scripts/remote.py      remote hosts over SSH control mode; the fzf session switcher
scripts/sidebar.sh     open / close / focus / move the sidebar pane; builds the overview
scripts/sidebar-ui.sh  the list that runs inside the sidebar pane
scripts/preview.sh     one tile of the overview
```

## Debug

```sh
tmux set-environment -g TMAX_DEBUG /tmp/tmax.log
```

Then open the sidebar. The script traces every command to that file.
Remove with `tmux set-environment -gu TMAX_DEBUG`.

## Notes

- Scripts run on the bash 3.2 that ships with macOS. No Homebrew bash needed.
- Tested on tmux 3.5a.

## Test

```sh
python3 test/sidebar_test.py
```

Starts a throwaway tmux server, drives the sidebar with fake key presses, and
prints the state after each step. Your real tmux server is not touched.

```sh
python3 test/switch_test.py
```

Opens the `prefix + Space` popup on a throwaway server, types into fzf, and
checks that the client lands on the chosen or newly created session.

The remote integration suite creates and removes its own tmux servers on
both ends, using an existing SSH connection or configured key:

```sh
python3 test/remote_integration_test.py --host user@test-host
python3 test/remote_integration_test.py --native --host user@test-host
# For password authentication or an executable outside the remote SSH PATH:
python3 test/remote_integration_test.py --host user@laptop \
  --master ~/.ssh/tmax-laptop.sock --tmux /opt/homebrew/bin/tmux
```

It checks host folding, interactive input/output, remote windows/splits/zoom,
local sidebar access, hidden output, reconnection, Vim restoration, and
confirmed session/pane deletion. Existing remote sessions are not targeted.
