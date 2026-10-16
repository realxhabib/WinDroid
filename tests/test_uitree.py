"""Reading AgentHelper's accessibility tree, and making it small for an AI agent (uitree.py)."""
import json
import unittest
from pathlib import Path

from androiddesktop import uitree
from androiddesktop.uitree import (CHECKABLE, CHECKED, CLICKABLE, EDITABLE, ENABLED, FOCUSABLE, PASSWORD, SCROLLABLE,
                                   SELECTED, compact, compact_node, element, matches, nodes, parse, parse_node,
                                   scroll_target)

# Clock's Timer tab on a 720x1520 hidden display, as AgentHelper read it (tests/clock_timer_tree.json).
CLOCK_TIMER = json.loads((Path(__file__).with_name("clock_timer_tree.json")).read_text(encoding="utf-8"))


def raw(i, cls="android.view.ViewGroup", b=(0, 0, 100, 100), f=ENABLED, kids=(), **fields):
    out = {"i": i, "c": cls, "b": list(b), "f": f, **fields}
    if kids:
        out["k"] = list(kids)
    return out


def reply(*roots, **window):
    return {"display": 7, "windows": [{"id": n, "type": 1, "layer": 0, "focused": n == 0, "b": [0, 0, 720, 1520],
                                       "title": "App", "pkg": "com.example", "root": r, **window}
                                      for n, r in enumerate(roots)]}


def ids(elements):
    return [e["id"] for e in elements]


class Parsing(unittest.TestCase):
    def test_node_fields(self):
        n = parse_node(raw(4, "android.widget.EditText", (10, 20, 300, 80), ENABLED | EDITABLE | FOCUSABLE,
                           t="hello", d="Search", r="com.example:id/query", h="Search apps", s="on", e="Too long"))
        self.assertEqual((n.id, n.cls, n.text, n.desc, n.res, n.hint, n.state, n.error),
                         (4, "android.widget.EditText", "hello", "Search", "com.example:id/query", "Search apps",
                          "on", "Too long"))
        self.assertEqual(n.bounds, (10, 20, 300, 80))
        self.assertEqual(n.center, (155, 50))
        self.assertTrue(n.actionable)

    def test_empty_field_shows_its_hint_not_as_text(self):
        n = parse_node(raw(8, "android.widget.EditText", f=ENABLED | EDITABLE | uitree.SHOWING_HINT, t="Type here",
                           h="Type here"))
        self.assertEqual((n.text, n.hint), ("", "Type here"))
        n = parse_node(raw(8, "android.widget.EditText", f=ENABLED | EDITABLE | uitree.SHOWING_HINT, t="Search"))
        self.assertEqual((n.text, n.hint), ("", "Search"))  # no hint of its own: the text is it
        n = parse_node(raw(8, "android.widget.EditText", f=ENABLED | EDITABLE, t="hello", h="Type here"))
        self.assertEqual((n.text, n.hint), ("hello", "Type here"))

    def test_windows(self):
        windows = parse({"windows": [
            {"id": 3, "type": 1, "b": [199, 24, 640, 115], "title": "Popup Window", "pkg": "com.example",
             "root": raw(0)},
            {"id": 1, "type": 2, "focused": True, "b": [0, 1000, 720, 1520], "root": raw(1)},
            {"id": 9, "type": 42, "b": [0, 0, 1, 1]}]})
        self.assertEqual([w.type for w in windows], ["app", "keyboard", "other"])
        self.assertEqual(windows[0].title, "Popup Window")
        self.assertTrue(windows[1].focused)
        self.assertIsNone(windows[2].root)  # a window whose content Android didn't give us

    def test_real_tree(self):
        windows = parse(CLOCK_TIMER)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].package, "com.google.android.deskclock")
        self.assertEqual(len(nodes(windows)), 61)


class Compacting(unittest.TestCase):
    def test_layout_goes_and_children_move_up(self):
        tree = raw(0, kids=[raw(1, kids=[raw(2, "android.widget.Button", f=ENABLED | CLICKABLE, t="OK")])])
        (window,) = compact(parse(reply(tree)))
        self.assertEqual(window["elements"], [{"id": 2, "class": "Button", "text": "OK", "bounds": [0, 0, 100, 100],
                                               "clickable": True}])

    def test_tappable_row_takes_its_labels(self):
        row = raw(5, "android.widget.LinearLayout", (0, 200, 720, 320), ENABLED | CLICKABLE, kids=[
            raw(6, "android.widget.TextView", (40, 210, 400, 260), t="Wi-Fi"),
            raw(7, kids=[raw(8, "android.widget.TextView", (40, 260, 400, 300), t="Connected")]),
            raw(9, "android.widget.TextView", (40, 260, 400, 300), t="Wi-Fi"),  # repeated: once is enough
        ])
        (row_element,) = compact(parse(reply(row)))[0]["elements"]
        self.assertEqual(row_element["text"], "Wi-Fi Connected")
        self.assertNotIn("children", row_element)

    def test_row_with_a_switch_keeps_its_children(self):
        row = raw(5, f=ENABLED | CLICKABLE, kids=[
            raw(6, "android.widget.TextView", t="Bluetooth"),
            raw(7, "android.widget.Switch", f=ENABLED | CLICKABLE | CHECKABLE | CHECKED)])
        (row_element,) = compact(parse(reply(row)))[0]["elements"]
        self.assertNotIn("text", row_element)
        self.assertEqual(ids(row_element["children"]), [6, 7])
        self.assertIs(row_element["children"][1]["checked"], True)

    def test_label_repeating_its_parent_goes(self):
        tab = raw(45, "android.widget.FrameLayout", f=ENABLED | SELECTED, d="Timer",
                  kids=[raw(50, "android.widget.TextView", t="Timer"), raw(51, "android.widget.TextView", t="2")])
        (tab_element,) = compact(parse(reply(tab)))[0]["elements"]
        self.assertEqual(ids(tab_element["children"]), [51])

    def test_empty_bounds_go_with_their_children(self):
        hidden = raw(1, b=(0, 0, 0, 0), kids=[raw(2, "android.widget.Button", f=ENABLED | CLICKABLE, t="Ghost")])
        self.assertEqual(compact_node(parse_node(raw(0, kids=[hidden]))), [])

    def test_element_flags(self):
        n = parse_node(raw(3, "android.widget.EditText", f=EDITABLE | FOCUSABLE | PASSWORD | CHECKABLE,
                           r="com.example:id/pin", h="PIN"))
        e = element(n)
        self.assertEqual(e["class"], "EditText")
        self.assertEqual(e["res"], "pin")
        self.assertIs(e["editable"], True)
        self.assertIs(e["password"], True)
        self.assertIs(e["checked"], False)  # checkable: always says which
        self.assertIs(e["disabled"], True)  # no ENABLED flag
        self.assertNotIn("clickable", e)  # flags only when true

    def test_popup_window_says_where_it_is(self):
        windows = parse(reply(raw(0, t="Tip"), b=[199, 24, 640, 115]))
        self.assertEqual(compact(windows)[0]["bounds"], [199, 24, 640, 115])
        self.assertNotIn("bounds", compact(parse(reply(raw(0, t="Full screen"))))[0])

    def test_real_tree_is_small_and_complete(self):
        windows = parse(CLOCK_TIMER)
        (window,) = compact(windows)
        flat = {e["id"]: e for e in walk(window["elements"])}
        digits = {e["text"]: e for e in flat.values() if e.get("res", "").startswith("timer_setup_digit_")}
        self.assertEqual(sorted(digits), sorted(["0", "00", "1", "2", "3", "4", "5", "6", "7", "8", "9"]))
        self.assertTrue(all(d["clickable"] for d in digits.values()))
        self.assertEqual(flat[17]["text"], "00h 00m 00s")  # the time the timer is set to
        self.assertEqual(flat[17]["desc"], "0 hours, 0 minutes, 0 seconds")
        self.assertIs(flat[45]["selected"], True)  # the Timer tab is the current one
        self.assertIs(flat[31]["disabled"], True)  # nothing to delete yet
        self.assertEqual(len(flat), 20)  # title, menu, time, 12 keys, 5 tabs: of 61 nodes
        self.assertLess(len(json.dumps(window)), len(json.dumps(CLOCK_TIMER)) / 3)
        every = nodes(windows)
        for e in flat.values():  # every id the agent sees names a node it can act on
            self.assertEqual(list(every[e["id"]].bounds), e["bounds"])


def walk(elements):
    for e in elements:
        yield e
        yield from walk(e.get("children", ()))


class Lookups(unittest.TestCase):
    def test_matches_smallest_first_any_case(self):
        windows = parse(CLOCK_TIMER)
        found = matches(windows, "timer")
        self.assertEqual([n.id for n in found[:2]], [50, 9])  # the tab's label, then the title
        self.assertIn(45, [n.id for n in found])  # by description too
        self.assertEqual([n.id for n in matches(windows, "0 MINUTES")], [17])
        self.assertEqual(matches(windows, "nothing like this"), [])

    def test_scroll_target_is_the_biggest_scrollable(self):
        tree = raw(0, b=(0, 0, 720, 1520), kids=[
            raw(1, b=(0, 0, 720, 200), f=ENABLED | SCROLLABLE),
            raw(2, b=(0, 200, 720, 1400), f=ENABLED | SCROLLABLE),
            raw(3, b=(0, 0, 0, 0), f=ENABLED | SCROLLABLE)])
        self.assertEqual(scroll_target(parse(reply(tree))).id, 2)
        self.assertIsNone(scroll_target(parse(CLOCK_TIMER)))

    def test_short_names(self):
        self.assertEqual(uitree.short_class("android.widget.Button"), "Button")
        self.assertEqual(uitree.short_class(""), "View")
        self.assertEqual(uitree.short_res("com.example:id/send"), "send")
        self.assertEqual(uitree.short_res("send"), "send")


if __name__ == "__main__":
    unittest.main()
