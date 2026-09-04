#!/usr/bin/env bash
# tmax - small additions on top of tmux.
# Entry point. Load it from ~/.tmux.conf with:
#   run-shell ~/tmax/tmax.tmux
# (Also works with TPM, which runs every *.tmux file in the plugin dir.)

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

get_opt() {
  local value
  value="$(tmux show-option -gqv "$1")"
  printf '%s' "${value:-$2}"
}

# --- Session sidebar --------------------------------------------------------
# User options (set in ~/.tmux.conf before the run-shell line):
#   set -g @tmax-sidebar-key   "s"    # prefix + key toggles the sidebar
#   set -g @tmax-sidebar-width "28"   # width in columns
#   set -g @tmax-sidebar-follow "on"  # sidebar moves with you when you change session

sidebar_key="$(get_opt "@tmax-sidebar-key" "s")"
tmux bind-key "$sidebar_key" run-shell "$CURRENT_DIR/scripts/sidebar.sh toggle"

# Keep the stock tmux session tree reachable on prefix + S.
tmux bind-key S choose-tree -Zs

# When the client changes session by any other route (prefix + ( or ), etc.),
# bring the sidebar along.
if [ "$(get_opt "@tmax-sidebar-follow" "on")" = "on" ]; then
  tmux set-hook -g client-session-changed "run-shell '$CURRENT_DIR/scripts/sidebar.sh follow'"
fi
