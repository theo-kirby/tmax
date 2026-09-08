#!/usr/bin/env python3
"""Install observational hooks without replacing existing agent configuration."""
import argparse
import json
import shlex
import subprocess
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def merge(path, events, command):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    original = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    for event in events:
        entries = hooks.setdefault(event, [])
        for entry in entries:
            entry["hooks"] = [item for item in entry.get("hooks", [])
                              if "tmax/agent_status.py" not in item.get("command", "")]
        entries[:] = [entry for entry in entries if entry.get("hooks")]
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    if json.dumps(data, sort_keys=True) != original:
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".before-tmax-" + str(time.time_ns())))
        temporary = path.with_suffix(".tmax-tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="configured remote host (run from an unlocked local tmux session)")
    args = parser.parse_args()
    if args.host:
        import remote
        remote.setup()
        cfg = remote.hosts().get(args.host)
        if cfg is None:
            parser.error("unknown remote host: " + args.host)
        files = {name: (ROOT / name).read_text() for name in
                 ["scripts/agent_status.py", "scripts/install-agent-status.py", "integrations/pi-status.ts"]}
        installer = """import json,sys,tempfile,subprocess
from pathlib import Path
files=json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix='tmax-agent-install-') as directory:
    root=Path(directory)
    for name,text in files.items():
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text)
    subprocess.run([sys.executable,str(root/'scripts/install-agent-status.py')],check=True)
"""
        try:
            command = remote.auth.command(remote.RUNTIME, args.host, cfg)
        except remote.auth.Locked as exc:
            parser.error(str(exc))
        subprocess.run(command + ["python3 -c " + shlex.quote(installer)],
                       input=json.dumps(files), text=True, timeout=30, check=True)
        return
    home = Path.home()
    target = home / ".local/share/tmax"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/agent_status.py", target / "agent_status.py")
    common = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
              "PostToolUse", "PermissionRequest", "Stop", "PreCompact"]
    base = 'python3 "$HOME/.local/share/tmax/agent_status.py" hook '
    merge(home / ".claude/settings.json", common + ["PostToolUseFailure", "StopFailure", "Notification"], base + "claude")
    merge(home / ".codex/hooks.json", common + ["Interrupt", "PostCompact"], base + "codex")
    extensions = home / ".pi/agent/extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "integrations/pi-status.ts", extensions / "tmax-status.ts")
    print("Installed Claude Code and Codex hooks, Pi extension, and tmux activity collector.")
    print("Restart agents to load integrations. Review the new hooks in Codex /hooks.")


if __name__ == "__main__":
    main()
