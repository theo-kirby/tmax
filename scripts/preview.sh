#!/usr/bin/env bash
# One preview pane inside the overview window. Shows a title line and the
# text of one window's active pane, refreshed once a second.
# Works with the bash 3.2 that ships with macOS.
#
#   preview.sh @WINDOW_ID
#
# Keys:
#   Enter            go to this window and leave sidebar mode
#   h j k l arrows   move between previews
#   s                focus the sidebar
#   q / Esc          leave sidebar mode

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIN="$1"
ME="$TMUX_PANE"
SIDE_OPT="@tmax-sidebar-pane"

if [ -n "${TMAX_DEBUG:-}" ]; then exec 2>>"$TMAX_DEBUG"; set -x; fi

if [ -t 1 ] && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; RST="$(tput sgr0)"; GREEN="$(tput setaf 2)"
else
  BOLD=""; DIM=""; RST=""; GREEN=""
fi

cleanup() { tput cnorm 2>/dev/null; tput rmcup 2>/dev/null; }
trap cleanup EXIT
trap 'exit 0' TERM INT HUP
tput smcup 2>/dev/null
tput civis 2>/dev/null

# Fit a captured pane into N rows of W columns: drop the blank lines at the
# bottom, keep the last N lines, cut each to W visible columns. Colour escapes
# are kept and not counted. Continuation bytes of a UTF-8 character do not
# count either.
fit() {   # fit W N
  LC_ALL=C awk -v W="$1" -v N="$2" '
    BEGIN { for (k = 1; k < 256; k++) ord[sprintf("%c", k)] = k; last = 0 }
    {
      out = ""; n = 0; i = 1; L = length($0); keep = 1
      while (i <= L) {
        c = substr($0, i, 1); b = ord[c]
        if (b == 27) {
          j = i + 1
          if (substr($0, j, 1) == "[") {
            j++
            while (j <= L) { d = ord[substr($0, j, 1)]; if (d >= 64 && d <= 126) break; j++ }
          }
          out = out substr($0, i, j - i + 1); i = j + 1; continue
        }
        if (b >= 128 && b < 192) { if (keep) out = out c; i++; continue }
        if (n < W) { out = out c; n++; keep = 1 } else keep = 0
        i++
      }
      lines[NR] = out
      if (n > 0) last = NR
    }
    END {
      first = last - N + 1; if (first < 1) first = 1
      for (i = first; i <= last; i++) print lines[i] "\033[0m"
    }'
}

last=""
render() {
  local rows cols info src src_h idx name panes active title frame el line
  read -r rows cols < <(stty size 2>/dev/null || echo "24 80")
  info="$(tmux display-message -p -t "$WIN" '#{pane_id}	#{pane_height}	#{window_index}	#{window_name}	#{window_panes}	#{window_active}' 2>/dev/null)"
  if [ -z "$info" ]; then
    printf '%s%s (window is gone)%s' "$(tput home)" "$DIM" "$RST"
    tput ed; sleep 1; exit 0
  fi
  IFS='	' read -r src src_h idx name panes active <<EOF
$info
EOF
  # The sidebar may still be the active pane of that window for a moment.
  [ "$src" = "$(tmux show-option -gqv "$SIDE_OPT")" ] && return

  title=" $idx: $name"
  [ "$panes" -gt 1 ] && title="$title ($panes panes)"
  [ "$active" = "1" ] && title="$title *"
  frame="$(tmux capture-pane -p -e -t "$src" 2>/dev/null | fit "$cols" $((rows - 1)))"
  [ "$title|$frame|$rows|$cols" = "$last" ] && return
  last="$title|$frame|$rows|$cols"

  # Newlines go BEFORE each line, never after the last one: a newline on the
  # bottom row would scroll the whole pane up by one.
  el="$(tput el)"
  tput home
  if [ "$active" = "1" ]; then printf '%s%s%s%s%s' "$BOLD" "$GREEN" "${title:0:$cols}" "$RST" "$el"
  else printf '%s%s%s%s' "$BOLD" "${title:0:$cols}" "$RST" "$el"; fi
  while IFS= read -r line; do printf '\n%s%s' "$line" "$el"; done <<EOF
$frame
EOF
  tput ed
}

read_escape_rest() {
  local saved
  saved="$(stty -g)"
  stty -icanon -echo min 0 time 1
  dd bs=1 count=2 2>/dev/null
  stty "$saved"
}

while :; do
  render
  key=""
  if ! IFS= read -rsn1 -t 1 key; then
    [ -t 0 ] || exit 0
    continue
  fi
  case "$key" in
    $'\e')
      seq="$(read_escape_rest)"
      case "$seq" in
        '[A') key="k" ;; '[B') key="j" ;; '[C') key="l" ;; '[D') key="h" ;;
        *)    key="esc" ;;
      esac ;;
    "") key="enter" ;;
  esac
  case "$key" in
    enter) "$DIR/sidebar.sh" pick "$WIN"; exit 0 ;;
    h)     tmux select-pane -L -t "$ME" ;;
    j)     tmux select-pane -D -t "$ME" ;;
    k)     tmux select-pane -U -t "$ME" ;;
    l)     tmux select-pane -R -t "$ME" ;;
    s)     tmux select-pane -t "$(tmux show-option -gqv "$SIDE_OPT")" 2>/dev/null ;;
    q|esc) "$DIR/sidebar.sh" close; exit 0 ;;
  esac
done
