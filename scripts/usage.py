#!/usr/bin/env python3
"""Weekly usage of the accounts signed in on this machine, as btop-style meters.

Read-only. Each provider's token is read from where its own CLI keeps it and
sent only to that provider's usage endpoint. Tokens are never refreshed here:
that would rotate the CLI's own login. An expired or rejected token shows as
"sign in" until the CLI next refreshes it. Only percentages are cached.

  usage.py [--refresh] [--width N] [PROVIDER ...]   print the meters"""
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

STATE = Path(os.environ.get("TMAX_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tmax"))
CACHE = STATE / "usage.json"
WEEK = 7 * 24 * 3600
EMPTY = (0x3a, 0x3d, 0x3c)
DIM = (0x8a, 0x8f, 0x8d)
# btop's meter gradient: green at 0%, yellow halfway, red at 100%.
GRADIENT = ((0x4c, 0xd1, 0x7f), (0xe6, 0xc4, 0x4a), (0xe8, 0x4f, 0x4f))


class Unavailable(Exception):
    """A provider's usage cannot be read; the message is shown in place of the bar."""


def request(url, headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "tmax"})), timeout=8) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise Unavailable("sign in again" if exc.code in (401, 403) else "error " + str(exc.code)) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise Unavailable("offline") from None


def claude_token():
    """Claude Code's OAuth login: the macOS Keychain, else ~/.claude/.credentials.json."""
    raw = ""
    if sys.platform == "darwin":
        raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, timeout=5).stdout
    if not raw.strip():
        path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"
        raw = path.read_text() if path.exists() else ""
    try:
        login = json.loads(raw)["claudeAiOauth"]
    except (ValueError, KeyError, TypeError):
        raise Unavailable("not signed in") from None
    if login.get("expiresAt") and login["expiresAt"] / 1000 < time.time():
        raise Unavailable("sign in (token expired)")
    return login["accessToken"]


def claude():
    data = request("https://api.anthropic.com/api/oauth/usage",
                   {"Authorization": "Bearer " + claude_token(), "anthropic-beta": "oauth-2025-04-20"})
    week = data.get("seven_day") or {}
    if week.get("utilization") is None:
        raise Unavailable("no weekly limit")
    return {"percent": float(week["utilization"]), "resets": parse_time(week.get("resets_at"))}


def codex():
    """Codex's ChatGPT login in $CODEX_HOME/auth.json; the weekly rate-limit window."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        tokens = json.loads(path.read_text())["tokens"]
        headers = {"Authorization": "Bearer " + tokens["access_token"], "ChatGPT-Account-Id": tokens["account_id"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise Unavailable("not signed in") from None
    limits = request("https://chatgpt.com/backend-api/wham/usage", headers).get("rate_limit") or {}
    windows = [w for w in (limits.get("primary_window"), limits.get("secondary_window")) if w]
    if not windows:
        raise Unavailable("no weekly limit")
    week = min(windows, key=lambda w: abs((w.get("limit_window_seconds") or 0) - WEEK))
    return {"percent": float(week.get("used_percent", 0)), "resets": week.get("reset_at")}


PROVIDERS = {"claude": claude, "codex": codex}


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def cached():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def refresh(providers, max_age=300):
    """Fetch providers whose cached reading is older than max_age seconds.

    One popup fetches at a time; others keep showing the cache meanwhile."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "usage.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return cached()
        data = cached()
        for name in providers:
            entry = data.get(name, {})
            if name not in PROVIDERS or time.time() - entry.get("at", 0) < max_age:
                continue
            try:
                data[name] = dict(PROVIDERS[name](), at=time.time())
            except Unavailable as exc:
                # Keep the last good reading through a brief outage.
                kept = {k: entry[k] for k in ("percent", "resets") if k in entry and str(exc) == "offline"}
                data[name] = dict(kept, error=str(exc), at=time.time())
            except (OSError, subprocess.SubprocessError):
                data[name] = {"error": "unavailable", "at": time.time()}
        temporary = CACHE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.chmod(0o600)
        temporary.replace(CACHE)
        return data


def colour(position):
    """The gradient's colour at 0..1."""
    half = min(1, int(position * 2))
    t = position * 2 - half
    low, high = GRADIENT[half], GRADIENT[half + 1]
    return tuple(round(a + (b - a) * t) for a, b in zip(low, high))


def paint(text, rgb):
    return "\x1b[38;2;%d;%d;%dm" % rgb + text


def meters(providers, width, data=None):
    """One meter line per provider: name, bar of ■ cells, percent."""
    data = cached() if data is None else data
    label_width = max((len(name) for name in providers), default=0)
    lines = []
    for name in providers:
        entry = data.get(name, {})
        head = paint(name.ljust(label_width) + " ", DIM)
        if "percent" not in entry:
            lines.append(head + paint(entry.get("error", "…"), DIM) + "\x1b[0m")
            continue
        percent = max(0.0, min(100.0, entry["percent"]))
        cells = max(5, width - label_width - 6)
        filled = round(cells * percent / 100)
        # Like btop, each cell keeps its place on the gradient; empty cells are dark.
        bar, last = "", None
        for i in range(cells):
            rgb = colour(i / max(1, cells - 1)) if i < filled else EMPTY
            bar += ("■" if rgb == last else paint("■", rgb))
            last = rgb
        tail = "%4.0f%%" % percent if "error" not in entry else " " + entry["error"][:4]
        lines.append(head + bar + paint(tail, colour(percent / 100)) + "\x1b[0m")
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    width = int(sys.argv[sys.argv.index("--width") + 1]) if "--width" in sys.argv else 40
    providers = [a for a in args if not a.isdigit()] or list(PROVIDERS)
    data = refresh(providers) if "--refresh" in sys.argv else None
    print(meters(providers, width, data))


if __name__ == "__main__":
    main()
