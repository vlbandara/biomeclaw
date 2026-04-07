"""Safety rules for health-enabled workspaces."""

from __future__ import annotations

import re

_EMERGENCY_PATTERNS = (
    r"\bsuicid(?:e|al)\b",
    r"\bkill myself\b",
    r"\bself[- ]harm\b",
    r"\boverdos(?:e|ed)\b",
    r"\bcan't breathe\b",
    r"\bcannot breathe\b",
    r"\bchest pain\b",
    r"\bstroke\b",
    r"\bseizure\b",
    r"\bunconscious\b",
    r"\bpassed out\b",
    r"\bsevere bleeding\b",
    r"\bheart attack\b",
)

_EMERGENCY_RE = re.compile("|".join(_EMERGENCY_PATTERNS), re.IGNORECASE)

_EMERGENCY_RESPONSE = (
    "I may be wrong, but this sounds like a possible emergency. "
    "Please contact your local emergency services now or go to the nearest emergency department. "
    "If someone is with you, ask them to help you get immediate local care. "
    "If you are thinking about harming yourself or feel unsafe, call your local emergency number now."
)


def is_emergency_language(text: str) -> bool:
    return bool(text and _EMERGENCY_RE.search(text))


def emergency_response() -> str:
    return _EMERGENCY_RESPONSE
