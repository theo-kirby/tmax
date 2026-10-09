#!/usr/bin/env python3
"""Terminal version of the lab dashboard's Crown home: a dial of agent bars.

Terminals cannot antialias, so rather than tracing the dashboard's SVG the
dial is one smooth Braille circle split into session-coloured arcs whose
thickness shows each agent's state, around a pixel-font HH:MM:SS clock."""
import colorsys
import datetime
import json
import math
import os
import sys
import time

import lab_agents

MACHINES = {"mba": "air", "air": "air", "mmini": "mini", "sb1x": "heavy", "heavy": "heavy",
            "sb9x": "light", "light": "light", "pi": "pi", "notebook": "notebook"}
STATE_NAMES = {"attention": "needs you", "stale": "report stale", "unknown": "activity unknown",
               "working": "working", "starting": "starting", "retrying": "retrying",
               "waiting": "waiting", "idle": "idle"}
STATE_COLOURS = {"working": (0xa6, 0xe4, 0xc5), "starting": (0xa6, 0xe4, 0xc5), "waiting": (0x97, 0xbf, 0xea),
                 "attention": (0xf3, 0xbd, 0x73), "retrying": (0xd4, 0xb0, 0xf5),
                 "stale": (0x89, 0x91, 0x8f), "unknown": (0x89, 0x91, 0x8f), "idle": (0x91, 0xa3, 0x9d)}
# Strongest first: one state stands for a session or host.
STATE_ORDER = ["attention", "retrying", "working", "starting", "waiting", "idle", "unknown", "stale"]
MOVING = ("working", "starting", "retrying")
FOREGROUND = (0xed, 0xed, 0xeb)
BACKGROUND_AGENTS = (0xa0, 0xa5, 0xac)


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def session_colour(name):
    """The dashboard's Classic palette: a hue from the session name."""
    value = 0
    for char in name:
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb((value % 360) / 360, .72, .52))


def load_sessions():
    """Fleet-wide sessions cached from the lab, overlaid with this host's own records."""
    values = {}
    try:
        data = json.loads((lab_agents.state_dir() / "overview.json").read_text())
        values.update({v["id"]: v for v in data.get("sessions", [])})
    except (OSError, ValueError, KeyError):
        pass
    try:
        values.update({v["id"]: v for v in lab_agents.snapshot()})
    except Exception:
        pass
    return list(values.values())


def agent_state(members, now):
    current = [s for s in members if now - parse_time(s.get("observedAt")) < 90]
    if not current:
        return "stale"
    if any(s.get("attention") for s in current):
        return "attention"
    for state in ("retrying", "working", "starting", "waiting", "idle"):
        if any(s.get("activity") == state for s in current):
            return state
    return "unknown"


def strongest(states):
    return min(states, key=lambda s: STATE_ORDER.index(s) if s in STATE_ORDER else len(STATE_ORDER), default="stale")


def hierarchy(sessions, now=None):
    """Session groups and their agents with dial angles, as the dashboard arranges them."""
    now = time.time() if now is None else now
    live = [s for s in sessions if s.get("activity") != "stopped"
            and s.get("source") != "terminal-configuration" and s.get("runner") != "terminal"]
    # A shared backend is infrastructure for its connected terminal, not another agent.
    terminals = {s["id"] for s in live if (s.get("attachment") or {}).get("session")}
    visible = [s for s in live if not (s.get("role") == "backend" and s.get("parentId") in terminals)]
    by_id = {s["id"]: s for s in visible}
    roots = {}
    for s in visible:
        root, seen = s, {s["id"]}
        while root.get("parentId") in by_id and root["parentId"] not in seen:
            parent = by_id[root["parentId"]]
            # Collapse runner children only in the same location (or without one).
            if parent["host"] != s["host"] or (s.get("attachment") and parent.get("attachment")
                                               and s["attachment"] != parent["attachment"]):
                break
            seen.add(parent["id"])
            root = parent
        roots.setdefault(root["id"], []).append(s)
    groups = {}
    for root_id, members in roots.items():
        session = by_id[root_id]
        attachment = next((s.get("attachment") for s in [session, *members] if (s.get("attachment") or {}).get("session")), None)
        provider = session.get("provider", "")
        if attachment:
            name = attachment["session"]
        else:
            name = (provider == "codex" and "Codex" or provider) + " service" if session.get("role") == "backend" else "Background agents"
            name += " · " + session["project"] if session.get("project") else ""
        group_id = (session["host"], (attachment or {}).get("socket", ""), name)
        group = groups.setdefault(group_id, {
            "id": group_id, "name": name, "host": session["host"], "attached": bool(attachment), "agents": [],
            "colour": session_colour(name) if attachment else BACKGROUND_AGENTS})
        provider = "ouroboros" if "ouroboros" in (session.get("runner"), provider) else provider
        pane = (attachment or {}).get("pane", "")
        group["agents"].append({"id": root_id, "session": session, "state": agent_state(members, now),
                                "name": provider + (" · " + pane if pane else ""), "pane": pane})
    ordered = sorted(groups.values(), key=lambda g: (not g["attached"], g["name"].lower(), g["host"], g["id"][1]))
    step = 360 / max(1, len(ordered))
    for index, group in enumerate(ordered):
        group["start"] = index * step + min(3, step * .08)
        group["end"] = (index + 1) * step - min(3, step * .08)
        group["agents"].sort(key=lambda a: (pane_key(a["pane"]), a["id"]))
        width = (group["end"] - group["start"]) / len(group["agents"])
        for j, agent in enumerate(group["agents"]):
            gap = min(1, width * .08)
            agent["start"] = group["start"] + j * width + gap
            agent["end"] = group["start"] + (j + 1) * width - gap
        group["state"] = strongest([a["state"] for a in group["agents"]])
    return ordered


def pane_key(pane):
    digits = pane.lstrip("%")
    return (0, int(digits), "") if digits.isdigit() else (1, 0, pane)


def summary(groups):
    agents = [a for g in groups for a in g["agents"]]
    if not agents:
        return "no agents detected"
    working = sum(a["state"] in MOVING for a in agents)
    attention = sum(a["state"] == "attention" for a in agents)
    unknown = sum(a["state"] in ("unknown", "stale") for a in agents)
    text = str(working) + " working · " + str(len(agents)) + " agent" + ("" if len(agents) == 1 else "s")
    if attention:
        text += " · " + str(attention) + " need you"
    elif unknown:
        text += " · " + str(unknown) + " unknown"
    return text


def wave(angle, t):
    """Four shared waves in opposing directions and unrelated speeds (crown-flow etc.)."""
    a = math.radians(angle)
    phase, drift = 2 * math.pi * t / 3.7, 2 * math.pi * t / 5.3
    ripple, detail = 2 * math.pi * t / 8.9, 2 * math.pi * t / 2.9
    return (.55 + .09 * math.cos(2 * a - phase + math.radians(35) * math.sin(ripple))
            + .04 * math.sin(3 * a + drift) + .18 * math.cos(17 * a - ripple) + .14 * math.sin(23 * a + detail))


# --- Cells ----------------------------------------------------------------------
# The ring is Braille (2 x 4 dots a cell, square at a 1:2 cell) so its curve
# stays smooth; the clock is a 3 x 5 pixel font in half blocks. Colours are
# solid: a dot is on or off, never blended with the terminal's background.

BRAILLE = [(0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80)]
GUIDE = (0x4a, 0x4e, 0x4d)
FONT = {
    "0": ["###", "# #", "# #", "# #", "###"], "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["###", "  #", "###", "#  ", "###"], "3": ["###", "  #", "###", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"], "9": ["###", "# #", "###", "  #", "###"],
    ":": [" ", "#", " ", "#", " "],
}


def shade(colour, level):
    """A darker solid colour (for a dark terminal); no blending with the background."""
    return tuple(c * level for c in colour)


def rgb(colour):
    return tuple(max(0, min(255, round(c))) for c in colour)


class Grid:
    """Cells of (glyph, colour, reverse) over a layer of Braille dots.

    Cells win over dots; anything missing keeps the terminal background."""

    def __init__(self, columns, rows):
        self.columns, self.rows, self.cells, self.dots = columns, rows, {}, {}

    def put(self, row, column, glyph, colour, reverse=False):
        if 0 <= row < self.rows and 0 <= column < self.columns:
            self.cells[(row, column)] = (glyph, colour, reverse)

    def free(self, row, first, last):
        return all((row, c) not in self.cells for c in range(first - 1, last + 2))

    def write(self, row, column, text, colour, align="centre"):
        """Text anchored at a cell; returns False when it would overlap or leave the grid."""
        first = round(column - (len(text) / 2 if align == "centre" else len(text) if align == "right" else 0))
        if not text or row < 0 or row >= self.rows or first < 0 or first + len(text) > self.columns \
                or not self.free(row, first, first + len(text) - 1):
            return False
        for i, char in enumerate(text):
            self.put(row, first + i, char, colour)
        return True

    def pixels(self, points, colour):
        """Half-block pixels (x, y), two per cell vertically."""
        for (x, y) in points:
            row, column = y // 2, x
            glyph = self.cells.get((row, column), ("",))[0]
            halves = {"▀": 1, "▄": 2, "█": 3}.get(glyph, 0) | (1 if y % 2 == 0 else 2)
            self.put(row, column, " ▀▄█"[halves], colour)

    def braille(self, row, column):
        bits, lit = 0, []
        for dy in range(4):
            for dx in range(2):
                colour = self.dots.get((column * 2 + dx, row * 4 + dy))
                if colour is not None:
                    bits |= BRAILLE[dy][dx]
                    lit.append(colour)
        # One colour per cell: the brightest dot's.
        return (chr(0x2800 + bits), max(lit, key=sum), False) if bits else None

    def ansi(self):
        lines = []
        for row in range(self.rows):
            out, state = [], None
            for column in range(self.columns):
                cell = self.cells.get((row, column)) or self.braille(row, column)
                if cell is None:
                    style, glyph = "0", " "
                else:
                    glyph, colour, reverse = cell
                    style = ("0;7;38;2;%d;%d;%d" if reverse else "0;38;2;%d;%d;%d") % rgb(colour)
                if style != state:
                    out.append("\x1b[" + style + "m")
                    state = style
                out.append(glyph)
            lines.append("".join(out).rstrip(" ") + "\x1b[0m")
        return "\n".join(lines)


def clock_pixels(text, scale):
    """Lit pixels of text in the 3 x 5 font, and the text's width."""
    points, x, gap = [], 0, 1 if scale <= 2 else 2
    for char in text:
        glyph = FONT[char]
        for gy, line in enumerate(glyph):
            for gx, mark in enumerate(line):
                if mark == "#":
                    points += [(x + gx * scale + i, gy * scale + j) for i in range(scale) for j in range(scale)]
        x += len(glyph[0]) * scale + gap
    return points, x - gap


# --- Dial -----------------------------------------------------------------------

def render(groups, columns, rows, t=None, selected=(), title=None, labels="numbers", seconds=True):
    """The Crown face as ANSI lines. selected names group ids to emphasise;
    labels is numbers, names or off (what sits outside the ring)."""
    return draw(groups, columns, rows, t, selected, title, labels, seconds).ansi()


def ring(grid, groups, cx, cy, radius, t, focus):
    """One Braille circle split into each agent's arc (the dashboard's angles).

    The line itself stays two dots wide and round; state thickens it outward:
    working ripples with the shared waves, waiting and attention sit thicker,
    idle is a darker hairline and stale or unknown is dotted. A selected
    session also thickens inward."""
    arcs = [(agent["start"], agent["end"], group, agent) for group in groups for agent in group["agents"]]
    outer = radius + 4.5
    for y in range(max(0, int(cy - outer)), min(grid.rows * 4, int(cy + outer) + 1)):
        for x in range(max(0, int(cx - outer)), min(grid.columns * 2, int(cx + outer) + 1)):
            dx, dy = x + .5 - cx, y + .5 - cy
            distance = math.hypot(dx, dy) - radius
            if not -2 < distance < 4.5:
                continue
            angle = math.degrees(math.atan2(dx, -dy)) % 360
            owner = next((arc for arc in arcs if arc[0] <= angle < arc[1]), None)
            if owner is None:
                if not arcs and abs(distance) <= 1.0:
                    grid.dots[(x, y)] = GUIDE
                continue
            _, _, group, agent = owner
            state, chosen = agent["state"], group in focus
            if state in MOVING:
                extra = 3.2 * max(0.0, min(1.0, (wave(angle, t) - .15) / .8))
            else:
                extra = {"attention": 2.2, "waiting": 1.4}.get(state, 0.0)
            if not -(2.0 if chosen else 1.0) <= distance <= 1.0 + extra:
                continue
            if state in ("stale", "unknown") and int(angle * math.pi / 180 * radius / 2) % 2:
                continue
            colour = STATE_COLOURS["stale"] if state in ("stale", "unknown") else group["colour"]
            if state == "idle":
                colour = shade(colour, .8)
            if focus and not chosen:
                colour = shade(colour, .55)
            grid.dots[(x, y)] = colour


def draw(groups, columns, rows, t=None, selected=(), title=None, labels="numbers", seconds=True):
    t = time.time() if t is None else t
    grid = Grid(columns, rows)
    # Pixel units: a column wide, half a row tall (a Braille dot is half a pixel).
    cx, cy = columns / 2, rows
    radius = max(4.0, min(columns / 2, rows) - 3.5)
    selected = set(selected)
    focus = [g for g in groups if g["id"] in selected]
    ring(grid, groups, cx * 2, cy * 2, radius * 2, t, focus)

    # A diamond just beyond the ring marks agents that need you.
    outer = radius + 1.8
    for group in groups:
        for agent in group["agents"]:
            if agent["state"] == "attention":
                a = math.radians((agent["start"] + agent["end"]) / 2)
                grid.put(int((cy - outer * math.cos(a)) // 2), int(cx + outer * math.sin(a)), "◆", STATE_COLOURS["attention"])

    # Centre: date, HH:MM:SS, then the selection or the fleet summary.
    clock = datetime.datetime.fromtimestamp(t)
    inner = radius - 1.5
    middle = rows / 2

    def chord(row):
        offset = abs(row * 2 + 1 - cy)
        return 0 if offset >= inner else int(2 * math.sqrt(inner * inner - offset * offset))

    numbers = {g["id"]: i + 1 for i, g in enumerate(groups)} if labels == "numbers" else {}
    lines = detail(focus, title, numbers) if focus or title else [(summary(groups), shade(FOREGROUND, .6))]
    formats = ((3, "%H:%M:%S"), (2, "%H:%M:%S"), (1, "%H:%M:%S"), (1, "%H:%M")) if seconds else ((3, "%H:%M"), (2, "%H:%M"), (1, "%H:%M"))
    for scale, text in formats:
        points, width = clock_pixels(clock.strftime(text), scale)
        height = -(-5 * scale // 2)
        # The clock sits on the dial's widest rows; date above, details below.
        top = int(middle - height / 2 - .5)
        if all(chord(r) >= width for r in range(top, top + height)):
            break
    if chord(top - 1) >= 11:
        grid.write(top - 1, cx, clock.strftime("%d %b %Y").upper(), shade(FOREGROUND, .6))
    left = round(cx - width / 2)
    split = len(clock_pixels(clock.strftime("%H:%M:"), scale)[0]) if text == "%H:%M:%S" else len(points)
    grid.pixels([(left + x, top * 2 + y) for x, y in points[:split]], FOREGROUND)
    grid.pixels([(left + x, top * 2 + y) for x, y in points[split:]], shade(FOREGROUND, .62))
    row = top + height + 1
    for text, colour in lines:
        room = chord(row) - 1
        if room >= 4:
            grid.write(row, cx, text if len(text) <= room else text[:room - 1] + "…", colour)
        row += 1

    # Outside the ring each session gets its number (named under the clock when
    # selected) or, with labels = names, its name where that fits whole.
    label_radius = radius + 3.4
    for group in groups if labels in ("numbers", "names") else ():
        a = math.radians((group["start"] + group["end"]) / 2)
        x, y = cx + label_radius * math.sin(a), cy - label_radius * math.cos(a)
        colour = group["colour"] if not focus or group in focus else shade(group["colour"], .55)
        if labels == "numbers":
            grid.write(int(y // 2), x, str(numbers[group["id"]]), colour)
            continue
        side = math.sin(a)
        align = "left" if side > .35 else "right" if side < -.35 else "centre"
        for text in ((group["name"] + " · " + MACHINES.get(group["host"], group["host"])).lower(), group["name"].lower()):
            if grid.write(int(y // 2), x, text, colour, align):
                break
    return grid


def detail(focus, title, numbers=None):
    """Below-time lines for the selected session(s): its name, then its state."""
    numbers = numbers or {}
    if not focus:
        return [(title, FOREGROUND), ("no agents", shade(FOREGROUND, .6))]
    state = strongest([g["state"] for g in focus])
    if len(focus) == 1:
        group = focus[0]
        name = title or (group["name"] + " · " + MACHINES.get(group["host"], group["host"])).lower()
        if group["id"] in numbers:
            name = str(numbers[group["id"]]) + "  " + name
    else:
        name = title or focus[0]["host"]
    return [(name, FOREGROUND), (STATE_NAMES.get(state, state).upper(), STATE_COLOURS.get(state, FOREGROUND))]


def main():
    """Preview in this terminal: agent_view.py [--once] [--demo]."""
    sessions = demo() if "--demo" in sys.argv else load_sessions()
    try:
        while True:
            size = os.get_terminal_size()
            frame = render(hierarchy(sessions), size.columns, size.lines - 1)
            sys.stdout.write("\x1b[H\x1b[2J" + frame)
            sys.stdout.flush()
            if "--once" in sys.argv:
                print()
                return
            time.sleep(1 / 12)
    except KeyboardInterrupt:
        print()


def demo():
    """A synthetic fleet covering every state, for trying the dial out."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = [("mba", "lab", "codex", "working", ""), ("mba", "lab", "claude", "idle", ""),
            ("mba", "tmax", "claude", "working", ""), ("sb1x", "cadex", "claude", "working", ""),
            ("sb1x", "cadex", "claude", "idle", ""), ("mmini", "holo", "claude", "waiting", "approval"),
            ("mmini", "ares", "claude", "idle", ""), ("mmini", "vid", "claude", "unknown", ""),
            ("notebook", "lab", "codex", "retrying", ""), ("sb9x", "flywheel", "claude", "working", "")]
    return [{"id": str(i), "runId": str(i), "host": host, "provider": provider, "activity": state,
             "attention": attention, "observedAt": stamp, "role": "terminal",
             "attachment": {"host": host, "socket": "/tmp/s", "session": name, "pane": "%" + str(i)}}
            for i, (host, name, provider, state, attention) in enumerate(rows)]


if __name__ == "__main__":
    main()
