"""Classify a customer's free-form reply to the "save the code?" prompt.

The worker asks, in a Basecamp comment, whether to save the code it used. The
customer answers however they like — a word ("так", "збережи"), an emoji (👍), or
a boost/reaction on the prompt. :func:`classify` maps such a signal to one of
``"save"`` / ``"discard"`` / ``None`` (unclear). It is deliberately lenient and
pure so it can be unit-tested exhaustively.

Discard wins ties: if a signal carries both a positive and a negative cue, it is
treated as ``"discard"`` — never save against a hint of "no".
"""

from __future__ import annotations

SAVE = "save"
DISCARD = "discard"

# Affirmative / negative whole-word cues (compared case-folded against tokens).
_POSITIVE_WORDS = frozenset(
    {
        "так",
        "ага",
        "угу",
        "ок",
        "окей",
        "ok",
        "okay",
        "okey",
        "yes",
        "yep",
        "yeah",
        "y",
        "да",
        "давай",
        "давайте",
        "звісно",
        "звичайно",
        "згоден",
        "згодна",
        "згода",
        "погоджуюсь",
        "save",
        "сейв",
        "+",
    }
)
_NEGATIVE_WORDS = frozenset(
    {
        "ні",
        "нi",  # latin-i typo variant
        "неа",
        "нєа",
        "нет",
        "no",
        "nope",
        "nah",
        "n",
        "-",
    }
)

# Verb stems matched as substrings (cover the inflected forms of "зберегти" /
# "видалити"). Kept short so "збережи", "зберігай", "збережіть" all match.
_POSITIVE_STEMS = ("збереж", "зберіг", "зберег")
_NEGATIVE_STEMS = (
    "не збереж",
    "не зберіг",
    "не зберег",
    "не треба",
    "не потрібно",
    "не варто",
    "видал",
)

_POSITIVE_EMOJI = frozenset("👍👌✅✔🆗🙂😊😀😁😄🎉❤🔥💯🤙👏🙏💾🫡")
_NEGATIVE_EMOJI = frozenset("👎❌🚫✋🙅🗑")


def _strip_variation_selectors(text: str) -> str:
    """Drop emoji variation selectors / skin-tone modifiers for plain matching."""
    return "".join(ch for ch in text if ch not in "️︎" and not ("\U0001f3fb" <= ch <= "\U0001f3ff"))


def classify(signal: str) -> str | None:
    """Return ``"save"``, ``"discard"`` or ``None`` for a reply/boost content.

    ``None`` means the signal is not a recognisable yes/no (e.g. an unrelated
    comment), so the caller should keep waiting.
    """
    text = _strip_variation_selectors(signal or "").casefold().strip()
    if not text:
        return None

    positive = False
    negative = False

    # Emoji cues (scan every character).
    for ch in text:
        if ch in _NEGATIVE_EMOJI:
            negative = True
        elif ch in _POSITIVE_EMOJI:
            positive = True

    # Phrase/stem cues (substring).
    if any(stem in text for stem in _NEGATIVE_STEMS):
        negative = True
    elif any(stem in text for stem in _POSITIVE_STEMS):
        positive = True

    # Whole-word cues.
    tokens = {tok for tok in _tokenize(text) if tok}
    if tokens & _NEGATIVE_WORDS:
        negative = True
    if tokens & _POSITIVE_WORDS:
        positive = True

    if negative:  # discard wins ties
        return DISCARD
    if positive:
        return SAVE
    return None


def _tokenize(text: str) -> list[str]:
    """Split on anything that is not a letter/digit, keeping bare '+'/'-' tokens."""
    tokens: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isalnum():
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
            if ch in "+-":
                tokens.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def classify_boost(content: str) -> str:
    """Classify a boost (reaction) on the prompt.

    A boost is an approval gesture by nature, so anything that is not an explicit
    "no" counts as ``"save"``.
    """
    return DISCARD if classify(content) == DISCARD else SAVE
