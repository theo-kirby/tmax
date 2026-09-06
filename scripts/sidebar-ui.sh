#!/usr/bin/env bash
# The session list that runs INSIDE the sidebar pane.
# Works with the bash 3.2 that ships with macOS.
#
# Keys:
#   j / k / arrows   move
#   Tab              switch to session, keep focus in the sidebar
#                    (with option @tmax-sidebar-hover on, moving does this too)
#   Enter            switch to session and close the sidebar
#   Space            fold / unfold the group under the cursor
#   h / Left         same as Space (vim-style)
#   J / K            move the group, or the session inside its group, down / up
#   t                tag: put the selected session in a group ("-" = no group)
#   n                new session, in the group the cursor is in
#   r                rename session, or rename the group when on a group line
#   d                kill session (asks y/n)
#   Esc / l          focus your work pane, keep sidebar open
#   q                close sidebar
#   R                refresh list
#
# On a group line, Tab and Enter also fold / unfold.
#
# Groups and fold state live in $TMAX_STATE_DIR (default ~/.local/state/tmax):
#   groups      one "session<TAB>group" line per tagged session; line order
#               is the order of sessions inside a group
#   order       one group name per line, top to bottom
#   collapsed   one group name per line

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT="@tmax-sidebar-pane"
ME="$TMUX_PANE"

# Debug: tmux set-environment -g TMAX_DEBUG /path/to/log ; then open the sidebar.
if [ -n "${TMAX_DEBUG:-}" ]; then exec 2>>"$TMAX_DEBUG"; set -x; fi

width="$(tmux show-option -gqv "@tmax-sidebar-width")"
width="${width:-28}"
hover=0
[ "$(tmux show-option -gqv "@tmax-sidebar-hover")" = "on" ] && hover=1

STATE="${TMAX_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tmax}"
GROUPS_FILE="$STATE/groups"
FOLD_FILE="$STATE/collapsed"
ORDER_FILE="$STATE/order"
mkdir -p "$STATE" 2>/dev/null

# --- colours ---------------------------------------------------------------
if [ -t 1 ] && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; REV="$(tput rev)"; RST="$(tput sgr0)"
  GREEN="$(tput setaf 2)"; YELLOW="$(tput setaf 3)"; CYAN="$(tput setaf 6)"
else
  BOLD=""; DIM=""; REV=""; RST=""; GREEN=""; YELLOW=""; CYAN=""
fi

cleanup() {
  trap '' TERM INT HUP
  tput cnorm 2>/dev/null
  tput rmcup 2>/dev/null
  # Only forget the option if it still points at us.
  [ "$(tmux show-option -gqv "$OPT")" = "$ME" ] && tmux set-option -gu "$OPT"
  # Overview windows go away with the sidebar. This may kill our own window.
  "$DIR/sidebar.sh" prune
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP

tput smcup 2>/dev/null
tput civis 2>/dev/null

# --- saved state: groups and folds ------------------------------------------
# The groups file is read once per load() into these two parallel arrays.
gs_name=()
gs_group=()

read_groups_file() {
  gs_name=(); gs_group=()
  [ -f "$GROUPS_FILE" ] || return 0
  local line
  while IFS= read -r line; do
    gs_name+=("${line%%	*}"); gs_group+=("${line#*	}")
  done < "$GROUPS_FILE"
}

lookup_group() {   # lookup_group SESSION -> echoes its group ("" = none)
  local i
  for i in ${gs_name[@]+"${!gs_name[@]}"}; do
    [ "${gs_name[$i]}" = "$1" ] && { printf '%s' "${gs_group[$i]}"; return; }
  done
}

set_group() {      # set_group SESSION GROUP   ("" removes the tag)
  local name="$1" group="$2" line
  {
    if [ -f "$GROUPS_FILE" ]; then
      while IFS= read -r line; do
        if [ "${line%%	*}" != "$name" ]; then printf '%s\n' "$line"; fi
      done < "$GROUPS_FILE"
    fi
    if [ -n "$group" ]; then printf '%s\t%s\n' "$name" "$group"; fi
  } > "$GROUPS_FILE.tmp" && mv "$GROUPS_FILE.tmp" "$GROUPS_FILE"
}

rename_group_everywhere() {   # rename_group_everywhere OLD NEW
  local old="$1" new="$2" line
  if [ -f "$GROUPS_FILE" ]; then
    {
      while IFS= read -r line; do
        if [ "${line#*	}" = "$old" ]; then printf '%s\t%s\n' "${line%%	*}" "$new"
        else printf '%s\n' "$line"; fi
      done < "$GROUPS_FILE"
    } > "$GROUPS_FILE.tmp" && mv "$GROUPS_FILE.tmp" "$GROUPS_FILE"
  fi
  if [ -f "$ORDER_FILE" ]; then
    {
      while IFS= read -r line; do
        if [ "$line" = "$old" ]; then printf '%s\n' "$new"; else printf '%s\n' "$line"; fi
      done < "$ORDER_FILE"
    } > "$ORDER_FILE.tmp" && mv "$ORDER_FILE.tmp" "$ORDER_FILE"
  fi
  if is_folded "$old"; then unfold "$old"; fold "$new"; fi
}

in_list() {   # in_list NEEDLE ITEM...
  local needle="$1" item; shift
  for item in "$@"; do [ "$item" = "$needle" ] && return 0; done
  return 1
}

# write_session_order GROUP NAME...  rewrite the groups file so the sessions of
# GROUP come in this order. Ungrouped sessions (GROUP = "") get a line too.
write_session_order() {
  local group="$1" name line; shift
  {
    if [ -f "$GROUPS_FILE" ]; then
      while IFS= read -r line; do
        if [ "${line#*	}" != "$group" ]; then printf '%s\n' "$line"; fi
      done < "$GROUPS_FILE"
    fi
    for name in "$@"; do printf '%s\t%s\n' "$name" "$group"; done
  } > "$GROUPS_FILE.tmp" && mv "$GROUPS_FILE.tmp" "$GROUPS_FILE"
}

# write_group_order NAME...  these groups first, then any others already in the file.
write_group_order() {
  local line
  {
    printf '%s\n' "$@"
    if [ -f "$ORDER_FILE" ]; then
      while IFS= read -r line; do
        if [ -n "$line" ] && ! in_list "$line" "$@"; then printf '%s\n' "$line"; fi
      done < "$ORDER_FILE"
    fi
  } > "$ORDER_FILE.tmp" && mv "$ORDER_FILE.tmp" "$ORDER_FILE"
}

is_folded() { [ -f "$FOLD_FILE" ] && grep -qxF -- "$1" "$FOLD_FILE"; }

fold() { is_folded "$1" || printf '%s\n' "$1" >> "$FOLD_FILE"; }

unfold() {
  local line
  [ -f "$FOLD_FILE" ] || return 0
  {
    while IFS= read -r line; do
      if [ "$line" != "$1" ]; then printf '%s\n' "$line"; fi
    done < "$FOLD_FILE"
  } > "$FOLD_FILE.tmp" && mv "$FOLD_FILE.tmp" "$FOLD_FILE"
}

toggle_fold() { if is_folded "$1"; then unfold "$1"; else fold "$1"; fi; }

# --- screen state ------------------------------------------------------------
# The list is a flat array of visible rows. Each row is one of:
#   S  a session          rname = session name,  rinfo = "2w *", rgroup = its group
#   G  an open group      rname = group name,    rinfo = ""
#   F  a folded group     rname = group name,    rinfo = session count
rtype=(); rname=(); rinfo=(); rgroup=(); rtarget=()
sel=0
current=""        # session the sidebar currently lives in
current_group=""  # its group
status=""         # one-line message shown at bottom
nsessions=0

add_row() { rtype+=("$1"); rname+=("$2"); rinfo+=("$3"); rgroup+=("$4"); rtarget+=("${5:-$2}"); }

load() {
  local line i j group count
  local selected_identity=""
  [ "${#rtype[@]}" -gt 0 ] && selected_identity="${rtype[$sel]}|${rgroup[$sel]}|${rtarget[$sel]}"
  local anames=() ainfos=() placed=()
  local snames=() sinfos=() sgroups=() inuse=() glist=()

  # 1. live sessions, alphabetical
  while IFS= read -r line; do
    anames+=("${line%%	*}"); ainfos+=("${line#*	}")
  done < <(tmux list-sessions -F '#{?@tmax-remote-host,,#{session_name}	#{session_windows}w#{?session_attached, *,}}' 2>/dev/null | sed '/^$/d' | sort -f)
  nsessions=${#anames[@]}
  current="$(tmux display-message -p -t "$ME" '#{session_name}')"
  # The overview window (sidebar mode) is ours, do not count it.
  if [ "$(tmux list-windows -t "$current" -F '#{@tmax-overview}' 2>/dev/null | grep -c 1)" -gt 0 ]; then
    for i in "${!anames[@]}"; do
      if [ "${anames[$i]}" = "$current" ]; then
        ainfos[$i]="$(( ${ainfos[$i]%%w*} - 1 ))w${ainfos[$i]#*w}"
      fi
    done
  fi

  # 2. put them in display order: the order of the groups file first, then the rest
  read_groups_file
  for i in ${gs_name[@]+"${!gs_name[@]}"}; do
    for j in ${anames[@]+"${!anames[@]}"}; do
      if [ "${anames[$j]}" = "${gs_name[$i]}" ] && [ -z "${placed[$j]:-}" ]; then
        placed[$j]=1; snames+=("${anames[$j]}"); sinfos+=("${ainfos[$j]}")
      fi
    done
  done
  for j in ${anames[@]+"${!anames[@]}"}; do
    [ -z "${placed[$j]:-}" ] && { snames+=("${anames[$j]}"); sinfos+=("${ainfos[$j]}"); }
  done

  # 3. the group of each session, and the groups in use: order file first, then alphabetical
  for i in ${snames[@]+"${!snames[@]}"}; do
    sgroups[$i]="$(lookup_group "${snames[$i]}")"
  done
  current_group="$(lookup_group "$current")"
  if [ "$nsessions" -gt 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && inuse+=("$line")
    done < <(printf '%s\n' "${sgroups[@]}" | sort -fu)
  fi
  if [ -f "$ORDER_FILE" ]; then
    while IFS= read -r line; do
      for j in ${inuse[@]+"${!inuse[@]}"}; do
        [ "${inuse[$j]}" = "$line" ] && { glist+=("$line"); inuse[$j]=""; }
      done
    done < "$ORDER_FILE"
  fi
  for j in ${inuse[@]+"${!inuse[@]}"}; do
    [ -n "${inuse[$j]}" ] && glist+=("${inuse[$j]}")
  done

  # 4. rows: ungrouped sessions first (flat), then one block per group
  rtype=(); rname=(); rinfo=(); rgroup=(); rtarget=()
  if is_folded 'host:local'; then
    add_row h local "$nsessions" 'host:local'
  else
    add_row H local "" 'host:local'
  for i in ${snames[@]+"${!snames[@]}"}; do
    [ -z "${sgroups[$i]}" ] && add_row S "${snames[$i]}" "${sinfos[$i]}" ""
  done

  for group in ${glist[@]+"${glist[@]}"}; do
    if is_folded "$group"; then
      count=0
      for i in "${!snames[@]}"; do
        [ "${sgroups[$i]}" = "$group" ] && count=$((count + 1))
      done
      add_row F "$group" "$count" "$group"
    else
      add_row G "$group" "" "$group"
      for i in "${!snames[@]}"; do
        [ "${sgroups[$i]}" = "$group" ] && add_row S "${snames[$i]}" "${sinfos[$i]}" "$group"
      done
    fi
  done

  fi
  local remote_type remote_name remote_info remote_id remote_host="" remote_folded=0
  while IFS=$'\t' read -r remote_type remote_name remote_info remote_id; do
    if [ "$remote_type" = H ]; then
      remote_host="$remote_name"; remote_folded=0
      if is_folded "host:$remote_host"; then remote_folded=1; remote_type=h; fi
      add_row "$remote_type" "$remote_host" "$remote_info" "host:$remote_host"
    elif [ "$remote_type" = R ] && [ "$remote_folded" -eq 0 ]; then
      add_row R "$remote_name" "$remote_info" "host:$remote_host" "$remote_id"
    fi
  done < <(python3 "$DIR/remote.py" rows)

  for i in ${rtype[@]+"${!rtype[@]}"}; do
    if [ "${rtype[$i]}|${rgroup[$i]}|${rtarget[$i]}" = "$selected_identity" ]; then sel=$i; break; fi
  done

  local n=${#rtype[@]}
  [ "$n" -eq 0 ] && sel=0
  [ "$sel" -ge "$n" ] && [ "$n" -gt 0 ] && sel=$((n - 1))
  [ "$sel" -lt 0 ] && sel=0
}

# Put the cursor on a session. If its group is folded, on the group line.
select_session() {
  local i
  for i in ${rtype[@]+"${!rtype[@]}"}; do
    [ "${rtype[$i]}" = "S" ] && [ "${rname[$i]}" = "$1" ] && { sel=$i; return; }
    if [ "${rtype[$i]}" = R ]; then
      [ "${rgroup[$i]#host:}/${rname[$i]}" = "$1" ] && { sel=$i; return; }
    fi
  done
  select_group "$(lookup_group "$1")"
}

# Put the cursor on a group line (open or folded).
select_group() {
  local i
  [ -z "$1" ] && return
  for i in ${rtype[@]+"${!rtype[@]}"}; do
    [ "${rtype[$i]}" != "S" ] && [ "${rname[$i]}" = "$1" ] && { sel=$i; return; }
  done
}

render() {
  local cols rows i type name info group mark indent line pad
  read -r rows cols < <(stty size 2>/dev/null || echo "24 $width")
  local el; el="$(tput el)"
  local first=0
  [ "$sel" -ge "$((rows - 1))" ] && first=$((sel - rows + 2))
  tput home
  for i in ${rtype[@]+"${!rtype[@]}"}; do
    [ "$i" -lt "$first" ] && continue
    [ "$i" -ge "$((first + rows - 1))" ] && break
    type="${rtype[$i]}"; name="${rname[$i]}"; info="${rinfo[$i]}"; group="${rgroup[$i]}"
    case "$type" in
      S) if [ "$name" = "$current" ]; then mark="●"; else mark=" "; fi
         if [ -n "$group" ]; then indent="     "; else indent="   "; fi ;;
      R) mark=" "; indent="   "
         [ "${group#host:}/$name" = "$current" ] && mark="●" ;;
      G) mark="▾"; indent="   " ;;
      F) mark="▸"; indent="   " ;;
      H) mark="▾"; indent=" " ;;
      h) mark="▸"; indent=" " ;;
    esac
    # name on the left, info on the right, fit to the pane width
    pad=$((cols - ${#indent} - 2 - ${#name} - ${#info} - 1))
    [ "$pad" -lt 1 ] && pad=1
    line="$(printf '%s%s %s%*s%s ' "$indent" "$mark" "$name" "$pad" '' "$info")"
    line="${line:0:$((cols - 1))}"
    if [ "$i" -eq "$sel" ]; then
      printf '%s%s%s%s\n' "$REV" "$line" "$RST" "$el"
    elif { [ "$type" = "S" ] && [ "$name" = "$current" ]; } || { [ "$type" = R ] && [ "$mark" = "●" ]; }; then
      printf '%s%s%s%s\n' "$GREEN" "$line" "$RST" "$el"
    elif [ "$type" = "F" ] && [ "$name" = "$current_group" ]; then
      printf '%s%s%s%s%s\n' "$BOLD" "$GREEN" "$line" "$RST" "$el"
    elif [ "$type" != "S" ] && [ "$type" != R ]; then
      printf '%s%s%s%s\n' "$BOLD" "$line" "$RST" "$el"
    else
      printf '%s%s\n' "$line" "$el"
    fi
  done
  [ "${#rtype[@]}" -eq 0 ] && printf '%s (no sessions)%s%s\n' "$DIM" "$RST" "$el"
  # blank the gap between the list and the footer
  tput ed

  # bottom line: short messages and prompts
  tput cup $((rows - 1)) 0
  printf '%s%s%s%s' "$YELLOW" "${status:0:$((cols - 1))}" "$RST" "$el"
}

# Everything the screen depends on, as one string. Redraw only when it changes.
snapshot() {
  local size; size="$(stty size 2>/dev/null)"
  printf '%s|%s|%s|%s|%s|%s|%s|%s' "$size" "$sel" "$current" "$current_group" "$status" \
    "${rtype[*]-}" "${rname[*]-}" "${rinfo[*]-}"
}

focus_work_pane() {
  # We are the leftmost full-height pane, so the neighbour is to the right.
  tmux select-pane -R -t "$ME" 2>/dev/null || tmux select-pane -l 2>/dev/null
}

# Switch the client to a session and bring the sidebar along.
#   goto NAME          switch, focus stays in the sidebar
#   goto NAME work     switch, focus goes to the work pane
#   goto NAME close    switch, then close the sidebar
goto() {
  local target="$1" after="${2:-}"
  [ -z "$target" ] && return
  if [ "$target" != "$current" ]; then
    tmux switch-client -t "$target" || { status="switch failed"; return; }
    "$DIR/sidebar.sh" follow "$target"
  fi
  case "$after" in
    work)  focus_work_pane ;;
    close) focus_work_pane; exit 0 ;;
    *)     tmux select-pane -t "$ME" ;;
  esac
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

# --- row helpers ---------------------------------------------------------------
on_row()     { [ "${#rtype[@]}" -gt 0 ]; }
row_type()   { printf '%s' "${rtype[$sel]}"; }
row_name()   { printf '%s' "${rname[$sel]}"; }
row_group()  { printf '%s' "${rgroup[$sel]}"; }
on_session() { on_row && [ "$(row_type)" = "S" ]; }

# Space / h: fold or unfold the group the cursor is in.
fold_here() {
  on_row || return
  local group; group="$(row_group)"
  [ -z "$group" ] && { status="not in a group"; return; }
  toggle_fold "$group"
  load
  if [[ "$group" = host:* ]]; then select_group "${group#host:}"; else select_group "$group"; fi
}

# Tab / Enter: go to the session, or fold/unfold when on a group line.
activate() {
  on_row || return
  if [ "$(row_type)" = R ]; then
    local target
    target="$(python3 "$DIR/remote.py" attach "${rgroup[$sel]#host:}" "${rtarget[$sel]}" 2>"$STATE/remote-attach-error")" || { status="could not open remote session"; return; }
    goto "$target" "${1:-}"
    return
  fi
  if on_session; then goto "$(row_name)" "${1:-}"; else fold_here; fi
}

# J / K: move the group, or the session inside its group, one step.
move_here() {
  on_row || return
  case "$(row_type)" in H|h|R) status="host order comes from remotes.json"; return ;; esac
  local dir="$1" type name group i pos=-1 k tmp sibs=()
  type="$(row_type)"; name="$(row_name)"; group="$(row_group)"
  # the rows this one can trade places with, in screen order
  for i in "${!rtype[@]}"; do
    if [ "$type" = "S" ]; then
      [ "${rtype[$i]}" = "S" ] && [ "${rgroup[$i]}" = "$group" ] && sibs+=("${rname[$i]}")
    else
      { [ "${rtype[$i]}" = G ] || [ "${rtype[$i]}" = F ]; } && sibs+=("${rname[$i]}")
    fi
  done
  for i in "${!sibs[@]}"; do [ "${sibs[$i]}" = "$name" ] && pos=$i; done
  k=$((pos + dir))
  if [ "$pos" -lt 0 ] || [ "$k" -lt 0 ] || [ "$k" -ge "${#sibs[@]}" ]; then return; fi
  tmp="${sibs[$pos]}"; sibs[$pos]="${sibs[$k]}"; sibs[$k]="$tmp"
  if [ "$type" = "S" ]; then
    write_session_order "$group" "${sibs[@]}"
    load; select_session "$name"
  else
    write_group_order "${sibs[@]}"
    load; select_group "$name"
  fi
}

# --- actions --------------------------------------------------------------------
tag_session() {
  on_session || { status="select a session to tag"; return; }
  local name group
  name="$(row_name)"
  group="$(ask "group for '$name' (- = none): ")"
  [ -z "$group" ] && { status="cancelled"; return; }
  [ "$group" = "-" ] && group=""
  set_group "$name" "$group"
  if [ -n "$group" ]; then status="'$name' -> $group"; else status="'$name' has no group"; fi
  load; select_session "$name"
}

new_session() {
  if on_row && { [ "$(row_type)" = R ] || [[ "$(row_group)" = host:* && "$(row_group)" != host:local ]]; }; then
    local remote_name target
    remote_name="$(ask 'new remote session name: ')"
    [ -z "$remote_name" ] && return
    target="$(python3 "$DIR/remote.py" create "${rgroup[$sel]#host:}" "$remote_name" 2>"$STATE/remote-attach-error")" || { status="remote creation failed"; return; }
    load; goto "$target" work; select_session "$target"
    return
  fi
  local name group=""
  on_row && group="$(row_group)"
  [ "$group" = host:local ] && group=""
  name="$(ask 'new session name: ')"
  [ -z "$name" ] && { status="cancelled"; return; }
  if tmux new-session -d -s "$name" 2>/dev/null; then
    [ -n "$group" ] && set_group "$name" "$group"
    load; goto "$name" work; select_session "$name"
  else
    status="could not create '$name'"
  fi
}

rename_session() {
  on_row || return
  case "$(row_type)" in H|h) status="host names come from remotes.json"; return ;; esac
  if [ "$(row_type)" = R ]; then
    local remote_name
    remote_name="$(ask "rename '$(row_name)' to: ")"
    [ -z "$remote_name" ] && return
    python3 "$DIR/remote.py" rename "${rgroup[$sel]#host:}" "${rtarget[$sel]}" "$remote_name" 2>"$STATE/remote-attach-error" && status="renamed" || status="remote rename failed"
    load; return
  fi
  local old new
  old="$(row_name)"
  if on_session; then
    new="$(ask "rename '$old' to: ")"
    [ -z "$new" ] && { status="cancelled"; return; }
    if tmux rename-session -t "$old" "$new" 2>/dev/null; then
      set_group "$new" "$(lookup_group "$old")"
      set_group "$old" ""
      status="renamed"
      load; select_session "$new"
    else
      status="rename failed"
    fi
  else
    new="$(ask "rename group '$old' to: ")"
    [ -z "$new" ] && { status="cancelled"; return; }
    rename_group_everywhere "$old" "$new"
    status="group renamed"
    load; select_group "$new"
  fi
}

kill_session() {
  if on_row && [ "$(row_type)" = R ]; then
    local remote_host="${rgroup[$sel]#host:}" remote_id="${rtarget[$sel]}" answer other
    answer="$(ask "kill $remote_host/$(row_name)? [y/N] ")"
    [ "$answer" = y ] || [ "$answer" = Y ] || return
    if [ "$current" = "$remote_host/$(row_name)" ]; then
      other="$(tmux list-sessions -F '#{?@tmax-remote-host,,#{session_name}}' | sed '/^$/d' | head -1)"
      if [ -z "$other" ]; then status="switch to a local session before killing this one"; return; fi
      goto "$other"
    fi
    python3 "$DIR/remote.py" remove "$remote_host" "$remote_id" 2>"$STATE/remote-attach-error" && status="killed remote session" || status="remote kill failed"
    load; return
  fi
  on_session || { status="select a session to kill"; return; }
  local target yn other i
  target="$(row_name)"
  yn="$(ask "kill '$target'? [y/N] ")"
  [ "$yn" = "y" ] || [ "$yn" = "Y" ] || { status="cancelled"; return; }
  if [ "$target" = "$current" ]; then
    # Move ourselves (and the client) somewhere else first, or we die with it.
    other="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -vx -- "$target" | head -1)"
    if [ -n "$other" ]; then
      goto "$other"
      tmux select-pane -t "$ME"
      load
    fi
  fi
  tmux kill-session -t "$target" 2>/dev/null && status="killed '$target'"
}

# --- main loop ---------------------------------------------------------------
# After an Esc byte, read up to 2 more bytes but give up after 0.1s. This tells
# a bare Esc apart from an arrow key. bash 3.2 (macOS default) cannot do
# fractional read -t, so use the tty's own timer (stty time = tenths of a second).
read_escape_rest() {
  local saved
  saved="$(stty -g)"
  stty -icanon -echo min 0 time 1
  dd bs=1 count=2 2>/dev/null
  stty "$saved"
}

# Read one key, but give up after 0.1s. Prints the key followed by "x" so a
# newline survives the command substitution.
peek_key() {
  local saved
  saved="$(stty -g)"
  stty -icanon -echo min 0 time 1
  dd bs=1 count=1 2>/dev/null
  stty "$saved"
  printf 'x'
}

# Hover: show the session under the cursor. Called when the keys pause.
hover_switch() {
  want_switch=0
  on_session || return
  [ "$(row_name)" = "$current" ] && return
  goto "$(row_name)"
}

load
select_session "$current"
last_snapshot=""
want_switch=0
while :; do
  snap="$(snapshot)"
  if [ "$snap" != "$last_snapshot" ]; then render; last_snapshot="$snap"; fi
  key=""
  if [ "$want_switch" -eq 1 ]; then
    # The cursor moved. Wait a moment for more keys; when they stop, switch.
    key="$(peek_key)"; key="${key%x}"
    if [ -z "$key" ]; then hover_switch; load; continue; fi
  # 1s timeout so the list refreshes and picks up resizes on its own.
  # NOTE: bash 3.2 returns 1 on timeout, bash 4+ returns >128. Treat any
  # failure as a timeout, but stop if the terminal is really gone.
  elif ! IFS= read -rsn1 -t 1 key; then
    [ -t 0 ] || exit 0
    load; continue
  fi
  status=""
  case "$key" in
    $'\e')
      # Escape sequence (arrow keys) or a bare Esc.
      seq="$(read_escape_rest)"
      case "$seq" in
        '[A') key="k" ;;
        '[B') key="j" ;;
        '[C') key="l" ;;
        '[D') key="h" ;;
        *)    key="esc" ;;
      esac
      ;;
    ""|$'\n'|$'\r') key="enter" ;;
    $'\t') key="tab" ;;
    " ")   key="space" ;;
  esac
  n=${#rtype[@]}
  case "$key" in
    j)       [ "$n" -gt 0 ] && sel=$(( (sel + 1) % n )); want_switch=$hover ;;
    k)       [ "$n" -gt 0 ] && sel=$(( (sel - 1 + n) % n )); want_switch=$hover ;;
    g)       sel=0; want_switch=$hover ;;
    G)       [ "$n" -gt 0 ] && sel=$((n - 1)); want_switch=$hover ;;
    tab)     want_switch=0; activate ;;
    enter)   want_switch=0; activate close ;;
    space|h) fold_here ;;
    J)       move_here 1 ;;
    K)       move_here -1 ;;
    t)       tag_session ;;
    n)       new_session ;;
    r)       rename_session ;;
    d)       kill_session ;;
    R)       status="refreshed" ;;
    esc|l)   focus_work_pane ;;
    q)       exit 0 ;;
  esac
  load
done
