# Remote tmux architecture

Remote access uses the session popup on `prefix + Space`.
The default tmux tree on `prefix + s` lists current local sessions.

## Configuration

No remote hosts are bundled or automatically discovered. Users copy
`remotes.example.json` to the Git-ignored `remotes.json`, or point
`TMAX_REMOTES_FILE` to another JSON file. Each entry supplies a display label,
SSH destination, and optional tmux executable or socket.
Normal SSH configuration supplies usernames and keys. Each destination must
require key plus account password; tmax unlocks a private connection per host
for at most 24 hours, with no unattended authentication fallback.
Tailscale can provide connectivity, but ordinary SSH networking works too.

## Transport and local representations

Local tmux owns the prefix, status bar, navigation, copy mode, and chooser.
Remote tmux runs in control mode over SSH. Each opened remote pane uses a
Python bridge process and an SSH channel; channels share a transport per host.
A topology watcher reconciles the windows and panes of each opened session.
Native-tree entries remain lightweight placeholders until selected.

Input bytes go through `send-keys -H`, bypassing remote prefix interpretation.
Recognized local bindings for creation, splitting, layouts, zoom, rename, and
confirmed pane/window deletion are routed to their remote targets. Other
local commands retain their original behavior.

Remote panes pause output subscriptions while hidden. Pausing discards output
without blocking remote applications. Returning to a pane restores its visible
screen. There is no remote preview polling. Reconnection discards input typed
while disconnected and verifies the remote server PID and session creation
time before accepting reused tmux IDs. When a remote session ends, its local
representations are removed and clients return to a local session when possible.

## Validation and remaining limits

Integration tests create isolated local and remote tmux servers and remove
only those servers. The bridge has been exercised against macOS and Linux
hosts with tmux 3.4, using local tmux 3.5a. Coverage includes popup selection and the native local tree, input/output, windows, splits, zoom, hidden output, reconnecting,
Vim restoration and cleanup. Local regression checks cover popup navigation,
filtering, favorites, session creation, and preservation of unrelated hooks.

Arbitrary custom commands are not translated. Native-tree deletion removes
the local representation; routed kill keys act remotely. Scrollback is not
imported, and exact restoration of all terminal modes and complex mixed layouts
is not guaranteed. Memory scales with opened remote panes. See README.md for
configuration and reproducible test commands.
