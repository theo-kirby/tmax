#!/usr/bin/env python3
"""tmax's settings file: one place for the switcher, agent dial, hosts and reporter.

~/.config/tmax/tmax.conf (or $TMAX_CONFIG) is a plain INI file. Every setting
is optional; anything left out falls back to the older places (tmux @tmax-*
options, remotes.json, agents.json) and then to the defaults below.

  settings.py init [--force]   write the file from your current setup
  settings.py show             print the effective settings
  settings.py get SECTION KEY  print one value
  settings.py apply            push [switcher] values into tmux options
  settings.py path             print the file's location"""
import argparse
import configparser
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PATH = Path(os.environ.get("TMAX_CONFIG", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tmax" / "tmax.conf"))
DEFAULTS = {
    "switcher": {"key": "Space", "width": "75%", "height": "65%", "hosts": "all", "preview": "agents", "footer": "legend"},
    "dial": {"labels": "numbers", "seconds": "on", "fps": "12"},
    "usage": {"providers": "claude, codex", "refresh": "300"},
}
# [switcher] keys and the tmux options tmax.tmux and remote.py read.
TMUX_OPTIONS = {"key": "@tmax-switch-key", "width": "@tmax-switch-width", "height": "@tmax-switch-height",
                "hosts": "@tmax-switch-hosts", "preview": "@tmax-switch-preview"}
HOST_KEYS = ("destination", "tmux", "label", "colour", "socket", "agent_host")
# [agents] keys as the reporter's agents.json spells them; lists are comma-separated here.
AGENT_KEYS = {"host": "host", "endpoint": "endpoint", "token_file": "tokenFile", "sockets": "sockets", "ouroboros": "ouroboros"}
AGENT_LISTS = ("sockets", "ouroboros")


def load():
    # No interpolation and no inline comments: values like "#ff8800" stay intact.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(PATH)
    except configparser.Error as exc:
        print("tmax: ignoring " + str(PATH) + ": " + str(exc).splitlines()[0], file=sys.stderr)
        return configparser.ConfigParser(interpolation=None)
    return parser


def present(section, key, parser=None):
    """A value written in the file, or None."""
    parser = parser or load()
    value = parser.get(section, key, fallback=None)
    return value.strip() if value is not None and value.strip() else None


def get(section, key, parser=None):
    """A value from the file, else its default."""
    value = present(section, key, parser)
    return value if value is not None else DEFAULTS.get(section, {}).get(key, "")


def flag(section, key, parser=None):
    return get(section, key, parser).lower() in ("on", "yes", "true", "1")


def number(section, key, parser=None):
    try:
        return float(get(section, key, parser))
    except ValueError:
        return float(DEFAULTS[section][key])


def hosts(parser=None):
    """[host NAME] sections in remotes.json's shape, or None when the file defines none."""
    parser = parser or load()
    found = {}
    for section in parser.sections():
        if section.startswith("host "):
            entry = {key: parser.get(section, key).strip() for key in HOST_KEYS if present(section, key, parser)}
            found[section[5:].strip()] = entry
    return found or None


def agents(parser=None):
    """[agents] in agents.json's shape (only keys written in the file)."""
    parser = parser or load()
    result = {}
    for key, name in AGENT_KEYS.items():
        value = present("agents", key, parser)
        if value is not None:
            result[name] = [part.strip() for part in value.split(",") if part.strip()] if key in AGENT_LISTS else value
    return result


def apply():
    """Set tmux options for [switcher] keys written in the file; others keep tmux.conf's values."""
    parser = load()
    for key, option in TMUX_OPTIONS.items():
        value = present("switcher", key, parser)
        if value is None:
            continue
        if key == "hosts":
            value = {"all": "on", "local": "off"}.get(value, value)
        subprocess.run(["tmux", "set-option", "-g", option, value], capture_output=True)


def tmux_option(name):
    try:
        return subprocess.run(["tmux", "show-options", "-gqv", name], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def init(force=False):
    """Write the settings file from the current setup, with every option documented."""
    if PATH.exists() and not force:
        raise SystemExit(str(PATH) + " exists; pass --force to rewrite it")
    switcher = {}
    for key in DEFAULTS["switcher"]:
        value = tmux_option(TMUX_OPTIONS[key]) if key in TMUX_OPTIONS else ""
        if key == "hosts":
            value = {"on": "all", "off": "local"}.get(value, value)
        switcher[key] = value or DEFAULTS["switcher"][key]
    remotes_file = Path(os.environ.get("TMAX_REMOTES_FILE", ROOT / "remotes.json"))
    try:
        remotes = json.loads(remotes_file.read_text())
    except (OSError, ValueError):
        remotes = {"local": {"label": "this machine"}}
    agents_file = Path(os.environ.get("TMAX_AGENT_CONFIG", Path.home() / ".config/tmax/agents.json"))
    try:
        reporter = json.loads(agents_file.read_text())
    except (OSError, ValueError):
        reporter = {}

    def line(key, value, note=""):
        return ("# " + note + "\n" if note else "") + key + " = " + value + "\n"

    text = "# tmax settings. Every line is optional: delete one to use its default.\n"
    text += "# Comments go on their own lines. Reload with: tmux source-file ~/.tmux.conf\n\n"
    text += "[switcher]\n"
    text += line("key", switcher["key"], "Key after the tmux prefix that opens the session switcher.")
    text += line("width", switcher["width"], "Popup size, in cells or percent of the terminal.")
    text += line("height", switcher["height"])
    text += line("hosts", switcher["hosts"], "Hosts listed on open: all, local, or one host name (H toggles).")
    text += line("preview", switcher["preview"], "Preview panel: agents (the agent dial) or pane (the selected window).")
    text += line("footer", switcher["footer"], "Under the list: usage (meters, see [usage]), legend (key help) or off. ? swaps in the legend.")
    text += "\n[dial]\n"
    text += line("labels", DEFAULTS["dial"]["labels"], "Around the ring: numbers, names, or off.")
    text += line("seconds", DEFAULTS["dial"]["seconds"], "Show seconds on the clock: on or off.")
    text += line("fps", DEFAULTS["dial"]["fps"], "Animation frames per second while agents work.")
    text += "\n[usage]\n"
    text += line("providers", DEFAULTS["usage"]["providers"], "Weekly usage meters, in order: claude, codex. Uses each CLI's signed-in account.")
    text += line("refresh", DEFAULTS["usage"]["refresh"], "Seconds between fetches while the switcher is open.")
    text += "\n# One section per machine. \"local\" only takes a label; the others need an SSH\n"
    text += "# destination. Optional: tmux (remote binary), socket (-L name), colour (badge,\n"
    text += "# a tmux colour or #rrggbb) and agent_host (the reporter's name for the host).\n"
    for name, entry in remotes.items():
        text += "\n[host " + name + "]\n" + "".join(line(key, str(entry[key])) for key in HOST_KEYS if entry.get(key))
    text += "\n# Agent reporter (lab_agents.py). Lists are comma-separated. agents.json on this\n"
    text += "# machine is still read; values here win. Leave endpoint empty to keep reports local.\n"
    text += "[agents]\n"
    for key, name in AGENT_KEYS.items():
        value = reporter.get(name, "")
        value = ", ".join(value) if isinstance(value, list) else str(value)
        text += ("" if value else "# ") + key + " = " + value + "\n"
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(text)
    PATH.chmod(0o600)
    print("Wrote " + str(PATH))


def show():
    parser = load()
    print("# " + str(PATH) + ("" if PATH.exists() else " (not found; defaults)"))
    for section, values in DEFAULTS.items():
        print("[" + section + "]")
        for key in values:
            print(key + " = " + get(section, key, parser) + ("" if present(section, key, parser) else "  # default"))
    for name, entry in (hosts(parser) or {}).items():
        print("[host " + name + "]")
        for key, value in entry.items():
            print(key + " = " + value)
    reporter = agents(parser)
    if reporter:
        print("[agents]")
        for key, value in reporter.items():
            print(key + " = " + (", ".join(value) if isinstance(value, list) else value))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "show", "get", "apply", "path"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args()
    if options.command == "init":
        init(options.force)
    elif options.command == "show":
        show()
    elif options.command == "get":
        print(get(*options.args[:2]))
    elif options.command == "apply":
        apply()
    else:
        print(PATH)


if __name__ == "__main__":
    main()
