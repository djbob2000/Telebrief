from src.publication.article_semantic_support import assess_semantic_support
from src.publication.article_claims import find_unsupported_claims

CASES = [
    # (label, support_text, writer_text)
    ("case", "На Горе света нет с утра.", "На ГОРЕ света нет с утра."),
    ("yo", "Ёлочная улица без света.", "Елочная улица без света."),
    ("apostrophe-drop", "Об этом сообщила Мар'їна з вулиці Центральної.", "Об этом сообщила Марїна з вулиці Центральної."),
    ("apostrophe-typo", "Знайомий Олександр Мар'яненко написав.", "Знайомий Олександр Мар’яненко написав."),
    ("short-name-inflection", "Света нет в АКЗ с утра.", "Света нет в АКЗе с утра."),
    ("ua-ru-surname", "Сообщил Горбенко из района.", "Сообщил Горбенка из района."),
    ("quote-marks", "Житель написал: «Свет дали на час».", "Житель написал: “Свет дали на час”."),
    ("quote-dash", "Житель написал: «Акз-ртс света нет».", "Житель написал: «Акз ртс света нет»."),
    ("quote-nbsp", "Житель написал: «Свет дали на час».", "Житель написал: «Свет дали на час»."),
    ("quote-yo", "Житель написал: «Всё ещё нет света».", "Житель написал: «Все еще нет света»."),
]

for label, support, writer in CASES:
    sem = assess_semantic_support(writer, [support])
    unsupported = find_unsupported_claims(writer, [support], direct_quote_source_texts=[support])
    print(f"{label:22} names={sem.blocking_proper_names} terms={sem.blocking_terms} crit={sem.blocking_critical_terms} claims={[c.kind+':'+c.raw for c in unsupported]}")
