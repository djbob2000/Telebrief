"""Conservative parsing and disposition for free-form article writer output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

ArticleWriterResponseDisposition = Literal["usable", "repairable", "unusable"]

_FENCE_LINE_RE = re.compile(r"^\s*```[\w+-]*\s*$")
_WHOLE_RESPONSE_REFUSAL_RE = re.compile(
    r"^(?:(?:к сожалению|извините)[,:]?\s*)?"
    r"(?:я\s+не\s+могу\s+(?:написать|создать|выполнить|составить)|"
    r"не\s+могу\s+(?:написать|создать|выполнить|составить)|"
    r"невозможно\s+(?:написать|создать|выполнить|составить)|"
    r"я\s+не\s+могу\s+(?:помочь|содействовать)\s+с\s+этим\s+запросом|"
    r"i\s+(?:cannot|can't)\s+(?:write|create|complete|fulfill|help\s+with\s+that\s+request))"
    r"(?:\s+(?:эту\s+статью|статью|этот\s+запрос|этот\s+текст|this\s+article|"
    r"this\s+request|the\s+requested\s+article|the\s+requested\s+text))?"
    r"[.!?…]?\s*$",
    re.IGNORECASE,
)
# Recovery needs positive recognition of every complete unit. These deliberately
# narrow procedural forms are not a vocabulary test: arbitrary trailing clauses,
# parentheticals, or unknown formulations remain eligible for normal assessment.
_MISSING_MATERIAL_CLAUSE = (
    r"в (?:переданном|предоставленном|исходном) материале (?:фактически )?"
    r"(?:отсутствует содержательное наполнение|нет (?:достаточных )?материалов|"
    r"отсутствуют (?:поддержанные записи|материалы))"
)
_PROCEDURAL_REFUSAL_OPENING_RE = re.compile(
    r"(?:(?:к сожалению|извините)[,:]?\s*)?"
    r"(?:(?:подготовить|написать|создать|составить) "
    r"(?:корректн(?:ую|ый|ое) )?(?:лонгрид|статью|текст) "
    r"(?:невозможно|нельзя|не получится)|"
    r"(?:я )?не (?:могу|смогу) (?:подготовить|написать|создать|составить) "
    r"(?:эту )?(?:статью|лонгрид|текст))"
    rf"(?::\s*(?P<missing>{_MISSING_MATERIAL_CLAUSE}))?[.!?…]?",
    re.IGNORECASE,
)
_PROCEDURAL_MISSING_MATERIAL_RE = re.compile(rf"{_MISSING_MATERIAL_CLAUSE}[.!?…]?", re.IGNORECASE)
# Proper names here are only arguments of an explicit editorial coverage scope;
# no unrestricted prose slot is permitted, including inside parentheses.
_COVERAGE_AREA_NAME = r"(?-i:[А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+)*)"
_PROCEDURAL_EXPLANATION_RE = re.compile(
    r"(?:после открывающего маркера идут только служебные фрагменты — "
    r"географический контекст(?: издания)?, правила композиции и навигационные подсказки"
    r"(?: \(упоминания номеров (?:стори|историй) и разделов\))?, "
    r"но ни одной поддержанной записи со сведениями о событиях, датах, местах их детализации|"
    r"писать статью без таких записей нельзя: это означало бы выдумку фактов от лица редакции|"
    r"географический справочник сам по себе не является доказательной основой "
    r"для публикационного текста — он лишь зада[её]т рамку покрытия"
    rf"(?: \(районы {_COVERAGE_AREA_NAME}, прилегающие с[её]ла "
    rf"{_COVERAGE_AREA_NAME} района, оговор[её]нные исключения\))?|"
    r"без (?:же )?подтверждаемых фактов любая попытка написания была бы равносильна "
    r"публикации недостоверной информации, чего рабочие процессы материалов "
    r"принципиально не допускают)[.!?…]?",
    re.IGNORECASE,
)
_PROCEDURAL_REQUEST_RE = re.compile(
    r"(?:(?:пришлите|предоставьте) (?:полный )?"
    r"(?:корпус материалов|пакет материалов|материалы)(?: для статьи|"
    r" между уже открытыми частями заявленного блока целиком, а не обрезанную версию "
    r"— включая содержательные части всех перечисленных сюжетных линий)?|"
    r"убедитесь, что внутри есть сами записи поддержки с временными метками "
    r"\(`observed_at`, `effective_from`, границы актуальности\) и характеристиками источника|"
    r"если предполагались цитаты жителей, проверьте, заполнен ли список разреш[её]нных "
    r"точных фраз(?: \(в текущем виде этот блок пуст\))?|"
    r"(?:по возможности )?уточните, какие из (?:двенадцати|\d+) тематических направлений "
    r"считаются ведущей(?:[-‑а-яёa-z]*| линией) — сейчас такой сигнал также остался "
    r"вне видимого сегмента материала)[.!?…]?",
    re.IGNORECASE,
)
_PROCEDURAL_PROMISE_RE = re.compile(
    r"после получения (?:полного )?пакета материалов смогу "
    r"(?:написать статью|собрать целостную городскую тему читаемого размера "
    r"с сохранением атрибуции источников, дат и отдельных деталей каждого места "
    r"— как и предусмотрено)[.!?…]?",
    re.IGNORECASE,
)
_PROCEDURAL_UNIT_SPLIT_RE = re.compile(r"(?<!\d[.!?…])(?<=[.!?…])\s+|;\s*(?=\d+[.)]\s+)")
_SENTENCE_END_RE = re.compile(r"[.!?…։]+$")
_HORIZONTAL_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_WHOLE_RESPONSE_SERVICE_PATTERNS = (
    re.compile(
        r"^(?:(?:http\s*)?(?:error\s*(?:code)?\s*[:#]?\s*)?)?"
        r"(?:408|413|429|500|502|503|504)"
        r"(?:\s*(?:-|:)\s*|\s+)?"
        r"(?:request timeout|payload too large|too many requests|internal server error|"
        r"bad gateway|service unavailable|gateway timeout)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:request failed with status code\s+(?:408|413|429|500|502|503|504)|"
        r"(?:internal server error|bad gateway|service unavailable|gateway timeout|"
        r"upstream(?: provider| server)? (?:error|unavailable|timeout)|too many requests|"
        r"rate limit exceeded|request timed out|the request timed out|"
        r"the server had an error while processing your request|"
        r"the ai service is currently unavailable|"
        r"(?:openrouter|provider|api) error\s*:\s*(?:rate limit exceeded|too many requests|"
        r"internal server error|bad gateway|service unavailable|gateway timeout)))\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:ошибка(?: сервера| провайдера| шлюза)?|сбой сервера|"
        r"сервис временно недоступен|слишком много запросов|превышен лимит запросов|"
        r"время ожидания запроса истекло|не удалось обработать запрос|"
        r"не удалось сгенерировать ответ из-за ошибки сервера|"
        r"не удалось обработать запрос из-за ошибки сервера|"
        r"ошибка при генерации ответа|ошибка при генерации статьи)"
        r"(?:\s*(?:-|:|—)\s*(?:408|413|429|500|502|503|504|ошибка сервера|"
        r"сервис временно недоступен|слишком много запросов|превышен лимит запросов))?\.?$",
        re.IGNORECASE,
    ),
)


def _normalized_response_body(lines: list[str]) -> str:
    visible_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or _FENCE_LINE_RE.fullmatch(line):
            continue
        line = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+)", "", line)
        if _HORIZONTAL_RULE_RE.fullmatch(line):
            continue
        visible_lines.append(line)
    return re.sub(r"\s+", " ", " ".join(visible_lines)).strip()


def _is_whole_response_service_message(text: str) -> bool:
    return any(pattern.fullmatch(text) for pattern in _WHOLE_RESPONSE_SERVICE_PATTERNS)


def _is_whole_response_procedural_refusal(blocks: list[str]) -> bool:
    """Reject only a completely recognized refusal, material gap, and input request."""
    units = [
        re.sub(r"^\s*(?:\d+[.)]\s*|[-*+]\s*)", "", unit.strip())
        for block in blocks
        for unit in _PROCEDURAL_UNIT_SPLIT_RE.split(block)
        if unit.strip()
    ]
    if len(units) < 2:
        return False

    opening = _PROCEDURAL_REFUSAL_OPENING_RE.fullmatch(units[0])
    if opening is None:
        return False

    has_missing_material = opening.group("missing") is not None
    has_request = False
    for unit in units[1:]:
        if _PROCEDURAL_REQUEST_RE.fullmatch(unit):
            has_request = True
        elif _PROCEDURAL_MISSING_MATERIAL_RE.fullmatch(unit):
            has_missing_material = True
        elif not (
            _PROCEDURAL_EXPLANATION_RE.fullmatch(unit)
            or _PROCEDURAL_PROMISE_RE.fullmatch(unit)
            or _WHOLE_RESPONSE_REFUSAL_RE.fullmatch(unit)
        ):
            # Any unaccounted-for prose may be reporting. Keep the complete
            # response for normal assessment rather than recover another slot.
            return False
    return has_missing_material and has_request


@dataclass(frozen=True)
class ArticleWriterResponse:
    """Parsed reader prose plus a conservative shape/recovery decision."""

    parsed: dict[str, Any]
    disposition: ArticleWriterResponseDisposition
    reason: str
    format_findings: tuple[str, ...] = ()


def parse_article_writer_markdown(response: str) -> ArticleWriterResponse:
    """Parse Markdown without promoting prose to an invented title or losing paragraphs."""
    cleaned = (response or "").strip()
    if not cleaned:
        return ArticleWriterResponse(
            parsed={"title": "", "lead": "", "sections": []},
            disposition="unusable",
            reason="empty_response",
            format_findings=("EMPTY_RESPONSE",),
        )

    lines = cleaned.splitlines()
    while lines and _FENCE_LINE_RE.fullmatch(lines[0]):
        lines.pop(0)
    while lines and _FENCE_LINE_RE.fullmatch(lines[-1]):
        lines.pop()

    if _is_whole_response_service_message(_normalized_response_body(lines)):
        return ArticleWriterResponse(
            parsed={"title": "", "lead": "", "sections": []},
            disposition="unusable",
            reason="whole_response_service_message",
            format_findings=("SERVICE_MESSAGE",),
        )

    title = ""
    lead_paragraphs: list[str] = []
    sections: list[dict[str, Any]] = []
    pre_heading_paragraphs: list[str] = []
    pre_heading_physical_line_counts: list[int] = []
    current_section: dict[str, Any] | None = None
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        physical_line_count = len(paragraph_lines)
        paragraph = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if not paragraph:
            return
        if current_section is None:
            pre_heading_paragraphs.append(paragraph)
            pre_heading_physical_line_counts.append(physical_line_count)
        else:
            current_section["paragraphs"].append(paragraph)

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if _HORIZONTAL_RULE_RE.fullmatch(line):
            flush_paragraph()
            continue
        if line.startswith("# "):
            flush_paragraph()
            if not title:
                title = line[2:].strip()
            continue
        if line.startswith("## ") or line.startswith("### "):
            flush_paragraph()
            heading = line.lstrip("#").strip()
            current_section = {"heading": heading, "paragraphs": []}
            sections.append(current_section)
            continue
        paragraph_lines.append(line)
    flush_paragraph()

    findings: list[str] = []
    if not title and sections and pre_heading_paragraphs:
        first = pre_heading_paragraphs[0]
        has_separate_lead = len(pre_heading_paragraphs) > 1
        if (
            pre_heading_physical_line_counts[0] == 1
            and len(first.split()) <= 14
            and not _SENTENCE_END_RE.search(first.rstrip('"”’)]}'))
            and has_separate_lead
        ):
            title = pre_heading_paragraphs.pop(0)
            findings.append("BARE_TITLE_NORMALIZED")

    if title and pre_heading_paragraphs:
        lead_paragraphs = [pre_heading_paragraphs.pop(0)]

    if not title:
        findings.append("MISSING_TITLE")
    if not lead_paragraphs:
        findings.append("MISSING_LEAD")
    if not sections:
        findings.append("MISSING_SECTIONS")

    # Preserve opening prose that cannot fit the lead field as an unnamed
    # internal section. ArticleSection permits an empty heading; rendering
    # emits only the original paragraphs, without adding a reader-facing label.
    if pre_heading_paragraphs:
        sections.insert(
            0,
            {
                "heading": "",
                "paragraphs": pre_heading_paragraphs,
            },
        )

    lead = "\n\n".join(lead_paragraphs)
    substantive_blocks = [
        title,
        lead,
        *(section["heading"] for section in sections),
        *(p for section in sections for p in section["paragraphs"]),
    ]
    substantive_blocks = [block for block in substantive_blocks if block.strip()]

    parsed = {"title": title, "lead": lead, "sections": sections}
    if substantive_blocks and all(
        not block.lstrip().startswith(("«", '"', "“", "‘"))
        and _WHOLE_RESPONSE_REFUSAL_RE.fullmatch(block)
        for block in substantive_blocks
    ):
        return ArticleWriterResponse(
            parsed=parsed,
            disposition="unusable",
            reason="whole_response_refusal",
            format_findings=(*findings, "WHOLE_RESPONSE_REFUSAL"),
        )

    if _is_whole_response_procedural_refusal(substantive_blocks):
        return ArticleWriterResponse(
            parsed=parsed,
            disposition="unusable",
            reason="whole_response_refusal",
            format_findings=(*findings, "WHOLE_RESPONSE_REFUSAL"),
        )

    if not substantive_blocks:
        return ArticleWriterResponse(
            parsed=parsed,
            disposition="unusable",
            reason="no_reader_prose",
            format_findings=(*findings, "NO_READER_PROSE"),
        )

    disposition: ArticleWriterResponseDisposition = "repairable" if findings else "usable"
    return ArticleWriterResponse(
        parsed=parsed,
        disposition=disposition,
        reason="format_repair_needed" if findings else "parsed_markdown",
        format_findings=tuple(findings),
    )
