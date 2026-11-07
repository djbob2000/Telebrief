from src.publication.article_semantic_support import assess_semantic_support
from src.publication.article_claims import find_unsupported_claims

CASES = [
    ("ua-i-vs-ru-i", "Сообщила Іванова з вулиці Миру.", "Сообщила Иванова с улицы Мира."),
    ("ua-yi", "Про це написала Їжакова.", "Про это написала Ижакова."),
    ("ua-g-ghe", "Сказав Ґудзь з району.", "Сказал Гудзь из района."),
    ("apostrophe-drop", "Об этом сообщила Мар'їна з вулиці.", "Об этом сообщила Марїна з вулиці."),
    ("apostrophe-drop-2", "Житель Бур'ян написал.", "Житель Бурян написал."),
    ("inflected-name", "Сообщил Горбенко из района.", "Сообщил о Горбенко."),
    ("NEG-invented-name", "Сообщил житель района.", "Сообщил Петренко из района."),
    ("NEG-invented-number", "Свет дали на час.", "Свет дали на 3 часа."),
]
for label, support, writer in CASES:
    sem = assess_semantic_support(writer, [support])
    unsupported = find_unsupported_claims(writer, [support], direct_quote_source_texts=[support])
    print(f"{label:20} names={sem.blocking_proper_names} terms={sem.blocking_terms} claims={[c.kind+':'+c.raw for c in unsupported]}")
