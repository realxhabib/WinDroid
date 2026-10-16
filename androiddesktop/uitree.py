"""The accessibility tree helper/src/AgentHelper.java reads from a display, made small for an AI agent.

AgentHelper sends every visible node, layout containers included: {"i": index, "c": class, "t": text,
"d": content description, "r": resource id, "h": hint, "s": state, "p": pane title, "e": error,
"b": [left, top, right, bottom], "f": flags, "k": children}. Most nodes are layout and carry nothing
an agent needs, so compact() keeps only what can be read or acted on:

- a node with nothing to read and nothing to do goes, and its children take its place;
- a tappable node with no label of its own takes the text of the plain labels inside it (a list row
  reads "Wi-Fi Connected" and is one element instead of three);
- a label that only repeats its parent's goes.

Each element keeps the helper's index as its id, so the agent can name it in tap, type_text and scroll.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# AgentHelper's node flags.
CLICKABLE, LONG_CLICKABLE, EDITABLE, SCROLLABLE = 1, 2, 4, 8
CHECKABLE, CHECKED, SELECTED, FOCUSED = 16, 32, 64, 128
ENABLED, PASSWORD, FOCUSABLE, HEADING = 256, 512, 1024, 2048
SHOWING_HINT = 4096  # an empty field: its "text" is the hint
ACTIONS = CLICKABLE | LONG_CLICKABLE | EDITABLE | SCROLLABLE | CHECKABLE

# AccessibilityWindowInfo.TYPE_*
WINDOW_TYPES = {1: "app", 2: "keyboard", 3: "system", 4: "accessibility", 5: "divider", 6: "magnifier"}


@dataclass
class Node:
    id: int
    cls: str = ""
    text: str = ""
    desc: str = ""
    res: str = ""
    hint: str = ""
    state: str = ""
    pane: str = ""
    error: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    flags: int = 0
    children: list[Node] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def actionable(self) -> bool:
        return bool(self.flags & ACTIONS)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def empty(self) -> bool:
        left, top, right, bottom = self.bounds
        return right <= left or bottom <= top

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Window:
    id: int
    type: str
    title: str
    package: str
    focused: bool
    bounds: tuple[int, int, int, int]
    root: Node | None


def parse_node(raw: dict) -> Node:
    text, hint, flags = raw.get("t", ""), raw.get("h", ""), raw.get("f", 0)
    if flags & SHOWING_HINT:  # the field is empty
        text, hint = "", hint or text
    return Node(id=raw["i"], cls=raw.get("c", ""), text=text, desc=raw.get("d", ""), res=raw.get("r", ""),
                hint=hint, state=raw.get("s", ""), pane=raw.get("p", ""), error=raw.get("e", ""),
                bounds=tuple(raw.get("b", (0, 0, 0, 0))), flags=flags,
                children=[parse_node(k) for k in raw.get("k", ())])


def parse(reply: dict) -> list[Window]:
    """AgentHelper's `tree` reply: the display's windows, top first."""
    return [Window(id=w["id"], type=WINDOW_TYPES.get(w.get("type"), "other"), title=w.get("title", ""),
                   package=w.get("pkg", ""), focused=bool(w.get("focused")), bounds=tuple(w.get("b", (0, 0, 0, 0))),
                   root=parse_node(w["root"]) if w.get("root") else None)
            for w in reply.get("windows", ())]


# ---------------------------------------------------------------------- compaction
def meaningful(n: Node) -> bool:
    return bool(n.label or n.hint or n.state or n.pane or n.error or n.actionable)


def compact_node(n: Node) -> list[Node]:
    """The node as the agent should see it: itself with its kept children, or, if it carries nothing,
    just its kept children."""
    if n.empty:
        return []
    kids = [c for child in n.children for c in compact_node(child)]
    if not meaningful(n):
        return kids
    kept = Node(n.id, n.cls, n.text, n.desc, n.res, n.hint, n.state, n.pane, n.error, n.bounds, n.flags, kids)
    if not kept.label and kept.actionable and kids and all(plain_label(k) for k in kids):
        kept.text = " ".join(dict.fromkeys(k.label for k in kids if k.label))  # in order, no repeats
        kept.state = kept.state or " ".join(dict.fromkeys(k.state for k in kids if k.state))
        kept.children = []
    elif kept.label:
        kept.children = [k for k in kids if not (plain_label(k) and k.label in (kept.text, kept.desc))]
    return [kept]


def plain_label(n: Node) -> bool:
    """Text to read and nothing more: no action, no children of its own."""
    return not n.actionable and not n.children and not n.hint and not n.error


def short_class(cls: str) -> str:
    return cls.rsplit(".", 1)[-1] if cls else "View"


def short_res(res: str) -> str:
    return res.split(":id/", 1)[-1]


def element(n: Node) -> dict:
    """The JSON the agent sees for one element. Flags appear only when they're true."""
    out: dict = {"id": n.id, "class": short_class(n.cls)}
    for key, value in (("text", n.text), ("desc", n.desc), ("hint", n.hint), ("state", n.state),
                       ("pane", n.pane), ("error", n.error), ("res", short_res(n.res))):
        if value:
            out[key] = value
    out["bounds"] = list(n.bounds)
    f = n.flags
    for key, bit in (("clickable", CLICKABLE), ("long_clickable", LONG_CLICKABLE), ("editable", EDITABLE),
                     ("scrollable", SCROLLABLE), ("selected", SELECTED), ("focused", FOCUSED),
                     ("password", PASSWORD), ("heading", HEADING)):
        if f & bit:
            out[key] = True
    if f & CHECKABLE:
        out["checked"] = bool(f & CHECKED)
    if not f & ENABLED:
        out["disabled"] = True
    if n.children:
        out["children"] = [element(c) for c in n.children]
    return out


def compact(windows: list[Window]) -> list[dict]:
    """The display's windows, top first, each with its elements."""
    out = []
    for w in windows:
        elements = compact_node(w.root) if w.root else []
        entry: dict = {"type": w.type}
        if w.title:
            entry["title"] = w.title
        if w.package:
            entry["package"] = w.package
        if w.focused:
            entry["focused"] = True
        if w.type != "app" or w.bounds[:2] != (0, 0):
            entry["bounds"] = list(w.bounds)  # a dialog or popup: say where it is
        entry["elements"] = [element(e) for e in elements]
        out.append(entry)
    return out


# ---------------------------------------------------------------------- lookups
def nodes(windows: list[Window]) -> dict[int, Node]:
    """Every node by id, compacted or not: the agent may name any id it was shown."""
    return {n.id: n for w in windows if w.root for n in w.root.walk()}


def matches(windows: list[Window], text: str) -> list[Node]:
    """Nodes whose text, description or hint contains `text` (any case), smallest first: the label
    itself rather than the screen that contains it."""
    needle = text.casefold()
    found = [n for n in nodes(windows).values()
             if not n.empty and any(needle in s.casefold() for s in (n.text, n.desc, n.hint, n.state) if s)]
    return sorted(found, key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]))


def scroll_target(windows: list[Window]) -> Node | None:
    """The biggest scrollable element: what "scroll down" means when the agent names none."""
    found = [n for n in nodes(windows).values() if n.flags & SCROLLABLE and not n.empty]
    return max(found, key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]), default=None)
