"""Checks on the words of a call that do not depend on a model behaving.

Two directions. What the contact says is watched for a request to stop calling and for
"am I talking to a person?". What the agent is about to say is checked for the things it
must never say, and any sentence that fails is withheld before it is spoken.
"""

import re

_OPT_OUT = re.compile(
    r"\bstop (calling|phoning|ringing|contacting)\b"
    r"|\b(take|remove|get|put) (me|my (number|name)|this number|us)\b.{0,25}\b(off|from|on)\b.{0,25}\b(list|database|system)\b"
    r"|\bdo[- ]not[- ]call\b"
    r"|\bdon'?t (ever )?(call|phone|ring|contact) (me|us|this number|here)( back)?( ever)? (again|any ?more)\b"
    r"|\bnever (call|phone|ring|contact) (me|us|this number|here)\b"
    r"|\bno more calls\b"
    r"|\blose (my|this) number\b"
    r"|\bunsubscribe\b",
    re.IGNORECASE,
)

_ASKS_IF_HUMAN = re.compile(
    r"\b(are|is) (you|this) (a |an )?(real |actual |live )?(person|human|robot|bot|machine|recording|computer|ai)\b"
    r"|\b(am i|i'?m) (talking|speaking) (to|with) (a |an )?(real |actual |live )?(person|human|robot|bot|machine|computer|ai)\b"
    r"|\bis this (a )?(real|automated|ai)\b",
    re.IGNORECASE,
)

_SAYS_AI = re.compile(r"\b(AI|A\.I\.|artificial intelligence|automated|virtual assistant)\b")
_RECORDED = re.compile(r"\brecord", re.IGNORECASE)

_NUMBER_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2}(?:\.\d{1,3})?)"
)
_RATE = re.compile(
    rf"\b{_NUMBER_WORD}(?: point (?:\w+ ?){{1,3}}| and (?:a|one|three) \w+)?\s?(?:%|percent)",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"\bguarantee[ds]?\b|\bpre-?approved\b"
    r"|\byou(?:'ll| will| would)(?: definitely| certainly| easily)? (?:be approved|get approved|qualify)\b"
    r"|\bno problem (?:getting|being) approved\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:can(?:'|no)?t|cannot|can not|not|no|never|unable|isn'?t|won'?t|don'?t|neither|nobody)\b",
    re.IGNORECASE,
)
_PRESSURE = re.compile(
    r"\b(?:today only|last chance|expires? (?:today|tonight)|before midnight|right now or|act now"
    r"|won'?t be available (?:again|later))\b",
    re.IGNORECASE,
)
_NINE_DIGITS = re.compile(r"(?<!\d)(?:\d[\s\-.]?){9}(?!\d)")
_ASIDE = re.compile(r"\[[^\]]*\]|\*[^*\n]+\*")  # [ends call], *pauses*
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")  # not the point in "6.25"


def sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_END.split(text.strip()) if part]


def asks_to_stop(text: str) -> bool:
    """The contact has asked not to be called again."""
    return _OPT_OUT.search(text) is not None


def asks_if_human(text: str) -> bool:
    return _ASKS_IF_HUMAN.search(text) is not None


def says_ai(text: str) -> bool:
    return _SAYS_AI.search(text) is not None


def has_disclosures(opening: str) -> bool:
    """The first thing said names the caller as an AI and says the call is recorded."""
    return says_ai(opening) and _RECORDED.search(opening) is not None


def violations(sentence: str) -> list[str]:
    """What one sentence does that the agent must never do."""
    found = []
    if _RATE.search(sentence):
        found.append("quotes a rate")
    if _PROMISE.search(sentence) and not _NEGATION.search(sentence):
        found.append("promises approval")
    if _PRESSURE.search(sentence):
        found.append("pressures the contact")
    if _NINE_DIGITS.search(sentence):
        found.append("repeats a nine-digit number")
    return found


def withhold(text: str, fallback: str) -> tuple[str, list[str]]:
    """Remove the sentences that break a rule. Returns what may be spoken and what was wrong.

    When anything is removed the fallback line is added, so the contact still gets an answer.
    """
    kept, problems = [], []
    spoken = _ASIDE.sub(" ", text)
    if spoken != text:
        problems.append("writes a stage direction")
    for sentence in sentences(spoken):
        found = violations(sentence)
        if found:
            problems += found
        else:
            kept.append(sentence.strip())
    if not problems:
        return text.strip(), []
    if problems == ["writes a stage direction"]:
        return " ".join(kept), problems
    return " ".join([*kept, fallback]).strip(), problems
