"""Domain definitions for service families and rubric mapping taxonomy.

Single public taxonomy for service detection across processing and publication.
"""

from __future__ import annotations

import re

SERVICE_FAMILY_STEMS: dict[str, frozenset[str]] = {
    "power": frozenset(
        {
            "свет",
            "электр",
            "энерг",
            "лэп",
            "питани",
            "обесточ",
            "подстанци",
            "трансформатор",
            "рэс",
            "горсвет",
            "power",
            "electric",
            "electricity",
            "blackout",
            "power_supply",
        }
    ),
    "water": frozenset(
        {
            "вода",
            "воды",
            "воде",
            "воду",
            "водой",
            "водопровод",
            "водоснабж",
            "водоканал",
            "водовод",
            "водозабор",
            "обезвож",
            "скважин",
            "water",
            "aqueduct",
            "water_supply",
        }
    ),
    "gas": frozenset(
        {
            "газ",
            "газа",
            "газу",
            "газом",
            "газов",
            "газоснабж",
            "газопровод",
            "gas",
            "gas_supply",
        }
    ),
    "heating": frozenset({"отоплен", "тепл", "котельн", "теплоснабж", "heat", "heating"}),
    "lift": frozenset({"лифт", "подъемник", "lift", "elevator"}),
    "telecom": frozenset(
        {
            "интернет",
            "связ",
            "провайдер",
            "сеть",
            "wi-fi",
            "wifi",
            "оптик",
            "7телеком",
            "миранд",
            "онэт",
            "миртелеком",
            "мобайл",
            "сим-карт",
            "симкарт",
            "сотов",
            "telecom",
            "internet",
            "cellular",
            "mobile",
            "internet_connectivity",
        }
    ),
    "banking": frozenset(
        {
            "банк",
            "банки",
            "банкомат",
            "банкоматы",
            "терминал",
            "платеж",
            "наличн",
            "сбер",
            "псб",
            "сбол",
            "bank",
            "atm",
            "banking",
            "payment",
        }
    ),
    "transport": frozenset(
        {
            "транспорт",
            "автобус",
            "маршрут",
            "поезд",
            "трамвай",
            "троллейбус",
            "рейс",
            "проезд",
            "transport",
            "bus",
            "train",
            "public_transport",
        }
    ),
    "urban_transport": frozenset(
        {
            "городской_транспорт",
            "городской_автобус",
            "маршрутка",
            "трамвай",
            "троллейбус",
        }
    ),
    "health": frozenset(
        {
            "больниц",
            "поликлиник",
            "аптек",
            "врач",
            "медицин",
            "стоматолог",
            "эндокринолог",
            "невролог",
            "педиатр",
            "хирург",
            "госпитал",
            "скорая",
            "медучрежден",
            "анализ",
            "лаборатор",
            "health",
            "hospital",
            "clinic",
            "pharmacy",
            "doctor",
        }
    ),
    "economy": frozenset(
        {
            "магазин",
            "рынок",
            "торгов",
            "супермаркет",
            "товар",
            "цена",
            "цены",
            "стоимост",
            "ozon",
            "озон",
            "wildberries",
            "вайлдберриз",
            "предприяти",
            "бизнес",
            "книжн",
            "зоомагазин",
            "retail",
            "market",
            "shop",
            "supermarket",
        }
    ),
    "social": frozenset(
        {
            "пенси",
            "пенсион",
            "соцвыплат",
            "пособи",
            "гуманитарн",
            "гумпомощ",
            "льгот",
            "соцзащит",
            "ецп",
            "соцпомощ",
        }
    ),
    "civic_services": frozenset(
        {
            "паспорт",
            "мфц",
            "госуслуг",
            "нотариус",
            "вытрезвител",
            "администраци",
            "загс",
            "документ",
        }
    ),
    "education_culture": frozenset(
        {
            "школ",
            "детсад",
            "училищ",
            "вуз",
            "музей",
            "библиотек",
            "театр",
            "культур",
            "образован",
        }
    ),
    "safety": frozenset(
        {
            "взрыв",
            "обстрел",
            "пво",
            "дрон",
            "бпла",
            "прилет",
            "пожар",
            "возгоран",
            "разрушен",
            "разрух",
            "сирен",
        }
    ),
    "logistics": frozenset({"доставк", "почт", "курьер", "parcel", "postal", "delivery"}),
    "municipal": frozenset({"жэк", "мусор", "вывоз", "коммунал", "municipal", "waste", "garbage"}),
}


def semantic_tokens(text: str) -> list[str]:
    """Extract normalized alphanumeric tokens from text."""
    clean = re.sub(r"[^\w\s-]", " ", text.casefold().replace("ё", "е"))
    return [token for token in clean.split() if token]


def matches_any_stem(tokens: list[str], stems: frozenset[str]) -> bool:
    """Return True if any token matches any stem (exact or prefix, avoiding substring false positives)."""
    for token in tokens:
        for stem in stems:
            if token == stem:
                return True
            if len(stem) >= 4 and token.startswith(stem):
                return True
    return False


# "Горят" said of windows, apartment blocks, lamps or a district normally means
# that the electric light is on there ("девятиэтажки все горят"). Such wording
# is a fire only with an explicit fire signal or a burnable object.
_BURNING_VERBS: frozenset[str] = frozenset(
    {"горит", "горят", "горел", "горела", "горело", "горели", "горить", "горять", "горіли"}
)
_BURNING_VERB_PREFIXES: tuple[str, ...] = ("горящ",)
_LIT_SUBJECT_STEMS: frozenset[str] = frozenset(
    {
        "окна",
        "окно",
        "вікн",
        "фонар",
        "ліхтар",
        "лампочк",
        "люстр",
        "гирлянд",
        "многоэтажк",
        "девятиэтажк",
        "пятиэтажк",
        "этажк",
        "высотк",
        "новостройк",
        "багатоповерхів",
        "будинк",
        "район",
        "квартал",
        "микрорайон",
        "мікрорайон",
    }
)
_LIT_SUBJECT_WORDS: frozenset[str] = frozenset({"дома", "домах", "домов", "лампы", "окон"})
_BURNABLE_OBJECT_STEMS: frozenset[str] = frozenset(
    {
        "трав",
        "камыш",
        "очерет",
        "сухост",
        "мусор",
        "смітт",
        "свалк",
        "машин",
        "автомоб",
        "автобус",
        "вагон",
        "гараж",
        "сара",
        "склад",
        "завод",
        "магазин",
        "крыш",
        "кровл",
        "покрышк",
        "покришк",
        "квартир",
        "посадк",
        "лесополос",
        "подстанци",
        "трансформатор",
    }
)
_BURNABLE_OBJECT_WORDS: frozenset[str] = frozenset(
    {
        "лес",
        "леса",
        "лесу",
        "поле",
        "поля",
        "баня",
        "бани",
        "авто",
        "шины",
        "дача",
        "дачи",
        "рынок",
        "цех",
    }
)
# A single house that "горит" is a fire report; lit windows are plural.
_SINGLE_BUILDING_WORDS: frozenset[str] = frozenset({"дом", "здание", "хата", "будинок"})
_INCIDENT_STEMS: frozenset[str] = SERVICE_FAMILY_STEMS["safety"] | frozenset(
    {"шахед", "ракет", "вибух", "приліт", "обстріл", "детонац", "сдетонир"}
)
_LIT_PROXIMITY_TOKENS = 4
_EXPLICIT_FIRE_RE = re.compile(
    r"пожар|пожеж|возгоран|загорянн|задымл|(?<!\w)дым|(?<!\w)(?:дим|диму|димом)(?!\w)|"
    r"(?<!\w)(?:огонь|огня|огнем|огне|огню|вогонь|вогню|вогнем)(?!\w)|"
    r"(?<!\w)плам[яеи]|(?<!\w)полум['ʼ’]?(?:я|ям)(?!\w)|"
    r"(?<!\w)(?:тушат|тушит|тушили|тушил|тушила|тушить|тушени\w*|потуш\w*)(?!\w)|"
    r"(?<!\w)(?:сгор|выгор|обгор)\w*|(?<!\w)згор(?:ів|іла|іло|іли|ає|ають)(?!\w)|"
    r"поджог|подожг|підпал|"
    r"(?<!\w)(?:мчс|дснс)(?!\w)"
)


def has_explicit_fire_signal(text: str) -> bool:
    """Return whether text names a fire itself, not merely something that "горит"."""
    return bool(_EXPLICIT_FIRE_RE.search(text.casefold().replace("ё", "е")))


def _is_burning_verb(token: str) -> bool:
    return token in _BURNING_VERBS or token.startswith(_BURNING_VERB_PREFIXES)


def mentions_burning_verb(text: str) -> bool:
    """Return whether text says that something "горит"/"горят"."""
    return any(_is_burning_verb(token) for token in semantic_tokens(text))


def names_burnable_object(text: str) -> bool:
    """Return whether text names an object that burns rather than lights up."""
    tokens = semantic_tokens(text)
    return any(token in _BURNABLE_OBJECT_WORDS for token in tokens) or matches_any_stem(
        tokens, _BURNABLE_OBJECT_STEMS
    )


def has_fire_or_incident_signal(text: str, *, include_single_building: bool = True) -> bool:
    """Return whether text supports reading "горит" as a fire.

    An explicit fire word, a strike or explosion, or a burnable object keeps the
    fire reading. A single house ("горит дом") counts only inside the burning
    sentence itself, because an address in a reply parent names no fire.
    """
    if has_explicit_fire_signal(text) or names_burnable_object(text):
        return True
    tokens = semantic_tokens(text)
    if matches_any_stem(tokens, _INCIDENT_STEMS):
        return True
    return include_single_building and any(token in _SINGLE_BUILDING_WORDS for token in tokens)


def reports_lit_buildings(text: str) -> bool:
    """Return whether text says that windows, buildings or a district are lit up.

    The light verb must stand within a few words of a lighting subject. An
    explicit fire signal or a burnable object (grass, car, shed) keeps the
    ordinary fire reading.
    """
    if has_explicit_fire_signal(text) or names_burnable_object(text):
        return False
    tokens = semantic_tokens(text)
    verb_positions = [index for index, token in enumerate(tokens) if _is_burning_verb(token)]
    if not verb_positions:
        return False
    subject_positions = [
        index
        for index, token in enumerate(tokens)
        if any(
            part in _LIT_SUBJECT_WORDS or matches_any_stem([part], _LIT_SUBJECT_STEMS)
            for part in (token, *token.split("-"))
        )
    ]
    return any(
        abs(verb - subject) <= _LIT_PROXIMITY_TOKENS
        for verb in verb_positions
        for subject in subject_positions
    )


def detect_service_families(text: str) -> frozenset[str]:
    """Detect core service families mentioned in text."""
    tokens = semantic_tokens(text)
    detected: set[str] = set()
    for family, stems in SERVICE_FAMILY_STEMS.items():
        if matches_any_stem(tokens, stems):
            detected.add(family)
    # A numeric voltage measurement identifies electricity even when a
    # resident omits "свет". Bare emotional "напряжение" does not.
    if re.search(r"\bнапряжени[а-яё]*\s+(?:в\s+)?\d{2,3}(?:[.,]\d+)?\b", text, re.IGNORECASE):
        detected.add("power")
    # Lit-up windows or apartment blocks report electric light, not a fire.
    if reports_lit_buildings(text):
        detected.add("power")
    # Resolve a dependent pronoun only when the parent explicitly says it is
    # being used to write, a narrow connectivity cue rather than generic use.
    if re.search(r"\bчерез\s+него\s+пишу\b", text, re.IGNORECASE):
        detected.add("telecom")
    return frozenset(detected)


def map_family_to_rubric(family: str) -> str | None:
    """Map a detected service family to its canonical digest rubric ID."""
    if family == "telecom":
        return "communications"
    if family in ("transport", "urban_transport"):
        return "mobility"
    if family in ("power", "water", "gas", "heating", "lift", "municipal"):
        return "infrastructure"
    if family == "health":
        return "health"
    if family == "economy":
        return "economy"
    if family == "social":
        return "society"
    if family in ("civic_services", "banking"):
        return "civic_services"
    if family == "education_culture":
        return "education_culture"
    if family == "safety":
        return "safety"
    return None
