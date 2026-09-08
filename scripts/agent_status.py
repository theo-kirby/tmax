#!/usr/bin/env python3
"""Agent lifecycle hooks and read-only tmux activity snapshots. No prompt storage."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

PRIORITY = {"plain": 0, "working": 1, "waiting": 2}
OPTION = "@tmax-agent"
WAIT_TOOLS = {"askuserquestion", "request_user_input", "request_user_input_async",
              "enterplanmode", "exitplanmode"}
WORK_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "PreCompact", "PostCompact", "agent_start", "ui_prompt_end"}
WAIT_EVENTS = {"SessionStart", "Stop", "StopFailure", "Interrupt", "PermissionRequest",
               "session_start", "agent_settled", "ui_prompt_start"}
END_EVENTS = {"SessionEnd", "session_shutdown"}


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=4).rstrip("\n")


def strongest(states):
    return max(states, key=lambda state: PRIORITY.get(state, 0), default="plain")


def agent_name(command):
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    executable = Path(words[0]).name
    if executable in {"codex", "claude", "pi", "pi-coding-agent"}:
        return executable
    if "/claude/versions/" in words[0] or executable.startswith("codex-aarch64-"):
        return "claude" if "/claude/" in words[0] else "codex"
    if executable in {"node", "bun"} and len(words) > 1:
        script = words[1]
        if any(part in script for part in ("/pi-coding-agent/", "/pi-mono/", "/pi/packages/coding-agent/")):
            return "pi"
        if "/@anthropic-ai/claude-code/" in script:
            return "claude"
        if "/@openai/codex/" in script:
            return "codex"
    return None


def processes():
    # lstart protects against a later process reusing the recorded PID.
    result = {}
    for line in run(["ps", "-axo", "pid=,ppid=,lstart=,command="]).splitlines():
        fields = line.strip().split(None, 7)
        if len(fields) == 8:
            pid, parent = fields[:2]
            result[int(pid)] = (int(parent), " ".join(fields[2:7]), fields[7])
    return result


def descendants(root, table):
    children = {}
    for pid, (parent, _, _) in table.items():
        children.setdefault(parent, []).append(pid)
    pending, found = [root], set()
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    return found


def event_state(event, payload):
    if event in END_EVENTS:
        return "plain"
    if event == "Notification":
        return "waiting" if payload.get("notification_type") in {"permission_prompt", "idle_prompt", "elicitation_dialog"} else None
    if event in WAIT_EVENTS:
        return "waiting"
    if event in WORK_EVENTS:
        tool = payload.get("tool_name", "").split(".")[-1].lower()
        return "waiting" if event == "PreToolUse" and tool in WAIT_TOOLS else "working"
    return None


def hook(provider, event=None):
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%\d+", pane) or not os.environ.get("TMUX"):
        return
    payload = {} if event else json.load(sys.stdin)
    # A background subagent must not overwrite the interactive parent's state.
    if payload.get("agent_id"):
        return
    state = event_state(event or payload.get("hook_event_name"), payload)
    if state is None:
        return
    table = processes()
    pid = os.getppid()
    owner = None
    while pid in table:
        parent, started, command = table[pid]
        if agent_name(command):
            owner = (pid, started)
            break
        if parent == pid:
            break
        pid = parent
    # Pi supplies its actual PID; hooks otherwise walk their parent processes.
    explicit = os.environ.get("TMAX_AGENT_PID")
    if explicit and explicit.isdigit() and int(explicit) in table:
        pid = int(explicit)
        owner = (pid, table[pid][1])
    if not owner:
        return
    data = json.dumps({"state": state, "pid": owner[0], "started": owner[1], "provider": provider, "event": event or payload.get("hook_event_name")})
    run(["tmux", "set-option", "-p", "-t", pane, OPTION, data])


def screen_state(text):
    lines = "\n".join(text.splitlines()[-14:]).lower()
    waiting = (r"(?m)^.*(?:usage limit reached|you.ve hit your limit|rate limit exceeded|"
               r"do you want to proceed|would you like to run|enter to confirm|"
               r"waiting for (?:your|user)|requires? (?:your )?approval).*$")
    if re.search(waiting, lines):
        return "waiting"
    if re.search(r"esc(?:ape)? to (?:interrupt|stop)|ctrl[+-]c to interrupt", lines):
        return "working"
    return "waiting"


def pane_state(root, record, table, capture):
    candidates = {pid for pid in descendants(root, table) if pid in table and agent_name(table[pid][2])}
    if not candidates:
        return "plain"
    try:
        saved = json.loads(record)
        pid = saved["pid"]
        if pid in candidates and table[pid][1] == saved["started"] and saved["state"] in PRIORITY:
            # Permission hooks fire before approval, not when a long tool starts.
            # A visible interrupt hint confirms the UI has resumed after approval.
            if saved["state"] == "waiting" and saved.get("event") == "PermissionRequest":
                return screen_state(capture())
            return saved["state"]
    except (ValueError, KeyError, TypeError):
        pass
    return screen_state(capture())


def snapshot(tmux="tmux", socket=None):
    command = [tmux] + (["-L", socket] if socket else [])
    fmt = "#{session_id}\t#{window_id}\t#{window_index}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{@tmax-remote-host}\t#{@tmax-agent}"
    table = processes()
    windows = {}
    for line in run(command + ["list-panes", "-a", "-F", fmt]).splitlines():
        sid, wid, index, pane, pid, dead, proxy, record = line.split("\t", 7)
        if proxy:
            continue
        state = "plain" if dead == "1" else pane_state(
            int(pid), record, table,
            lambda: run(command + ["capture-pane", "-p", "-t", pane, "-S", "-20"]))
        entry = windows.setdefault(sid, {})
        old = entry.get(wid, (int(index), "plain"))
        entry[wid] = (int(index), strongest([old[1], state]))
    return {sid: [state for _, state in sorted(entries.values())] for sid, entries in windows.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "hook"])
    parser.add_argument("provider", nargs="?")
    parser.add_argument("event", nargs="?")
    parser.add_argument("--tmux", default="tmux")
    parser.add_argument("--socket")
    args = parser.parse_args()
    if args.command == "snapshot":
        print(json.dumps(snapshot(args.tmux, args.socket)))
    else:
        # Observational hooks must never block or change an agent's decisions.
        with contextlib.suppress(Exception):
            hook(args.provider, args.event)


if __name__ == "__main__":
    main()
