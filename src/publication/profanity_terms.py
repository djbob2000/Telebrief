"""Shared profanity and abuse pattern for publication quality checks."""

from __future__ import annotations

import re

PROFANITY_AND_ABUSE_RE = re.compile(
    r"\b(?:"
    r"ху[йяеёюиы]\w*|"
    r"[нп]аху[йяе]\w*|"
    r"поху[йяе]\w*|"
    r"пизд\w*|"
    r"[её]б[аеёиуытлн]\w*|"
    r"у[её]б\w*|"
    r"в[ъь][её]б\w*|"
    r"за[её]б\w*|"
    r"от[её]б\w*|"
    r"раз[её]б\w*|"
    r"до[её]б\w*|"
    r"пере[её]б\w*|"
    r"бл[яя]т\w*|"
    r"бляд\w*|"
    r"сук[аиоуые]\w*|"
    r"мудак\w*|"
    r"мудил\w*|"
    r"залуп\w*|"
    r"срак\w*|"
    r"г[іие]вн\w*|"
    r"дерьм\w*|"
    r"хер\w*|"
    r"жоп\w*"
    r")\b",
    re.IGNORECASE,
)
