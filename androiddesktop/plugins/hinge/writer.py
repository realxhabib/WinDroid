"""Claude drafts: which card to like and three openers for a profile, or three replies in a chat.

Ported from Hinge Matcher (the iPhone version), whose prompts these are. Who the user is and how they
text come from the "me" section of the config, never from this file. Two ways to reach Claude:
  - "api":        the Anthropic SDK (needs the `anthropic` package and an API key; billed per call)
  - "claude-cli": the local `claude` CLI in headless mode (uses your Claude login)
"auto" uses the API when a key and the package are there, otherwise the CLI.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass

from .model import Chat, Profile

SYSTEM = """You write the user's first message on Hinge. On Hinge you like one specific card of \
someone's profile (a photo, a prompt answer, a video, a date prompt) and attach a comment. That \
comment is the whole first impression: it decides whether they match and whether they reply.

How Hinge works, and what that implies:
- They see your like and comment next to your profile and decide in a few seconds. Comments \
  beat bare likes, and Hinge says liking a specific prompt or photo leads to more dates than \
  generic interest. Always write a comment.
- Prompt answers are their own words, so a comment on one shows you actually read them. Prefer \
  prompts. Pick a photo only when it shows something specific (a place, a dish, a sport, a pet, \
  a funny moment), and name what's in it.
- The algorithm rewards quality over volume. Recommend "skip" when there's a real mismatch with \
  the user's dealbreakers or dating intentions, or the profile gives nothing to connect with.

Read each prompt's direction. Prompts are written in their voice, and many are invitations \
aimed at whoever likes them; answer the invitation:
- "Teach me something about X" / "Give me travel tips for X": they want to learn about X. \
  Offer to teach or show them something about X (with a hook), don't ask them to teach you.
- "Convince me that…", "Let's debate this topic", "Change my mind about…": take a side or \
  make the case, playfully.
- "I'll fall for you if…", "I want someone who…", "We'll get along if…", "The way to win me \
  over is…": show (lightly, never by bragging) that the user fits, or tease the requirement.
- "You should leave a comment if…", "Try to guess…", "Two truths and a lie": do the thing \
  they asked for.
- "I know the best spot in town for…", "My most irrational fear…", "Typical Sunday…": these \
  are about them; react, guess, or ask them to back it up.

What gets a reply:
- Specific. It could not be pasted onto anyone else's profile. The strongest ones join two \
  details, e.g. the prompt and their hometown, or a photo and a prompt.
- Easy and fun to answer: a playful assumption they can confirm or correct, a two-option \
  choice, a light challenge ("prove it"), a ranking, a tiny game. Bonus if replying naturally \
  leads toward a date idea.
- A take, not an interview. React with a bit of personality, then hand them an easy hook. Never \
  ask questions they've answered a hundred times ("what do you do for fun?", "how's your week?").
- Low-effort profiles (one-word answers) are an opening: tease the vagueness warmly and make \
  them commit to something specific. For "My simple pleasures: Coffee", something like "just \
  'coffee'? that's either very chill or hiding a 6-shot order. which is it".
- Confident, warm, a little cheeky. Flirty is fine. Creepy, sexual, negging, or fixating on \
  looks is not. If you mention looks at all, mention a choice they made (style, the dog, the \
  place), not their body.

Sound like a person texting, not an assistant:
- 6-20 words, one or two short sentences. Lowercase and loose punctuation are fine; no ending \
  period needed.
- No em dashes, semicolons, "I couldn't help but notice", "I love that", "as a fellow", \
  "adventure", "vibes", "journey", "haha" openers, or exclamation marks on everything. No emoji \
  unless the user's voice uses them.
- Use the user's voice below. Never invent facts about the user (their job, travels, skills). \
  If the joke needs a fact about the user that isn't given, choose a different joke.
- When one of the user's interests genuinely connects to the card (a food prompt and the user \
  cooks, a dog photo and the user has a dog), make one of the three options share that small true \
  thing. It shows overlap and gives them something easy to ask back. Don't force it when the link \
  is a stretch, and don't add specifics the user hasn't given.
- Use their language only if the user speaks it. If the profile is bilingual and the user isn't, \
  a nod to the culture (food, city) still works.

The three options, in this order:
1. witty: a clever observation, wordplay, or an unexpected angle on what they wrote.
2. funny: a playful tease, a bold guess, or a ridiculous-but-specific scenario.
3. cute: warm and a little sweet or flirty, the kind that makes them smile at their phone.
Every option must invite a reply: end with an easy hook (a question, a this-or-that, a guess \
to confirm, a playful challenge). A line that's only a compliment or only a joke is not done.

Process: think of several candidates per angle, check each against the rules above, keep the \
best one per angle, and set `angle` to witty / funny / cute. Mark the one you'd send. The comment \
shows up attached to one card, so each option must be about its own target_id card. Options may \
target different cards, and the top-level target_id is the card of the option you'd send.

About the user:
{persona}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One line on who they seem to be, from the profile."},
        "recommend": {"type": "string", "enum": ["like", "skip"]},
        "skip_reason": {"type": "string", "description": "Why skip (empty when recommending like)."},
        "target_id": {"type": "string", "description": "id of the card to like, from the list given."},
        "target_why": {"type": "string", "description": "Why this card is the best hook, in a few words."},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment": {"type": "string"},
                    "angle": {"type": "string", "description": "2-4 word label for the approach."},
                    "target_id": {"type": "string", "description": "id of the card this comment is attached to."},
                },
                "required": ["comment", "angle", "target_id"],
                "additionalProperties": False,
            },
        },
        "best": {"type": "integer", "description": "0-based index of the option you'd send."},
    },
    "required": ["summary", "recommend", "skip_reason", "target_id", "target_why", "options", "best"],
    "additionalProperties": False,
}

REPLY_SYSTEM = """You write the user's next message in a Hinge chat with someone they matched with. \
The goal is a real connection that turns into a date. Keep it warm and genuine, never a game, and \
true to what the user is looking for (below).

How good Hinge conversations go:
- Answer what they asked, then hand them something easy to reply to: a question that follows \
  from what they said, a playful take, a callback to something earlier. Mix sharing and asking; \
  three questions in a row is an interview.
- Match their energy and length. Short reply gets a short reply. Never send a paragraph to a \
  one-liner.
- Use callbacks and running jokes from earlier in the chat. They show the user is paying attention.
- Move toward a date. Once there's real back-and-forth (roughly 6-10 messages in, or sooner if \
  they're engaged and flirty), suggest a specific plan: an activity tied to something you both \
  talked about, plus a rough day ("thursday or saturday?"). Specific beats "we should hang out \
  sometime". Lean on the user's interests for the plan. Once a date is set, it's fine to ask for \
  their number.
- If they give low-effort replies a few times in a row, either make a light, direct date ask or \
  suggest letting it rest. Don't chase.
- Bring up the user's values only when they do, or when it's natural. Never preach.
- If the last message is the user's and they haven't replied, recommend "wait". A follow-up is only \
  worth it after a long silence, and then it should be light and new (not "hey?").

Sound like a person texting:
- Usually 5-25 words, one message. Lowercase and loose punctuation are fine.
- No em dashes, semicolons, "haha" openers, "I love that", "that's awesome", "vibes", \
  "adventure", corporate or therapy language, or emoji unless they use them.
- Never invent facts about the user (their job, their past, where they've been, what they did this \
  week). If the natural reply needs a fact they haven't given (they asked what the user does for \
  work), write the options so they don't need it, and put the question for the user in needs_info.

Give three options, in this order: witty (clever, an unexpected angle), funny (playful tease or \
a ridiculous-but-specific bit), cute (warm, a little sweet or flirty). When the chat is ready for \
a date ask, make the cute or funny one the ask. Every option must invite a reply: end with an \
easy hook (a question, a this-or-that, a guess to confirm, a playful challenge). Set `angle` to \
witty / funny / cute (add "+ date ask" when it is one) and mark the one you'd send.

About the user:
{persona}"""

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "read": {"type": "string", "description": "One line on where the chat stands and their interest level."},
        "stage": {"type": "string", "enum": ["opening", "rapport", "ready_to_ask", "planning", "stalled"]},
        "recommend": {"type": "string", "enum": ["reply", "wait"]},
        "wait_reason": {"type": "string", "description": "Why wait (empty when recommending reply)."},
        "needs_info": {"type": "string", "description": "A fact you need from the user to answer well, or empty."},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "angle": {"type": "string", "description": "2-4 word label for the approach."},
                },
                "required": ["text", "angle"],
                "additionalProperties": False,
            },
        },
        "best": {"type": "integer", "description": "0-based index of the option you'd send."},
    },
    "required": ["read", "stage", "recommend", "wait_reason", "needs_info", "options", "best"],
    "additionalProperties": False,
}


@dataclass
class Tone:
    """The dock's tone control: playful (-1) to sincere (+1), shorter (-1) to longer (+1)."""
    warmth: float = 0.0
    length: float = 0.0

    def hint(self) -> str:
        out = []
        if self.warmth <= -0.34:
            out.append("Lean playful: cheekier, teasing, more banter." if self.warmth > -0.8 else
                       "Go fully playful: bold, teasing banter, a confident bit of fun.")
        elif self.warmth >= 0.34:
            out.append("Lean sincere: warmer and more genuine, less joking." if self.warmth < 0.8 else
                       "Go fully sincere: warm, genuine interest, no teasing.")
        if self.length <= -0.34:
            out.append("Keep every option short: 4-10 words.")
        elif self.length >= 0.34:
            out.append("Make the options a bit longer: 15-30 words, still one message.")
        return " ".join(out)


def persona_text(cfg: dict) -> str:
    me = cfg.get("me") or {}
    lines = []
    for k, label in (("name", "Name"), ("age", "Age"), ("about", "About"), ("voice", "How they text"),
                     ("interests", "Interests (use one only when it genuinely connects to the card)"),
                     ("values", "Values"), ("looking_for", "Looking for"),
                     ("prefers", "Good signs (lean toward like, not requirements)"),
                     ("dealbreakers", "Dealbreakers (recommend skip)"), ("avoid", "Never say")):
        v = me.get(k)
        if v:
            lines.append(f"- {label}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines) or ("- (not filled in yet: write in a relaxed, playful, confident voice "
                                "and don't invent facts about them)")


def _image(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(data).decode()}}


def build_content(prof: Profile, screens: list[bytes], recent: list[str], hint: str = "",
                  avoid: list[str] | None = None, tone: Tone | None = None) -> list[dict]:
    """User-turn content blocks: one image per photo card (labelled), then the text."""
    blocks: list[dict] = []
    for c in prof.cards:
        if c.image:
            blocks.append({"type": "text", "text": f"Card {c.id}:"})
            blocks.append(_image(c.image))
    if any(c.kind == "photo" and not c.image for c in prof.cards) and screens:
        blocks.append({"type": "text", "text": "The screen as it was read (some photo cards weren't cropped):"})
        blocks.extend(_image(s) for s in screens[:4])
    cards = []
    for c in prof.cards:
        desc = c.text or ("(photo above)" if c.image else "(no text)")
        if c.kind in ("voice", "video"):
            desc += f" [{c.kind}: you can't hear/see it, avoid unless the text is enough]"
        if c.partial and c.kind != "photo":
            desc += " [only partly on screen so far: the rest isn't read yet, so don't treat it as blank]"
        cards.append(f"- {c.id} ({c.kind}): {desc}")
    det = "; ".join(f"{k}: {v}" for k, v in prof.details.items())
    text = [f"Profile: {prof.name or '(name not read)'}", f"Details: {det or 'n/a'}",
            "Cards read so far, top to bottom (target_id must be one of these ids). There may be more "
            "further down that aren't read yet; don't judge the profile as empty because of that:", *cards]
    if recent:
        text += ["", "Openers the user already sent recently. Don't reuse their structure or jokes:",
                 *[f"- {r}" for r in recent[-25:]]]
    if avoid:
        text += ["", "Options already shown for this profile that the user passed on. Write new ones:",
                 *[f"- {a}" for a in avoid]]
    style = tone.hint() if tone else ""
    if style:
        text += ["", f"Tone the user asked for: {style}"]
    if hint:
        text += ["", "The user's direction for this one. Top priority: follow it closely, and treat facts in "
                     f"it as true facts about the user:\n{hint}"]
    blocks.append({"type": "text", "text": "\n".join(text)})
    return blocks


def build_reply_content(chat: Chat, background: str, recent: list[str], hint: str = "",
                        avoid: list[str] | None = None, tone: Tone | None = None) -> list[dict]:
    lines = [f"Chat with {chat.name or 'your match'} (oldest first; 'user' is the user):"]
    if chat.context:
        lines.append(f"[how it started: {chat.context}]")
    for m in chat.messages:
        who = "user" if m.who == "me" else ("them" if m.who == "them" else "?")
        lines.append(f"{who}: {m.text}")
    if not chat.messages:
        lines.append("(no messages yet: write the user's first message in the chat)")
    if background:
        lines += ["", f"What we know about {chat.name or 'them'} from their profile:", background]
    if recent:
        lines += ["", "Messages the user sent in other chats recently. Don't reuse their jokes:",
                  *[f"- {r}" for r in recent[-15:]]]
    if avoid:
        lines += ["", "Options already shown for this message that the user passed on. Write new ones:",
                  *[f"- {a}" for a in avoid]]
    style = tone.hint() if tone else ""
    if style:
        lines += ["", f"Tone the user asked for: {style}"]
    if hint:
        lines += ["", "The user's direction for this reply. Top priority: follow it closely, keep any wording "
                      f"they say they like, and treat facts in it as true facts about the user:\n{hint}"]
    return [{"type": "text", "text": "\n".join(lines)}]


def tidy_draft(out: dict, prof: Profile) -> dict:
    """Keep the model's answer to cards that exist and options with text."""
    ids = {c.id for c in prof.cards}
    if out.get("target_id") not in ids:  # fall back to the first prompt, else the first card
        first = next((c for c in prof.cards if c.kind == "prompt"), prof.cards[0] if prof.cards else None)
        out["target_id"] = first.id if first else ""
    opts = [o for o in out.get("options") or [] if (o.get("comment") or "").strip()]
    for o in opts:  # each option is attached to its own card
        if o.get("target_id") not in ids:
            o["target_id"] = out["target_id"]
    out["options"] = opts
    out["best"] = min(max(int(out.get("best") or 0), 0), max(len(opts) - 1, 0))
    if opts:
        out["target_id"] = opts[out["best"]]["target_id"]
    return out


class _WarmPool:
    """Pre-started `claude -p` processes, one per exact command line.

    The CLI spends ~2 s starting up before it reads its input; with a process already waiting
    on stdin, that happens while you're reading the last draft instead of after you click.
    """

    def __init__(self) -> None:
        self.procs: dict[tuple, subprocess.Popen] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _spawn(args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def take(self, args: list[str]) -> subprocess.Popen:
        with self.lock:
            proc = self.procs.pop(tuple(args), None)
        if proc is None or proc.poll() is not None:
            proc = self._spawn(args)
        return proc

    def refill(self, args: list[str]) -> None:
        with self.lock:
            cur = self.procs.get(tuple(args))
            if cur is None or cur.poll() is not None:
                self.procs[tuple(args)] = self._spawn(args)

    def close(self) -> None:
        with self.lock:
            procs, self.procs = list(self.procs.values()), {}
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass


_WARM = _WarmPool()


def _has_anthropic() -> bool:
    return importlib.util.find_spec("anthropic") is not None


class Writer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = cfg.get("model") or "claude-opus-5-5"
        self.effort = cfg.get("effort") or "medium"  # openers: worth the thought
        self.reply_effort = cfg.get("reply_effort") or "low"  # chat replies: short, fast
        self._key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""
        b = cfg.get("backend") or "auto"
        if b == "auto" or (b == "api" and not (self._key and _has_anthropic())):
            b = "api" if self._key and _has_anthropic() else "claude-cli"
        self.backend = b
        self.system = SYSTEM.format(persona=persona_text(cfg))
        self.reply_system = REPLY_SYSTEM.format(persona=persona_text(cfg))

    def describe(self) -> str:
        return f"{self.backend} · {self.model} · effort {self.effort}/{self.reply_effort}"

    def prewarm(self) -> None:
        """Start the CLI for both kinds of request now, so the first draft doesn't wait for it."""
        if self.backend != "claude-cli":
            return
        _WARM.refill(self._cli_args(self.system, SCHEMA, self.effort))
        _WARM.refill(self._cli_args(self.reply_system, REPLY_SCHEMA, self.reply_effort))

    def draft(self, prof: Profile, screens: list[bytes], recent: list[str], hint: str = "",
              avoid: list[str] | None = None, tone: Tone | None = None) -> dict:
        content = build_content(prof, screens, recent, hint, avoid, tone)
        return tidy_draft(self._ask(self.system, SCHEMA, content, self.effort), prof)

    def reply(self, chat: Chat, background: str = "", recent: list[str] | None = None, hint: str = "",
              avoid: list[str] | None = None, tone: Tone | None = None) -> dict:
        """Three options for the next message in a chat."""
        out = self._ask(self.reply_system, REPLY_SCHEMA,
                        build_reply_content(chat, background, recent or [], hint, avoid, tone), self.reply_effort)
        opts = [o for o in out.get("options") or [] if (o.get("text") or "").strip()]
        out["options"] = opts
        out["best"] = min(max(int(out.get("best") or 0), 0), max(len(opts) - 1, 0))
        return out

    # ------------------------------------------------------------------ backends
    def _ask(self, system: str, schema: dict, content: list[dict], effort: str) -> dict:
        if self.backend == "api":
            return self._api(system, schema, content, effort)
        return self._cli(system, schema, content, effort)

    def _api(self, system: str, schema: dict, content: list[dict], effort: str) -> dict:
        import anthropic
        client = anthropic.Anthropic(api_key=self._key)
        resp = client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined to write this one")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)

    def _cli_args(self, system: str, schema: dict, effort: str) -> list[str]:
        exe = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
        return [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--tools", "", "--no-session-persistence", "--setting-sources", "",
                "--model", self.model, "--effort", effort,
                "--system-prompt", system, "--json-schema", json.dumps(schema)]

    def _cli(self, system: str, schema: dict, content: list[dict], effort: str) -> dict:
        args = self._cli_args(system, schema, effort)
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        proc = _WARM.take(args)
        try:
            stdout, stderr = proc.communicate(json.dumps(msg) + "\n", timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("claude CLI took over 5 minutes")
        finally:
            threading.Thread(target=_WARM.refill, args=(args,), daemon=True).start()  # ready for next time
        result = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                result = ev
        if not result or result.get("is_error"):
            err = (result or {}).get("result") or stderr.strip()[-400:] or f"exit {proc.returncode}"
            raise RuntimeError(f"claude CLI failed: {err}")
        out = result.get("structured_output")
        if out is None:
            out = json.loads(result.get("result") or "{}")
        return out


def close() -> None:
    """Stop the waiting CLI processes (when the plugin's last window closes)."""
    _WARM.close()
