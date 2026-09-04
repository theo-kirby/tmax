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
| `Esc` or `l`  | focus work pane, keep sidebar open                 |
| `q`           | close sidebar                                      |

The sidebar follows you. If you change session another way (for example
`prefix + (`), the sidebar moves to the new session too.

The stock tmux session tree is still there on `prefix + S`.

### Options

Put these in `~/.tmux.conf` **before** the `run-shell` line:

```tmux
set -g @tmax-sidebar-key    "s"    # prefix + key
set -g @tmax-sidebar-width  "28"   # columns
set -g @tmax-sidebar-follow "on"   # "off" = sidebar stays where it was opened
```

## Files

```
tmax.tmux              entry point: key bindings and hooks
scripts/sidebar.sh     open / close / focus / move the sidebar pane
scripts/sidebar-ui.sh  the list that runs inside the sidebar pane
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
