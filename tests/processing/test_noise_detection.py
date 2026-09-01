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
        assert (
            not is_noise
        ), f"Questions must reach Gate triage for context handling, not dropped: {text!r}"
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
