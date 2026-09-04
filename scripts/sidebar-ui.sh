#!/usr/bin/env bash
# The session list that runs INSIDE the sidebar pane.
# Works with the bash 3.2 that ships with macOS.
#
# Keys:
#   j / k / arrows   move
#   Enter            switch to session (sidebar follows, focus goes to your work pane)
#   n                new session
#   r                rename session
#   d                kill session (asks y/n)
#   Esc / l          focus your work pane, keep sidebar open
#   q                close sidebar
#   R                refresh list

set -u
OPT="@tmax-sidebar-pane"
ME="$TMUX_PANE"

# Debug: tmux set-environment -g TMAX_DEBUG /path/to/log ; then open the sidebar.
if [ -n "${TMAX_DEBUG:-}" ]; then exec 2>>"$TMAX_DEBUG"; set -x; fi

width="$(tmux show-option -gqv "@tmax-sidebar-width")"
width="${width:-28}"

# --- colours ---------------------------------------------------------------
if [ -t 1 ] && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; REV="$(tput rev)"; RST="$(tput sgr0)"
  GREEN="$(tput setaf 2)"; YELLOW="$(tput setaf 3)"; CYAN="$(tput setaf 6)"
else
  BOLD=""; DIM=""; REV=""; RST=""; GREEN=""; YELLOW=""; CYAN=""
fi

cleanup() {
  tput cnorm 2>/dev/null
  tput rmcup 2>/dev/null
  # Only forget the option if it still points at us.
  [ "$(tmux show-option -gqv "$OPT")" = "$ME" ] && tmux set-option -gu "$OPT"
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP

tput smcup 2>/dev/null
tput civis 2>/dev/null

# --- state -----------------------------------------------------------------
names=()      # session names
infos=()      # "windows attached" per session
sel=0
current=""    # session the sidebar currently lives in
status=""     # one-line message shown at bottom

load() {
  names=(); infos=()
  local line
  while IFS= read -r line; do
    names+=("${line%%	*}")
    infos+=("${line#*	}")
  done < <(tmux list-sessions -F '#{session_name}	#{session_windows}w#{?session_attached, *,}' 2>/dev/null | sort -f)
  current="$(tmux display-message -p -t "$ME" '#{session_name}')"
  local n=${#names[@]}
  [ "$n" -eq 0 ] && sel=0
  [ "$sel" -ge "$n" ] && [ "$n" -gt 0 ] && sel=$((n - 1))
  [ "$sel" -lt 0 ] && sel=0
}

# Put the cursor on the current session (used once at start).
select_current() {
  local i
  for i in "${!names[@]}"; do
    [ "${names[$i]}" = "$current" ] && { sel=$i; return; }
  done
}

render() {
  local cols rows i mark name info line pad
  read -r rows cols < <(stty size 2>/dev/null || echo "24 $width")
  tput clear
  printf '%s%s SESSIONS%s\n' "$BOLD" "$CYAN" "$RST"
  printf '%s%s%s\n' "$DIM" "$(printf '%*s' "$cols" '' | tr ' ' '-')" "$RST"
  for i in "${!names[@]}"; do
    name="${names[$i]}"; info="${infos[$i]}"
    if [ "$name" = "$current" ]; then mark="●"; else mark=" "; fi
    # name on the left, info on the right, fit to the pane width
    pad=$((cols - 3 - ${#name} - ${#info} - 1))
    [ "$pad" -lt 1 ] && pad=1
    line="$(printf ' %s %s%*s%s ' "$mark" "$name" "$pad" '' "$info")"
    if [ "$i" -eq "$sel" ]; then
      printf '%s%s%s\n' "$REV" "$line" "$RST"
    elif [ "$name" = "$current" ]; then
      printf '%s%s%s\n' "$GREEN" "$line" "$RST"
    else
      printf '%s\n' "$line"
    fi
  done
  [ "${#names[@]}" -eq 0 ] && printf '%s (no sessions)%s\n' "$DIM" "$RST"

  # footer
  tput cup $((rows - 4)) 0
  printf '%s%s%s\n' "$DIM" "$(printf '%*s' "$cols" '' | tr ' ' '-')" "$RST"
  printf '%s j/k move  ⏎ go  n new%s\n' "$DIM" "$RST"
  printf '%s d del  r rename  q close%s\n' "$DIM" "$RST"
  printf '%s%s%s' "$YELLOW" "${status:0:$((cols - 1))}" "$RST"
}

focus_work_pane() {
  # We are the leftmost full-height pane, so the neighbour is to the right.
  tmux select-pane -R -t "$ME" 2>/dev/null || tmux select-pane -l 2>/dev/null
}

# Switch the client to a session and bring the sidebar along.
goto() {
  local target="$1"
  [ -z "$target" ] && return
  if [ "$target" != "$current" ]; then
    tmux switch-client -t "$target" || { status="switch failed"; return; }
    tmux join-pane -fhb -l "$width" -d -s "$ME" -t "${target}:" 2>/dev/null
  fi
  focus_work_pane
}

ask() {
  # ask "prompt" -> echoes answer (empty if cancelled)
  local answer
  local rows cols
  read -r rows cols < <(stty size 2>/dev/null || echo "24 $width")
  { tput cnorm; tput cup $((rows - 1)) 0; tput el; } >/dev/tty
  IFS= read -r -e -p "$1" answer
  tput civis >/dev/tty
  printf '%s' "$answer"
}

new_session() {
  local name
  name="$(ask 'new session name: ')"
  [ -z "$name" ] && { status="cancelled"; return; }
  if tmux new-session -d -s "$name" 2>/dev/null; then
    load; goto "$name"
  else
    status="could not create '$name'"
  fi
}

rename_session() {
  [ "${#names[@]}" -eq 0 ] && return
  local old="${names[$sel]}" new
  new="$(ask "rename '$old' to: ")"
  [ -z "$new" ] && { status="cancelled"; return; }
  if tmux rename-session -t "$old" "$new" 2>/dev/null; then
    status="renamed"
  else
    status="rename failed"
  fi
}

kill_session() {
  [ "${#names[@]}" -eq 0 ] && return
  local target="${names[$sel]}" yn other i
  yn="$(ask "kill '$target'? [y/N] ")"
  [ "$yn" = "y" ] || [ "$yn" = "Y" ] || { status="cancelled"; return; }
  if [ "$target" = "$current" ]; then
    # Move ourselves (and the client) somewhere else first, or we die with it.
    other=""
    for i in "${!names[@]}"; do
      [ "${names[$i]}" != "$target" ] && { other="${names[$i]}"; break; }
    done
    if [ -n "$other" ]; then
      goto "$other"
      tmux select-pane -t "$ME"
      load
    fi
  fi
  tmux kill-session -t "$target" 2>/dev/null && status="killed '$target'"
}

# --- main loop ---------------------------------------------------------------
# bash 3.2 (macOS default) only takes whole seconds for read -t; bash 4+ takes
# fractions. This only affects how long a bare Esc waits for the rest of an
# arrow-key sequence.
if [ "${BASH_VERSINFO[0]}" -ge 4 ]; then ESC_WAIT=0.05; else ESC_WAIT=1; fi

load
select_current
while :; do
  render
  key=""
  # 1s timeout so the list refreshes and picks up resizes on its own.
  # NOTE: bash 3.2 returns 1 on timeout, bash 4+ returns >128. Treat any
  # failure as a timeout, but stop if the terminal is really gone.
  if ! IFS= read -rsn1 -t 1 key; then
    [ -t 0 ] || exit 0
    load; continue
  fi
  status=""
  case "$key" in
    $'\e')
      # Escape sequence (arrow keys) or a bare Esc.
      seq=""
      IFS= read -rsn2 -t "$ESC_WAIT" seq
      case "$seq" in
        '[A') key="k" ;;
        '[B') key="j" ;;
        '[C') key="l" ;;
        *)    key="esc" ;;
      esac
      ;;
    "") key="enter" ;;
  esac
  n=${#names[@]}
  case "$key" in
    j)     [ "$n" -gt 0 ] && sel=$(( (sel + 1) % n )) ;;
    k)     [ "$n" -gt 0 ] && sel=$(( (sel - 1 + n) % n )) ;;
    g)     sel=0 ;;
    G)     [ "$n" -gt 0 ] && sel=$((n - 1)) ;;
    enter) [ "$n" -gt 0 ] && goto "${names[$sel]}" ;;
    n)     new_session ;;
    r)     rename_session ;;
    d)     kill_session ;;
    R)     status="refreshed" ;;
    esc|l) focus_work_pane ;;
    q)     exit 0 ;;
  esac
  load
done
