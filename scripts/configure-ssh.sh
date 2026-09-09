#!/usr/bin/env bash
# Run as root on a destination: sudo bash configure-ssh.sh USER
# Requires both the existing SSH key and the remote account password.
set -euo pipefail
user="${1:-}"
if [ "$(id -u)" != 0 ] || [[ ! "$user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]; then
  echo "Usage: sudo bash configure-ssh.sh USER" >&2
  exit 1
fi
id "$user" >/dev/null
# tmax shares one SSH master per host and opens a channel for each remote
# session, plus its pollers. The OpenSSH default of 10 is too few: channel 11
# is refused with "Connection closed by UNKNOWN port 65535".
sessions=64
sshd=/usr/sbin/sshd
config=/etc/ssh/sshd_config
begin="# BEGIN TMAX PASSWORD AUTH"
end="# END TMAX PASSWORD AUTH"
candidate="$(mktemp /etc/ssh/sshd_config.tmax.XXXXXX)"
backup="$(mktemp /etc/ssh/sshd_config.before-tmax.XXXXXX)"
cp -p "$config" "$backup"
installed=0
complete=0
cleanup() {
  rm -f "$candidate"
  if [ "$installed" = 1 ] && [ "$complete" = 0 ]; then
    cp -p "$backup" "$config"
    echo "Restored original SSH configuration from $backup." >&2
  fi
}
trap cleanup EXIT
awk -v begin="$begin" -v end="$end" '
  $0 == begin { skip=1; next }
  $0 == end { skip=0; next }
  !skip { print }
' "$config" > "$candidate"
cat >> "$candidate" <<EOF

$begin
Match User $user
    PubkeyAuthentication yes
    PasswordAuthentication yes
    AuthenticationMethods publickey,password
    MaxSessions $sessions
$end
EOF
chmod 600 "$candidate"
"$sshd" -t -f "$candidate"
effective="$("$sshd" -T -f "$candidate" -C "user=$user,host=localhost,addr=127.0.0.1")"
for requirement in 'authenticationmethods publickey,password' 'passwordauthentication yes' \
                   'pubkeyauthentication yes' "maxsessions $sessions"; do
  if ! printf '%s\n' "$effective" | grep -qx "$requirement"; then
    echo "An earlier SSH policy overrides $requirement. No change installed." >&2
    exit 1
  fi
done
cp "$candidate" "$config"
installed=1
"$sshd" -t
if [ "$(uname -s)" != Darwin ]; then
  if command -v systemctl >/dev/null; then
    if systemctl is-active --quiet ssh; then
      systemctl reload ssh
    else
      systemctl reload sshd
    fi
  else
    service ssh reload
  fi
fi
complete=1
echo "Enabled key + account password for $user, with MaxSessions $sessions."
echo "Backup: $backup"
echo "Keep this connection open until a fresh login succeeds."
echo "Rollback: sudo cp '$backup' '$config' (then reload ssh/sshd on Linux)."
