"""Deterministic quality, predicate, and entity grounding guards for publication stories."""

from __future__ import annotations

import re
from typing import Any

RECOGNIZED_CORE_SERVICE_KEYS = frozenset(
    {
        "electricity",
        "power",
        "power_supply",
        "water",
        "water_supply",
        "gas",
        "gas_supply",
        "heating",
        "sewage",
        "refuse",
        "transport",
        "connectivity",
        "telecom",
        "telecommunications",
        "internet",
        "banking",
        "municipal_service",
        "municipal_infrastructure",
        "utilities",
    }
)

_GENERIC_ENTITY_WORDS = frozenset(
    {
        "сервис",
        "проблемный сервис",
        "услуга",
        "приложение",
        "мобильное приложение",
        "компания",
        "организация",
        "провайдер",
        "служба",
        "некий сервис",
        "сторонний сервис",
        "городской сервис",
    }
)

_GENERIC_ENTITY_PATTERN = re.compile(
    r"^(?:проблемн\w*|сторонн\w*|неки\w*|данн\w*|городск\w*)?\s*(?:сервис|услуг\w*|приложен\w*|провайдер\w*|служб\w*)$",
    re.IGNORECASE,
)

_ATTRIBUTION_PREFIX_RE = re.compile(
    r"^(?:по\s+(?:сообщениям|сообщению|словам|информации|данным)\s+(?:жителей|жителя|горожан|горожанина|очевидцев|очевидца)(?:\s+[а-яё-]+)?|"
    r"(?:жители|житель|горожане|горожанин|очевидцы|очевидец)(?:\s+[а-яё-]+)?\s+(?:сообщают|сообщает|пишут|пишет|отмечают|отмечает)(?:\s*,\s*что)?|"
    r"в\s+(?:местных\s+чатах|соцсетях|сетях|пабликах)(?:\s+(?:жители|житель|горожане|горожанин))?(?:\s+(?:подтверждают|подтверждает|сообщают|сообщает|пишут|пишет))?(?:\s*,\s*что)?|"
    r"(?:несколько\s+)?(?:горожан|жителей)\s+(?:сообщают|сообщает|пишут|пишет|подтверждают|подтверждает)(?:\s*,\s*что)?)[,:\s]+",
    re.IGNORECASE,
)

_CHATTER_META_RE = re.compile(
    r"\b(?:"
    r"(?:(?:жители|житель|горожане|горожанин|в\s+соцсетях|в\s+чатах)\s+)?"
    r"(?:обсуждают|выясняют|интересуются|сообщают\s+о|сообщает\s+о|сообщени[ея]\s+о)?(?:\s+текущ\w*)?\s+ситуаци[июей]\w*|"
    r"подробности\s+уточняются|информация\s+уточняется|ситуация\s+уточняется|"
    r"детали\s+не\s+раскрыты|без\s+конкретных\s+деталей|эмоциональное\s+сообщение|"
    r"выясняют\s+обстоятельства|жители\s+интересуются|"
    r"сообщается\s+о\s+(?:событии|ситуации)|"
    r"в\s+радиусе\s+\d+\s*[–-]\s*\d+\s*км|"
    r"точнее\s+не\s+работает\s+вообще|"
    r"не\s+может\s+пройти\s+через\s+кпп|"
    r"обсуждают\s+(?:старый\s+)?(?:ж[её]лтый\s+)?автобус\w*|"
    r"упоминают\s+автобус\w*\s+[^.!?]{0,50}\s+производств\w*|"
    r"жителям\s+сообщают\s+о\s+записи\s+на\s+при[её]м|"
    r"планирует\s+забрать\s+(?:горячую\s+)?воду|"
    r"комментирует,?\s+что\s+никто\s+не\s+спорит|"
    r"(?:жител(?:ь|и)|горожан(?:ин|е))\s+(?:упомина(?:ет|ют)|обсужда(?:ет|ют)|"
    r"вспомина(?:ет|ют)|иронизиру(?:ет|ют)|высказыва(?:ет|ют)\s+мнени\w*)|"
    r"сообщени[ея]\s+о\s+(?:районе|событии|ситуации)|"
    r"жител(?:ь|и)\s+(?:сообща(?:ет|ют)|пиш(?:ет|ут))\s+о\s+ситуации|"
    r"жителям(?:\s+[а-яё-]+){0,2}\s+сообщают\s+о\s+записи\s+на\s+при[её]м|"
    r"в\s+городском\s+чате\s+обсужда(?:ется|ются)\s+вопрос\w*\s+о\s+возможн\w*\s+появлени\w*|"
    r"сообщени[ея]\s+(?:из|о)\s+[^.!?]{1,80}|"
    r"сообщени[ея]\s+сообщества\s+о\s+выполнении\s+работ|"
    r"конкретный\s+вид\s+сервиса\s+(?:в\s+сообщениях\s+)?не\s+уточняется|"
    r"вид\s+сервиса\s+(?:в\s+сообщениях\s+)?не\s+уточняется"
    r")\b",
    re.IGNORECASE,
)

_ADVICE_MARKER_RE = re.compile(
    r"\b(?:совет(?:ую|ует|уем|уют)|рекоменд(?:ую|ует|уем|уют)|"
    r"призыва(?:ет|ют)|призыв|не\s+(?:появляйтесь|ходите|обстреливайте|"
    r"звоните|заходите|выходите)|выражает\s+надежду|наде(?:юсь|ется|емся))\b",
    re.IGNORECASE,
)

_QUESTION_CONTEXT_RE = re.compile(
    r"(?:\?|\b(?:интересу(?:ется|ются)|спрашива(?:ет|ют)|зада(?:ёт|ет)\s+вопрос|"
    r"кто\s+знает|есть\s+ли|подскажите)\b)",
    re.IGNORECASE,
)

_CONCRETE_EVENT_SIGNAL_RE = re.compile(
    r"\b(?:взрыв\w*|обстрел\w*|пожар\w*|авари\w*|ремонт\w*|"
    r"отключ\w*|выключ\w*|включ\w*|нет\s+(?:свет\w*|вод\w*|газ\w*)|"
    r"не\s+(?:было|включали)\s+(?:свет\w*|вод\w*|газ\w*)|"
    r"дали\s+(?:свет\w*|вод\w*|газ\w*|электр\w*|\d+\s*мин\w*)|"
    r"пода[чл]\w*\s+(?:свет\w*|вод\w*|газ\w*|электр\w*)|"
    r"восстанов\w*|поврежд\w*|прорыв\w*|перебо\w*|"
    r"ветряк\w*|солнечн\w*\s+электростанци\w*|редкост\w*|"
    r"напряжен\w*|сирен\w*|дтп|маршрут\w*|рейс\w*|аптек\w*)\b",
    re.IGNORECASE,
)

_NON_EDITORIAL_PAYLOAD_RE = re.compile(
    r"\b(?:аренд\w*\s+жиль\w*|ищет\s+(?:работ\w*|подработ\w*)|"
    r"поиск\s+(?:работ\w*|подработ\w*)|шабашк\w*|"
    r"автозапчаст\w*|автомобил\w*\s+в\s+разбор|"
    r"маленьк\w*\s+леди|атмосфер\w*\s+красот\w*|"
    r"намерени\w*\s+порыбач\w*|порыбач\w*[^.!?]{0,80}снять\s+видео|"
    r"вакцинирован\w*[^.!?]{0,80}стерилизован\w*|"
    r"стерилизован\w*[^.!?]{0,80}разведени\w*|"
    r"может\s+кто[- ]то\s+потерял|"
    r"ежедневн\w*\s+(?:автобусн\w*\s+)?(?:рейс\w*|пассажирск\w*\s+перевоз\w*)|"
    r"не\s+может\s+пройти\s+через\s+кпп|"
    r"не\s+вернул\w*\s+деньги|"
    r"иронизиру\w*|вспомина\w*|делится\s+(?:своим\s+)?опытом|делится\s+мнени\w*|рассказыва(?:ет|ют)\s+(?:о\s+сво[её]м|как\s+(?:он|она|они)|что\s+у\s+(?:него|неё|них))|"
    r"(?:газов\w*\s+баллон\w*[^.!?]{0,40}(?:хватает|заправк\w*|деньги\s+на|купил|купили|есть\s+деньги))|"
    r"жив\w*\s+(?:дома|как\s+и\s+жил)|"
    r"упомина\w*\s+(?:район|бердянск)|"
    r"депутат\w*[^.!?]{0,80}(?:чат|пиар)|"
    r"почему\s+район\w*[^.!?]{0,40}называ\w*|"
    r"(?:немц\w*\s+там\s+селил\w*|подземн\w*\s+ход\w*)|"
    r"район\w*\s+самол[её]т[^.!?]{0,40}забрал\w*|"
    r"громк\w*\s+зву\w*[^.!?]{0,60}(?:требу\w*\s+уточн|детал\w*\s+не\s+уточн)|"
    r"движух\w*[^.!?]{0,60}(?:детал\w*\s+не\s+уточн)|"
    r"шут\w*[^.!?]{0,60}шум|"
    r"не\s+тихо[^.!?]{0,60}детал\w*\s+не\s+уточн|"
    r"услуг\w*\s+предоставля\w*\s+только\s+некотор\w*\s+улиц\w*|"
    r"обсуждают\s+(?:старый\s+)?(?:ж[её]лтый\s+)?автобус\w*|"
    r"упоминают\s+автобус\w*\s+[^.!?]{0,50}\s+производств\w*|"
    # Carrier, taxi, carpool, passenger and parcel transport advertisements
    r"(?:мест\w*\s+на\s+(?:завтра|сегодня)|есть\s+(?:несколько\s+)?мест\w*)|"
    r"(?:пассажирск\w*\s+перевоз\w*|перевозк\w*\s+пассажир\w*)|"
    r"(?:поездк\w*\s+(?:в|из|до)\s+[А-Яа-я]+|доставк\w*\s+посыл\w*|попутк\w*)|"
    # Lost and found (keys, glasses, wallets, pets, documents)
    r"(?:найден\w*|потеря\w*|потерян\w*|бюро\s+находок)\s+(?:очки|ключ\w*|документ\w*|кошелек|кошелёк|телефон|вещи|сумк\w*|номер\w*)|"
    r"на\s+улице\s+[^\n.,!?]{1,30}\s+найден\w*|"
    r"найден\w*\s+в\s+кофейн\w*|"
    r"потерял\w*\s+(?:очки|ключ\w*|карточк\w*|кошелек|кошелёк|телефон)|"
    # Chat memories, reviews, and non-event reactions
    r"(?:изменени\w*\s+не\s+заметно|не\s+заметно\s+изменени\w*)|"
    r"ездили\s+(?:месяц|неделю|год|несколько\s+дней)\s+назад|"
    r"делится\s+(?:своим\s+)?опытом\s+(?:быстрой\s+)?поездки|"
    r"прогулк\w*\s+с\s+(?:четвероног\w*|собак\w*)|"
    r"(?:(?:раньше|ранее)\s+[^.!?]{0,30}действовал|прежн\w*\s+(?:режим|график)\s+комендантск\w*|вспомнили\s+прежний\s+режим)|"
    # Private beauty, repair, and personal services / advertisements
    r"(?:маникюр\w*|педикюр\w*|мастер\w*\s+маникюра|наращиван\w*\s+ресниц|ресничк\w*)|"
    r"(?:стрижк\w*|парикмахер\w*|ногт\w*|бров\w*|косметолог\w*|массаж\w*)|"
    r"(?:где\s+(?:делают|сделать)\s+пирсинг|пирсинг\s+ух\w*|тату[- ]салон\w*|студи\w*\s+рутрокс|услуг\w*\s+по\s+уборк\w*|уборк\w*\s+(?:квартир|домов|помещени\w*)|клининг\w*)|"
    r"(?:ремонт\s+(?:стиральн\w*|холодильн\w*|телевизор\w*|обув\w*|одежд\w*|замк\w*|двер\w*|окон\w*))|"
    # Vehicle and property classifieds / sales / rentals
    r"(?:прода[её]тся|продам|куплю|сдам|сда[её]тся)\s+(?:ваз\b|авто\b|машин\w*|квартир\w*|дом\b|гараж\w*|вещи|мебель)|"
    r"(?:(?:уголь\b|дров\w*|песок\b|щебень\b|сыпуч\w*\s+материал\w*)\s+[^.!?]{0,30}прода[юе]т|(?:прода[её]тся|продам|куплю|сдам|сда[её]тся|прода[юе]т)\s+[^.!?]{0,20}(?:уголь\b|дров\w*|песок\b|щебень\b|сыпуч\w*\s+материал\w*))|"
    r"(?:(?:бесплатно\s+(?:отда[её]м|отда[юе]т|забирайте|забрать)|отда[её]м\s+бесплатно|отда[юе]т\s+даром|отдам\s+даром|забирать,?\s+даром)\b(?![^.!?]*?(?:пункт|волонт[её]р|библиотек|помощ|семь[яеи]м))[^.!?]*?(?:дров\w*|древесин\w*|доск\w*|бруск\w*|вещ\w*)|(?:кому\s+дрова|распилить\s+и\s+забрать|отдам\s+дрова|отдадут\s+дрова|доски\s+и\s+бруски|на\s+даче\s+[^\n.,!?]{0,30}вишн\w*))|"
    r"в\s+отличном\s+состоянии|торг\s+(?:уместен|при\s+осмотре)|"
    # Private freight, moving, cargo requests
    r"(?:нужна\s+(?:грузов\w*|машин\w*|газель)|грузов\w*\s+машин\w*\s+для\s+перевоз\w*|перевозк\w*\s+вещей|перевезти\s+вещи|без\s+мебели,?\s+просто\s+коробки)|"
    # Domestic pet gossip / runaway animal banter
    r"(?:переночевала\s+и\s+опять|пыталась\s+смыться|смыться\s+на\s+гульки|привязал\s+пока|словил,?\s+когда\s+она|не\s+было,?\s+появилась,?\s+переночевала)|"
    # Messenger tech issues, premium subscription, white lists tips
    r"(?:не\s+может\s+написать\s+сообщение|требуется\s+премиум[- ]подписка|для\s+отправки\s+сообщения\s+требуется|добав(?:ьте|ить)\s+контакты\s+и\s+потом|обход\s+белых\s+списков|белы[ех]\s+списк\w*)|"
    # Domestic DIY appliance problems / handyman repair chatter
    r"(?:пытался\s+и\s+болгаркой\s+и\s+дрелью|вилка\s+горит\s+когда|кабель\s+слабый\s+для\s+бойлера)|"
    # Job recruitment, warehouse vacancies, hiring ads
    r"(?:(?:объявлен\w*|старт\w*|открыт\w*|вед[её]тся|объявление\s+о)\s+(?:набор\w*|ваканси\w*|при[её]м\w*\s+на\s+работ\w*)|мест\w*\s+осталось\s+мало|строит\w*\s+склад[^.!?]{0,50}вокзал\w*)|"
    # Small commercial retail, coffee stalls, dry cleaning trivia
    r"(?:возобновил\w*\s+работу\s+кофе[- ]брейк|кофе[- ]брейк\s+возле|химчистк\w*\s+возле\s+военкомат\w*)|"
    # Chat location Q&A and trivia without an event
    r"(?:бывш\w*\s+вытрезвител\w*|здани[ие]\s+бывшего\s+вытрезвител\w*)|"
    # Tree branch bickering and emotional venting
    r"(?:обрезк\w*\s+веток[^.!?]{0,50}(?:не\s+зеленхоз|вечность\s+будут)|вечность\s+будут\s+обрезать)|"
    r"(?:ледян\w*\s+душ|пальцы\s+рук\s+онемели|просто\s+издевательство|ваши\s+улицы\s+не\s+касается|тишина\s+полнейшая|плохиши\s+пили\w*)|"
    # Contextless fragments without a named subject
    r"(?:служба\s+работает\s+в\s+(?:нынешнем|текущем)\s+режиме\s+с\s+\d+|работает\s+в\s+таком\s+режиме\s+с\s+\d+)|"
    # Relocated Ukrainian administration and educational programs outside the city
    r"(?:бердянск\w*\s+гимнази\w*\s+гармони\w*|олимпийск\w*\s+урок|шлях\s+до\s+олімпу|министерств\w*\s+культур\w*\s+украин\w*|тысячевесн\w*|тисячовесн\w*)|"
    # Informal voting gossip, chat sarcasm, and non-verified election chatter
    r"(?:кому\s+надо\s+(?:уже\s+)?проголосовал|я\s+не\s+ванга|смысл\s+в\s+голосовани|уже\s+идут\s+у\s+нас\s+\d+\s+дней|голосовани\w*[^.!?]{0,60}(?:десятый\s+день|\d+\s+дней|результат\b)|за\s+кого\s+надо\s+(?:уже\s+)?проголосовал|выборы\s+в\s+госдуму))\b",
    re.IGNORECASE,
)

_CIVIC_EVENT_TOKENS_RE = re.compile(
    r"\b(?:"
    # Verbs / participles of action, state, change (past, present, future)
    r"отключ\w*|выключ\w*|включ\w*|пропа[лв]\w*|исчез\w*|верну\w*|возвращ\w*|"
    r"восстанов\w*|возобнов\w*|заработа\w*|работа\w*|"
    r"выш[ели]\w*|выход\w*|пополн\w*|поступ\w*|"
    r"прорва\w*|прорыв\w*|теч[её]\w*|капа\w*|ут[её]к\w*|утечк\w*|"
    r"перекры\w*|затоп\w*|гор[яеи]\w*|потуш\w*|сби\w*|упа[лд]\w*|пада\w*|"
    r"взорв\w*|взрыв\w*|повред\w*|разруш\w*|"
    r"ремонтир\w*|почин\w*|чин[яи]\w*|провод\w*|прове[лд]\w*|"
    r"откры\w*|закры\w*|запуст\w*|пуск\w*|пода[юе]\w*|подач\w*|"
    r"выда[юеё]\w*|разда[юеё]\w*|отда[юеё]\w*|"
    r"ход[яи]\w*|курсир\w*|перевоз\w*|"
    r"списа\w*|подорож\w*|подешев\w*|зафиксир\w*|наблюда\w*|"
    r"сниз\w*|повыс\w*|вырос\w*|раст[еу]\w*|увелич\w*|уменьш\w*|"
    r"выплат\w*|начисл\w*|получ\w*|направ\w*|"
    r"приним\w*|приня\w*|утверд\w*|ввел\w*|ввод\w*|измен\w*|отмен\w*|"
    r"сообщи\w*|сообща\w*|подтверд\w*|устрани\w*|ликвиди\w*|"
    r"заверш\w*|оконч\w*|нач[ая]\w*|продолж\w*|"
    r"произош\w*|происход\w*|случи\w*|обнаруж\w*|установ\w*|постро\w*|сдела\w*|"
    r"огранич\w*|перенес\w*|достав\w*|привез\w*|"
    r"прибы\w*|приезжа\w*|приеха\w*|дела\w*|сдела\w*|сто[яи]\w*|появи\w*|появля\w*|"
    r"вед\w*|выполн\w*|осуществл\w*|производ\w*|обеспеч\w*|организов\w*|заяв\w*|предупред\w*|опубликов\w*|планиру\w*|оста[её]тся|сохран\w*|"
    # Event / state nouns
    r"авари[яи]|прорыв\w*|ремонт\w*|отключени[ея]|перебо[яев]|восстановлени[ея]|возобновлени[ея]|"
    r"взрыв\w*|обстрел\w*|сирен\w*|пожар\w*|дым\w*|дтп|сбой\w*|неисправност\w*|проблем\w*|"
    r"напряжени[ея]|скач[ок]\w*|график\w*|подвоз\w*|задержк\w*|отмен\w*|рейс\w*|маршрут\w*|"
    r"выплат\w*|пособи[ея]|запрет\w*|штраф\w*|при[её]м\w*|проверк\w*|редкост\w*|"
    # Predicates / states
    r"нет|нету|есть|доступен|доступна|доступно|доступны|недоступен|недоступна|недоступно|недоступны|"
    r"отсутству\w*|восстановлен\w*|отключен\w*|перекрыт\w*|открыт\w*|закрыт\w*|завершен\w*|поврежден\w*|"
    r"0\s+по\s+(?:свету|воде|газу)|"
    # General Russian verb morphology fallback (verbs ending in -лся, -лась, -лось, -лись, -ется, -ются, -ится, -ятся)
    r"[а-яё]{3,}(?:лся|лась|лось|лись|ется|ются|ится|ятся)|"
    # Quantitative facts / measurements
    r"\d+\s*(?:минут\w*|мин\w*|час\w*|суток|дня|дней|в|вольт|квт|руб|рублей|р\.|грн|мбит|%|процент\w*|автобус\w*|рейс\w*|человек\w*|дом\w*|улиц\w*)"
    r")\b",
    re.IGNORECASE,
)

_PURE_GEOGRAPHIC_FRAGMENT_RE = re.compile(
    r"^(?:(?:в|на|возле|около|у|вблизи|по|со\s+стороны|в\s+районе)\s+[\w\s«»\"'\.\-]+)+$",
    re.IGNORECASE,
)

_PLACE_ENTITY_RE = re.compile(
    r"\b(?:улиц\w*|переул\w*|проспект\w*|площад\w*|набережн\w*|"
    r"район\w*|микрорайон\w*|часть\s+города|центр(?:альн\w*)?(?:\s+част\w*)?|"
    r"город\w*|село\w*|пос[её]лок\w*|кос\w*|остановк\w*|маршрут\w*|"
    r"санатор\w*|аквапарк\w*|школ\w*|училищ\w*|рын\w*|парк\w*|"
    r"здани\w*|дом\w*|музе\w*|больниц\w*|поликлиник\w*|мфц\b|"
    r"военкомат\w*|студи\w*|"
    r"магазин\w*|аптек\w*|офис\w*|филиал\w*|пункт\w*|отделени\w*|"
    r"вокзал\w*|станци\w*|подстанци\w*|электростанци\w*|стадион\w*|"
    r"мост\w*|перекр[её]сток\w*|территори\w*|участок\w*|объект\w*)\b",
    re.IGNORECASE,
)

_LOCATION_RELATION_PREDICATE_RE = re.compile(
    r"\b(?:наход\w*|располож\w*|располага\w*|леж\w*|относ\w*\s+к|"
    r"явля\w*\s+(?:частью|частью\s+города|районом)|"
    r"вход\w*\s+в\s+состав|проход\w*\s+по)\b",
    re.IGNORECASE,
)

_LOCATION_RELATION_WORD_RE = re.compile(
    r"\b(?:в|во|на|у|около|возле|рядом\s+с|вблизи|за|перед|напротив|"
    r"между|по\s+соседству\s+с|вдоль|от|до)\b",
    re.IGNORECASE,
)

_LOCATION_STATIC_ROUTE_REFERENCE_RE = re.compile(
    r"\b(?:маршрут\w*|остановк\w*)\s*(?:№\s*)?\d*\b|\b№\s*\d+\b|\b\d+\b",
    re.IGNORECASE,
)

_INTERNAL_REPLY_ANNOTATION_RE = re.compile(
    r"\s*\(in_reply_to:\s*\".*\"\)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_GIVEAWAY_RE = re.compile(
    r"\b(?:бесплатно\s+(?:отда[её]м|отда[юе]т|забирайте|забрать)|"
    r"отда[её]м\s+бесплатно|отда[юе]т\s+даром|отдам\s+даром|"
    r"отда[юе]т\s+древесин\w*|распилить\s+и\s+забрать|отдам\s+дрова|"
    r"отдадут\s+дрова|доски\s+и\s+бруски)\b",
    re.IGNORECASE,
)
_DOCTOR_OPINION_RE = re.compile(
    r"\b(?:нет\s+адекватных\s+(?:врачей|специалистов|докторов)|"
    r"критически\s+(?:отозва\w*|высказа\w*)\s+о\s+(?:доступности|качестве)\s+врачей)\b",
    re.IGNORECASE,
)
_ANSWER_CONTEXT_RE = re.compile(
    r"\b(?:в\s+ответ\s+на|отвечая\s+на)\s+вопрос[,\s]+где\b[^,.:;!?]*",
    re.IGNORECASE,
)
_COMMUNITY_DISTRIBUTION_RE = re.compile(
    r"\b(?:пункт\w*\s+(?:выдач\w*|помощ\w*)|"
    r"семь\w*\s+без\s+отоплен\w*|нуждающ\w*)\b",
    re.IGNORECASE,
)
# Only match an unidentified object and a switch verb with conversational
# qualifiers between them. A named monument, lighting, repair or other actual
# event remains publishable from one community report.
_MONUMENT_SWITCH_RE = re.compile(
    r"\b(?:"
    r"(?:памятник\w*|монумент\w*|стел\w*)"
    r"(?:[\s,—-]|по\s+сообщению|только\s+что|сегодня|вчера|сейчас|уже|снова|опять)+"
    r"(?:выключ\w*|отключ\w*)|"
    r"(?:выключ\w*|отключ\w*)"
    r"(?:[\s,—-]|только\s+что|сегодня|вчера|сейчас|уже|снова|опять)+"
    r"(?:памятник\w*|монумент\w*|стел\w*))\b",
    re.IGNORECASE,
)
_PUBLIC_ACCESS_SUBJECT_RE = re.compile(
    r"\b(?:вод\w*|водоканал\w*|электр\w*|свет\w*|газ\w*|отоплен\w*|"
    r"интернет\w*|связ\w*|автобус\w*|транспорт\w*|банк\w*|почт\w*|"
    r"больниц\w*|поликлиник\w*|врач\w*|терапевт\w*|пенсион\w*|соцзащит\w*|"
    r"пункт\w*\s+(?:выдач\w*|помощ\w*|обогрев\w*))\b",
    re.IGNORECASE,
)
_PUBLIC_ACCESS_PREDICATE_RE = re.compile(
    r"\b(?:доступ\w*|недоступ\w*|работа\w*|приним\w*|пода[юе]\w*|"
    r"выда[юе]\w*|откры\w*|закры\w*|организ\w*|подвоз\w*|нет|нету|отсутству\w*)\b",
    re.IGNORECASE,
)
_BARE_OPENING_FRAGMENT_RE = re.compile(
    r"(?:с\s+\d{1,2}\s*(?:[-–]?\s*го)?\s+числа\s+в\s+[«\"]?[\w-]+[»\"]?\s+открытие|"
    r"открытие\s+в\s+[«\"]?[\w-]+[»\"]?\s+с\s+\d{1,2}\s*(?:[-–]?\s*го)?\s+числа)"
    r"[.!\s]*",
    re.IGNORECASE,
)
_ANNOUNCEMENT_SIGNPOST_RE = re.compile(
    r"\b(?:(?:посмотреть|написано|пишут)\s+(?:может\s+)?на\s+двери|"
    r"(?:может\s+)?на\s+двери\s+(?:посмотреть|написано|пишут)|"
    r"информаци\w*\s+на\s+двери)\b",
    re.IGNORECASE,
)
_EMPTY_ANNOUNCEMENT_META_RE = re.compile(
    r"\bинформаци\w*\s+об\s+открытии\s+(?:заведения|учреждения)\b",
    re.IGNORECASE,
)
_EMPTY_SIGNPOST_RESIDUAL_RE = re.compile(
    r"(?:[\s,.:;!?—-]|(?:может|наверное|там|информация|объявление)\b)*",
    re.IGNORECASE,
)


def is_generic_service_entity(entity: str, subject_label: str = "", subject_key: str = "") -> bool:
    """Check if an entity represents a generic placeholder without concrete service identity."""
    ent = (entity or "").strip().lower()
    lbl = (subject_label or "").strip().lower()
    key = (subject_key or "").strip().lower()

    if ent in _GENERIC_ENTITY_WORDS or _GENERIC_ENTITY_PATTERN.match(ent):
        return True

    if not ent:
        if key in RECOGNIZED_CORE_SERVICE_KEYS:
            return False
        if not lbl or lbl in _GENERIC_ENTITY_WORDS or _GENERIC_ENTITY_PATTERN.match(lbl):
            return True
        return False

    return False


def has_meaningful_predicate(text: str) -> bool:
    """Check if text contains a concrete civic action, state change, event, or consequence.

    Rejects predicateless geographic prepositional fragments (e.g. 'В районе Водоканала на Пролетарском')
    and chat meta-inquiries ('жители обсуждают текущую ситуацию... подробности уточняются').
    """
    if not text or not text.strip():
        return False

    cleaned = _INTERNAL_REPLY_ANNOTATION_RE.sub("", text.strip()).strip()
    # Strip leading attribution phrases
    cleaned = _ATTRIBUTION_PREFIX_RE.sub("", cleaned).strip()
    # Strip chatter meta / filler
    without_chatter = _CHATTER_META_RE.sub("", cleaned).strip()
    without_chatter = re.sub(r"[,\s\.\!\?\–—\-]+$", "", without_chatter).strip()

    if not without_chatter:
        return False

    # Check if what remains is purely a geographic phrase without verbs/states
    if _PURE_GEOGRAPHIC_FRAGMENT_RE.match(without_chatter):
        # Even if it matches pure geographic pattern, verify if any civic event token is present
        if not _CIVIC_EVENT_TOKENS_RE.search(without_chatter):
            return False

    # Must contain at least one civic event / action / state token
    if not _CIVIC_EVENT_TOKENS_RE.search(without_chatter):
        return False

    return True


def _is_location_context_without_event(text: str) -> bool:
    """Return whether a claim only explains where a place or facility is.

    A static location answer can contain a grammatical predicate ("улица
    находится в центре") without reporting a civic event, service state, or
    practical change. Do not mistake that grammar for digest-worthy news.
    """
    if not text or not text.strip():
        return False

    cleaned = _INTERNAL_REPLY_ANNOTATION_RE.sub("", text.strip()).strip()
    cleaned = _ATTRIBUTION_PREFIX_RE.sub("", cleaned).strip()
    if not (
        _PLACE_ENTITY_RE.search(cleaned)
        and _LOCATION_RELATION_PREDICATE_RE.search(cleaned)
        and _LOCATION_RELATION_WORD_RE.search(cleaned)
    ):
        return False

    # Keep actual updates and service reports even when they also explain an
    # address. Remove the static place names and locative wording first so a
    # route number or the verb "находится" cannot masquerade as an event.
    residual = _PLACE_ENTITY_RE.sub(" ", cleaned)
    residual = _LOCATION_RELATION_PREDICATE_RE.sub(" ", residual)
    residual = _LOCATION_RELATION_WORD_RE.sub(" ", residual)
    residual = _LOCATION_STATIC_ROUTE_REFERENCE_RE.sub(" ", residual)
    if _CIVIC_EVENT_TOKENS_RE.search(residual):
        return False
    return True


def _is_advice_without_event(text: str) -> bool:
    """Return whether text is only advice/appeal and contains no concrete event."""
    if not text or not _ADVICE_MARKER_RE.search(text):
        return False
    # Remove the short advice target as well ("советую не появляться",
    # "призывает не выходить"). Otherwise the generic verb "появляться"
    # would be mistaken for an event by the broad civic-predicate guard.
    without_advice = re.sub(
        r"\b(?:совет(?:ую|ует|уем|уют)|рекоменд(?:ую|ует|уем|уют)|"
        r"призыва(?:ет|ют)|призыв)\b(?:\s+\w+){0,4}",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    without_advice = re.sub(
        r"\bне\s+(?:появляйтесь|ходите|обстреливайте|звоните|заходите|выходите)\b",
        " ",
        without_advice,
        flags=re.IGNORECASE,
    )
    return not _CIVIC_EVENT_TOKENS_RE.search(without_advice)


def _is_question_without_event(text: str) -> bool:
    """Return whether a source item is only a resident question/context line."""
    return bool(
        text and _QUESTION_CONTEXT_RE.search(text) and not _CONCRETE_EVENT_SIGNAL_RE.search(text)
    )


def _is_announcement_context_without_event(text: str) -> bool:
    """Reject empty opening/signpost replies, retaining actual posted facts.

    A partial date is not a reason to drop an identified shop or service.
    Match only a complete bare fragment with no institution type or event
    detail, and inspect what a door notice actually says before filtering it.
    """
    cleaned = _INTERNAL_REPLY_ANNOTATION_RE.sub("", text.strip()).strip()
    cleaned = _ATTRIBUTION_PREFIX_RE.sub("", cleaned).strip()
    if _BARE_OPENING_FRAGMENT_RE.fullmatch(cleaned):
        return True
    if not _ANNOUNCEMENT_SIGNPOST_RE.search(cleaned):
        return False
    residual = _ANNOUNCEMENT_SIGNPOST_RE.sub(" ", cleaned)
    residual = _EMPTY_ANNOUNCEMENT_META_RE.sub(" ", residual)
    # Posted hours and access details may be useful noun phrases. Lack of a
    # recognized verb is not evidence that a notice contains no actual facts.
    return bool(_EMPTY_SIGNPOST_RESIDUAL_RE.fullmatch(residual))


# A private lost/found document notice is personal payload, not city news:
# require both the document and a return request or private contact, so a
# civic explanation of how to replace a lost passport is not caught.
_LOST_DOCUMENT_RE = re.compile(
    r"\b(?:утерян\w*|утеря\w*|потерян\w*|потерял\w*|найден\w*|нашл\w*)\s+"
    r"(?:[\w-]+\s+){0,3}"
    r"(?:документ\w*|удостоверени\w*|паспорт\w*|свидетельств\w*|полис\w*|снилс\w*|"
    r"пропуск\w*|водительск\w*\s+прав\w*|банковск\w*\s+карт\w*)",
    re.IGNORECASE,
)
_PRIVATE_RETURN_RE = re.compile(
    r"(?:(?:просьба|прошу|просим)\s+вернуть|верн(?:уть|ите)\s+за\s+вознагражд\w*|"
    r"\bв\s+личк\w*|\bв\s+лс\b|\+?\d[\d\s()-]{8,}\d)",
    re.IGNORECASE,
)


def is_non_editorial_fact(text: str) -> bool:
    """Recognize pure noise without vetoing factual reports on the same topic.

    Context markers are not topic bans or corroboration requirements. Check
    each evidence text separately so a private reply cannot delete a mixed
    Story. This also keeps noise out of required digest facts while leaving
    the original evidence and its provenance untouched.
    """
    if not text:
        return False
    if (
        _NON_EDITORIAL_PAYLOAD_RE.search(text)
        or (_LOST_DOCUMENT_RE.search(text) and _PRIVATE_RETURN_RE.search(text))
        or _is_location_context_without_event(text)
        or _is_announcement_context_without_event(text)
    ):
        return True

    markers = (_GIVEAWAY_RE, _DOCTOR_OPINION_RE, _ANSWER_CONTEXT_RE, _MONUMENT_SWITCH_RE)
    if not any(marker.search(text) for marker in markers):
        return False

    # Keep independent factual clauses in the same support, whatever their
    # theme. A school enrollment, fair or library update does not need to be
    # an operational utility report to survive a neighbouring chat opener.
    if any(
        not any(marker.search(clause) for marker in markers)
        and not _is_location_context_without_event(clause)
        and has_meaningful_predicate(clause)
        for clause in re.split(r"[.!?;:]\s*", text)
        if clause.strip()
    ):
        return False
    if _GIVEAWAY_RE.search(text) and _COMMUNITY_DISTRIBUTION_RE.search(text):
        return False

    residual = text
    for marker in markers:
        residual = marker.sub(" ", residual)
    # A where-question is context, but its answer may contain any useful
    # local update. Assess the answer with the existing predicate rule,
    # without requiring a utility topic or a particular punctuation style.
    if (
        _ANSWER_CONTEXT_RE.search(text)
        and not _is_location_context_without_event(residual)
        and has_meaningful_predicate(residual)
    ):
        return False
    # The opinion or unidentified switch itself must not count as the event.
    # Preserve concrete source-backed information alongside it, including a
    # practical public-service answer or access to a relief distribution.
    if _CONCRETE_EVENT_SIGNAL_RE.search(residual):
        return False
    if _PUBLIC_ACCESS_SUBJECT_RE.search(residual) and _PUBLIC_ACCESS_PREDICATE_RE.search(residual):
        return False
    if _MONUMENT_SWITCH_RE.search(text) and re.search(
        r"\b(?:подсвет\w*|освещ\w*|электр\w*)\b", text, re.IGNORECASE
    ):
        return False
    return True


def validate_story_publication_eligibility(
    payload: Any, fallback_text: str = "", edition_slug: str = "berdyansk"
) -> tuple[bool, str | None]:
    """Validate whether an event payload is eligible to produce a publishable story card."""
    if payload is None:
        if fallback_text and has_meaningful_predicate(fallback_text):
            return True, None
        return False, "empty_payload"

    evidence_items = getattr(payload, "evidence_items", ()) or ()
    if not evidence_items:
        # Fallback to headline / summary / key facts if no evidence items
        headline = getattr(payload, "headline", "") or ""
        summary = getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or ""
        key_facts = getattr(payload, "key_facts", ()) or ()
        texts = [headline, summary, fallback_text] + list(key_facts)
        if not any(has_meaningful_predicate(t) for t in texts if t):
            return False, "lacks_meaningful_predicate"
        return True, None

    publish_items = [
        item for item in evidence_items if getattr(item, "publication_use", "PUBLISH") != "EXCLUDE"
    ]
    if not publish_items:
        return False, "no_publishable_evidence"

    # Rule 1: resident_question cannot standalone produce a publishable story
    non_question_items = [
        item for item in publish_items if getattr(item, "kind", "") != "resident_question"
    ]
    if not non_question_items:
        return False, "resident_question_only"

    # A bare appeal or emotional warning is not a reportable event. Keep
    # practical warnings when the same evidence also contains a concrete
    # event (for example, an explosion, outage, repair, or evacuation).
    if non_question_items and all(
        _is_advice_without_event(getattr(item, "text", "")) for item in non_question_items
    ):
        return False, "advice_without_event"

    if non_question_items and all(
        _is_question_without_event(getattr(item, "text", "")) for item in non_question_items
    ):
        return False, "resident_question_only"

    # A bare answer about where a street, district, landmark, or facility is
    # located is conversational context, not a city update. Retain it when it
    # accompanies a concrete event/state, and never apply this rule to explicit
    # service-access evidence.
    if (
        non_question_items
        and not any(getattr(item, "kind", "") == "service_access" for item in non_question_items)
        and all(
            _is_location_context_without_event(getattr(item, "text", ""))
            for item in non_question_items
        )
    ):
        return False, "location_context_without_event"

    # Rule 2: Generic anonymous service check
    all_story_text = " ".join(
        [
            getattr(payload, "headline", "") or "",
            getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or "",
        ]
        + [getattr(item, "text", "") for item in non_question_items]
    ).lower()

    # Rule 2a: Exclude lost & found animals / pets and personal lost items
    if re.search(
        r"\b(?:нашли\s+собаку|найдена\s+собака|найденная\s+собака|найденной\s+собаке|найденную\s+собаку|"
        r"найденного\s+кота|найденный\s+кот|найденная\s+кошка|нашли\s+щенка|"
        r"ищ(?:ет|ут|ем)\s+хозяина|поиск\s+хозяев|поиски\s+хозяина|"
        r"(?:пропавш\w*|потерявш\w*|потерян\w*|потерял\w*|пропал\w*|найден\w*|нашл\w*|ищ[еу]т)\s+[^.!?]{0,30}(?:кошк\w*|собак\w*|кот[а-я]*|п[её]с\w*|щен\w*|питомц\w*)|"
        r"(?:кошк\w*|собак\w*|кот[а-я]*|п[её]с\w*|щен\w*|питомц\w*)[^.!?]{0,40}(?:пропавш\w*|потерявш\w*|потерян\w*|потерял\w*|пропал\w*|нашл\w*|найден\w*|видели\s+возле|сбежал\w*)|"
        r"потерял\s+рюкзак|оставил\s+рюкзак|потеряли\s+вещи)\b",
        all_story_text,
        re.IGNORECASE,
    ):
        return False, "lost_and_found_pet"

    if _NON_EDITORIAL_PAYLOAD_RE.search(all_story_text):
        return False, "non_editorial_payload"

    # Reject only a noise-only Story; do not let its headline upgrade pure
    # context into an event or let one irrelevant reply suppress useful facts.
    non_question_items = [
        item for item in non_question_items if not is_non_editorial_fact(getattr(item, "text", ""))
    ]
    if not non_question_items:
        return False, "non_editorial_payload"

    # Rule 2b: Check for external city events without focus anchors
    from src.domain.edition_geography import resolve_edition_geography
    from src.processing.edition_scope import _contains_any_anchor, _norm_geo_text

    geo = resolve_edition_geography(edition_slug or "berdyansk")
    focus_anchors = (*geo.target_locations, *geo.district_locations)
    norm_focus = {_norm_geo_text(a) for a in focus_anchors if a}
    out_of_scope = tuple(
        loc for loc in geo.out_of_scope_locations if loc and _norm_geo_text(loc) not in norm_focus
    )

    # Use evidence text for geographic checks to avoid hallucinated headlines bypassing the guard
    evidence_text = " ".join([getattr(item, "text", "") for item in non_question_items]).lower()
    evidence_has_focus = _contains_any_anchor(evidence_text, focus_anchors)
    evidence_has_out_of_scope = _contains_any_anchor(evidence_text, out_of_scope)

    if not evidence_has_focus and evidence_has_out_of_scope:
        return False, "external_city_without_focus_impact"

    # Rule 2c: Reject generic or unanchored military strikes without local focus
    _GENERIC_STRIKE_KEYWORDS_RE = re.compile(
        r"\b(?:авіабомб\w*|авиабомб\w*|умпк|fpv-дрон\w*|шахед\w*|обстріл\w*|обстрел\w*|ракет\w*|влучання|попадани\w*|ппо)\b",
        re.IGNORECASE,
    )
    if not evidence_has_focus and _GENERIC_STRIKE_KEYWORDS_RE.search(evidence_text):
        return False, "external_or_unanchored_strike"

    # A persisted Event-First revision may contain a fluent-looking headline
    # for a pure chat meta-line ("a resident mentions a district", "the chat
    # discusses a situation", etc.).  Such a line is not an event and must not
    # become a digest card merely because the verbs "сообщает" or "обсуждает"
    # satisfy the broad predicate guard.
    if _CHATTER_META_RE.search(all_story_text) and not _CONCRETE_EVENT_SIGNAL_RE.search(
        all_story_text
    ):
        return False, "lacks_meaningful_predicate"

    # "Something was switched off" is not a usable local report unless the
    # source identifies what changed.  A generated headline may still contain
    # the generic word "отключение", so check for the concrete service subject
    # rather than relying on the broad civic predicate regex.
    if re.search(r"\bчто[- ]то\s+(?:произошло|отключилось|случилось)\b", all_story_text):
        if not re.search(
            r"\b(?:свет\w*|электр\w*|вод\w*|газ\w*|отоплен\w*|интернет\w*|связ\w*)\b",
            all_story_text,
            re.IGNORECASE,
        ):
            return False, "lacks_meaningful_predicate"

    cat = getattr(payload, "category", "") or ""
    tags = {str(t).lower() for t in (getattr(payload, "tags", ()) or ())}
    has_recognized_domain = cat.lower() in RECOGNIZED_CORE_SERVICE_KEYS or bool(
        tags.intersection(RECOGNIZED_CORE_SERVICE_KEYS)
    )
    has_concrete_service_state = any(
        getattr(item, "kind", "") == "service_access"
        and not is_generic_service_entity(
            getattr(getattr(item, "service_state", None), "entity", ""),
            getattr(getattr(item, "service_state", None), "subject_label", ""),
            getattr(getattr(item, "service_state", None), "subject_key", ""),
        )
        for item in non_question_items
    )

    if not has_recognized_domain and not has_concrete_service_state:
        if ("вид сервиса" in all_story_text and "не уточняется" in all_story_text) or re.search(
            r"\b(?:проблемный\s+сервис|сторонний\s+сервис|городской\s+сервис)\b",
            all_story_text,
        ):
            return False, "service_access_without_concrete_entity"

        hl = getattr(payload, "headline", "") or ""
        if re.search(r"\bсервис\b", hl, re.IGNORECASE):
            named_tokens = {
                "водоканал",
                "горгаз",
                "россети",
                "банк",
                "связь",
                "интернет",
                "провайдер",
                "телеком",
                "автобус",
                "такси",
                "почта",
                "нотариус",
            }
            if not any(token in all_story_text for token in named_tokens):
                return False, "service_access_without_concrete_entity"

    service_items = [
        item for item in non_question_items if getattr(item, "kind", "") == "service_access"
    ]
    if service_items and len(service_items) == len(non_question_items):
        all_generic = True
        for s_item in service_items:
            s_state = getattr(s_item, "service_state", None)
            if s_state:
                ent = getattr(s_state, "entity", "")
                lbl = getattr(s_state, "subject_label", "")
                key = getattr(s_state, "subject_key", "")
                if not is_generic_service_entity(ent, lbl, key):
                    all_generic = False
                    break
            else:
                op_obs = getattr(payload, "operational_observations", ()) or ()
                found_valid_op = False
                for obs in op_obs:
                    o_key = getattr(obs, "subject_key", "") or ""
                    o_lbl = getattr(obs, "subject_label", "") or ""
                    o_ent = getattr(obs, "entity", "") or ""
                    if not is_generic_service_entity(o_ent, o_lbl, o_key):
                        found_valid_op = True
                        break
                if found_valid_op:
                    all_generic = False
                    break

                if has_recognized_domain:
                    all_generic = False
                    break

                text_to_test = getattr(s_item, "text", "") or ""
                hl = getattr(payload, "headline", "") or ""
                combined = f"{hl} {text_to_test}".lower()
                has_generic_word = any(w in combined for w in _GENERIC_ENTITY_WORDS)
                if not has_generic_word:
                    all_generic = False
                    break

        if all_generic:
            return False, "service_access_without_concrete_entity"

    # Rule 3: at least one substantive evidence item or story summary must contain a meaningful predicate.
    # Structured service_state is not sufficient by itself: corrupted reply metadata can
    # otherwise produce a publishable-looking operational card with no grounded event text.
    has_predicate = any(
        has_meaningful_predicate(getattr(item, "text", "")) for item in non_question_items
    )
    if not has_predicate:
        hl = getattr(payload, "headline", "") or ""
        sm = getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or ""
        if not has_meaningful_predicate(hl) and not has_meaningful_predicate(sm):
            return False, "lacks_meaningful_predicate"

    # Rule 4: Exclude pure conversational chatter lacking civic event/status (e.g. chat recipe recollections)
    if re.search(r"\b(?:рецепт\w*|кулинарн\w*)\b", all_story_text, re.IGNORECASE):
        if not any(
            re.search(
                r"\b(?:откры\w*|закры\w*|подорож\w*|подешев\w*|дефицит\w*|хлеб\w*|цен\w*)\b",
                getattr(item, "text", ""),
                re.IGNORECASE,
            )
            for item in non_question_items
        ):
            return False, "conversational_chatter_recipe"

    # Keep eligibility aligned with digest presentation.  Some stale
    # revisions have a predicate-bearing generated headline, while their only
    # evidence is a chat fragment that the reader-facing sanitizer removes.
    # Letting those revisions through creates a Story that has no TopicBundle
    # and later fails the deterministic Story partition invariant.
    from src.publication.digest_presentation import _is_usable_fact_line

    reader_texts = [
        getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or "",
    ] + [getattr(item, "text", "") or "" for item in non_question_items]
    key_facts = getattr(payload, "key_facts", ()) or ()
    reader_texts.extend(str(fact) for fact in key_facts if fact)
    hl = getattr(payload, "headline", "") or ""
    if hl and _CONCRETE_EVENT_SIGNAL_RE.search(hl) and has_meaningful_predicate(hl):
        reader_texts.append(hl)

    # A generated headline is often only a noun phrase ("Контакт скорой
    # помощи", "День города") and must not make an otherwise unusable
    # directory/chat payload eligible.  Eligibility needs a grounded reader
    # fact with an event/state predicate; concrete safety signals such as a
    # possible explosion or concrete outage are accepted even when the source
    # uses colloquial wording that lacks a standard verb.
    if not any(
        not is_non_editorial_fact(text)
        and (_is_usable_fact_line(text) or _CONCRETE_EVENT_SIGNAL_RE.search(text))
        and (has_meaningful_predicate(text) or _CONCRETE_EVENT_SIGNAL_RE.search(text))
        for text in reader_texts
        if text
    ):
        return False, "lacks_meaningful_predicate"

    return True, None
