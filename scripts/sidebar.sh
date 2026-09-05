#!/usr/bin/env bash
# Controls the session sidebar pane from outside the pane.
#
#   sidebar.sh toggle [SESSION [PANE]]   open it / focus it / close it (see below)
#   sidebar.sh open [SESSION]    open it and focus it
#   sidebar.sh close             close it (and every overview window)
#   sidebar.sh follow [SESSION]  move it into that session (default: the
#                                client's session); builds the overview there
#   sidebar.sh pick WINDOW_ID    go to that window and close sidebar mode
#   sidebar.sh prune             kill every overview window (used on exit)
#
# SESSION and PANE say where the client is. Pass them from a key binding or a
# hook as '#{q:session_name}' and '#{pane_id}': tmux fills them in. Without
# them the script asks tmux, which only works when run from inside a pane.
#
# Toggle logic:
#   no sidebar                     -> open + focus
#   sidebar exists, not focused    -> move it here if needed, then focus
#   sidebar focused                -> close
#
# Overview (option @tmax-sidebar-overview, default on):
#   While the sidebar is open, the session shows an extra window named
#   "overview": one preview pane per window (scripts/preview.sh), tiled, with
#   the sidebar as the left column. Overview windows carry the window option
#   @tmax-overview=1 so we can find and remove them again.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT="@tmax-sidebar-pane"
OVW_OPT="@tmax-overview"

get_opt() {
  local value
  value="$(tmux show-option -gqv "$1")"
  printf '%s' "${value:-$2}"
}
width="$(get_opt "@tmax-sidebar-width" 28)"
pane_id="$(tmux show-option -gqv "$OPT")"

STATE="${TMAX_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tmax}"
LOCK="$STATE/lock"

overview_on() { [ "$(get_opt "@tmax-sidebar-overview" on)" = "on" ]; }

pane_alive() {
  [ -n "$1" ] && tmux list-panes -a -F '#{pane_id}' | grep -qx -- "$1"
}

forget() { tmux set-option -gu "$OPT"; }

# --- lock ---------------------------------------------------------------------
# follow is called twice on a session switch from the sidebar: once by the
# sidebar itself and once by the client-session-changed hook. Serialize them so
# only one overview window is built.
lock() {
  local i=0
  mkdir -p "$STATE" 2>/dev/null
  while ! mkdir "$LOCK" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then rmdir "$LOCK" 2>/dev/null; else sleep 0.1; fi
  done
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
}

# --- overview -----------------------------------------------------------------
all_overviews() {
  tmux list-windows -a -F "#{window_id} #{$OVW_OPT}" 2>/dev/null | awk '$2 == "1" { print $1 }'
}

find_overview() {   # find_overview SESSION -> window id or ""
  tmux list-windows -t "$1" -F "#{window_id} #{$OVW_OPT}" 2>/dev/null | awk '$2 == "1" { print $1; exit }'
}

# Kill every overview window except $1 (may be empty).
prune_overviews() {
  local w
  for w in $(all_overviews); do
    [ "$w" != "${1:-}" ] && tmux kill-window -t "$w" 2>/dev/null
  done
}

# Build the overview window for a session: one preview pane per window, tiled.
build_overview() {   # build_overview SESSION -> echoes the new window id
  local sess="$1" ovw="" w
  for w in $(tmux list-windows -t "$sess" -F '#{window_id}'); do
    if [ -z "$ovw" ]; then
      ovw="$(tmux new-window -d -t "$sess:" -n overview -P -F '#{window_id}' "$DIR/preview.sh $w")"
      tmux set-option -w -t "$ovw" "$OVW_OPT" 1
    else
      tmux split-window -d -t "$ovw" "$DIR/preview.sh $w"
      tmux select-layout -t "$ovw" tiled
    fi
  done
  printf '%s' "$ovw"
}

# Make sure the session shows its overview with the sidebar in it.
show_overview() {   # show_overview SESSION
  local sess="$1" ovw side_win
  ovw="$(find_overview "$sess")"
  [ -z "$ovw" ] && ovw="$(build_overview "$sess")"
  side_win="$(tmux display-message -p -t "$pane_id" '#{window_id}')"
  if [ "$side_win" != "$ovw" ]; then
    tmux join-pane -fhb -l "$width" -d -s "$pane_id" -t "$ovw"
  fi
  tmux select-window -t "$ovw"
  prune_overviews "$ovw"
}

# --- sidebar pane ---------------------------------------------------------------
open_sidebar() {   # open_sidebar [SESSION]
  local id sess="${1:-}" ovw
  [ -z "$sess" ] && sess="$(tmux display-message -p '#{session_name}')"
  if overview_on; then
    # Build the overview first, then start the sidebar inside it. This way the
    # window you were on never gets touched.
    lock
    ovw="$(find_overview "$sess")"
    [ -z "$ovw" ] && ovw="$(build_overview "$sess")"
    id="$(tmux split-window -fhb -l "$width" -t "$ovw" -P -F '#{pane_id}' "$DIR/sidebar-ui.sh")"
    tmux set-option -g "$OPT" "$id"
    tmux select-window -t "$ovw"
    tmux select-pane -t "$id"
    prune_overviews "$ovw"
  else
    id="$(tmux split-window -fhb -l "$width" -t "$sess:" -P -F '#{pane_id}' "$DIR/sidebar-ui.sh")"
    tmux set-option -g "$OPT" "$id"
  fi
  pane_id="$id"
}

close_sidebar() {
  pane_alive "$pane_id" && tmux kill-pane -t "$pane_id"
  forget
  prune_overviews
}

# Move the sidebar into a session's current window (or its overview).
bring_here() {   # bring_here [SESSION]
  local sess="${1:-}" cur_win side_win
  [ -z "$sess" ] && sess="$(tmux display-message -p '#{session_name}')"
  lock
  if overview_on; then
    show_overview "$sess"
    return
  fi
  cur_win="$(tmux display-message -p -t "$sess:" '#{window_id}')"
  side_win="$(tmux display-message -p -t "$pane_id" '#{window_id}')"
  if [ "$cur_win" != "$side_win" ]; then
    tmux join-pane -fhb -l "$width" -d -s "$pane_id" -t "$cur_win"
  fi
}

case "${1:-toggle}" in
  open)
    if pane_alive "$pane_id"; then bring_here "${2:-}"; tmux select-pane -t "$pane_id"
    else open_sidebar "${2:-}"; fi
    ;;
  close)
    close_sidebar
    ;;
  follow)
    pane_alive "$pane_id" && bring_here "${2:-}"
    ;;
  pick)
    [ -n "${2:-}" ] && tmux select-window -t "$2"
    close_sidebar
    ;;
  prune)
    prune_overviews
    ;;
  toggle)
    if pane_alive "$pane_id"; then
      active="${3:-$(tmux display-message -p '#{pane_id}')}"
      if [ "$active" = "$pane_id" ]; then
        close_sidebar
      else
        bring_here "${2:-}"
        tmux select-pane -t "$pane_id"
      fi
    else
      forget
      open_sidebar "${2:-}"
    fi
    ;;
  *)
    echo "usage: sidebar.sh [toggle|open|close|follow [session]|pick window|prune]" >&2; exit 1
    ;;
esac
