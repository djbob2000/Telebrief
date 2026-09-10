from src.processing.noise_detection import (
    classify_text_noise_or_exclusion,
    is_obvious_noise,
    is_question_only,
)


def test_uncertainty_is_obvious_noise():
    uncertainty_cases = [
        "Не знаю.",
        "не знаю",
        "хз",
        "без понятия",
        "кто его знает",
        "хз, сам в шоке",
        "никто не знает!",
    ]
    for text in uncertainty_cases:
        is_noise, reason = is_obvious_noise(text)
        assert is_noise, f"Expected noise for: {text!r}"
        assert reason == "obvious_noise"
        is_ex, ex_reason = classify_text_noise_or_exclusion(text)
        assert is_ex and ex_reason == "obvious_noise"


def test_question_only_preserved_for_gate_triage():
    question_cases = [
        "А возле Грации?",
        "А интернет как?",
        "до скольки работает кож-вен?",
        "Кто знает, где можно перевести документы с нотариальным заверением?",
        "Что со светом есть какие то новости? Хоть какие-то ?",
        "Подскажите где купить детский электросамокат",
        "Вам сообщение в личку не отсылается?",
        "А дроны чьи? Или кто то видел откуда они взлетают, что бы утверждать?",
        "Куйбышево было название раньше?",
        "Выросли цены?",
    ]
    for text in question_cases:
        assert is_question_only(text), f"Expected is_question_only for: {text!r}"
        is_noise, _ = is_obvious_noise(text)
        assert not is_noise, (
            f"Questions must reach Gate triage for context handling, not dropped: {text!r}"
        )
        is_ex, _ = classify_text_noise_or_exclusion(text)
        assert not is_ex, f"Questions must not be hard-excluded deterministically: {text!r}"


def test_short_concrete_assertions_are_preserved():
    assertions = [
        "Центр воду дали",
        "Азмол — нет",
        "в нас дощ",
        "Слободка есть вода",
        "На Пионерской электричества нет",
        "Маршрутка 4 ходит примерно раз в час",
        "На Гагарина 1 бесплатная зарядка",
        "В центре только в 9 утра связь воскресла",
    ]
    for text in assertions:
        assert not is_question_only(text), f"Did not expect question for: {text!r}"
        is_noise, _ = is_obvious_noise(text)
        assert not is_noise, f"Did not expect noise for legitimate assertion: {text!r}"
        is_ex, _ = classify_text_noise_or_exclusion(text)
        assert not is_ex, f"Did not expect exclusion for legitimate assertion: {text!r}"


def test_commercial_classified_freight_and_junk_hauling_excluded():
    freight_cases = [
        "🔴 ВЫВОЗ МУСОРА 🔴\n(строймусор,старая мебель,техника и любой другой хлам)\nГрузовые перевозки Бердянск и р-он.",
        "Вывоз строймусора, старой мебели, хлама. Грузоперевозки по городу и району.",
        "Замена водопровода, канализации, замена счетчиков под ключ. Звонить: +79900797078",
    ]
    for text in freight_cases:
        is_ex, reason = classify_text_noise_or_exclusion(text)
        assert is_ex, f"Expected exclusion for freight/hauling ad: {text!r}"
        assert reason in ("commercial_classified", "directory_payload")


def test_commercial_classified_passenger_transit_and_carpools_excluded():
    transit_cases = [
        "Поездка до Ростова 10 сентября числа на машине . Могу взять попутчиков.выезд 10 :00...тел..±79900312672",
        "Пассажирские перевозки Бердянск - Ростов - Москва. Ежедневные рейсы, комфортные микроавтобусы. Тел: +79901234567",
        "Возьму попутчиков до Мелитополя завтра утром. Выезд в 8:00, обращаться в лс.",
    ]
    for text in transit_cases:
        is_ex, reason = classify_text_noise_or_exclusion(text)
        assert is_ex, f"Expected exclusion for passenger transit/carpool ad: {text!r}"
        assert reason in ("commercial_classified", "directory_payload")


def test_directory_payload_medical_clinic_and_business_cards_excluded():
    directory_cases = [
        "**ВИЗАНТ**\n🏛 г. Бердянск, ул. Карла-Маркса 49 (бывшая ул. Центральная)\n🕗 Режим работы: Пн.-Сб. с 8:00 до 16:00, Вс. — выходной.",
        '🌟 Медицинский центр "ВИЗАНТ" — забота о вашем здоровье !\nУЗИ, ЭКГ, прием врачей. Адрес: ул. Тверская, 49. Режим работы: с 8:00 до 16:00.',
    ]
    for text in directory_cases:
        is_ex, reason = classify_text_noise_or_exclusion(text)
        assert is_ex, f"Expected exclusion for clinic directory card: {text!r}"
        assert reason in ("directory_payload", "commercial_classified")


def test_civic_reports_and_emergency_services_preserved():
    civic_cases = [
        "Водоканал ждет возврата генератора для работы насоса",
        "С любой острой болью в животе на приёмное отделение горбольницы, в семиэтажку.",
        "Автобус №4 ходит примерно раз в час",
        "В связи с нехваткой донорской крови просим откликнуться, центр крови работает с 7 до 13",
        "На маршрут №4 в Бердянске вышел новый автобус, стоимость проезда 17 рублей.",
    ]
    for text in civic_cases:
        is_ex, _ = classify_text_noise_or_exclusion(text)
        assert not is_ex, f"Civic report or medical emergency must NOT be excluded: {text!r}"


def test_intercity_carrier_and_grey_banking_excluded():
    ad_cases = [
        "Пассажирские перевозки через Мелитополь и Бердянск в Грузию, цена 450$. Бронирование по телефону.",
        "Рейсы в Крым и Ростов, комфортные автобусы, запись ведётся через busking.pro",
        "Разблокировка банковских карт Приват и Сбер, оформление ЕЦП. Обращаться в telegram: @endofmee_13 или +79900236421.",
        "Услуги по восстановлению доступа к онлайн-банкингу, помощь с картами.",
        "Спортшкола им. Назарова проводит набор детей в секцию дзюдо. Бесплатные тренировки для мальчиков и девочек.",
    ]
    for text in ad_cases:
        is_ex, reason = classify_text_noise_or_exclusion(text)
        assert is_ex, f"Expected exclusion for commercial/ad: {text!r}"
        assert reason in ("commercial_classified", "directory_payload")
