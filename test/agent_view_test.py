import datetime
from pathlib import Path
import re
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_view as view

ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def session(sid, host, name, activity="idle", **extra):
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    value = {"id": sid, "runId": sid, "host": host, "provider": "claude", "activity": activity, "attention": "",
             "observedAt": stamp, "role": "terminal", "attachment": {"host": host, "socket": "s", "session": name, "pane": "%" + sid}}
    value.update(extra)
    return value


class AgentViewTests(unittest.TestCase):
    def test_groups_by_session_and_hides_connected_backends(self):
        groups = view.hierarchy([session("1", "mba", "lab", "working"), session("2", "mba", "lab"),
                                 dict(session("3", "mba", "lab"), role="backend", parentId="1", attachment=None),
                                 session("4", "sb1x", "cadex", "waiting", attention="approval"),
                                 session("5", "mba", "old", "stopped")])
        self.assertEqual([(g["name"], len(g["agents"]), g["state"]) for g in groups],
                         [("cadex", 1, "attention"), ("lab", 2, "working")])
        self.assertLess(groups[0]["end"], groups[1]["start"])

    def test_needs_you_only_while_blocked_on_the_operator(self):
        groups = view.hierarchy([session("1", "mba", "a", attention="review failure"),
                                 session("2", "mba", "b", "waiting", attention="answer")])
        self.assertEqual([(g["name"], g["state"]) for g in groups], [("a", "idle"), ("b", "attention")])

    def test_old_reports_are_stale(self):
        old = session("1", "mba", "lab", "working", observedAt="2020-01-01T00:00:00Z")
        self.assertEqual(view.hierarchy([old])[0]["state"], "stale")

    def test_colours_match_the_dashboard_classic_palette(self):
        # hsl(hash % 360, 52%, 72%) with the dashboard's 31-multiplier string hash.
        self.assertEqual(view.session_colour("lab"), view.session_colour("lab"))
        self.assertNotEqual(view.session_colour("lab"), view.session_colour("cadex"))

    def test_frames_fit_the_viewport(self):
        groups = view.hierarchy(view.demo())
        for columns, rows in ((46, 22), (80, 30), (20, 8)):
            lines = view.render(groups, columns, rows, t=1e9, selected=[groups[0]["id"]]).split("\n")
            self.assertEqual(len(lines), rows)
            self.assertTrue(all(len(ESCAPE.sub("", line)) <= columns for line in lines))

    def test_ring_is_a_circle_of_session_arcs(self):
        groups = view.hierarchy([session("1", "mba", "lab"), session("2", "mba", "tmax")])
        grid = view.draw(groups, 60, 26, t=1e9)
        cx, cy = 60, 52
        radii = [((x + .5 - cx) ** 2 + (y + .5 - cy) ** 2) ** .5 for x, y in grid.dots]
        self.assertTrue(radii)
        self.assertLess(max(radii) - min(radii), 2.5)  # idle: a band two dots wide
        self.assertEqual(set(grid.dots.values()), {view.shade(g["colour"], .8) for g in groups})

    def test_clock_shows_seconds_when_it_fits(self):
        groups = view.hierarchy(view.demo())
        grid = view.draw(groups, 46, 22, t=1e9)
        lit = sum(1 for glyph, colour, _ in grid.cells.values() if glyph in "▀▄█" and colour == view.FOREGROUND)
        seconds = sum(1 for glyph, colour, _ in grid.cells.values() if glyph in "▀▄█" and colour == view.shade(view.FOREGROUND, .62))
        self.assertGreater(lit, 0)
        self.assertGreater(seconds, 0)

    def test_sessions_are_numbered_outside_the_ring(self):
        groups = view.hierarchy([session("1", "mba", "lab"), session("2", "mba", "tmax")])
        cells = view.draw(groups, 60, 26, t=1e9).cells
        labels = {(glyph, colour) for glyph, colour, _ in cells.values() if glyph.isdigit()
                  and colour in [g["colour"] for g in groups]}
        self.assertEqual(labels, {(str(i + 1), g["colour"]) for i, g in enumerate(groups)})
        names = ESCAPE.sub("", view.render(groups, 80, 26, t=1e9, labels="names"))
        self.assertIn("tmax · air", names)

    def test_selection_shows_its_session_below_the_clock(self):
        groups = view.hierarchy([session("1", "mba", "tmax", "working")])
        text = ESCAPE.sub("", view.render(groups, 60, 26, t=1e9, selected=[groups[0]["id"]]))
        self.assertIn("1  tmax · air", text)
        self.assertIn("WORKING", text)
        self.assertNotIn("claude", text)  # status only, not the agent
        empty = ESCAPE.sub("", view.render(groups, 60, 26, t=1e9, title="notes"))
        self.assertIn("no agents", empty)


if __name__ == "__main__":
    unittest.main()
