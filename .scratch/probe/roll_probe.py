from types import SimpleNamespace as NS
from src.publication.article_quality import (
    _has_consecutive_direct_speech_roll as roll,
    _supported_non_speech_name_quote_spans as name_spans,
)

SOURCE = (
    "Вокруг вас везде Зирочка Дом, Арле Дом, 14 этажка, Пионерская нечетная сторона вся, "
    "Гайдара всё, Грация, Восьмуха, Кирова"
)
PARA = (
    "В нагорной части города на улицах Гайдара, Кирова и Пионерской, а также возле "
    "«Зирочки Дом», «Арле Дом», 14-этажки, «Грации» и «Восьмухи» окна светились подолгу. "
    "Один из жителей перечислял эти адреса как карту освещённости, другой уточнял: "
    "«Не начало Гайдара а все Гайдара, и четные и нечетные горят». Возле «Черепахи» свет был."
)
ctx = NS(support_by_id={
    "s1": NS(text=SOURCE, source_text=SOURCE, publication_use="PUBLISH", evidence_kind="community_report",
             temporal_role="CURRENT_WINDOW", reply_parent_context_text=""),
})
spans = name_spans(PARA, ["s1"], ctx, None)
print("name spans:", [PARA[a:b] for a, b in spans])
print("roll:", roll(PARA, spans))
print("roll without exclusion:", roll(PARA, ()))
