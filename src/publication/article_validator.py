"""Deterministic structural and evidence-bound validator for editorial articles."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from src.config_loader import PublicationEditorialConfig
from src.publication.article_claim_support import assess_claim_against_supports
from src.publication.article_claims import ConcreteClaim, _stem, find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_geography import (
    resolve_article_place_resolver,
)
from src.publication.article_length import ArticleLengthProfile
from src.publication.article_material import (
    ArticleMaterialProjection,
    materialize_article_validation_context,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    StructuredArticleDraft,
    _normalize_for_dedup,
    _split_sentences_safe,
)
from src.publication.article_semantic_lexicon import canonical_semantic_concepts
from src.publication.article_semantic_support import assess_semantic_support

_INTERNAL_HANDLE_PATTERN = re.compile(
    r"\[(?:story:\d+:evidence:\d+:frag:\d+|story:\d+|evidence:\d+:frag:\d+|op:[^\]]+|SUPPORT\s+[^\]]+)\]",
    re.IGNORECASE,
)

_META_OMISSION_PATTERN = re.compile(
    r"\b(?:контактн[а-я]+\s+данн[а-я]+\s+опущен[а-я]*|"
    r"телефон[а-я]*\s+не\s+(?:указыва[а-я]+|привод[а-я]+|публику[а-я]+)|"
    r"контакт[а-я]*\s+скрыт[а-я]*|"
    r"дат[а-я]*\s+не\s+(?:указыва[а-я]+|уточня[а-я]+)|"
    r"номера\s+не\s+публику[а-я]+)\b",
    re.IGNORECASE,
)

_CHAT_KITCHEN_LEAK_PATTERN = re.compile(
    r"\b(?:"
    r"перекличк[а-я]*|"
    r"в\s+перекличк[а-я]*|"
    r"в\s+(?:местных\s+|городских\s+)?(?:чатах|чате|пабликах|каналах|группах)|"
    r"участник[а-я]*\s+чата|"
    r"в\s+комментариях|"
    r"в\s+(?:телеграм|telegram)-канал[а-я]*|"
    r"фигн[яеиюей]|"
    r"хрен[яеиюь]|"
    r"херн[яеиюей]|"
    r"хренов[а-я]*|"
    r"нафиг|"
    r"пофиг"
    r")\b",
    re.IGNORECASE,
)

_HEADING_EDITORIAL_FILLER = {
    "город",
    "города",
    "городской",
    "городская",
    "городские",
    "жизнь",
    "жизни",
    "обстановка",
    "обстановке",
    "обстановки",
    "хроника",
    "хроники",
    "хронике",
    "ситуация",
    "ситуации",
    "ситуацию",
    "события",
    "событий",
    "событиях",
    "район",
    "района",
    "районы",
    "районах",
    "улица",
    "улицы",
    "улицах",
    "улице",
    "день",
    "дня",
    "днем",
    "днях",
    "ночь",
    "ночи",
    "ночью",
    "утро",
    "утром",
    "вечер",
    "вечером",
    "вечерний",
    "время",
    "времени",
    "неделя",
    "недели",
    "мелочи",
    "мелочах",
    "сервис",
    "сервисы",
    "сервисах",
    "главное",
    "фокус",
    "фокусе",
    "вопрос",
    "вопросы",
    "вопросах",
    "проблема",
    "проблемы",
    "проблемах",
    "решение",
    "решения",
    "решениях",
    "картина",
    "картине",
    "новости",
    "новостей",
    "детали",
    "деталях",
    "сфера",
    "сферы",
    "перспективы",
    "последствия",
    "опыт",
    "итоги",
    "итог",
    "итогах",
    "другие",
    "также",
}

_WEEKLY_EXPANSION_RE = re.compile(
    r"\b(?:хроник[а-я]*\s+недел[а-я]*|итог[а-я]*\s+недел[а-я]*|событи[а-я]*\s+недел[а-я]*|обзор[а-я]*\s+недел[а-я]*|за\s+недел[а-я]*)\b",
    re.IGNORECASE,
)

_MONTHLY_EXPANSION_RE = re.compile(
    r"\b(?:итог[а-я]*\s+месяц[а-я]*|событи[а-я]*\s+месяц[а-я]*|обзор[а-я]*\s+месяц[а-я]*|за\s+месяц[а-я]*)\b",
    re.IGNORECASE,
)

_EXPANSION_RE = re.compile(
    r"\b(?:хроник[а-я]*\s+недел[а-я]*|итог[а-я]*\s+недел[а-я]*|событи[а-я]*\s+недел[а-я]*|обзор[а-я]*\s+недел[а-я]*|за\s+недел[а-я]*|итог[а-я]*\s+месяц[а-я]*|событи[а-я]*\s+месяц[а-я]*|обзор[а-я]*\s+месяц[а-я]*|за\s+месяц[а-я]*)\b",
    re.IGNORECASE,
)


_CONTINUATION_RE = re.compile(
    r"\b(?:продолжа[а-я]+|сохраня[а-я]+|оста[её]т[а-я]*|длительн[а-я]*|на\s+фоне|по-прежнему|ранее|с начала|до этого|прежде)\b",
    re.IGNORECASE,
)

_FUTURE_MARKER_RE = re.compile(
    r"\b(?:будет|будут|запланирован[а-я]*|предстоит|ожидает[а-я]*|намечен[а-я]*|планирует[а-я]*)\b|\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|\b\d{1,2}\.\d{2}\b",
    re.IGNORECASE,
)

_CURRENT_STATE_OUTAGE_RE = re.compile(
    r"\b(?:отключен[оаыи]|отключен|не\s+работа[а-я]+|отсутству[а-я]+|прекращен[оаыи]|прекращен|обесточен[оаыи]|обесточен)\b",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

_PROXIMITY_NUMERAL = (
    r"(?:\d+(?:[-–]\d+)?|один|одна|одно|одного|одной|одну|одном|одним|одних|одному|"
    r"два|две|дві|двух|двом|двум|двома|двумя|три|трех|трёх|трьох|трем|трём|"
    r"трьом|тремя|трьома|четыре|четырех|четырёх|четырем|четырём|четырьмя|"
    r"чотири|чотирьох|чотирьом|чотирма|пять|пяти|шість|шести|"
    r"семь|семи|восемь|восьми|девять|девяти|десять|десяти|"
    r"несколько|нескольких|кілька|кількох|декілька|декількох|пару|паре|пары|парой)"
)
_PROXIMITY_RELATION_RE = re.compile(
    rf"\b(?:"
    rf"через\s+(?:дорог\w*|вулиц\w*|улиц\w*|квартал\w*|{_PROXIMITY_NUMERAL}\s+квартал\w*)|"
    rf"(?:в|у)\s+(?:{_PROXIMITY_NUMERAL}\s+квартал\w*)|"
    rf"за\s+(?:{_PROXIMITY_NUMERAL}\s+квартал\w*)|"
    rf"на\s+(?:расстоянии|відстані)\s+(?:{_PROXIMITY_NUMERAL}\s+квартал\w*)|"
    r"соседств\w*|соседнич\w*|сусід\w*|гранич\w*|межу\w*|примык\w*|"
    r"приляга\w*|соприкаса\w*|стыку\w*|смежн\w*|суміжн\w*|"
    r"рядом|поруч|поряд|поблизости|поблизу|вблизи|навпроти|напротив|"
    r"в\s+шаге\s+от|по\s+соседству|за\s+(?:углом|рогом)|неподалек\w*|"
    r"близк\w*|близьк\w*|недалек\w*|біля"
    r")\b",
    re.IGNORECASE,
)
_PROXIMITY_CONTRADICTION_RE = re.compile(
    r"(?:"
    r"\bне\s+(?:(?:явля\w*|наход\w*|располож\w*|счит\w*)\s+)?"
    r"(?:сосед\w*|сусід\w*|гранич\w*|межу\w*|примык\w*|приляга\w*|"
    r"соприкаса\w*|стыку\w*|смежн\w*|суміжн\w*|рядом|поруч|поряд|"
    r"поблизости|поблизу|вблизи|напротив|навпроти|близк\w*|близьк\w*|"
    r"недалек\w*|неподалек\w*|біля|соседств\w*|соседнич\w*)\b|"
    r"\bне\s+(?:наход\w*|знаход\w*|располож\w*|розташ\w*)\s+"
    r"(?:рядом|поруч|поряд|поблизости|поблизу|вблизи|близк\w*|близьк\w*)\b|"
    r"\b(?:сосед\w*|сусід\w*|гранич\w*|межу\w*|примык\w*|приляга\w*|"
    r"соприкаса\w*|стыку\w*|смежн\w*|суміжн\w*|рядом|поруч|поряд|"
    r"поблизости|поблизу|вблизи|близк\w*|близьк\w*|соседств\w*|соседнич\w*)\b"
    r"[^.!?;]{0,40}\bне\s+(?:наход\w*|располож\w*|явля\w*|счит\w*)\b|"
    r"\bдалек\w*\s+(?:друг\s+от\s+друг\w*|от)\b|"
    r"\b(?:удален\w*|отдален\w*)\s+(?:друг\s+от\s+друг\w*|от)\b|"
    r"\bразделен\w*\s+(?:(?:значительн\w*|больш\w*)\s+)?"
    r"(?:расстояни\w*|\d+\s*(?:км\.?|километр\w*))\b|"
    r"\b(?:значительн\w*|больш\w*)\s+расстояни\w*\b"
    r")",
    re.IGNORECASE,
)
_PROXIMITY_NEGATED_SPECIFIC_RE = re.compile(
    rf"\bне\s+(?:"
    rf"через\s+(?:дорог\w*|вулиц\w*|улиц\w*|{_PROXIMITY_NUMERAL}\s+квартал\w*)|"
    rf"(?:в|у|за)\s+{_PROXIMITY_NUMERAL}\s+квартал\w*|"
    rf"на\s+(?:расстоянии|відстані)\s+{_PROXIMITY_NUMERAL}\s+квартал\w*|"
    r"за\s+(?:углом|рогом)|в\s+шаге\s+от"
    r")\b",
    re.IGNORECASE,
)
_PROXIMITY_UNCERTAINTY_RE = re.compile(
    r"\b(?:вряд\s+ли|едва\s+ли|возможн\w*|похоже|кажет\w*|вероятн\w*|"
    r"предположительн\w*|сомнительн\w*|не\s+уверен\w*|не\s+ясн\w*|"
    r"скорее\s+всего|может\s+быть|вроде(?:\s+бы)?|как\s+будто|"
    r"по[-\s]видимому|не\s+исключено|не\s+факт|сомневаюсь|"
    r"можливо|ймовірн\w*|мабуть|здаєтьс\w*|не\s+впевнен\w*|"
    r"неясн\w*|схоже|нібито|напевн\w*)\b",
    re.IGNORECASE,
)

_PROXIMITY_ADJACENCY_CUE_RE = re.compile(
    r"\b(?:сосед\w*|сусід\w*|соседств\w*|соседнич\w*|гранич\w*|межу\w*|"
    r"примык\w*|приляга\w*|соприкаса\w*|стыку\w*|смежн\w*|суміжн\w*)\b",
    re.IGNORECASE,
)
_PROXIMITY_DISTANCE_CUE_RE = re.compile(r"\b(?:квартал\w*|расстояни\w*)\b", re.IGNORECASE)
_PROXIMITY_PLACE_DESCRIPTOR = (
    r"(?:район\w*|мікрорайон\w*|микрорайон\w*|округ\w*|улиц\w*|вулиц\w*|ул\.?|"
    r"переул\w*|провул\w*|пер\.?|бульвар\w*|проспект\w*|шоссе|шосе|"
    r"посел\w*|селищ\w*|город\w*|міст\w*|село|деревн\w*|квартал\w*|"
    r"площад\w*|площ\w*|част\w*)"
)


@dataclass(frozen=True)
class _ResolvedPlaceMention:
    canonical_name: str
    object_type: str
    start: int
    end: int


@dataclass(frozen=True)
class _ExplicitPlaceRelation:
    places: frozenset[str]
    kind: Literal[
        "adjacency",
        "across_road",
        "around_corner",
        "distance",
        "near_step",
        "opposite",
        "proximity",
    ]
    distance_blocks: tuple[int, int] | None = None
    distance_qualifier: str | None = None


def _normalize_place_match_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", normalized).strip()


def _resolved_place_mentions(
    text: str,
    place_resolver: Any | None,
) -> tuple[_ResolvedPlaceMention, ...]:
    """Resolve high-confidence edition places with their local text spans."""
    if place_resolver is None or not text.strip():
        return ()
    try:
        normalized_text = _normalize_place_match_text(text)
        entities = place_resolver.resolve(text).entities
    except Exception:
        return ()

    accepted_types = {
        "street",
        "lane",
        "boulevard",
        "prospect",
        "highway",
        "district",
        "neighborhood",
        "settlement",
        "village",
        "city",
        "",
    }
    candidates: dict[tuple[int, int], set[tuple[str, str]]] = {}
    for entity in entities:
        if (
            entity.kind not in {"place", "area"}
            or entity.confidence != "high"
            or entity.object_type not in accepted_types
            or not entity.canonical_name
        ):
            continue
        matched_text = _normalize_place_match_text(entity.matched_text)
        if not matched_text:
            continue
        start = 0
        while (index := normalized_text.find(matched_text, start)) >= 0:
            end = index + len(matched_text)
            if (index == 0 or not normalized_text[index - 1].isalnum()) and (
                end == len(normalized_text) or not normalized_text[end].isalnum()
            ):
                candidates.setdefault((index, end), set()).add(
                    (
                        entity.canonical_name.casefold().replace("ё", "е"),
                        entity.object_type,
                    )
                )
            start = index + max(1, len(matched_text))

    mentions: list[_ResolvedPlaceMention] = []
    for (start, end), names_and_types in candidates.items():
        names = {name for name, _object_type in names_and_types}
        if len(names) != 1:
            continue
        canonical_name = next(iter(names))
        object_types = {object_type for _name, object_type in names_and_types}
        object_type = next((item for item in object_types if item != "city"), "city")
        mentions.append(_ResolvedPlaceMention(canonical_name, object_type, start, end))

    # Prefer the most specific recognized name when profile aliases overlap.
    selected: list[_ResolvedPlaceMention] = []
    for mention in sorted(mentions, key=lambda item: (item.start, -(item.end - item.start))):
        if any(mention.start < item.end and item.start < mention.end for item in selected):
            continue
        selected.append(mention)

    specific_places = {item.canonical_name for item in selected if item.object_type != "city"}
    if len(specific_places) >= 2:
        selected = [item for item in selected if item.object_type != "city"]
    return tuple(sorted(selected, key=lambda item: (item.start, item.end)))


def _proximity_relation_kind(
    relation_text: str,
) -> Literal[
    "adjacency",
    "across_road",
    "around_corner",
    "distance",
    "near_step",
    "opposite",
    "proximity",
]:
    if _PROXIMITY_DISTANCE_CUE_RE.search(relation_text):
        return "distance"
    if re.search(r"\bчерез\s+(?:дорог\w*|вулиц\w*|улиц\w*)\b", relation_text):
        return "across_road"
    if re.search(r"\b(?:напротив|навпроти)\b", relation_text):
        return "opposite"
    if re.search(r"\bза\s+(?:углом|рогом)\b", relation_text):
        return "around_corner"
    if re.search(r"\bв\s+шаге\s+от\b", relation_text):
        return "near_step"
    if _PROXIMITY_ADJACENCY_CUE_RE.search(relation_text):
        return "adjacency"
    return "proximity"


_DISTANCE_WORD_VALUES = {
    **dict.fromkeys(
        (
            "один",
            "одна",
            "одно",
            "одного",
            "одной",
            "одну",
            "одном",
            "одним",
            "одних",
            "одному",
        ),
        1,
    ),
    **dict.fromkeys(
        (
            "два",
            "две",
            "дві",
            "двух",
            "двум",
            "двом",
            "двумя",
            "двома",
            "пару",
            "паре",
            "пары",
            "парой",
        ),
        2,
    ),
    **dict.fromkeys(
        ("три", "трех", "трёх", "трьох", "трем", "трём", "трьом", "тремя", "трьома"),
        3,
    ),
    **dict.fromkeys(
        (
            "четыре",
            "четырех",
            "четырёх",
            "четырем",
            "четырём",
            "четырьмя",
            "чотири",
            "чотирьох",
            "чотирьом",
            "чотирма",
        ),
        4,
    ),
    "пять": 5,
    "пяти": 5,
    "шість": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
}
_DISTANCE_QUALIFIER_EQUIVALENTS = {
    "нескольких": "несколько",
    "кількох": "кілька",
    "декількох": "декілька",
}


def _proximity_distance_blocks(relation_text: str) -> tuple[int, int] | None:
    match = re.search(
        rf"(?P<quantity>\d+(?:[-–]\d+)?|{_PROXIMITY_NUMERAL})\s+квартал\w*",
        relation_text,
        re.IGNORECASE,
    )
    if match is None:
        if re.search(r"\b(?:через|за)\s+квартал\w*", relation_text):
            return (1, 1)
        return None
    quantity = match.group("quantity").casefold().replace("ё", "е")
    if quantity.isdigit():
        value = int(quantity)
        return (value, value)
    range_match = re.fullmatch(r"(\d+)[-–](\d+)", quantity)
    if range_match:
        low, high = (int(part) for part in range_match.groups())
        return (min(low, high), max(low, high))
    word_value = _DISTANCE_WORD_VALUES.get(quantity)
    return (word_value, word_value) if word_value is not None else None


def _proximity_distance_qualifier(relation_text: str) -> str | None:
    match = re.search(
        rf"(?P<quantity>\d+(?:[-–]\d+)?|{_PROXIMITY_NUMERAL})\s+квартал\w*",
        relation_text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    quantity = match.group("quantity").casefold().replace("ё", "е")
    if quantity.isdigit() or re.fullmatch(r"\d+[-–]\d+", quantity):
        return None
    if quantity in _DISTANCE_WORD_VALUES:
        return None
    return _DISTANCE_QUALIFIER_EQUIVALENTS.get(quantity, quantity)


def _direct_relation_right_gap_is_valid(gap: str, relation_text: str) -> bool:
    relation = _normalize_place_match_text(relation_text)
    if re.search(r"\b(?:гранич|межу|соседств|соседнич|соприкаса|стыку)", relation):
        preposition = r"(?:с|со|з|із|зі)"
    elif re.search(r"\b(?:примык|приляга)", relation):
        preposition = r"(?:к|ко|до)"
    elif _PROXIMITY_DISTANCE_CUE_RE.search(relation):
        preposition = r"(?:от|від|до)"
    elif re.search(r"\b(?:соседн|сусід|смежн|суміжн)", relation):
        preposition = r"(?:с|со|з|із|зі|к|ко|до)?"
    elif re.search(r"\b(?:рядом|по соседству|поруч|поряд)", relation):
        preposition = r"(?:с|со|з|із|зі)?"
    elif re.search(r"\b(?:недалек|неподалек|близк|близьк)", relation):
        preposition = r"(?:от|від|к|ко|до)?"
    elif re.search(
        r"\b(?:поблизости|поблизу|вблизи|навпроти|напротив|за углом|в шаге|біля)", relation
    ):
        preposition = r"(?:от|від|с|со|з|із|зі)?"
    else:
        preposition = r"(?:с|со|з|із|зі|к|ко|от|від|до)?"

    return bool(
        re.fullmatch(
            rf"\s*{preposition}\s*(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*[,.!?;:—–-]*\s*",
            gap,
            re.IGNORECASE,
        )
    )


def _direct_relation_left_gap_is_valid(gap: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:[,—–-]\s*)?(?:(?:котор\w*|який\w*|що)\s+)?"
            r"(?:(?:наход\w*|знаход\w*|располож\w*|розташ\w*|располага\w*|"
            r"леж\w*|сто\w*|проход\w*|ид\w*|явля\w*|быва\w*|находящ\w*|"
            r"расположенн\w*|розташован\w*)\s+)?",
            gap,
            re.IGNORECASE,
        )
    )


def _postposed_relation_pair(
    sentence: str,
    relation: re.Match[str],
    mentions: tuple[_ResolvedPlaceMention, ...],
) -> tuple[_ResolvedPlaceMention, _ResolvedPlaceMention] | None:
    preceding = [mention for mention in mentions if mention.end <= relation.start()]
    if len(preceding) < 2:
        return None
    first, second = preceding[-2:]
    list_connector = sentence[first.end : second.start]
    if not re.fullmatch(
        rf"\s*(?:,|и|та)\s*(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*",
        list_connector,
        re.IGNORECASE,
    ):
        return None

    relation_prefix = sentence[second.end : relation.start()]
    relation_text = relation.group(0)
    if not _direct_relation_left_gap_is_valid(relation_prefix):
        return None

    relation_tail = sentence[relation.end() :]
    if _PROXIMITY_ADJACENCY_CUE_RE.search(relation_text):
        tail_is_clear = bool(
            re.fullmatch(
                rf"\s*(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*[,.!?;:—–-]*\s*",
                relation_tail,
                re.IGNORECASE,
            )
            or re.fullmatch(
                r"\s+друг\s+(?:с|к|от)\s+друг\w*\s*[,.!?;:—–-]*\s*",
                relation_tail,
                re.IGNORECASE,
            )
        )
    elif _PROXIMITY_DISTANCE_CUE_RE.search(relation_text):
        tail_is_clear = bool(
            re.fullmatch(r"\s+друг\s+от\s+друг\w*\s*[,.!?;:—–-]*\s*", relation_tail, re.IGNORECASE)
        )
    else:
        tail_is_clear = bool(
            re.fullmatch(
                r"\s*(?:(?:друг\s+(?:с|к|от)\s+друг\w*|друг\s+друг\w*)\s*)?"
                r"[,.!?;:—–-]*\s*",
                relation_tail,
                re.IGNORECASE,
            )
        )
    return (first, second) if tail_is_clear else None


def _inverse_relation_pairs(
    sentence: str,
    relation: re.Match[str],
    mentions: tuple[_ResolvedPlaceMention, ...],
) -> tuple[tuple[_ResolvedPlaceMention, _ResolvedPlaceMention], ...]:
    """Handle "near A is B" forms when the relation precedes both places."""
    if any(mention.end <= relation.start() for mention in mentions):
        return ()
    following = [mention for mention in mentions if mention.start >= relation.end()]
    if len(following) < 2:
        return ()
    target, subject = following[:2]
    target_gap = sentence[relation.end() : target.start]
    subject_gap = sentence[target.end : subject.start]
    if not _direct_relation_right_gap_is_valid(target_gap, relation.group(0)):
        return ()
    subject_pattern = (
        rf"\s*(?:[,—–-]\s*)?(?:наход\w*|знаход\w*|располож\w*|розташ\w*|"
        rf"располага\w*|леж\w*|сто\w*|явля\w*|быва\w*)\s+"
        rf"(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*"
    )
    if not re.fullmatch(subject_pattern, subject_gap, re.IGNORECASE):
        return ()
    pairs = [(target, subject)]
    previous = subject
    for additional in following[2:]:
        list_connector = sentence[previous.end : additional.start]
        if not re.fullmatch(
            rf"\s*(?:,|и|та)\s*(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*",
            list_connector,
            re.IGNORECASE,
        ):
            break
        pairs.append((target, additional))
        previous = additional
    return tuple(pairs)


def _explicit_place_relations(
    text: str,
    place_resolver: Any | None,
) -> tuple[_ExplicitPlaceRelation, ...]:
    """Extract only profile-place pairs directly linked by a proximity predicate."""
    relations: list[_ExplicitPlaceRelation] = []
    for raw_sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        sentence = _normalize_place_match_text(raw_sentence)
        mentions = _resolved_place_mentions(sentence, place_resolver)
        if len({item.canonical_name for item in mentions}) < 2:
            continue
        for cue in _PROXIMITY_RELATION_RE.finditer(sentence):
            candidates: list[tuple[_ResolvedPlaceMention, _ResolvedPlaceMention]] = []
            left_mentions = [item for item in mentions if item.end <= cue.start()]
            right_mentions = [item for item in mentions if item.start >= cue.end()]
            if left_mentions and right_mentions:
                left, right = left_mentions[-1], right_mentions[0]
                left_gap = sentence[left.end : cue.start()]
                right_gap = sentence[cue.end() : right.start]
                if _direct_relation_left_gap_is_valid(
                    left_gap
                ) and _direct_relation_right_gap_is_valid(right_gap, cue.group(0)):
                    candidates.append((left, right))
                    previous = right
                    for additional in right_mentions[1:]:
                        list_connector = sentence[previous.end : additional.start]
                        if not re.fullmatch(
                            rf"\s*(?:,|и|та)\s*(?:{_PROXIMITY_PLACE_DESCRIPTOR}\s*)*",
                            list_connector,
                            re.IGNORECASE,
                        ):
                            break
                        candidates.append((left, additional))
                        previous = additional

            postposed_pair = _postposed_relation_pair(sentence, cue, mentions)
            if postposed_pair is not None:
                candidates.append(postposed_pair)
            candidates.extend(_inverse_relation_pairs(sentence, cue, mentions))

            for first, second in candidates:
                places = frozenset((first.canonical_name, second.canonical_name))
                if len(places) != 2:
                    continue
                start = max(0, min(first.start, second.start) - 40)
                end = min(len(sentence), max(first.end, second.end) + 24)
                relation_context = sentence[start:end]
                if (
                    _PROXIMITY_CONTRADICTION_RE.search(relation_context)
                    or _PROXIMITY_NEGATED_SPECIFIC_RE.search(relation_context)
                    or _PROXIMITY_UNCERTAINTY_RE.search(relation_context)
                ):
                    continue
                relations.append(
                    _ExplicitPlaceRelation(
                        places=places,
                        kind=_proximity_relation_kind(cue.group(0)),
                        distance_blocks=_proximity_distance_blocks(cue.group(0)),
                        distance_qualifier=_proximity_distance_qualifier(cue.group(0)),
                    )
                )
    return tuple(dict.fromkeys(relations))


def _source_explicitly_relates_places(
    source_text: str,
    expected_relation: _ExplicitPlaceRelation,
    place_resolver: Any | None,
) -> bool:
    """Require a positive source relation connecting the exact same place pair."""
    for source_relation in _explicit_place_relations(source_text, place_resolver):
        if source_relation.places != expected_relation.places:
            continue
        # A general claim of nearness does not establish that two places are
        # opposite, across the street, around the corner, or exactly distanced.
        if expected_relation.kind == "proximity" and source_relation.kind == "distance":
            if source_relation.distance_blocks is None or source_relation.distance_blocks[1] > 2:
                continue
        elif expected_relation.kind != "proximity" and (
            source_relation.kind != expected_relation.kind
            or (
                expected_relation.kind == "distance"
                and (
                    source_relation.distance_blocks != expected_relation.distance_blocks
                    or (
                        expected_relation.distance_blocks is None
                        and source_relation.distance_qualifier
                        != expected_relation.distance_qualifier
                    )
                )
            )
        ):
            continue
        return True
    return False


_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@dataclass(frozen=True)
class ArticleValidationIssue:
    """A specific validation violation within a draft unit."""

    code: str
    unit_id: str
    message: str
    support_ids: tuple[str, ...] = ()
    unsupported_claims: tuple[ConcreteClaim, ...] = ()
    severity: Literal["error", "warning"] = "error"
    blocking: bool = True
    claim_text: str | None = None


@dataclass(frozen=True)
class ArticleValidationResult:
    """Outcome of deterministic article draft validation."""

    is_valid: bool
    word_count: int
    section_count: int
    issues: tuple[ArticleValidationIssue, ...] = ()
    unknown_evidence_ids: tuple[str, ...] = ()

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(f"{iss.code}:{iss.unit_id}" for iss in self.issues if iss.blocking)

    @property
    def all_violations(self) -> tuple[str, ...]:
        return tuple(f"{iss.code}:{iss.unit_id}" for iss in self.issues)

    @property
    def unsupported_claims(self) -> tuple[ConcreteClaim, ...]:
        claims: list[ConcreteClaim] = []
        for iss in self.issues:
            claims.extend(iss.unsupported_claims)
        return tuple(claims)


def validate_article_draft(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig | None = None,
    *,
    length_profile: ArticleLengthProfile | None = None,
    material_projection: ArticleMaterialProjection | None = None,
) -> ArticleValidationResult:
    """Validate structured article draft against support bounds, factual claims, and length constraints."""
    if config is None:
        config = PublicationEditorialConfig()

    from src.publication.article_quote_allowlist import build_article_quote_allowlist

    original_support_by_id = context.support_by_id
    place_resolver = resolve_article_place_resolver(context)

    quote_allowlist = build_article_quote_allowlist(
        context,
        excluded_support_ids=(
            {
                support_id
                for support_id, action in material_projection.actions_by_support_id.items()
                if action == "SUPPRESS_PROMOTION_ONLY"
            }
            if material_projection is not None
            else set()
        ),
        excluded_story_ids=(
            material_projection.suppressed_story_ids if material_projection is not None else ()
        ),
        candidate_text_by_support_id=(
            material_projection.text_by_support_id if material_projection is not None else None
        ),
    )
    if material_projection is not None:
        context = materialize_article_validation_context(context, material_projection)

    issues: list[ArticleValidationIssue] = []
    unknown_evidence_ids: list[str] = []

    # 1. Structural constraints: Title and Lead presence
    if not draft.title or not draft.title.strip():
        issues.append(
            ArticleValidationIssue(
                code="EMPTY_TITLE",
                unit_id="TITLE",
                message="Draft title cannot be empty",
            )
        )
    if not draft.lead or not draft.lead.strip():
        issues.append(
            ArticleValidationIssue(
                code="EMPTY_LEAD",
                unit_id="LEAD",
                message="Draft lead cannot be empty",
            )
        )

    # 2. Section count and word count
    if length_profile is None:
        min_words = config.article_min_words
        max_words = config.article_max_words
        min_sections = config.article_min_sections
        max_sections = config.article_max_sections
    else:
        min_words = length_profile.hard_min_words
        max_words = length_profile.hard_max_words
        min_sections = 1
        max_sections = config.article_max_sections

    section_count = len(draft.sections)
    if section_count < min_sections:
        issues.append(
            ArticleValidationIssue(
                code="SECTION_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {section_count} sections, minimum required is {min_sections}",
            )
        )
    elif section_count > max_sections:
        issues.append(
            ArticleValidationIssue(
                code="SECTION_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {section_count} sections, maximum allowed is {max_sections}",
            )
        )

    word_count = draft.word_count
    if word_count < min_words:
        issues.append(
            ArticleValidationIssue(
                code="WORD_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {word_count} words, minimum required is {min_words}",
            )
        )
    elif word_count > max_words:
        issues.append(
            ArticleValidationIssue(
                code="WORD_COUNT_OUT_OF_BOUNDS",
                unit_id="DRAFT",
                message=f"Draft has {word_count} words, maximum allowed is {max_words}",
            )
        )

    # 3. Reporting window expansion check
    lookback_hours = 24
    if context.publication_window is not None:
        delta = context.publication_window.snapshot_at - context.publication_window.lookback_start
        lookback_hours = int(delta.total_seconds() // 3600)

    disallowed_patterns: list[re.Pattern[str]] = []
    if lookback_hours <= 48:
        disallowed_patterns.extend([_WEEKLY_EXPANSION_RE, _MONTHLY_EXPANSION_RE])
    elif lookback_hours < 336:
        disallowed_patterns.append(_MONTHLY_EXPANSION_RE)

    for pattern in disallowed_patterns:
        if pattern.search(draft.title):
            issues.append(
                ArticleValidationIssue(
                    code="REPORTING_WINDOW_EXPANSION",
                    unit_id="TITLE",
                    message=f"Draft title expands reporting window beyond configured lookback: '{draft.title}'",
                )
            )
            break
    for pattern in disallowed_patterns:
        if pattern.search(draft.lead):
            issues.append(
                ArticleValidationIssue(
                    code="REPORTING_WINDOW_EXPANSION",
                    unit_id="LEAD",
                    message=f"Draft lead expands reporting window beyond configured lookback: '{draft.lead}'",
                )
            )
            break

    # 4. Unit-by-unit validation
    # Construct sequence of units: (unit_id, unit_type, text, cited_support_ids, claim_atoms)
    units: list[tuple[str, str, str, tuple[str, ...], tuple[ArticleClaimAtom, ...]]] = []
    units.append(("TITLE", "title", draft.title, draft.title_support_ids, draft.title_claims))
    units.append(("LEAD", "lead", draft.lead, draft.lead_support_ids, draft.lead_claims))

    p_idx = 1
    all_cited_ids = set(draft.title_support_ids) | set(draft.lead_support_ids)
    for sec in draft.sections:
        all_cited_ids.update(sec.heading_support_ids)
        for para in sec.paragraphs:
            all_cited_ids.update(para.cited_support_ids)

    # Support map from context
    support_map = context.support_by_id

    all_known_draft_supports: list[ArticleSupport] = [
        support_map[sid] for sid in all_cited_ids if sid in support_map
    ]
    all_draft_support_texts: list[str] = [s.text for s in all_known_draft_supports if s.text] + [
        s.source_text for s in all_known_draft_supports if s.source_text
    ]
    for s in all_known_draft_supports:
        if (obs := getattr(s, "observed_at", None)) is not None:
            all_draft_support_texts.append(obs.strftime("%H:%M"))
            all_draft_support_texts.append(obs.strftime("%-H:%M"))
            all_draft_support_texts.append(obs.strftime("%d.%m"))
            all_draft_support_texts.append(f"{obs.day} {_MONTHS_RU[obs.month - 1]}")

    all_draft_concepts: set[str] = set()
    for st in all_draft_support_texts:
        all_draft_concepts.update(canonical_semantic_concepts(st))

    from src.publication.article_coverage import _story_id_from_support_id

    coverage_plan = getattr(context, "coverage_plan", None)
    story_topics: dict[str, str] = {}
    if coverage_plan and hasattr(coverage_plan, "stories"):
        for sc in coverage_plan.stories:
            if getattr(sc, "story_id", None) and getattr(sc, "topic", None):
                story_topics[sc.story_id] = sc.topic

    all_edition_support_texts = [s.text for s in context.supports if s.text] + [
        s.source_text for s in context.supports if s.source_text
    ]
    for s in context.supports:
        s_story_id = getattr(s, "story_id", "") or _story_id_from_support_id(
            getattr(s, "support_id", "")
        )
        if s_story_id in story_topics:
            all_edition_support_texts.append(story_topics[s_story_id])
    for s in context.supports:
        if (obs := getattr(s, "observed_at", None)) is not None:
            all_edition_support_texts.append(obs.strftime("%H:%M"))
            all_edition_support_texts.append(obs.strftime("%-H:%M"))
            all_edition_support_texts.append(obs.strftime("%d.%m"))
            all_edition_support_texts.append(f"{obs.day} {_MONTHS_RU[obs.month - 1]}")
    all_edition_tokens: set[str] = {
        tok.lower()
        for st in all_edition_support_texts
        for tok in _TOKEN_RE.findall(st)
        if len(tok) >= 2
    }
    window_terms: set[str] = set()
    pub_window = getattr(context, "publication_window", None)
    if pub_window is not None:
        import datetime as _dt

        w_start = pub_window.lookback_start
        w_end = pub_window.snapshot_at
        cur_d = w_start.date()
        while cur_d <= w_end.date():
            window_terms.add(cur_d.strftime("%d.%m"))
            window_terms.add(str(cur_d.day))
            m_name = _MONTHS_RU[cur_d.month - 1]
            window_terms.add(m_name)
            window_terms.add(f"{cur_d.day} {m_name}")
            cur_d += _dt.timedelta(days=1)

    allowed_edition_terms = tuple(
        set(context.edition_anchor_terms) | all_edition_tokens | window_terms
    )

    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        units.append((h_id, "heading", sec.heading, sec.heading_support_ids, sec.heading_claims))
        for para in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            units.append((p_id, "paragraph", para.text, para.cited_support_ids, para.claims))
            p_idx += 1

    # Support map from context
    support_map = context.support_by_id

    for unit_id, unit_type, unit_text, cited_ids, claim_atoms in units:
        if not unit_text.strip():
            continue

        # Check for internal handle leaks in raw text
        if _INTERNAL_HANDLE_PATTERN.search(unit_text):
            issues.append(
                ArticleValidationIssue(
                    code="INTERNAL_HANDLE_LEAK",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} contains internal evidence handle",
                )
            )

        # Check for leaked meta omission phrases in raw text
        if _META_OMISSION_PATTERN.search(unit_text):
            issues.append(
                ArticleValidationIssue(
                    code="LEAKED_META_OMISSION",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} contains leaked meta-omission commentary",
                )
            )

        # Check for leaked chat kitchen / source references
        if _CHAT_KITCHEN_LEAK_PATTERN.search(unit_text):
            issues.append(
                ArticleValidationIssue(
                    code="CHAT_KITCHEN_LEAK",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} contains leaked chat-room kitchen or source reference",
                )
            )

        # Check for repetitive sentence loops within a single paragraph / unit
        if unit_type in ("lead", "paragraph"):
            sentences = _split_sentences_safe(unit_text)
            if len(sentences) >= 2:
                seen_unit_norms: list[str] = []
                loop_detected = False
                for sent in sentences:
                    norm = _normalize_for_dedup(sent)
                    if len(norm) < 15:
                        continue
                    if norm in seen_unit_norms:
                        loop_detected = True
                        break
                    toks = {_stem(w) for w in _TOKEN_RE.findall(norm.lower()) if len(w) >= 3}
                    if len(toks) >= 5:
                        s_nums = set(re.findall(r"\b\d+\b", norm))
                        for prev_norm in seen_unit_norms:
                            prev_toks = {
                                _stem(w)
                                for w in _TOKEN_RE.findall(prev_norm.lower())
                                if len(w) >= 3
                            }
                            if len(prev_toks) >= 5:
                                prev_s_nums = set(re.findall(r"\b\d+\b", prev_norm))
                                if s_nums and prev_s_nums and s_nums != prev_s_nums:
                                    continue
                                shared = toks & prev_toks
                                if len(shared) / max(len(toks), len(prev_toks)) >= 0.90:
                                    loop_detected = True
                                    break
                        if loop_detected:
                            break
                    seen_unit_norms.append(norm)

                if loop_detected:
                    issues.append(
                        ArticleValidationIssue(
                            code="REPEATED_CONTENT_LOOP",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} contains repeated identical or near-duplicate sentences within the paragraph",
                            severity="error",
                            blocking=True,
                        )
                    )

        # Check missing support IDs
        if not cited_ids:
            blocking = unit_type not in ("heading", "title")
            missing_support_severity: Literal["error", "warning"] = (
                "error" if blocking else "warning"
            )
            issues.append(
                ArticleValidationIssue(
                    code=f"MISSING_SUPPORT:{unit_type}",
                    unit_id=unit_id,
                    message=f"Unit {unit_id} is missing support citation",
                    severity=missing_support_severity,
                    blocking=blocking,
                )
            )
            continue

        # Check claim atoms existence
        if not claim_atoms:
            if unit_type != "heading":
                issues.append(
                    ArticleValidationIssue(
                        code="MISSING_CLAIM_ATOMS",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) must contain at least one claim atom",
                        support_ids=cited_ids,
                    )
                )
        else:
            unit_sids = set(cited_ids)
            claim_sids = {sid for c in claim_atoms for sid in c.cited_support_ids}
            for claim in claim_atoms:
                if not claim.cited_support_ids:
                    issues.append(
                        ArticleValidationIssue(
                            code="MISSING_CLAIM_SUPPORT",
                            unit_id=unit_id,
                            message=(
                                f"Unit {unit_id} claim '{claim.text}' has no claim-specific "
                                "support citation"
                            ),
                            support_ids=cited_ids,
                            severity="error",
                            blocking=True,
                            claim_text=claim.text,
                        )
                    )
            if unit_sids != claim_sids:
                all_known = unit_sids.issubset(support_map.keys()) and claim_sids.issubset(
                    support_map.keys()
                )
                blocking = not all_known
                severity: Literal["error", "warning"] = "error" if blocking else "warning"
                issues.append(
                    ArticleValidationIssue(
                        code="CLAIM_SUPPORT_MISMATCH",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} support IDs {sorted(unit_sids)} do not match claim atom support IDs {sorted(claim_sids)}",
                        support_ids=cited_ids,
                        severity=severity,
                        blocking=blocking,
                    )
                )

        valid_supports: list[ArticleSupport] = []
        has_unknown = False
        for sid in cited_ids:
            if sid not in support_map:
                unknown_evidence_ids.append(sid)
                has_unknown = True
                issues.append(
                    ArticleValidationIssue(
                        code="UNKNOWN_SUPPORT_ID",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} cites unknown support ID '{sid}'",
                        support_ids=(sid,),
                    )
                )
            else:
                valid_supports.append(support_map[sid])

        # Also verify claim atoms' support IDs
        for claim in claim_atoms:
            for csid in claim.cited_support_ids:
                if csid not in support_map:
                    unknown_evidence_ids.append(csid)
                    has_unknown = True
                    issues.append(
                        ArticleValidationIssue(
                            code="UNKNOWN_CLAIM_SUPPORT_ID",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} claim '{claim.text}' cites unknown support ID '{csid}'",
                            support_ids=(csid,),
                        )
                    )

        if has_unknown or not valid_supports:
            continue

        proximity_assertions = [
            (claim.text, claim.cited_support_ids) for claim in claim_atoms if claim.text.strip()
        ]
        # Claim Atoms may omit an editorial connective or contain only one of
        # several reader-facing relations. Always validate each full sentence
        # against the unit's own citations; relation parsing binds its cue to a
        # directly linked profile-place pair within that sentence.
        for sentence in _split_sentences_safe(unit_text):
            if _PROXIMITY_RELATION_RE.search(sentence):
                proximity_assertions.append((sentence, cited_ids))
        reported_proximity_findings: set[tuple[frozenset[str], frozenset[str]]] = set()
        for assertion_text, assertion_support_ids in proximity_assertions:
            if not _PROXIMITY_RELATION_RE.search(assertion_text):
                continue
            expected_relations = _explicit_place_relations(assertion_text, place_resolver)
            for expected_relation in expected_relations:
                supported_relation = False
                for support_id in assertion_support_ids:
                    support = original_support_by_id.get(support_id)
                    if (
                        support is None
                        or support.publication_use != "PUBLISH"
                        or support.evidence_kind == "resident_question"
                        or (
                            material_projection is not None
                            and (
                                support.story_id in material_projection.suppressed_story_ids
                                or material_projection.actions_by_support_id.get(support_id)
                                == "SUPPRESS_PROMOTION_ONLY"
                            )
                        )
                    ):
                        continue
                    exact_source_text = (support.source_text or support.text).strip()
                    if _source_explicitly_relates_places(
                        exact_source_text,
                        expected_relation,
                        place_resolver,
                    ):
                        supported_relation = True
                        break
                if not supported_relation:
                    finding_key = (
                        expected_relation.places,
                        frozenset(assertion_support_ids),
                    )
                    if finding_key in reported_proximity_findings:
                        continue
                    reported_proximity_findings.add(finding_key)
                    issues.append(
                        ArticleValidationIssue(
                            code="UNSUPPORTED_PROXIMITY_RELATION",
                            unit_id=unit_id,
                            message=(
                                f"Unit {unit_id} asserts proximity between named places without a cited "
                                "source explicitly relating those same places"
                            ),
                            support_ids=tuple(assertion_support_ids),
                            severity="error",
                            blocking=True,
                            claim_text=assertion_text,
                        )
                    )

        # Check publication policy and temporal roles for title and lead
        if unit_type in ("title", "lead"):
            has_publish_current = any(
                s.publication_use == "PUBLISH" and s.temporal_role == "CURRENT_WINDOW"
                for s in valid_supports
            )
            if not has_publish_current:
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) requires at least one PUBLISH support with CURRENT_WINDOW temporal role",
                        support_ids=cited_ids,
                    )
                )
        elif unit_type == "heading":
            if not any(s.publication_use == "PUBLISH" for s in valid_supports):
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Unit {unit_id} ({unit_type}) requires at least one PUBLISH support",
                        support_ids=cited_ids,
                    )
                )

        # Temporal framing checks
        # 1. Historical context framing
        has_hist = any(s.temporal_role == "HISTORICAL_CONTEXT" for s in valid_supports)
        if has_hist:
            if unit_type in ("title", "lead"):
                has_curr = any(s.temporal_role == "CURRENT_WINDOW" for s in valid_supports)
                has_continuation = bool(_CONTINUATION_RE.search(unit_text))
                if not (has_curr and has_continuation):
                    issues.append(
                        ArticleValidationIssue(
                            code="HISTORICAL_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} cites historical context without current window evidence and continuation framing",
                            support_ids=cited_ids,
                        )
                    )

        # 2. Future scheduled framing
        all_future = bool(valid_supports) and all(
            s.temporal_role == "FUTURE_SCHEDULED" for s in valid_supports
        )
        if all_future:
            has_active_outage_desc = bool(_CURRENT_STATE_OUTAGE_RE.search(unit_text))
            if unit_type in ("lead", "paragraph"):
                has_future_marker = bool(_FUTURE_MARKER_RE.search(unit_text))
                if not has_future_marker or has_active_outage_desc:
                    issues.append(
                        ArticleValidationIssue(
                            code="FUTURE_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Unit {unit_id} ({unit_type}) with future scheduled supports lacks explicit future marker or describes outage as current",
                            support_ids=cited_ids,
                        )
                    )
            elif unit_type == "heading":
                if has_active_outage_desc:
                    issues.append(
                        ArticleValidationIssue(
                            code="FUTURE_CONTEXT_UNFRAMED",
                            unit_id=unit_id,
                            message=f"Heading {unit_id} with future scheduled supports describes outage as currently active",
                            support_ids=cited_ids,
                        )
                    )

        # Check claim atoms against their cited supports
        for claim in claim_atoms:
            c_supports = (
                all_known_draft_supports
                if unit_type in ("title", "lead")
                else [support_map[sid] for sid in claim.cited_support_ids if sid in support_map]
            )
            if c_supports:
                if all(
                    s.evidence_kind == "resident_question" or s.publication_use == "CONTEXT"
                    for s in c_supports
                ):
                    question_markers = (
                        "вопрос",
                        "спрашива",
                        "интересу",
                        "уточня",
                        "выясня",
                        "неизвестно",
                        "неясно",
                        "?",
                    )
                    claim_lower = claim.text.lower()
                    unit_lower = unit_text.lower()
                    if not any(m in claim_lower or m in unit_lower for m in question_markers):
                        issues.append(
                            ArticleValidationIssue(
                                code="QUESTION_CONTEXT_OVERCLAIM",
                                unit_id=unit_id,
                                message=(
                                    f"Unit {unit_id} claim atom '{claim.text}' is supported only by resident "
                                    f"questions/context but asserts a factual proposition without inquiry framing"
                                ),
                                support_ids=claim.cited_support_ids,
                                severity="error",
                                blocking=True,
                                claim_text=claim.text,
                            )
                        )

                allowed_context_terms = allowed_edition_terms

                assessment = assess_claim_against_supports(
                    claim.text,
                    c_supports,
                    min_content_coverage=config.article_claim_min_content_coverage,
                    allowed_context_terms=allowed_context_terms,
                    all_known_draft_supports=all_edition_support_texts,
                    direct_quote_allowlist=quote_allowlist,
                )

                if not assessment.supported:
                    if any(
                        c.kind == "direct_quote" for c in assessment.unsupported_concrete_claims
                    ):
                        issues.append(
                            ArticleValidationIssue(
                                code="UNSUPPORTED_DIRECT_QUOTE",
                                unit_id=unit_id,
                                message=f"Unit {unit_id} claim atom '{claim.text}' contains direct quote not matching exact primary source text",
                                support_ids=claim.cited_support_ids,
                                unsupported_claims=assessment.unsupported_concrete_claims,
                                claim_text=claim.text,
                            )
                        )
                    elif assessment.blocking_proper_names:
                        issues.append(
                            ArticleValidationIssue(
                                code="UNSUPPORTED_PROPER_NAME",
                                unit_id=unit_id,
                                message=f"Unit {unit_id} claim atom '{claim.text}' contains unsupported proper names {assessment.blocking_proper_names}",
                                support_ids=claim.cited_support_ids,
                                unsupported_claims=assessment.unsupported_concrete_claims,
                                claim_text=claim.text,
                            )
                        )
                    elif assessment.blocking_critical_terms:
                        novel_critical = [
                            c
                            for c in assessment.blocking_critical_terms
                            if c not in all_draft_concepts
                        ]
                        if novel_critical:
                            issues.append(
                                ArticleValidationIssue(
                                    code="UNSUPPORTED_CRITICAL_TERM",
                                    unit_id=unit_id,
                                    message=f"Unit {unit_id} claim atom '{claim.text}' contains unsupported critical concepts {tuple(novel_critical)}",
                                    support_ids=claim.cited_support_ids,
                                    unsupported_claims=assessment.unsupported_concrete_claims,
                                    claim_text=claim.text,
                                )
                            )
                    elif assessment.unsupported_concrete_claims:
                        details_str = ", ".join(
                            f"'{getattr(c, 'raw', str(c))}' ({getattr(c, 'kind', 'concrete')})"
                            for c in assessment.unsupported_concrete_claims
                        )
                        issues.append(
                            ArticleValidationIssue(
                                code="UNSUPPORTED_CONCRETE_CLAIM",
                                unit_id=unit_id,
                                message=f"Unit {unit_id} claim atom '{claim.text}' contains unsupported concrete details: {details_str}",
                                support_ids=claim.cited_support_ids,
                                unsupported_claims=assessment.unsupported_concrete_claims,
                                claim_text=claim.text,
                            )
                        )
                    elif (
                        assessment.causal_analysis is not None
                        and getattr(assessment.causal_analysis, "has_causal_relation", False)
                        and not getattr(assessment.causal_analysis, "supported", True)
                    ):
                        causal = assessment.causal_analysis
                        verb = getattr(causal, "causal_verb", "")
                        issues.append(
                            ArticleValidationIssue(
                                code="UNSUPPORTED_CAUSAL_RELATION",
                                unit_id=unit_id,
                                message=f"Unit {unit_id} claim atom '{claim.text}' asserts unsupported causal relation '{verb}'",
                                support_ids=claim.cited_support_ids,
                                claim_text=claim.text,
                            )
                        )
                    else:
                        is_blocking = bool(
                            assessment.unsupported_concrete_claims
                            or assessment.blocking_proper_names
                            or assessment.blocking_critical_terms
                            or (
                                assessment.blocking_semantic_terms
                                and unit_type not in ("title", "lead")
                            )
                        )
                        issues.append(
                            ArticleValidationIssue(
                                code="UNSUPPORTED_CLAIM_ATOM",
                                unit_id=unit_id,
                                message=f"Unit {unit_id} claim atom '{claim.text}' is not supported: missing stems {assessment.unsupported_content_stems}",
                                support_ids=claim.cited_support_ids,
                                unsupported_claims=assessment.unsupported_concrete_claims,
                                severity="error" if is_blocking else "warning",
                                blocking=is_blocking,
                                claim_text=claim.text,
                            )
                        )
                elif assessment.lexical_only_warning:
                    issues.append(
                        ArticleValidationIssue(
                            code="CLAIM_LEXICAL_DIVERGENCE",
                            unit_id=unit_id,
                            message=(
                                f"Unit {unit_id} claim atom '{claim.text}' has low lexical overlap with cited supports "
                                f"(coverage={assessment.content_coverage:.2f}, unmatched={assessment.unsupported_content_stems})"
                            ),
                            support_ids=claim.cited_support_ids,
                            severity="warning",
                            blocking=False,
                            claim_text=claim.text,
                        )
                    )

        support_texts = [t for s in valid_supports for t in (s.text, s.source_text) if t]
        for s in valid_supports:
            if (obs := getattr(s, "observed_at", None)) is not None:
                support_texts.append(obs.strftime("%H:%M"))
                support_texts.append(obs.strftime("%-H:%M"))
                support_texts.append(obs.strftime("%d.%m"))
                support_texts.append(f"{obs.day} {_MONTHS_RU[obs.month - 1]}")
        primary_source_texts = [s.source_text for s in valid_supports if s.source_text]
        unit_context_terms = allowed_edition_terms

        claims_support_texts = (
            all_draft_support_texts if unit_type in ("title", "lead") else support_texts
        )
        claims_primary_sources = (
            [s.source_text for s in all_known_draft_supports if s.source_text]
            if unit_type in ("title", "lead")
            else primary_source_texts
        )

        unsupported = find_unsupported_claims(
            unit_text,
            claims_support_texts,
            all_known_draft_supports=all_edition_support_texts,
            allowed_context_terms=unit_context_terms,
            direct_quote_source_texts=claims_primary_sources,
            direct_quote_allowlist=quote_allowlist,
        )
        if unsupported:
            if unit_type == "paragraph" and not any(
                s.publication_use == "PUBLISH" for s in valid_supports
            ):
                issues.append(
                    ArticleValidationIssue(
                        code="INVALID_SUPPORT_POLICY",
                        unit_id=unit_id,
                        message=f"Paragraph {unit_id} with concrete claims requires at least one PUBLISH support",
                        support_ids=cited_ids,
                    )
                )

            for claim_item in unsupported:
                if claim_item.kind == "direct_quote":
                    code = "UNSUPPORTED_DIRECT_QUOTE"
                elif claim_item.kind == "causal_relation":
                    code = "UNSUPPORTED_CAUSAL_RELATION"
                elif claim_item.kind == "mechanism_relation":
                    code = "UNSUPPORTED_MECHANISM"
                else:
                    code = "UNSUPPORTED_CONCRETE_CLAIM"

                issues.append(
                    ArticleValidationIssue(
                        code=code,
                        unit_id=unit_id,
                        message=f"Unit {unit_id} contains unsupported {claim_item.kind} claim '{claim_item.raw}'",
                        support_ids=cited_ids,
                        unsupported_claims=(claim_item,),
                    )
                )

        # Defense in depth: Check reader-facing unit text for unsupported proper names or critical concepts
        if unit_type != "heading" or claim_atoms:
            semantic_supports = (
                all_draft_support_texts if unit_type in ("title", "lead") else support_texts
            )
            unit_semantic = assess_semantic_support(
                unit_text,
                semantic_supports,
                allowed_context_terms=unit_context_terms,
            )
            if unit_semantic.blocking_proper_names:
                issues.append(
                    ArticleValidationIssue(
                        code="UNSUPPORTED_PROPER_NAME",
                        unit_id=unit_id,
                        message=(
                            f"Unit {unit_id} reader-facing text contains unsupported proper names "
                            f"{unit_semantic.blocking_proper_names}"
                        ),
                        support_ids=cited_ids,
                    )
                )
            if unit_semantic.blocking_critical_terms:
                novel_critical = [
                    c for c in unit_semantic.blocking_critical_terms if c not in all_draft_concepts
                ]
                if novel_critical:
                    issues.append(
                        ArticleValidationIssue(
                            code="UNSUPPORTED_CRITICAL_TERM",
                            unit_id=unit_id,
                            message=(
                                f"Unit {unit_id} reader-facing text contains unsupported critical concepts "
                                f"{tuple(novel_critical)}"
                            ),
                            support_ids=cited_ids,
                        )
                    )

    # Section Heading vs Section Body Congruence (PHANTOM_HEADING_TOPIC)
    # When a section heading explicitly enumerates subtopics after a colon (e.g. "Тема: подтема 1, подтема 2 и подтема 3"),
    # verify that each promised subtopic is actually mentioned in the section paragraphs.
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        h_text = sec.heading.strip()
        if not h_text or ":" not in h_text:
            continue
        body_text = " ".join(p.text for p in sec.paragraphs if p.text)
        body_words = _TOKEN_RE.findall(body_text.lower())
        body_stems = {_stem(w) for w in body_words if len(w) >= 3}

        pre, post = h_text.split(":", 1)
        clauses = re.split(r"[,;]|\s+и\s+", post)

        for clause in clauses:
            clause_clean = clause.strip().lower()
            if not clause_clean:
                continue
            c_words = [
                w
                for w in _TOKEN_RE.findall(clause_clean)
                if len(w) >= 3 and w not in _HEADING_EDITORIAL_FILLER
            ]
            if not c_words:
                continue
            # At least one non-filler word from this enumerated clause must appear in the section body
            c_stems = [_stem(w) for w in c_words]
            if not any(s in body_stems for s in c_stems):
                issues.append(
                    ArticleValidationIssue(
                        code="PHANTOM_HEADING_TOPIC",
                        unit_id=h_id,
                        message=f"Section heading {h_id} announces topic '{clause.strip()}' which is never mentioned in section paragraphs",
                        support_ids=sec.heading_support_ids,
                        severity="error",
                        blocking=True,
                    )
                )
                break

    is_valid = not any(iss.blocking for iss in issues)

    return ArticleValidationResult(
        is_valid=is_valid,
        word_count=word_count,
        section_count=section_count,
        issues=tuple(issues),
        unknown_evidence_ids=tuple(dict.fromkeys(unknown_evidence_ids)),
    )
