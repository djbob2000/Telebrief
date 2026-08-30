"""Deterministic token normalization, semantic-equivalence matching, and risk classification."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.publication.article_claims import stem_word
from src.publication.article_semantic_lexicon import (
    canonical_semantic_concepts,
    canonicalize_semantic_token,
    is_critical_semantic_concept,
)

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")
_QUOTE_NAME_RE = re.compile(r"[«\"“]([A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9\s\-]+)[»\"”]")
_ROUTE_RE = re.compile(
    r"\b(?:рейс[а-я]*|маршрут[а-я]*|направлен[а-я]*|следует\s+в|едет\s+в|курсиру[а-я]*)\b",
    re.IGNORECASE,
)


_STOPWORDS = frozenset(
    {
        "и",
        "в",
        "во",
        "не",
        "на",
        "с",
        "со",
        "по",
        "к",
        "ко",
        "у",
        "о",
        "об",
        "обо",
        "от",
        "ото",
        "до",
        "из",
        "изо",
        "за",
        "над",
        "надо",
        "под",
        "подо",
        "при",
        "про",
        "через",
        "для",
        "без",
        "безо",
        "а",
        "но",
        "да",
        "или",
        "либо",
        "то",
        "что",
        "чтобы",
        "как",
        "так",
        "если",
        "хотя",
        "когда",
        "где",
        "куда",
        "откуда",
        "почему",
        "зачем",
        "же",
        "ли",
        "бы",
        "только",
        "уже",
        "еще",
        "ещё",
        "все",
        "всё",
        "это",
        "эта",
        "этот",
        "эти",
        "этом",
        "этой",
        "этих",
        "тот",
        "та",
        "те",
        "том",
        "той",
        "тех",
        "он",
        "она",
        "оно",
        "они",
        "его",
        "ее",
        "её",
        "их",
        "ему",
        "ей",
        "им",
        "нем",
        "нём",
        "ней",
        "них",
        "мы",
        "вы",
        "я",
        "ты",
        "нас",
        "вас",
        "меня",
        "тебя",
        "нам",
        "вам",
        "мне",
        "тебе",
        "собой",
        "собою",
        "свой",
        "своя",
        "свое",
        "своё",
        "свои",
        "своих",
        "своим",
        "был",
        "была",
        "было",
        "были",
        "быть",
        "будет",
        "будут",
        "есть",
        "является",
        "являются",
        "также",
        "тоже",
        "очень",
        "даже",
        "вдруг",
        "между",
        "после",
        "перед",
        "около",
        "вокруг",
        "такой",
        "такая",
        "такое",
        "такие",
        "который",
        "которая",
        "которое",
        "которые",
        # Reporting and temporal discourse markers
        "сохраняется",
        "сохраняются",
        "продолжается",
        "продолжаются",
        "сообщается",
        "сообщают",
        "сообщили",
        "сообщил",
        "отмечается",
        "отмечают",
        "отметили",
        "наблюдается",
        "наблюдаются",
        "зафиксировано",
        "произошло",
        "произошла",
        "произошли",
        "произойти",
        "ранее",
        "по-прежнему",
        "прежде",
        "снова",
        "вновь",
        "опять",
        "сейчас",
        "ныне",
        "сегодня",
        "накануне",
        # Ukrainian basic grammatical words
        "чи",
        "що",
        "як",
        "проте",
        "але",
        "від",
        "зі",
        "із",
        "щодо",
        "він",
        "вона",
        "вони",
        "його",
        "її",
        "їх",
        "йому",
        "їй",
        "їм",
        "нами",
        "вами",
        "ними",
        "був",
        "була",
        "було",
        "були",
        "буде",
        "будуть",
        "є",
        "це",
        "цей",
        "ця",
        "ці",
    }
)

_EDITORIAL_GLUE = frozenset(
    {
        "часть",
        "возможность",
        "предоставлять",
        "предоставляет",
        "один",
        "одном",
        "несколько",
        "тема",
        "обсуждать",
        "обсуждают",
        "район",
        "город",
        "житель",
        "жители",
        "сообщать",
        "сообщают",
        "зафиксировать",
        "зафиксировано",
    }
)

_EQUIVALENCE_FAMILIES = (
    frozenset({"свет", "электрич", "электроснабж"}),
    frozenset({"вод", "водоснабж"}),
    frozenset({"отсутств", "нет"}),
    frozenset({"отключ", "обесточ"}),
    frozenset({"выбор", "выбир"}),
    frozenset({"назнач", "назнача"}),
    frozenset({"заряд", "заряж"}),
)

_CALENDAR_WORDS = frozenset(
    {
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
        "январь",
        "января",
        "февраль",
        "февраля",
        "март",
        "марта",
        "апрель",
        "апреля",
        "май",
        "мая",
        "июнь",
        "июня",
        "июль",
        "июля",
        "август",
        "августа",
        "сентябрь",
        "сентября",
        "октябрь",
        "октября",
        "ноябрь",
        "ноября",
        "декабрь",
        "декабря",
    }
)


def _stems_match(stem_a: str, stem_b: str) -> bool:
    if stem_a == stem_b:
        return True
    for fam in _EQUIVALENCE_FAMILIES:
        if any(stem_a.startswith(m) or m.startswith(stem_a) for m in fam) and any(
            stem_b.startswith(m) or m.startswith(stem_b) for m in fam
        ):
            return True
    if len(stem_a) >= 4 and len(stem_b) >= 4:
        if stem_a.startswith(stem_b) or stem_b.startswith(stem_a):
            return True
    return False


def _extract_proper_name_candidates(text: str) -> set[str]:
    """Extract candidate proper names (destinations, places, quoted names)."""
    candidates: set[str] = set()

    # 1. Quoted terms
    for match in _QUOTE_NAME_RE.finditer(text):
        quoted = match.group(1).strip()
        for tok in _TOKEN_RE.findall(quoted):
            cand = tok.lower().replace("ё", "е")
            if len(cand) >= 2 and cand not in _STOPWORDS and cand not in _CALENDAR_WORDS:
                candidates.add(cand)

    # 2. Non-sentence-initial TitleCase words
    sentences = _SENTENCE_SPLIT_RE.split(text)
    for sent in sentences:
        tokens = _TOKEN_RE.findall(sent)
        if not tokens:
            continue
        # Skip the very first word of the sentence
        for tok in tokens[1:]:
            if (
                tok[0].isupper() and (len(tok) == 1 or tok[1:].islower() or tok.isupper())
            ) and tok.isalpha():
                cand = tok.lower().replace("ё", "е")
                if len(cand) >= 2 and cand not in _STOPWORDS and cand not in _CALENDAR_WORDS:
                    candidates.add(cand)

    return candidates


@dataclass(frozen=True)
class SemanticSupportSignals:
    lexical_coverage: float
    unmatched_terms: tuple[str, ...]
    blocking_terms: tuple[str, ...]
    blocking_critical_terms: tuple[str, ...]
    unmatched_proper_names: tuple[str, ...]
    blocking_proper_names: tuple[str, ...]


def assess_semantic_support(
    claim_text: str, support_texts: Sequence[str]
) -> SemanticSupportSignals:
    """Assess semantic overlap and risk-based factual novelty between claim and supports."""
    if not claim_text.strip():
        return SemanticSupportSignals(
            lexical_coverage=1.0,
            unmatched_terms=(),
            blocking_terms=(),
            blocking_critical_terms=(),
            unmatched_proper_names=(),
            blocking_proper_names=(),
        )

    # 1. Prepare support tokens and stems
    support_stems: set[str] = set()
    support_tokens_lower: set[str] = set()
    support_concepts: set[str] = set()
    for st in support_texts:
        if not st:
            continue
        support_concepts.update(canonical_semantic_concepts(st))
        cleaned = st.lower().replace("ё", "е")
        for tok in _TOKEN_RE.findall(cleaned):
            if len(tok) >= 2 and tok not in _STOPWORDS:
                support_tokens_lower.add(tok)
                support_stems.add(stem_word(tok))

    claim_concepts = canonical_semantic_concepts(claim_text)
    diff_concepts = claim_concepts - support_concepts
    blocking_critical_terms = tuple(
        sorted(c for c in diff_concepts if is_critical_semantic_concept(c))
    )

    # 2. Extract and check claim proper names
    claim_proper_names = _extract_proper_name_candidates(claim_text)
    unmatched_proper_names_set: set[str] = set()
    for pn in claim_proper_names:
        pn_stem = stem_word(pn)
        pn_concept = canonicalize_semantic_token(pn)
        matched = (
            pn in support_tokens_lower
            or any(_stems_match(pn_stem, s_stem) for s_stem in support_stems)
            or any(pn in st.lower().replace("ё", "е") for st in support_texts)
            or (pn_concept.startswith("concept:") and pn_concept in support_concepts)
        )
        if not matched:
            unmatched_proper_names_set.add(pn)

    unmatched_proper_names = tuple(sorted(unmatched_proper_names_set))
    is_route_claim = bool(_ROUTE_RE.search(claim_text))

    if is_route_claim and len(unmatched_proper_names) >= 1:
        blocking_proper_names = unmatched_proper_names
    elif len(unmatched_proper_names) >= 2:
        blocking_proper_names = unmatched_proper_names
    else:
        blocking_proper_names = ()

    # 3. Extract and match claim content terms
    claim_cleaned = claim_text.lower().replace("ё", "е")
    claim_raw_tokens = _TOKEN_RE.findall(claim_cleaned)
    claim_content_stems: list[str] = []
    claim_content_tokens: list[str] = []

    for tok in claim_raw_tokens:
        if len(tok) < 2 or tok in _STOPWORDS or tok.isdigit():
            continue
        claim_content_tokens.append(tok)
        claim_content_stems.append(stem_word(tok))

    if not claim_content_stems:
        return SemanticSupportSignals(
            lexical_coverage=1.0,
            unmatched_terms=(),
            blocking_terms=(),
            blocking_critical_terms=blocking_critical_terms,
            unmatched_proper_names=unmatched_proper_names,
            blocking_proper_names=blocking_proper_names,
        )

    glue_stems = {stem_word(g.lower().replace("ё", "е")) for g in _EDITORIAL_GLUE} | {
        g.lower().replace("ё", "е") for g in _EDITORIAL_GLUE
    }

    unmatched_terms_list: list[str] = []
    unmatched_non_glue: list[str] = []
    matched_count = 0

    has_quantity_100 = "quantity:>100" in claim_concepts and "quantity:>100" in support_concepts
    quantity_100_tokens = {
        "более",
        "свыше",
        "понад",
        "більше",
        "сто",
        "ста",
        "сотня",
        "сотню",
        "сотні",
    }

    for tok, stem in zip(claim_content_tokens, claim_content_stems, strict=True):
        tok_concept = canonicalize_semantic_token(tok)
        matched = (
            tok in support_tokens_lower
            or stem in support_stems
            or any(_stems_match(stem, s_stem) for s_stem in support_stems)
            or (tok_concept.startswith("concept:") and tok_concept in support_concepts)
            or (has_quantity_100 and tok in quantity_100_tokens)
        )
        if matched:
            matched_count += 1
        else:
            unmatched_terms_list.append(tok)
            is_glue = (
                stem in glue_stems
                or tok in glue_stems
                or any(stem.startswith(g) or g.startswith(stem) for g in glue_stems if len(g) >= 4)
            )
            if not is_glue:
                unmatched_non_glue.append(tok)

    lexical_coverage = matched_count / len(claim_content_stems)
    unmatched_terms = tuple(sorted(set(unmatched_terms_list)))

    # Novel content is blocking only when >= 2 non-glue terms remain
    if len(set(unmatched_non_glue)) >= 2:
        blocking_terms = tuple(sorted(set(unmatched_non_glue)))
    else:
        blocking_terms = ()

    return SemanticSupportSignals(
        lexical_coverage=lexical_coverage,
        unmatched_terms=unmatched_terms,
        blocking_terms=blocking_terms,
        blocking_critical_terms=blocking_critical_terms,
        unmatched_proper_names=unmatched_proper_names,
        blocking_proper_names=blocking_proper_names,
    )
