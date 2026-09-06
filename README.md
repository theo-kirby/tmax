# tmax

Small additions on top of tmux, built for one person, one feature at a time.

tmax gives one tmux client a view of every session on every machine you care
about. `prefix + Space` opens an fzf popup listing local sessions and the
sessions on remote computers side by side; picking a remote one attaches it
through SSH as ordinary local windows and panes, with your own prefix, status
bar and key bindings. An optional sidebar lists sessions in a pane instead.

## Status

Working and in daily use:

- **Session switcher** (`prefix + Space`): fzf popup, vim-style normal and
  insert modes, local and remote sessions in one list, hosts refreshed in the
  background, creates a session when nothing matches.
- **Remote sessions**: discovered over SSH and attached through tmux control
  mode. New windows, splits, layouts, zoom, window rename and confirmed
  pane/window kills are routed to the remote machine. Reconnects after a
  dropped link.
- **Native tree** (`prefix + s` with the sidebar off): tmux's own session
  tree with remote sessions included.
- **Sidebar** (optional): session list in a left pane with groups, folding,
  reordering and an overview of every window.

Known limits of the remote bridge:

- Remote scrollback is not imported; copy-mode history starts when a pane is
  opened locally.
- Only the recognised bindings are translated. Commands typed at the `:`
  prompt act on the local proxy, not the remote machine.
- Each opened remote pane costs a small Python process and an SSH channel
  (one SSH transport per host). Listing sessions costs nothing.
- Exact restoration of every terminal mode and of mixed local/remote layouts
  is not guaranteed.

Requirements:

| Where  | Needs                                                        |
|--------|--------------------------------------------------------------|
| local  | tmux 3.3+, Python 3.9+, fzf 0.62+ (`brew install fzf`), macOS bash 3.2 is fine |
| remote | tmux 3.4+ reachable over SSH with key or shared-socket auth; nothing is installed there |

Developed on macOS with tmux 3.4 locally and macOS/Linux hosts on tmux 3.4.

## Install

Add this line to `~/.tmux.conf`:

```tmux
run-shell ~/tmax/tmax.tmux
```

Then reload tmux:

```sh
tmux source-file ~/.tmux.conf
```

No plugin manager is needed. (It also works with TPM, which runs every
`*.tmux` file in a plugin folder.) Settings go **before** the `run-shell`
line; see [Options](#options).

For remote machines, copy `remotes.example.json` to `remotes.json` and edit
it; see [Remote computers](#remote-computers). A fresh checkout makes no
remote connections.

## Quick use

| Key               | Action                                                  |
|-------------------|---------------------------------------------------------|
| `prefix + Space`  | session switcher popup                                  |
| `prefix + s`      | sidebar, or tmux's session tree when the sidebar is off |
| `prefix + S`      | tmux's session tree (sidebar mode)                      |

In the switcher: `j`/`k` move, `Enter` goes, `i` types a filter, `Esc` or
`q` closes. Remote sessions attach as you pick them. Once attached, they are
ordinary tmux sessions: `prefix + (` and `)`, the tree, and the switcher all
move between them.

## Session switcher

`prefix + Space` opens a popup with an [fzf](https://github.com/junegunn/fzf)
list of every session, local and remote, in the spirit of
[tmux-fzf](https://github.com/sainnhe/tmux-fzf). It works with the sidebar on
or off.

```
╭─ sessions ─────────────────────────────────╮
│ normal>                             11/11  │
│▌ lab     2 windows (attached)   macbook    │
│  tmax    2 windows              macbook    │
│  ouro    2 windows              mac mini   │
│  0       2 windows              5090 box   │
╰────────────────────────────────────────────╯
```

Each row is the session name, its window count, and the name of the
computer. Local sessions come first, then each host in the order of
`remotes.json`. Computer names are written in the terminal's own colours:
blue for local, then magenta, red and yellow for the hosts in order. On the
highlighted row the name takes the row's colour like the rest of the text.
The name is the entry's `label` in `remotes.json`, or the host key when
there is none; an optional `colour` (a tmux colour name, `colourN`, or
`#rrggbb`) overrides the palette. A `local` entry without a `destination`
names this machine:

```json
{
  "local": {"label": "macbook air"},
  "mmini": {"destination": "mmini", "tmux": "/opt/homebrew/bin/tmux", "label": "mac mini"},
  "sb1x":  {"destination": "sb1x", "tmux": "tmux", "label": "5090 box", "colour": "yellow"}
}
```

### Keys

The popup starts in a vim-like normal mode. Letters do nothing there except
the keys below, so `j` and `k` move without typing into the filter. Press
`i` (or `/`) for insert mode: type to filter on the session name, `Esc` goes
back to normal mode with the filter kept.

| Mode   | Key               | Action                                       |
|--------|-------------------|----------------------------------------------|
| normal | `j` `k` arrows    | move                                         |
| normal | `g` `G`           | first / last                                 |
| normal | `Ctrl-d` `Ctrl-u` | half page down / up                          |
| normal | `i` `/`           | insert mode                                  |
| normal | `Enter`           | go to the session                            |
| normal | `q` `Esc`         | close                                        |
| insert | typing            | filter; `Ctrl-j` `Ctrl-k` still move         |
| insert | `Backspace`       | edit the filter                              |
| insert | `Enter`           | go to the session; with no match, create a local session named after the text |
| insert | `Esc`             | back to normal mode                          |

Remote sessions connect when selected. (Their local proxies are still named
`host/session` in the native tmux tree.)

### Behaviour

The list appears at once with what tmux already knows: local sessions and
the remote ones seen before. The configured hosts are then refreshed in the
background, and the list is reloaded only if something changed, keeping the
cursor on the same session. Hosts that are offline keep their cached
entries. The popup shows in about 100 ms; a refresh takes half a second or
so per round trip and never blocks typing.

The popup has rounded corners. Its border and the highlighted line use the
colours of your status bar (`status-style`); the title is white. A status
bar without a background colour leaves the border in the default colour.

Without fzf the key shows a short message in the status line. This binding
replaces tmux's default `prefix + Space` (`next-layout`); that command is
still available from the `:` prompt or by binding another key.

Options, in `~/.tmux.conf` before the `run-shell` line:

```tmux
set -g @tmax-switch-key    "Space"  # prefix + key
set -g @tmax-switch-width  "60%"    # popup size, columns or percent
set -g @tmax-switch-height "50%"
```

## Remote computers

Hosts are entirely user-configured in `remotes.json` next to `tmax.tmux`
(ignored by Git). Copy `remotes.example.json` and edit it:

```json
{
  "local": {"label": "this machine"},
  "laptop": {
    "destination": "user@laptop",
    "tmux": "/opt/homebrew/bin/tmux",
    "control_path": "~/.ssh/tmax-laptop.sock",
    "label": "laptop"
  },
  "server": {"destination": "server", "tmux": "tmux", "label": "big server", "colour": "red"}
}
```

| Field          | Meaning                                                            |
|----------------|--------------------------------------------------------------------|
| `destination`  | what `ssh` gets; normal SSH configuration applies. Without it the entry is not a host (used for `local`) |
| `tmux`         | path of the tmux executable on that machine                        |
| `socket`       | optional; selects a nondefault tmux server there                   |
| `control_path` | optional; an existing SSH master socket to reuse (password hosts)  |
| `label`        | name shown in the session switcher; defaults to the key            |
| `colour`       | colour of that name; defaults to the palette                       |

Set `TMAX_REMOTES_FILE` in the local tmux environment to use a different
JSON file. An empty object disables remote hosts.

Hosts without `control_path` use unattended SSH authentication (put your key
on the box, for example with `ssh-copy-id host`) and share one private master
socket per host. For a host that needs a password, open a shared connection
from a terminal first; the password stays there and tmax will not prompt:

```sh
ssh -M -S ~/.ssh/tmax-laptop.sock -o ControlPersist=30m -fnN user@laptop
```

### How it works

Remote sessions appear locally as proxy sessions named `host/session` (the
host key, then the remote session name; they follow remote renames). Until
selected, a proxy is a lightweight placeholder. Selecting it connects its
windows and panes through tmux control mode over SSH. There is one local
status bar and one local prefix; the remote machine keeps its own tmux
configuration and running programs. Nothing is installed remotely.

Recognised bindings for new windows, splitting, pane/window deletion, window
renaming, zoom and layouts are routed to the remote machine. Navigation,
copy mode, paste, the switcher and the sidebar stay local. Window rename uses
a small prompt popup. Custom bindings and commands typed at the tmux `:`
prompt are **not** translated; use the routed keys for remote creation and
deletion. Deleting a proxy through a direct local tmux command only removes
the local copy, and the synchroniser may recreate it.

Hidden panes pause their output subscriptions without blocking the remote
programs; returning to a window restores its current screen. Metadata
refreshes every 10 seconds while the sidebar is open, and opened sessions
reconcile their windows and panes every 5 seconds while attached (15 seconds
while detached). Connections retry after interruption; keystrokes typed while
disconnected are discarded. If a host runs tmax itself, its own proxy
sessions (for example the ones it holds for this machine) are skipped, so
nothing shows up twice.

Diagnostics go to `remote.log` in the private `tmax-UID-HASH` directory under
`/tmp`. Failed sidebar mutations are recorded at
`$TMAX_STATE_DIR/remote-attach-error`.

## Native session tree

With the sidebar off (`set -g @tmax-sidebar off`), `prefix + s` refreshes
the configured hosts and opens tmux's normal session tree with remote
sessions included as `host/session` entries. Selecting one connects it.
Deleting there removes only the local representation; the routed kill keys
act remotely.

## Session sidebar

The sidebar is on by default and off with:

```tmux
set -g @tmax-sidebar off
```

`prefix + s` opens a sidebar on the left. Your work stays on the right.

```
+-----------+-----------------------------------+
| ● work    |                                   |
|   notes   |   your current session            |
|   scratch |                                   |
|           |                                   |
+-----------+-----------------------------------+
```

The sidebar shows only the session names, under a `local` heading followed
by one heading per host in `remotes.json`. The bottom line is used for
prompts and short messages.

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
| `n`           | new session (also on remote hosts)                 |
| `r`           | rename session                                     |
| `d`           | kill session (asks `y`/`N`)                        |
| `Space` `h`   | fold / unfold the group or host the cursor is in   |
| `t`           | tag: put the session in a group (`-` = no group)   |
| `J` `K`       | move the group, or the session in its group, down/up |
| `Esc` or `l`  | focus work pane, keep sidebar open                 |
| `q`           | close sidebar                                      |

On a group line, `Tab` and `Enter` also fold / unfold. `n` creates the new
session inside the group the cursor is in. `r` on a group line renames the
group. The sidebar follows you: if you change session another way, it moves
to the new session too. The stock tmux session tree is on `prefix + S`.

### Groups

Sessions can be put in groups, like folders. Press `t` on a session and type
a group name. Sessions without a group stay at the top. Groups come after,
sorted by name. Groups live inside `local`.

```
+-----------+
|   scratch |
| ▸ work  3 |    folded: shows the session count
| ▾ personal|
|   ● notes |
|     blog  |
+-----------+
```

`Space` (or `h`) folds or unfolds the group under the cursor. `J` and `K`
move a group, or a session inside its group. Groups, order and fold state
are saved under `~/.local/state/tmax/` (`groups`, `order`, `collapsed`) and
survive a tmux restart; a session name remembers its group. Set
`TMAX_STATE_DIR` in the tmux environment to use a different folder.

### Overview

With `@tmax-sidebar-overview` set to `on`, the right side shows every window
of the session, tiled, refreshed once a second. Each tile has a title line
(`1: logs *`, the `*` marks the current window) and the live text of that
window.

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

Keys inside a tile: `j` `k` `h` `l` or arrows move between tiles, `Enter`
goes to that window and leaves sidebar mode, `s` focuses the sidebar, `Esc`
or `q` leaves sidebar mode. The overview is a real tmux window named
`overview` that exists only while sidebar mode is on. Remote sessions have no
overview.

## Options

Put these in `~/.tmux.conf` **before** the `run-shell` line. Defaults shown.

```tmux
# switcher
set -g @tmax-switch-key    "Space"
set -g @tmax-switch-width  "60%"
set -g @tmax-switch-height "50%"

# sidebar
set -g @tmax-sidebar        "on"    # "off" = native tree on prefix + s
set -g @tmax-sidebar-key    "s"
set -g @tmax-sidebar-width  "28"    # columns
set -g @tmax-sidebar-follow "on"    # "off" = sidebar stays where it was opened
set -g @tmax-sidebar-overview "off" # "on" = window overview while the sidebar is open
set -g @tmax-sidebar-hover  "off"   # "on" = moving the cursor switches at once
```

Environment variables (set with `tmux set-environment -g`): `TMAX_REMOTES_FILE`
(hosts file), `TMAX_STATE_DIR` (groups and state), `TMAX_DEBUG` (trace file
for the sidebar scripts).

## Files

```
tmax.tmux              entry point: key bindings and hooks
scripts/remote.py      remote hosts over SSH control mode; the fzf session switcher
scripts/sidebar.sh     open / close / focus / move the sidebar pane; builds the overview
scripts/sidebar-ui.sh  the list that runs inside the sidebar pane
scripts/preview.sh     one tile of the overview
remotes.example.json   template for remotes.json
test/                  throwaway-server tests (see below)
```

## How a tmux plugin works

- A plugin is a folder with one `*.tmux` file at the top.
- That file is a normal shell script. tmux runs it once at start.
- The script calls `tmux bind-key`, `tmux set-option`, `tmux set-hook`.
- Key bindings point to helper scripts in `scripts/`.
- State is kept in tmux user options. They start with `@`.

## Debug

```sh
tmux set-environment -g TMAX_DEBUG /tmp/tmax.log
```

Then open the sidebar. The scripts trace every command to that file. Remove
with `tmux set-environment -gu TMAX_DEBUG`. Remote diagnostics are in
`remote.log` in the private `tmax-UID-HASH` directory under `/tmp`.

## Test

Each test starts its own throwaway tmux server and never touches yours.

```sh
python3 test/switch_test.py     # prefix + Space popup: modes, filter, create, cancel
python3 test/sidebar_test.py    # sidebar: groups, order, overview cleanup
```

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
