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

## Session sidebar

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

While the sidebar is open, the right side does not show one window. It shows
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
set -g @tmax-sidebar-overview "on" # "off" = no overview, sidebar next to your window
set -g @tmax-sidebar-hover "off"   # "on" = moving the cursor switches at once, no Tab needed
```

## Files

```
tmax.tmux              entry point: key bindings and hooks
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
