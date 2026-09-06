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
#   set -g @tmax-sidebar       "off" # native tmux tree with remote discovery
#   set -g @tmax-sidebar-key   "s"    # prefix + key toggles the sidebar
#   set -g @tmax-sidebar-width "28"   # width in columns
#   set -g @tmax-sidebar-follow "on"  # sidebar moves with you when you change session
#   set -g @tmax-sidebar-overview "on" # show every window of the session while the sidebar is open
#   set -g @tmax-sidebar-hover "off"  # "on" = moving the cursor shows that session right away

sidebar_key="$(get_opt "@tmax-sidebar-key" "s")"

# Migrate the old unindexed hook only if it belongs to this plugin.
legacy_hook="$(tmux show-hooks -g client-session-changed 2>/dev/null | head -1)"
case "$legacy_hook" in
  "client-session-changed[0]"*"$CURRENT_DIR/scripts/sidebar.sh follow"*)
    tmux set-hook -gu 'client-session-changed[0]' ;;
esac
tmux set-hook -gu 'client-session-changed[471]'
tmux set-hook -gu 'client-session-changed[472]'

if [ "$(get_opt "@tmax-sidebar" on)" = off ]; then
  "$CURRENT_DIR/scripts/sidebar.sh" close
  tmux bind-key "$sidebar_key" run-shell -b "python3 '$CURRENT_DIR/scripts/remote.py' tree '#{q:client_name}' '#{pane_id}'"
  tmux set-hook -g 'client-session-changed[472]' "run-shell -b \"python3 '$CURRENT_DIR/scripts/remote.py' activate '#{session_id}'\""
else
# The script cannot ask tmux "which session is this client on?" reliably (a
# run-shell has no tty), so tmux fills the session and pane in for us here.
tmux bind-key "$sidebar_key" run-shell "$CURRENT_DIR/scripts/sidebar.sh toggle '#{q:session_name}' '#{pane_id}'"

# Keep the stock tmux session tree reachable on prefix + S.
tmux bind-key S choose-tree -Zs

# When the client changes session by any other route (prefix + ( or ), etc.),
# bring the sidebar along.
if [ "$(get_opt "@tmax-sidebar-follow" "on")" = "on" ]; then
  tmux set-hook -g 'client-session-changed[471]' "run-shell \"$CURRENT_DIR/scripts/sidebar.sh follow '#{q:session_name}'\""
fi
fi

# --- Session switcher -------------------------------------------------------
# prefix + Space opens an fzf popup listing local and remote sessions.
#   set -g @tmax-switch-key    "Space"
#   set -g @tmax-switch-width  "60%"
#   set -g @tmax-switch-height "50%"
# display-popup does not expand formats in its command (tmux 3.4), so run-shell
# fills in the client name and opens the popup on that client. The border takes
# the status bar's background colour (or the default when it has none).
switch_key="$(get_opt "@tmax-switch-key" "Space")"
switch_size="-w '$(get_opt "@tmax-switch-width" "60%")' -h '$(get_opt "@tmax-switch-height" "50%")'"
switch_border="-S 'fg=#{?#{m/r:bg=,#{status-style}},#{s/.*bg=([^,]*).*/\\1/:status-style},default}'"
tmux bind-key "$switch_key" run-shell -b "tmux display-popup -c '#{q:client_name}' -E -b rounded -T '#[fg=white] sessions ' $switch_size $switch_border \"python3 '$CURRENT_DIR/scripts/remote.py' switch '#{q:client_name}'\""

# Keep recognized local bindings intact; route their remote branch via control mode.
python3 "$CURRENT_DIR/scripts/remote.py" install
