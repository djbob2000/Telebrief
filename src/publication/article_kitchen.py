"""Closed chat-kitchen vocabulary for article prose.

Reader-facing article text must not reveal how a community report reached the
edition ("в чатах", "перекличка", "участник чата"). The validator blocks any
remaining match; the deterministic repair below rewrites only the closed
forms listed here into neutral attribution, so a writer slip does not need
another model call. Facts, numbers, names and quotes are never touched.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_CHAT_ROOM = r"(?:чатах|чате|пабликах|каналах|канале)"

CHAT_KITCHEN_LEAK_PATTERN = re.compile(
    r"\b(?:"
    r"перекличк[а-я]*|"
    rf"в\s+(?:(?:местн|городск)[а-я]*\s+)?{_CHAT_ROOM}|"
    r"участник[а-я]*\s+чата|"
    r"участниц[а-я]*\s+чата|"
    r"в\s+комментариях|"
    r"в\s+(?:телеграм|telegram)-канал[а-я]*"
    r")\b",
    re.IGNORECASE,
)

_NEUTRAL_LOCATIVE = "в обсуждениях"

_PARTICIPANT_FORMS = {
    "участник": "житель",
    "участника": "жителя",
    "участнику": "жителю",
    "участником": "жителем",
    "участнике": "жителе",
    "участники": "жители",
    "участников": "жителей",
    "участникам": "жителям",
    "участниками": "жителями",
    "участниках": "жителях",
    "участница": "жительница",
    "участницы": "жительницы",
    "участницу": "жительницу",
    "участницей": "жительницей",
    "участнице": "жительнице",
    "участниц": "жительниц",
    "участницам": "жительницам",
    "участницами": "жительницами",
    "участницах": "жительницах",
}

_ROLLCALL_FORMS = {
    "перекличка": "сводка жителей",
    "переклички": "сводки жителей",
    "перекличку": "сводку жителей",
    "перекличкой": "сводкой жителей",
    "перекличке": "сводке жителей",
    "перекличек": "сводок жителей",
    "перекличкам": "сводкам жителей",
    "перекличками": "сводками жителей",
}


def _match_case(source: str, replacement: str) -> str:
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _replace_participant(match: re.Match[str]) -> str:
    word = match.group(1)
    replacement = _PARTICIPANT_FORMS.get(word.lower())
    if replacement is None:
        return match.group(0)
    return _match_case(word, replacement)


def _replace_rollcall(match: re.Match[str]) -> str:
    word = match.group(0)
    replacement = _ROLLCALL_FORMS.get(word.lower())
    if replacement is None:
        return word
    return _match_case(word, replacement)


_REPAIRS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(rf"\bв\s+(?:(?:местн|городск)[а-я]*\s+)?{_CHAT_ROOM}\b", re.IGNORECASE),
        lambda m: _match_case(m.group(0), _NEUTRAL_LOCATIVE),
    ),
    (
        re.compile(r"\bв\s+комментариях\b", re.IGNORECASE),
        lambda m: _match_case(m.group(0), _NEUTRAL_LOCATIVE),
    ),
    (
        re.compile(r"\bв\s+(?:телеграм|telegram)-канал[а-я]*\b", re.IGNORECASE),
        lambda m: _match_case(m.group(0), _NEUTRAL_LOCATIVE),
    ),
    (
        re.compile(r"\b(участник[а-я]*|участниц[а-я]*)\s+чата\b", re.IGNORECASE),
        _replace_participant,
    ),
    (re.compile(r"\bперекличк[а-я]*\b", re.IGNORECASE), _replace_rollcall),
)


def repair_chat_kitchen_phrases(text: str) -> str:
    """Rewrite closed chat-kitchen forms into neutral wording.

    Unknown inflections are left untouched so the validator still blocks them.
    The function is idempotent: repaired text contains no closed form.
    """
    repaired = text
    for pattern, replacement in _REPAIRS:
        repaired = pattern.sub(replacement, repaired)
    return repaired
