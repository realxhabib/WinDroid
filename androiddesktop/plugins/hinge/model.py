"""What the Hinge plugin knows about the screen: a profile's cards, or a chat."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Card:
    """One card of a profile. Its place is in page pixels: view pixels plus how far the profile has
    been scrolled, so it stays put while the view scrolls over it."""
    id: str  # "photo_1", "prompt_2", "vitals_1"...
    kind: str  # photo | prompt | vitals | video | voice | other
    top: float  # page pixels
    bottom: float
    left: float
    right: float
    text: str = ""  # prompt question and answer; the vitals
    image: bytes | None = None  # a JPEG of the card (photos)
    seen: float = 0.0  # how much of the card the best crop showed, 0..1
    sig: tuple[int, ...] = ()  # a small picture of the card, to know it again (photos)
    partial: bool = True  # never seen whole yet: its text may go on below (or above) the screen
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)  # its lines of text, page pixels

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class Profile:
    name: str = ""
    details: dict[str, str] = field(default_factory=dict)
    cards: list[Card] = field(default_factory=list)

    def card(self, card_id: str) -> Card | None:
        return next((c for c in self.cards if c.id == card_id), None)

    @property
    def sig(self) -> str:
        """Tells profiles apart: name and prompts."""
        return self.name + "|" + "|".join(c.text for c in self.cards if c.kind == "prompt")


@dataclass
class Msg:
    who: str  # me | them | ?
    text: str


@dataclass
class Chat:
    name: str = ""
    messages: list[Msg] = field(default_factory=list)
    context: str = ""  # how it started: "you liked their prompt ..."

    @property
    def sig(self) -> str:
        return self.name + "|" + "|".join(f"{m.who}:{m.text}" for m in self.messages[-3:])
