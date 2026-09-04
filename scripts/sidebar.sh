#!/usr/bin/env bash
# Controls the session sidebar pane from outside the pane.
#
#   sidebar.sh toggle   open it / focus it / close it (see below)
#   sidebar.sh open     open it and focus it
#   sidebar.sh close    close it
#   sidebar.sh follow   move it into the window the client shows now
#
# Toggle logic:
#   no sidebar                     -> open + focus
#   sidebar exists, not focused    -> move it here if needed, then focus
#   sidebar focused                -> close

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT="@tmax-sidebar-pane"

width="$(tmux show-option -gqv "@tmax-sidebar-width")"
width="${width:-28}"

pane_id="$(tmux show-option -gqv "$OPT")"

pane_alive() {
  [ -n "$1" ] && tmux list-panes -a -F '#{pane_id}' | grep -qx -- "$1"
}

forget() { tmux set-option -gu "$OPT"; }

open_sidebar() {
  local id
  id="$(tmux split-window -fhb -l "$width" -P -F '#{pane_id}' "$DIR/sidebar-ui.sh")"
  tmux set-option -g "$OPT" "$id"
}

close_sidebar() {
  pane_alive "$pane_id" && tmux kill-pane -t "$pane_id"
  forget
}

# Move the sidebar into the window the current client shows, if it is not there.
bring_here() {
  local cur_win side_win
  cur_win="$(tmux display-message -p '#{window_id}')"
  side_win="$(tmux display-message -p -t "$pane_id" '#{window_id}')"
  if [ "$cur_win" != "$side_win" ]; then
    tmux join-pane -fhb -l "$width" -d -s "$pane_id" -t "$cur_win"
  fi
}

case "${1:-toggle}" in
  open)
    if pane_alive "$pane_id"; then bring_here; tmux select-pane -t "$pane_id"
    else open_sidebar; fi
    ;;
  close)
    close_sidebar
    ;;
  follow)
    pane_alive "$pane_id" && bring_here
    ;;
  toggle)
    if pane_alive "$pane_id"; then
      active="$(tmux display-message -p '#{pane_id}')"
      if [ "$active" = "$pane_id" ]; then
        close_sidebar
      else
        bring_here
        tmux select-pane -t "$pane_id"
      fi
    else
      forget
      open_sidebar
    fi
    ;;
  *)
    echo "usage: sidebar.sh [toggle|open|close|follow]" >&2; exit 1
    ;;
esac
