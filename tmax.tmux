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

# Keep the stock local session tree on prefix + s.
tmux bind-key s choose-tree -Zs

# Connect an existing remote proxy when selected through normal tmux navigation.
tmux set-hook -g 'client-session-changed[472]' "run-shell -b \"python3 '$CURRENT_DIR/scripts/remote.py' activate '#{session_id}'\""

# --- Session switcher -------------------------------------------------------
# prefix + Space opens an fzf popup listing local and remote sessions.
#   set -g @tmax-switch-key    "Space"
#   set -g @tmax-switch-width  "75%"
#   set -g @tmax-switch-height "65%"
#   set -g @tmax-switch-hosts  "on" # remote sessions start shown; H toggles them
# display-popup does not expand formats in its command (tmux 3.4), so run-shell
# fills in the client name and opens the popup on that client. The border takes
# the status bar's background colour (or the default when it has none).
switch_key="$(get_opt "@tmax-switch-key" "Space")"
switch_size="-w '$(get_opt "@tmax-switch-width" "75%")' -h '$(get_opt "@tmax-switch-height" "65%")'"
switch_border="-S 'fg=#{?#{m/r:bg=,#{status-style}},#{s/.*bg=([^,]*).*/\\1/:status-style},default}'"
tmux bind-key "$switch_key" run-shell -b "tmux display-popup -c '#{q:client_name}' -E -b rounded -T '#[fg=white] sessions ' $switch_size $switch_border \"python3 '$CURRENT_DIR/scripts/remote.py' switch '#{q:client_name}'\""

# Keep recognized local bindings intact; route their remote branch via control mode.
python3 "$CURRENT_DIR/scripts/remote.py" install
