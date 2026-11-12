"""Who is speaking: opaque speaker identity, gender cues and speaker-count checks.

A support's speaker is the platform author of its own message. The key is an
internal hash; the writer dossier receives only per-article aliases (S1, S2...).
A display name is a weaker identity than a platform id and is used only when the
platform gave no id for that message.

Several messages do not prove several people (AGENTS.md 0.10): a claim that
introduces "another resident" must be backed by a different speaker, and a plural
"residents advise" must rest on at least two speakers.

This module is dependency-light so the writer contract can import it. The checks
import the finding types lazily to avoid an import cycle with the validator and
the quality diagnostics.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Literal

from src.publication.article_quote_masks import (
    QUOTE_MASK_EDITOR_INSTRUCTIONS,
    QUOTE_MASK_WRITER_RULE,
)

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext
    from src.publication.article_material import ArticleMaterialProjection
    from src.publication.article_models import ArticleClaimAtom, StructuredArticleDraft
    from src.publication.article_quality import ArticleReaderQualityFinding
    from src.publication.article_validator import ArticleValidationIssue

SpeakerGender = Literal["f", "m", "unknown"]

SPEAKER_MULTIPLICITY_CODE = "SPEAKER_MULTIPLICITY_MISMATCH"
PLURAL_SOURCE_INFLATION_CODE = "PLURAL_SOURCE_INFLATION"
UNSUPPORTED_SPEAKER_GENDER_CODE = "UNSUPPORTED_SPEAKER_GENDER"

_UNKNOWN_NAMES = frozenset({"", "unknown", "неизвестно"})

# "another resident": a further speaker is introduced.
_OTHER_SPEAKER_RE = re.compile(
    r"\b(?:друг(?:ой|ая|ие|ого|ую|ому|им|ими|ом)|ещ[её]\s+(?:один|одна|одни|одного|одну|одной))"
    r"\s+(?:из\s+)?(?:житель|жительниц|жител|горожан|горожанк)\w*",
    re.IGNORECASE,
)
_PLURAL_SPEECH_RE = re.compile(
    r"\b(?:жители|горожане|местные|люди)\b(?:\s+[^\s,.;:!?«»\"]+){0,6}?\s+"
    r"(?:советуют|рассказывают|сообщают|пишут|жалуются|отмечают|предлагают)\b",
    re.IGNORECASE,
)
_FEMALE_NOUN_RE = re.compile(r"\b(?:жительниц\w*|горожанк\w*|читательниц\w*)\b", re.IGNORECASE)

_WORD_RE = re.compile(r"[а-яё]+")
# Words before «другой житель» that make a clause naming its own subject.
_MIN_CLAUSE_WORDS = 3
# Frequent nouns and adjectives whose endings look like past-tense verb endings.
_NOT_VERBS = frozenset(
    {
        "смысла",
        "тепла",
        "зла",
        "дела",
        "стекла",
        "числа",
        "тела",
        "мгла",
        "стола",
        "зала",
        "стол",
        "пол",
        "зал",
        "вокзал",
        "сигнал",
        "гол",
    }
)
_FEMALE_PAST_RE = re.compile(r"[а-яё]{2,}(?:ла|лась)")
_MALE_PAST_RE = re.compile(r"[а-яё]{2,}(?:л|лся)")
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;:,—–\n()«»\"]+|\s+(?:а|но|и)\s+")
# Adverbials that may precede a verb at the start of a clause.
_CLAUSE_ADVERBIAL_PREFIX_RE = re.compile(
    r"^\s*(?:(?:сегодня|вчера|уже|только|наконец|ещё|еще|тоже|сама|сам|также)\s+"
    r"|на\s+[^\s,.;:!?]+(?:\s+[^\s,.;:!?]+)?\s+(?:день|сутки|неделю)\s+)*",
    re.IGNORECASE,
)
_CLAUSE_VERB_RE = re.compile(r"\s*([а-яё]+)", re.IGNORECASE)

_SPEAKER_RULE_TEXT = (
    "Говорящие (досье): speaker — один человек. Один псевдоним speaker нельзя подавать как "
    "«другой житель», «ещё один горожанин» или «другая жительница»: это разные люди только "
    "при разных псевдонимах. Если автор отвечает на уточняющий вопрос о том же событии, это "
    "одно сообщение, а не второе. Слова «жители», «горожане», «местные», «люди» в значении "
    "нескольких сообщивших требуют не меньше двух разных speaker; если все подтверждения одного "
    "speaker, пишите «житель сообщил» или «по сообщению жителя». reply_to_speaker — автор "
    "сообщения, на которое отвечают; не выдавайте его за другого человека без оснований. "
    "Слова «жительница», «горожанка», «читательница» используйте только при speaker_gender f; "
    "при m и unknown пишите «житель», «горожанин» или «автор сообщения». Женский род не "
    "приписывайте диспетчеру и другим третьим лицам по их глаголам в источнике. Псевдонимы S1, "
    "S2 — внутренние метки досье: не выводите их в текст."
)

ARTICLE_SPEAKER_WRITER_RULE = _SPEAKER_RULE_TEXT + " " + QUOTE_MASK_WRITER_RULE

ARTICLE_SPEAKER_EDITOR_INSTRUCTIONS: dict[str, str] = {
    SPEAKER_MULTIPLICITY_CODE: (
        " -> Эта фраза приписывает утверждение другому человеку, но все её подтверждения "
        "принадлежат тому же автору, что и предыдущее утверждение абзаца. Уберите «другой житель» "
        "или «ещё один горожанин» и изложите фразу как сообщение того же автора либо опирайтесь на "
        "подтверждения другого говорящего. Не придумывайте второго человека и не меняйте факты."
    ),
    PLURAL_SOURCE_INFLATION_CODE: (
        " -> Все подтверждения этого абзаца принадлежат одному говорящему. Замените «жители», "
        "«горожане», «местные» или «люди» на «житель» либо «по сообщению жителя» с тем же смыслом; "
        "не создавайте впечатление нескольких независимых источников. Факты не меняйте."
    ),
    UNSUPPORTED_SPEAKER_GENDER_CODE: (
        " -> Подтверждения этого абзаца не показывают, что автор — женщина. Замените «жительница», "
        "«горожанка» или «читательница» на нейтральное «житель», «горожанин» либо «автор "
        "сообщения», сохранив факт и атрибуцию."
    ),
}


def article_speaker_editor_instruction(code: str) -> str | None:
    """Editor instruction for a speaker or quote-mask finding code, or None."""
    return ARTICLE_SPEAKER_EDITOR_INSTRUCTIONS.get(code) or QUOTE_MASK_EDITOR_INSTRUCTIONS.get(code)


def speaker_key_for(
    source_id: int | str,
    author_external_id: str | None,
    author_name: str | None,
) -> str:
    """Internal opaque speaker key, or "" when no speaker identity is known.

    A platform author id is preferred. A display name is used only inside the same
    source, and generic placeholders such as "Unknown" are not identities.
    """
    external = (author_external_id or "").strip()
    if external:
        identity = external
    else:
        name = " ".join((author_name or "").split())
        if name.lower() in _UNKNOWN_NAMES:
            return ""
        identity = f"name:{name}"
    basis = f"{source_id}:{identity}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def detect_speaker_gender(text: str) -> SpeakerGender:
    """Gender of the author from the author's own text only; conflicts are unknown.

    Marks f or m when «я» is followed within three words by a past-tense verb
    (-ла/-лась or -л/-лся), or when a clause starts, after optional adverbials,
    with such a verb. A third-person subject ("диспетчер сказала") stays unknown.
    """
    if not text:
        return "unknown"
    lowered = text.lower()
    signals: set[str] = set()
    words = _WORD_RE.findall(lowered)
    for index, word in enumerate(words):
        if word != "я":
            continue
        for candidate in words[index + 1 : index + 4]:
            if candidate not in _NOT_VERBS and _FEMALE_PAST_RE.fullmatch(candidate):
                signals.add("f")
                break
            if candidate not in _NOT_VERBS and _MALE_PAST_RE.fullmatch(candidate):
                signals.add("m")
                break
    for clause in _CLAUSE_SPLIT_RE.split(lowered):
        rest = _CLAUSE_ADVERBIAL_PREFIX_RE.sub("", clause, count=1)
        verb = _CLAUSE_VERB_RE.match(rest)
        if verb is None:
            continue
        token = verb.group(1)
        if token in _NOT_VERBS:
            continue
        if _FEMALE_PAST_RE.fullmatch(token):
            signals.add("f")
        elif _MALE_PAST_RE.fullmatch(token):
            signals.add("m")
    if signals == {"f"}:
        return "f"
    if signals == {"m"}:
        return "m"
    return "unknown"


def speaker_gender_by_key(supports: Iterable[object]) -> dict[str, SpeakerGender]:
    """Per-speaker gender from every own message of that speaker, conflicts unknown."""
    signals: dict[str, set[str]] = {}
    for support in supports:
        key = str(getattr(support, "speaker_key", "") or "")
        if not key:
            continue
        gender = detect_speaker_gender(str(getattr(support, "source_text", "") or ""))
        if gender != "unknown":
            signals.setdefault(key, set()).add(gender)
    result: dict[str, SpeakerGender] = {}
    for key, values in signals.items():
        result[key] = "f" if values == {"f"} else "m" if values == {"m"} else "unknown"
    return result


def support_speaker_gender(
    support: object,
    gender_by_key: Mapping[str, SpeakerGender],
) -> SpeakerGender:
    """Gender of one support's speaker; without a key, its own message decides."""
    key = str(getattr(support, "speaker_key", "") or "")
    if key:
        return gender_by_key.get(key, "unknown")
    return detect_speaker_gender(str(getattr(support, "source_text", "") or ""))


def _claim_speaker_keys(
    support_ids: Iterable[str],
    support_by_id: Mapping[str, object],
) -> frozenset[str] | None:
    """Speaker keys of cited supports, or None when any identity is unknown."""
    keys: set[str] = set()
    for support_id in support_ids:
        support = support_by_id.get(support_id)
        if support is None:
            return None
        key = str(getattr(support, "speaker_key", "") or "")
        if not key:
            return None
        keys.add(key)
    return frozenset(keys) if keys else None


def _multiplicity_hits(
    claims: Iterable[ArticleClaimAtom],
    support_by_id: Mapping[str, object],
) -> list[ArticleClaimAtom]:
    """Claims that introduce another speaker while citing only the previous speaker.

    A claim is judged only when every cited support has a known speaker key and the
    claim cites a single speaker. Claim atoms may be generated per sentence from the
    paragraph's whole citation set, so a multi-speaker citation set is not evaluated.
    """
    hits: list[ArticleClaimAtom] = []
    previous: frozenset[str] | None = None
    for claim in claims:
        text = str(getattr(claim, "text", "") or "")
        cited = tuple(getattr(claim, "cited_support_ids", ()) or ())
        keys = _claim_speaker_keys(cited, support_by_id)
        marker = _OTHER_SPEAKER_RE.search(text)
        if marker is not None:
            # A clause before the marker in the same claim shares this claim's citations.
            # A lone preposition («У другого горожанина…») is not such a clause.
            before = text[: marker.start()].strip(" ,;")
            is_clause = len(_WORD_RE.findall(before.lower())) >= _MIN_CLAUSE_WORDS
            reference = keys if is_clause else previous
            if keys is not None and len(keys) == 1 and reference and keys <= reference:
                hits.append(claim)
        previous = keys
    return hits


def speaker_validation_issues(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
) -> list[ArticleValidationIssue]:
    """Blocking SPEAKER_MULTIPLICITY_MISMATCH findings for paragraph claims."""
    from src.publication.article_validator import ArticleValidationIssue

    support_by_id = getattr(context, "support_by_id", {})
    issues: list[ArticleValidationIssue] = []
    paragraph_index = 0
    for section in draft.sections:
        for paragraph in section.paragraphs:
            paragraph_index += 1
            unit_id = f"P{paragraph_index:03d}"
            for claim in _multiplicity_hits(paragraph.claims, support_by_id):
                issues.append(
                    ArticleValidationIssue(
                        code=SPEAKER_MULTIPLICITY_CODE,
                        unit_id=unit_id,
                        message=(
                            f"Unit {unit_id} introduces another speaker ('{claim.text}'), "
                            "but all cited supports come from the speaker of the previous claim"
                        ),
                        support_ids=tuple(claim.cited_support_ids),
                        severity="error",
                        blocking=True,
                        claim_text=claim.text,
                    )
                )
    return issues


def prose_units(draft: StructuredArticleDraft) -> list[tuple[str, str, tuple[str, ...]]]:
    """(unit_id, text, cited support IDs including claim citations) for prose units."""

    def cited(raw: Iterable[str], claims: Iterable[ArticleClaimAtom]) -> tuple[str, ...]:
        ids = [*raw, *(sid for claim in claims for sid in claim.cited_support_ids)]
        return tuple(dict.fromkeys(str(sid) for sid in ids if sid))

    units: list[tuple[str, str, tuple[str, ...]]] = [
        ("TITLE", draft.title, cited(draft.title_support_ids, draft.title_claims)),
        ("LEAD", draft.lead, cited(draft.lead_support_ids, draft.lead_claims)),
    ]
    paragraph_index = 0
    for section_index, section in enumerate(draft.sections, start=1):
        units.append(
            (
                f"H{section_index:03d}",
                section.heading,
                cited(section.heading_support_ids, section.heading_claims),
            )
        )
        for paragraph in section.paragraphs:
            paragraph_index += 1
            units.append(
                (
                    f"P{paragraph_index:03d}",
                    paragraph.text,
                    cited(paragraph.cited_support_ids, paragraph.claims),
                )
            )
    return units


def speaker_quality_findings(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None = None,
) -> list[ArticleReaderQualityFinding]:
    """Repair findings for plural inflation and unsupported female-gender nouns."""
    from src.publication.article_quality import (
        ArticleReaderQualityFinding,
        _citable_support_ids,
    )

    support_by_id = getattr(context, "support_by_id", {})
    supports_for_gender = tuple(getattr(context, "support_index", ()) or support_by_id.values())
    gender_by_key = speaker_gender_by_key(supports_for_gender)
    findings: list[ArticleReaderQualityFinding] = []

    for unit_id, text, cited_ids in prose_units(draft):
        if not text.strip():
            continue
        citable = _citable_support_ids(cited_ids, context, material_projection)
        supports = [support_by_id[sid] for sid in citable if sid in support_by_id]
        if not supports:
            continue

        plural_speech = any(_PLURAL_SPEECH_RE.search(sentence) for sentence in _sentences(text))
        keys = {str(getattr(support, "speaker_key", "") or "") for support in supports}
        if plural_speech and "" not in keys and len(keys) == 1:
            findings.append(
                ArticleReaderQualityFinding(
                    code=PLURAL_SOURCE_INFLATION_CODE,
                    unit_id=unit_id,
                    message=(
                        "Множественное подлежащее («жители», «горожане» и т.п.) опирается на "
                        "сообщения одного автора; назовите одного говорящего."
                    ),
                    support_ids=citable,
                    severity="repair",
                )
            )

        if _FEMALE_NOUN_RE.search(text) and all(
            support_speaker_gender(support, gender_by_key) != "f" for support in supports
        ):
            findings.append(
                ArticleReaderQualityFinding(
                    code=UNSUPPORTED_SPEAKER_GENDER_CODE,
                    unit_id=unit_id,
                    message=(
                        "Женский род («жительница», «горожанка» и т.п.) не подтверждён автором "
                        "сообщений; используйте нейтральное обозначение."
                    ),
                    support_ids=citable,
                    severity="repair",
                )
            )
    return findings


def assign_speaker_aliases(
    records: Iterable[dict[str, object]],
    identity_by_record_id: Mapping[int, tuple[str, str]],
    gender_by_key: Mapping[str, SpeakerGender],
) -> None:
    """Attach per-article speaker aliases (S1, S2...) to dossier records in order.

    The keys are internal hashes and never leave this function; records receive only
    aliases and the gender of that speaker.
    """
    alias_by_key: dict[str, str] = {}

    def alias(key: str) -> str:
        if key not in alias_by_key:
            alias_by_key[key] = f"S{len(alias_by_key) + 1}"
        return alias_by_key[key]

    for record in records:
        speaker_key, reply_key = identity_by_record_id.get(id(record), ("", ""))
        if speaker_key:
            record["speaker"] = alias(speaker_key)
            record["speaker_gender"] = gender_by_key.get(speaker_key, "unknown")
        if reply_key:
            record["reply_to_speaker"] = alias(reply_key)


def _sentences(text: str) -> list[str]:
    from src.publication.article_models import _split_sentences_safe

    return _split_sentences_safe(text) or [text]
