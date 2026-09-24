"""Targeted editorial copy-editor and fact-checker for structured article drafts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from src.ai_providers import AIProvider
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_material import (
    ArticleMaterialProjection,
    materialize_article_validation_context,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_homoglyphs,
    _split_sentences_safe,
    _strip_internal_handles,
)
from src.publication.article_quality import (
    ARTICLE_WHOLE_DRAFT_FINDING_CODES,
    ArticleReaderQualityFinding,
    ArticleReaderQualityReport,
    diagnose_article_quality,
)
from src.publication.article_validator import ArticleValidationResult, validate_article_draft
from src.publication.article_writer_context import sanitize_writer_source_text

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _reground_support_ids(
    text: str,
    context: ArticleEditorialContext,
    allowed_support_ids: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """Return only support packets with a concrete lexical anchor in ``text``.

    Editor patches replace reader-facing prose, so citations from the old
    paragraph cannot be carried forward.  This deliberately has no
    best-match fallback: an unmatched patch must remain unsupported and be
    rejected by the normal fail-closed validator.
    """
    from src.publication.article_claims import _stem
    from src.publication.article_semantic_support import _EDITORIAL_GLUE, _STOPWORDS

    token_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

    def distinctive_stems(value: str) -> set[str]:
        stems: set[str] = set()
        for token in token_re.findall(value or ""):
            normalized = token.casefold().replace("ё", "е")
            if len(normalized) < 3 or normalized in _STOPWORDS:
                continue
            stem = _stem(normalized)
            if stem in _EDITORIAL_GLUE or normalized in _EDITORIAL_GLUE:
                continue
            stems.add(stem)
        return stems

    text_stems = distinctive_stems(text)
    text_numbers = set(re.findall(r"\b\d+\b", text))
    matched: list[str] = []
    allowed = set(allowed_support_ids) if allowed_support_ids is not None else None
    for support in context.supports:
        if allowed is not None and support.support_id not in allowed:
            continue
        if support.publication_use != "PUBLISH":
            continue
        support_text = f"{support.text} {support.source_text}"
        shared_stems = text_stems & distinctive_stems(support_text)
        support_numbers = set(re.findall(r"\b\d+\b", support_text))
        if (
            len(shared_stems) >= 2
            or (shared_stems and text_numbers & support_numbers)
            or len(text_numbers & support_numbers) >= 2
        ):
            matched.append(support.support_id)
    return tuple(dict.fromkeys(matched))


class ArticleEditor:
    """Targeted fact-checking editor that fixes isolated validation issues without full draft rewrite."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 32768,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = min(max_output_tokens, 32768)
        self.last_attempt_count = 0
        self.last_patched_unit_ids: tuple[str, ...] = ()

    async def edit_draft(
        self,
        draft: StructuredArticleDraft,
        validation_result: ArticleValidationResult,
        context: ArticleEditorialContext,
        *,
        config: Any | None = None,
        length_profile: Any | None = None,
        attempt_observer: Any | None = None,
        max_attempts: int = 2,
        quality_report: ArticleReaderQualityReport | None = None,
        coverage_plan: ArticleCoveragePlan | None = None,
        material_projection: ArticleMaterialProjection | None = None,
        place_resolver: Any | None = None,
    ) -> tuple[StructuredArticleDraft, ArticleValidationResult]:
        """Apply targeted editorial corrections to units with blocking validation issues."""
        current_draft = draft
        current_val = validation_result
        current_quality = quality_report or ArticleReaderQualityReport()
        patched_unit_ids: list[str] = []
        validation_context = (
            materialize_article_validation_context(context, material_projection)
            if material_projection is not None
            else context
        )

        for attempt in range(1, max_attempts + 1):
            self.last_attempt_count = attempt
            blocking_issues = [
                iss
                for iss in current_val.issues
                if iss.blocking and iss.unit_id not in ("DRAFT", "")
            ]
            quality_issues = [
                finding
                for finding in current_quality.repair_findings
                if finding.code not in ARTICLE_WHOLE_DRAFT_FINDING_CODES
            ]
            if not blocking_issues and not quality_issues:
                break

            # Group blocking issues by unit_id
            issues_by_unit: dict[str, list[Any]] = {}
            for iss in blocking_issues:
                issues_by_unit.setdefault(iss.unit_id, []).append(iss)
            for finding in quality_issues:
                if finding.unit_id not in ("DRAFT", ""):
                    issues_by_unit.setdefault(finding.unit_id, []).append(finding)

            logger.info(
                "ArticleEditor pass %d/%d targeting %d problematic unit(s): %s",
                attempt,
                max_attempts,
                len(issues_by_unit),
                list(issues_by_unit.keys()),
            )

            prompt_data = self._build_unit_contexts(
                current_draft,
                issues_by_unit,
                validation_context,
                material_projection=material_projection,
            )
            if not prompt_data:
                logger.warning("ArticleEditor could not build unit context for issues; stopping")
                break

            system_prompt = self._build_system_prompt()
            user_prompt = self._build_user_prompt(
                prompt_data, attempt=attempt, max_passes=max_attempts
            )

            obs_att_id = 0
            if attempt_observer is not None:
                obs_att_id = await attempt_observer.attempt_started(
                    "repair",
                    provider=self.provider.__class__.__name__,
                    model=self.model,
                    metadata={
                        "strategy": "article_editor",
                        "attempt": attempt,
                        "units": list(issues_by_unit.keys()),
                        "violations": [
                            f"{getattr(iss, 'code', 'QUALITY')}:{getattr(iss, 'unit_id', '')}"
                            for iss in (*blocking_issues, *quality_issues)
                        ],
                    },
                )

            try:
                response = await self.provider.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_output_tokens,
                    reasoning_effort="none",
                    response_format={"type": "json_object"},
                )
                patches = self._parse_editor_response(response)
                requested_units = {unit["unit_id"] for unit in prompt_data}
                patches = {
                    unit_id: value
                    for unit_id, value in patches.items()
                    if unit_id in requested_units
                }
                if not patches:
                    logger.warning("ArticleEditor returned no valid unit patches")
                    if attempt_observer is not None:
                        await attempt_observer.attempt_finished(
                            obs_att_id, "failed", error_kind="empty_patches"
                        )
                    break

                current_draft = self.apply_patches(
                    current_draft,
                    patches,
                    context=validation_context,
                    preserve_unmatched_supports=False,
                    allowed_support_ids_by_unit={
                        unit["unit_id"]: tuple(unit["support_ids"]) for unit in prompt_data
                    },
                )
                patched_unit_ids.extend(patches)
                self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                current_val = validate_article_draft(
                    current_draft,
                    context,
                    config=config,
                    length_profile=length_profile,
                    material_projection=material_projection,
                )
                if coverage_plan is not None:
                    current_quality = diagnose_article_quality(
                        current_draft,
                        coverage_plan,
                        context,
                        material_projection=material_projection,
                        place_resolver=place_resolver,
                    )

                if attempt_observer is not None:
                    is_clean = current_val.is_valid and not current_quality.needs_edit
                    status = "succeeded" if is_clean else "failed"
                    error_kind = None if is_clean else "remaining_violations"
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        status,
                        error_kind=error_kind,
                        metadata={
                            "editor_status": "succeeded" if is_clean else "partial",
                            "patched_units": list(patches.keys()),
                            "remaining_violations": list(current_val.violations),
                            "remaining_quality_findings": [
                                f"{finding.code}:{finding.unit_id}"
                                for finding in current_quality.repair_findings
                            ],
                        },
                    )

                if current_val.is_valid and not current_quality.needs_edit:
                    logger.info("ArticleEditor successfully resolved all validation issues!")
                    break
                else:
                    remaining_issues = [
                        f"{issue.code}:{issue.unit_id}"
                        for issue in current_val.issues
                        if issue.blocking
                    ] + [
                        f"{finding.code}:{finding.unit_id}"
                        for finding in current_quality.repair_findings
                    ]
                    logger.warning(
                        "ArticleEditor pass %d left remaining issues: %s",
                        attempt,
                        remaining_issues[:10],
                    )

            except Exception as exc:
                logger.warning("ArticleEditor pass %d encountered error: %s", attempt, exc)
                if attempt_observer is not None:
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        "failed",
                        error_kind=type(exc).__name__,
                    )
                break

        return current_draft, current_val

    def _build_unit_contexts(
        self,
        draft: StructuredArticleDraft,
        issues_by_unit: Mapping[str, list[Any]],
        context: ArticleEditorialContext,
        *,
        material_projection: ArticleMaterialProjection | None = None,
    ) -> list[dict[str, Any]]:
        """Collect current text, cited supports, and issues for each target unit."""
        unit_data: list[dict[str, Any]] = []

        def support_text(support_id: str) -> str:
            support = context.support_by_id.get(support_id)
            if support is None:
                return ""
            fact = (support.text or "").strip()
            if material_projection is not None:
                if (
                    material_projection.actions_by_support_id.get(support_id)
                    == "SUPPRESS_PROMOTION_ONLY"
                ):
                    return ""
                projected = material_projection.text_by_support_id.get(support_id, "").strip()
                if not projected:
                    return ""
                return projected
            source = sanitize_writer_source_text((support.source_text or "").strip())
            if source and source != fact:
                return f"{fact}\nПервичный источник: {source}" if fact else source
            return fact or source

        # Index units across draft
        # 1. Title
        def unit_supports(
            unit_ids: list[str],
            claim_ids: list[str],
            unit_issues: list[Any],
        ) -> list[str]:
            # Required support IDs from a quality finding must appear first so
            # the prompt's bounded five-support display cannot hide the exact
            # evidence that the editor is asked to restore.
            ids: list[str] = []
            for issue in unit_issues:
                ids.extend(getattr(issue, "support_ids", ()) or ())
            ids.extend(unit_ids)
            ids.extend(claim_ids)
            return list(dict.fromkeys(ids))

        if "TITLE" in issues_by_unit:
            t_sups = unit_supports(
                list(draft.title_support_ids),
                [sid for claim in draft.title_claims for sid in claim.cited_support_ids],
                issues_by_unit["TITLE"],
            )
            unit_data.append(
                {
                    "unit_id": "TITLE",
                    "unit_type": "title",
                    "text": draft.title,
                    "support_ids": t_sups,
                    "supports": [
                        rendered
                        for sid in t_sups
                        if sid in context.support_by_id
                        for rendered in (support_text(sid),)
                        if rendered
                    ],
                    "issues": issues_by_unit["TITLE"],
                }
            )

        # 2. Lead
        if "LEAD" in issues_by_unit:
            lead_sups = unit_supports(
                list(draft.lead_support_ids),
                [sid for claim in draft.lead_claims for sid in claim.cited_support_ids],
                issues_by_unit["LEAD"],
            )
            unit_data.append(
                {
                    "unit_id": "LEAD",
                    "unit_type": "lead",
                    "text": draft.lead,
                    "support_ids": lead_sups,
                    "supports": [
                        rendered
                        for sid in lead_sups
                        if sid in context.support_by_id
                        for rendered in (support_text(sid),)
                        if rendered
                    ],
                    "issues": issues_by_unit["LEAD"],
                }
            )

        # 3. Sections (Headings and Paragraphs)
        p_idx = 1
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            if h_id in issues_by_unit:
                h_sups = unit_supports(
                    list(sec.heading_support_ids),
                    [sid for claim in sec.heading_claims for sid in claim.cited_support_ids],
                    issues_by_unit[h_id],
                )
                unit_data.append(
                    {
                        "unit_id": h_id,
                        "unit_type": "heading",
                        "text": sec.heading,
                        "reader_context": {
                            "article_title": draft.title,
                            "other_section_headings": tuple(
                                other_section.heading
                                for other_index, other_section in enumerate(draft.sections, start=1)
                                if other_index != s_idx
                            ),
                        },
                        "support_ids": h_sups,
                        "supports": [
                            rendered
                            for sid in h_sups
                            if sid in context.support_by_id
                            for rendered in (support_text(sid),)
                            if rendered
                        ],
                        "issues": issues_by_unit[h_id],
                    }
                )

            for paragraph_index, p in enumerate(sec.paragraphs):
                p_id = f"P{p_idx:03d}"
                if p_id in issues_by_unit:
                    p_sups = unit_supports(
                        list(p.cited_support_ids),
                        [sid for claim in p.claims for sid in claim.cited_support_ids],
                        issues_by_unit[p_id],
                    )
                    reader_context = {
                        "section_heading": sec.heading,
                        "previous_paragraph": (
                            sec.paragraphs[paragraph_index - 1].text if paragraph_index > 0 else ""
                        ),
                        "next_paragraph": (
                            sec.paragraphs[paragraph_index + 1].text
                            if paragraph_index + 1 < len(sec.paragraphs)
                            else ""
                        ),
                    }
                    unit_data.append(
                        {
                            "unit_id": p_id,
                            "unit_type": "paragraph",
                            "text": p.text,
                            "support_ids": p_sups,
                            "supports": [
                                rendered
                                for sid in p_sups
                                if sid in context.support_by_id
                                for rendered in (support_text(sid),)
                                if rendered
                            ],
                            "issues": issues_by_unit[p_id],
                            "reader_context": reader_context,
                        }
                    )
                p_idx += 1

        return unit_data

    def _build_system_prompt(self) -> str:
        return (
            "Вы — главный выпускающий редактор (Senior Fact-Checking Copy Editor) новостной редакции.\n"
            "Ваша задача — ТОЧЕЧНО отредактировать несколько фрагментов статьи, к которым у службы проверки фактов возникли строгие замечания.\n\n"
            "ПРАВИЛА РЕДАКТИРОВАНИЯ:\n"
            "1. ПРЯМАЯ РЕЧЬ И КАВЫЧКИ (UNSUPPORTED_DIRECT_QUOTE):\n"
            "   - Запрещено оставлять кавычки «...» вокруг слов или фраз, если они не являются 100% дословной цитатой из предоставленных фактов.\n"
            "   - Переведите фразу в естественную косвенную речь БЕЗ КАВЫЧЕК через союз «что» со строчной буквы (например: «житель признался, что...», «горожане отмечают, что...»).\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО оставлять двоеточие перед текстом без кавычек (например: «житель признался: Звук генераторов...» — это грубая грамматическая ошибка).\n\n"
            "2. ИМЕНА СОБСТВЕННЫЕ И ТОПОНИМЫ (UNSUPPORTED_PROPER_NAME / UNSUPPORTED_LOCATION):\n"
            "   - Если имя, аббревиатура, название стороннего города или организации выдуманы и отсутствуют в подтверждениях ниже — удалите их.\n"
            "   - СОХРАНЕНИЕ ПОДТВЕРЖДЕННЫХ ТОПОНИМОВ И ОРИЕНТИРОВ (AGENTS.md 0.4): Если название района, улицы, ориентира (например, Лиски, район Химиков, супермаркет «Зеркальный») присутствует в источниках ниже — ОБЯЗАТЕЛЬНО СОХРАНЯЙТЕ его! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО заменять подтвержденные топонимы абстрактными клише вроде «в одном из районов города» или «в неназванном месте».\n"
            "   - Если слово с заглавной буквы не в начале предложения отмечено как неподтвержденное, но сам объект есть в источниках, переведите его в строчные буквы (например, «военный городок»).\n\n"
            "3. КОНКРЕТНЫЕ ФАКТЫ И ЧИСЛА (UNSUPPORTED_CONCRETE_CLAIM):\n"
            "   - Если цифра, цена, процент или дата выдуманы и отсутствуют в источниках — удалите неподтвержденную цифру.\n"
            "   - Если же конкретная деталь (цена, скидка, время, напряжение вольт, этаж, возраст детей) ЕСТЬ в источниках ниже — ОБЯЗАТЕЛЬНО СОХРАНЯЙТЕ её! Запрещено выхолащивать подтвержденные факты в общие фразы.\n\n"
            "4. ПРИЧИННО-СЛЕДСТВЕННЫЕ СВЯЗИ (UNSUPPORTED_CAUSAL_RELATION):\n"
            "   - Запрещено утверждать причинно-следственные связи («из-за аварии», «вследствие чего», «по причине», «связано с тем, что»), если механизм прямо не подтвержден. Замените на нейтральное связывание фактов («в этот же период...», «наряду с этим...», «также в городе...»).\n\n"
            "5. КРИТИЧЕСКИЕ ТЕМЫ И ДОМЕНЫ (UNSUPPORTED_CRITICAL_TERM):\n"
            "   - Если валидатор указывает неподтвержденные критические концепции (например, «топливо», «горючее», «заправки», «бензин»), ВЫ ДОЛЖНЫ ПОЛНОСТЬЮ УДАЛИТЬ эти понятия и предложения из текста фрагмента, переписав его строго по подтвержденным фактам.\n\n"
            "6. НЕДОСТАТОЧНАЯ ПОДДЕРЖКА (UNSUPPORTED_CLAIM_ATOM):\n"
            "   - Замечание указывает, что в конкретном предложении есть утверждение или лексика, недостаточно подтвержденная источниками.\n"
            "   - ВАЖНО: ВЫПОЛНИТЕ ТОЧЕЧНУЮ КОРРЕКТИРОВКУ! Исправьте или перефразируйте именно проблемное утверждение, удалив неподтвержденные слова и домыслы.\n"
            "   - НЕ дублируйте факты, уже изложенные в соседних абзацах. Не добавляйте лишних обобщающих предложений. Опирайтесь только на факты, относящиеся к этому конкретному фрагменту.\n"
            "   - Категорически запрещено удалять весь абзац ([DELETE]), если в нем есть подтвержденная информация.\n\n"
            "7. ОТСУТСТВИЕ ИСТОЧНИКОВ (MISSING_SUPPORT):\n"
            '   - Только если к фрагменту вообще нет никаких подтверждающих фактов в источниках — верните пустую строку "" или "[DELETE]", чтобы удалить этот неподтвержденный фрагмент.\n\n'
            "8. ЧАТОВАЯ КУХНЯ И ЖАРГОН (CHAT_KITCHEN_LEAK):\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать слова чатовой кухни («перекличка», «в перекличках», «в чате», «в каналах», «в пабликах», «в группах», «в комментариях» и т.п.).\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать разговорный и сетевой сленг («фигня», «хрень», «херня», «хреново», «нафиг», «пофиг» и т.п.). Даже если в источниках жители выражаются неформально, в тексте статьи переводите их в литературный язык («сохраняются перебои», «трудности», «проблемы»).\n"
            "   - Замените их нейтральным описанием ситуации от сути события или стандартной городской журналистской атрибуцией («по сообщениям жителей», «горожане отмечают», «картина обратная»).\n\n"
            "9. ОБЪЕМ, СТИЛЬ И СОХРАННОСТЬ:\n"
            "   - Сохраняйте естественный журналистский стиль и связность с остальным текстом статьи.\n"
            "   - Не добавляйте никаких новых фактов или деталей, которых нет в предоставленных подтверждениях.\n"
            "   - Не переписывайте заново весь абзац, если нужно исправить только одну фразу или слово.\n"
            "   - Отредактируйте ТОЛЬКО запрошенные фрагменты.\n\n"
            "10. ПОВТОРЫ И ЗАЦИКЛИВАНИЕ (REPEATED_CONTENT_LOOP):\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО повторять одно и то же или почти идентичное предложение несколько раз подряд. Если абзац зациклился — оставьте мысль ровно один раз в грамотной формулировке и удалите повторы.\n\n"
            "11. СООТВЕТСТВИЕ ПОДТЕМ В ЗАГОЛОВКАХ РАЗДЕЛОВ (PHANTOM_HEADING_TOPIC):\n"
            "   - Если заголовок раздела содержит конкретное перечисление подтем после двоеточия (например: «Тема: подтема А, подтема Б и подтема В»), текст абзацев раздела ОБЯЗАН раскрывать ВСЕ перечисленные подтемы. Запрещено анонсировать темы, о которых в абзацах нет ни слова. Если тема не освещена — удалите её из заголовка или скорректируйте заголовок раздела.\n\n"
            "ФОРМАТ ОТВЕТА (строго валидный JSON):\n"
            "{\n"
            '  "units": {\n'
            '    "<unit_id>": "Исправленный текст фрагмента...",\n'
            "    ...\n"
            "  }\n"
            "}"
        )

    def _build_user_prompt(
        self,
        unit_contexts: list[dict[str, Any]],
        attempt: int = 1,
        max_passes: int = 3,
    ) -> str:
        is_final_pass = attempt >= max_passes
        blocks: list[str] = ["ФРАГМЕНТЫ ДЛЯ РЕДАКТИРОВАНИЯ И ЗАМЕЧАНИЯ ФАКТ-ЧЕКИНГА:\n"]
        if is_final_pass:
            blocks.append(
                "⚠️ ВНИМАНИЕ: Это ФИНАЛЬНЫЙ проход редактора. Любая нерешенная ошибка приведет к отклонению всей статьи! "
                "Если в абзаце есть неподтвержденные детали — аккуратно замените их подтвержденными фактами из источников ниже.\n"
            )

        for u in unit_contexts:
            uid = u["unit_id"]
            utype = u["unit_type"]
            text = u["text"]
            issues = u["issues"]
            supports = u["supports"]

            blocks.append("════════════════════════════════════════")
            blocks.append(f"ФРАГМЕНТ [{uid}] (тип: {utype})")
            blocks.append(f"Текущий текст:\n{text}\n")
            reader_context = u.get("reader_context") or {}
            if any(reader_context.values()):
                blocks.append(
                    "Неизменяемый контекст для связности (эти строки нельзя редактировать; "
                    "исправляйте только целевой фрагмент):"
                )
                if reader_context.get("section_heading"):
                    blocks.append(f"  Заголовок раздела: {reader_context['section_heading']}")
                if reader_context.get("article_title"):
                    blocks.append(f"  Заголовок статьи: {reader_context['article_title']}")
                for heading in reader_context.get("other_section_headings", ()):
                    blocks.append(f"  Заголовок другой главы: {heading}")
                if reader_context.get("previous_paragraph"):
                    blocks.append(f"  Предыдущий абзац: {reader_context['previous_paragraph']}")
                if reader_context.get("next_paragraph"):
                    blocks.append(f"  Следующий абзац: {reader_context['next_paragraph']}")
            blocks.append("Замечания валидатора:")
            if utype == "title":
                blocks.append(
                    "  ⚠️ ВНИМАНИЕ ДЛЯ ЗАГОЛОВКА (TITLE): Заголовок ОБЯЗАН быть в статье (ЗАПРЕЩЕНО возвращать [DELETE]!). "
                    "Удалите любые конкретные цифры, даты, проценты и неподтвержденные названия. "
                    "Напишите общий заголовок о ситуации в городе (например: «Ситуация со светом и городские будни Бердянска»)."
                )
            elif utype == "lead":
                blocks.append(
                    "  ⚠️ ВНИМАНИЕ ДЛЯ ЛИДА (LEAD): Вводный абзац ОБЯЗАН быть в статье (ЗАПРЕЩЕНО возвращать [DELETE] или пустую строку!). "
                    "Напишите емкий вводный абзац (2-3 предложения), обобщающий общую картину дня строго по предоставленным источникам ниже."
                )
            for iss in issues:
                is_quality = isinstance(iss, ArticleReaderQualityFinding)
                msg = f"  • [{'READER_QUALITY' if is_quality else 'FACTUAL'}:{iss.code}] {iss.message}"
                if is_quality:
                    msg += f" (severity={iss.severity})"
                    if iss.support_ids:
                        msg += (
                            " -> Сохраните подтверждённые детали и опирайтесь именно на support IDs: "
                            + ", ".join(iss.support_ids)
                        )
                    msg += self._quality_repair_instruction(iss.code)
                    blocks.append(msg)
                    continue
                if iss.code == "UNSUPPORTED_CLAIM_ATOM":
                    if utype in ("title", "lead"):
                        msg += " -> ВАЖНО: перепишите предложение строго по фактам из источников ниже, сохраняя связность!"
                    else:
                        msg += " -> ВАЖНО: перепишите текст фрагмента строго по фактам из источников ниже, удалив любые неподтвержденные домыслы или детали. Сохраняйте абзац, НЕ удаляйте его через [DELETE]!"
                elif iss.code.startswith("MISSING_SUPPORT"):
                    if utype in ("title", "lead"):
                        msg += " -> ВАЖНО: перепишите текст строго по фактам из источников ниже, не удаляя фрагмент!"
                    elif supports:
                        msg += " -> ВАЖНО: перепишите текст фрагмента строго по фактам из источников ниже, не удаляя абзац!"
                    else:
                        msg += ' -> КРИТИЧЕСКИ ВАЖНО: У этого фрагмента НЕТ подтверждающих фактов в источниках. Верните "" или "[DELETE]", чтобы полностью удалить его!'
                elif iss.code == "UNSUPPORTED_CONCRETE_CLAIM":
                    msg += " -> ВАЖНО: полностью удалите указанную неподтвержденную цифру/деталь/срок из текста, либо удалите предложение с ней!"
                elif iss.code in ("UNSUPPORTED_PROPER_NAME", "UNSUPPORTED_CRITICAL_TERM"):
                    msg += (
                        " -> ВАЖНО: полностью удалите указанное слово/термин из текста фрагмента!"
                    )
                elif iss.code == "CHAT_KITCHEN_LEAK":
                    msg += " -> ВАЖНО: полностью удалите слова чатовой кухни («перекличка», «в чате» и т.п.) либо разговорный сленг и перепишите фразу через нормальный литературный язык и городскую атрибуцию («по сообщениям жителей», «ситуация обратная» и т.п.)!"
                elif iss.code == "REPEATED_CONTENT_LOOP":
                    msg += " -> ВАЖНО: удалите дублирующиеся одинаковые предложения, оставив мысль ровно один раз!"
                elif iss.code == "INVALID_SUPPORT_POLICY":
                    msg += " -> ВАЖНО: заголовок раздела или лид обязан опираться на подтвержденные факты с публикацией (PUBLISH) текущего дня. Сформулируйте заголовок раздела строго по фактам из абзацев этого раздела!"
                elif iss.code == "QUESTION_CONTEXT_OVERCLAIM":
                    msg += " -> ВАЖНО: запрещено утверждать вопрос жителей из чата как установленный факт. Используйте вопросительную или исследовательскую формулировку («Что известно о...», «Вопросы жителей о...») либо перепишите по реальным подтвержденным фактам!"
                elif iss.code == "HISTORICAL_CONTEXT_UNFRAMED":
                    msg += " -> ВАЖНО: если событие длится уже несколько дней или произошло ранее, обязательно добавьте маркер продолжения («по-прежнему», «продолжаются», «сохраняются») либо сфокусируйте формулировку строго на событиях сегодняшнего дня!"
                elif iss.code == "PHANTOM_HEADING_TOPIC":
                    msg += " -> ВАЖНО: скорректируйте заголовок раздела, удалив из перечисления после двоеточия темы, которые фактически не освещены в тексте абзацев!"
                elif iss.code == "LEAKED_META_OMISSION":
                    msg += " -> ВАЖНО: полностью удалите любые мета-комментарии об опущенных контактах/телефонах!"
                blocks.append(msg)

            if supports:
                blocks.append("\nПодтверждающие факты (источники):")
                # A roster repair needs the complete evidence set.  The old
                # five-line budget hid later places/states from the editor,
                # which made it impossible to produce a faithful localized
                # contrast when the finding carried more than five supports.
                has_roster_finding = any(
                    getattr(issue, "code", "") == "OVERLOADED_ROSTER_PARAGRAPH" for issue in issues
                )
                support_lines = supports if has_roster_finding else supports[:5]
                for s_text in support_lines:
                    blocks.append(f"  - {s_text}")

                # Check topical overlap using stemming
                from src.publication.article_claims import _stem

                tok_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
                text_stems = {_stem(w.lower()) for w in tok_re.findall(text) if len(w) >= 3}
                support_stems = set()
                for s_text in supports:
                    support_stems |= {
                        _stem(w.lower()) for w in tok_re.findall(s_text) if len(w) >= 3
                    }
                if text_stems and support_stems and not (text_stems & support_stems):
                    blocks.append(
                        "\n⚠️ ВНИМАНИЕ: Текущий текст недостаточно согласован с источниками. "
                        "Перепишите этот абзац заново (2–3 предложения), опираясь строго на факты из источников ниже. "
                        "НЕ удаляйте абзац!"
                    )
            else:
                blocks.append(
                    '\n(Подтверждающих фактов в источниках нет — верните "" или "[DELETE]", чтобы удалить фрагмент)'
                )
            blocks.append("")

        blocks.append(
            "Верните валидный JSON вида:\n"
            "{\n"
            '  "units": {\n'
            '    "P007": "исправленный текст абзаца...",\n'
            '    "LEAD": "исправленный текст лида..."\n'
            "  }\n"
            "}"
        )
        return "\n".join(blocks)

    @staticmethod
    def _quality_repair_instruction(code: str) -> str:
        instructions = {
            "OVERLOADED_ROSTER_PARAGRAPH": (
                " -> Перепишите весь целевой абзац, используя все подтверждающие факты ниже. "
                "Объединяйте наблюдения только в поддержанное локальное сравнение или временную "
                "последовательность; если такой связи в источниках нет, используйте нейтральную "
                "связку и сохраните факты раздельно. Сохраните важные поддержанные исключения и "
                "конкретные различия, уберите только повторный перечень; не придумывайте контраст, "
                "хронологию, причины или детали и не переносите состояние между местами."
            ),
            "CROSS_SECTION_REPETITION": (
                " -> Оставьте повторяющееся утверждение в части, где оно лучше всего подтверждено; "
                "в этом целевом фрагменте удалите повтор или сохраните только новое поддержанное "
                "состояние, время либо последствие. Соседние части статьи не редактируйте."
            ),
            "DUPLICATE_ARTICLE_HEADING": (
                " -> Локально уточните только этот заголовок по подтверждённому содержанию раздела, "
                "чтобы он отличался от заголовка статьи и других глав."
            ),
            "UNDEVELOPED_LEAD_PROMISE": (
                " -> Локально исправьте лид: уберите обещание, которое основной текст не раскрывает, "
                "или сформулируйте его только в пределах подтверждённого материала. Не добавляйте "
                "новые утверждения и не переписывайте тело статьи."
            ),
            "ARTICLE_INVENTORY_RHYTHM": (
                " -> Свяжите соседние короткие сюжеты естественным переходом и сохраните их "
                "конкретные детали; не превращайте абзац в перечень и не добавляйте факты."
            ),
            "QUOTE_ROLL_PARAGRAPH": (
                " -> Перескажите все сообщения плавной косвенной речью и полностью уберите прямую речь. "
                "В исправленном абзаце не используйте кавычки совсем. Сохраните подтверждённые детали "
                "и не меняйте их смысл."
            ),
            "CONTRADICTORY_SERVICE_STATE": (
                " -> Передайте подтверждённое локальное различие для одной услуги, места и времени "
                "как явный контраст. Не обобщайте состояние на весь город, не выводите причину и "
                "не переносите состояние между адресами."
            ),
        }
        return instructions.get(code, "")

    def _parse_editor_response(self, response: str) -> dict[str, str]:
        """Extract unit_id -> edited_text mapping from model response."""
        cleaned = (response or "").strip()
        m = _JSON_BLOCK_RE.search(cleaned)
        if m:
            cleaned = m.group(1)
        elif cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            data = json.loads(cleaned)
        except Exception:
            s_idx = cleaned.find("{")
            e_idx = cleaned.rfind("}")
            if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                try:
                    data = json.loads(cleaned[s_idx : e_idx + 1])
                except Exception:
                    return {}
            else:
                return {}

        raw_units = data.get("units") if isinstance(data, dict) else None
        if not isinstance(raw_units, dict):
            raw_units = data if isinstance(data, dict) else {}

        patches: dict[str, str] = {}
        for k, v in raw_units.items():
            if isinstance(k, str) and isinstance(v, str):
                patches[k.strip()] = _normalize_homoglyphs(v.strip())
            elif isinstance(k, str) and isinstance(v, dict) and "text" in v:
                patches[k.strip()] = _normalize_homoglyphs(str(v["text"]).strip())
            elif isinstance(k, str) and v is None:
                patches[k.strip()] = ""

        return patches

    def apply_patches(
        self,
        draft: StructuredArticleDraft,
        patches: Mapping[str, str],
        context: ArticleEditorialContext | None = None,
        *,
        preserve_unmatched_supports: bool = False,
        allowed_support_ids_by_unit: Mapping[str, tuple[str, ...]] | None = None,
    ) -> StructuredArticleDraft:
        """Apply targeted text patches while preserving structure and valid provenance.

        Projected validation contexts intentionally omit suppressed material.  In
        every mode, replacement prose must be strictly re-grounded before the
        patch is accepted. A failed or unavailable re-grounding rejects the
        patch, leaving the original text and its original provenance for final
        validation. The legacy ``preserve_unmatched_supports`` argument remains
        accepted for call compatibility, but cannot authorize citations for new
        prose.
        """
        if not patches:
            return draft

        def allowed_ids(unit_id: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            if allowed_support_ids_by_unit is not None:
                return tuple(allowed_support_ids_by_unit.get(unit_id, ()))
            return tuple(fallback)

        title_sups = draft.title_support_ids
        if not title_sups and allowed_support_ids_by_unit is None:
            title_sups = draft.lead_support_ids or (
                draft.sections[0].heading_support_ids if draft.sections else ()
            )

        title = draft.title
        title_claims = draft.title_claims
        if "TITLE" in patches:
            raw_t = patches["TITLE"].strip()
            if raw_t.upper() not in ("", "[DELETE]", "DELETE", "NONE", "NULL", "[УДАЛИТЬ]"):
                candidate_title = _normalize_homoglyphs(_strip_internal_handles(raw_t))
                regrounded_title_sups = (
                    _reground_support_ids(
                        candidate_title, context, allowed_ids("TITLE", tuple(title_sups))
                    )
                    if context is not None
                    else ()
                )
                if regrounded_title_sups:
                    title = candidate_title
                    title_sups = regrounded_title_sups
                    title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_sups),)

        lead = draft.lead
        lead_claims = draft.lead_claims
        lead_sups = draft.lead_support_ids
        if "LEAD" in patches:
            raw_l = patches["LEAD"]
            candidate_lead = _normalize_homoglyphs(_strip_internal_handles(raw_l))
            regrounded_lead_sups = (
                _reground_support_ids(
                    candidate_lead,
                    context,
                    allowed_ids("LEAD", tuple(draft.lead_support_ids)),
                )
                if context is not None
                else ()
            )
            if regrounded_lead_sups:
                lead = candidate_lead
                lead_sups = regrounded_lead_sups
                lead_sentences = _split_sentences_safe(lead)
                lead_claims = tuple(
                    ArticleClaimAtom(text=s, cited_support_ids=lead_sups)
                    for s in (lead_sentences or [lead])
                )

        p_idx = 1
        new_sections: list[ArticleSection] = []
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            heading = sec.heading
            heading_claims = sec.heading_claims
            h_sups = sec.heading_support_ids
            if h_id in patches:
                candidate_heading = _normalize_homoglyphs(_strip_internal_handles(patches[h_id]))
                regrounded_heading_sups = (
                    _reground_support_ids(
                        candidate_heading,
                        context,
                        allowed_ids(h_id, tuple(sec.heading_support_ids)),
                    )
                    if context is not None
                    else ()
                )
                if regrounded_heading_sups:
                    heading = candidate_heading
                    h_sups = regrounded_heading_sups
                    heading_claims = (ArticleClaimAtom(text=heading, cited_support_ids=h_sups),)

            new_paragraphs: list[ArticleParagraph] = []
            for para in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                if p_id not in patches:
                    # Keep unpatched claim/citation tuples byte-for-byte intact.
                    # Rebuilding paragraph citations from their claims loses
                    # intentional distinctions between unit and claim support.
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue

                text = para.text
                claims = para.claims
                existing_supports = tuple(
                    dict.fromkeys(
                        (
                            *para.cited_support_ids,
                            *(sid for claim in para.claims for sid in claim.cited_support_ids),
                        )
                    )
                )
                raw_patch = patches[p_id].strip()
                if raw_patch.upper() in (
                    "",
                    "[DELETE]",
                    "DELETE",
                    "УДАЛИТЬ",
                    "[УДАЛИТЬ]",
                    "NONE",
                    "NULL",
                ):
                    # AGENTS.md 0.3, 0.7: do not drop substantive paragraphs with valid supports.
                    # If supports exist, synthesize safe factual sentences from evidence instead of deleting.
                    if context and existing_supports:
                        sup_texts = [
                            context.support_by_id[s_id].text
                            for s_id in existing_supports
                            if s_id in context.support_by_id and context.support_by_id[s_id].text
                        ]
                        if sup_texts:
                            from src.publication.article_recovery import (
                                _clean_support_text_for_reader,
                                _normalize_for_dedup,
                            )

                            existing_norms = {
                                _normalize_for_dedup(p.text)
                                for s in new_sections
                                for p in s.paragraphs
                            } | {_normalize_for_dedup(p.text) for p in new_paragraphs}
                            safe_sentences: list[str] = []
                            for st in sup_texts[:3]:
                                cleaned_s = _clean_support_text_for_reader(st.strip())
                                if (
                                    cleaned_s
                                    and _normalize_for_dedup(cleaned_s) not in existing_norms
                                ):
                                    safe_sentences.append(cleaned_s)
                            if safe_sentences:
                                safe_text = " ".join(safe_sentences)
                                sentences = _split_sentences_safe(safe_text)
                                claims = tuple(
                                    ArticleClaimAtom(
                                        text=sentence,
                                        cited_support_ids=existing_supports,
                                    )
                                    for sentence in (sentences or [safe_text])
                                )
                                new_paragraphs.append(
                                    ArticleParagraph(
                                        text=safe_text,
                                        cited_support_ids=existing_supports,
                                        claims=claims,
                                    )
                                )
                            p_idx += 1
                            continue
                    p_idx += 1
                    continue

                candidate_text = _normalize_homoglyphs(_strip_internal_handles(raw_patch))
                p_sups = (
                    _reground_support_ids(
                        candidate_text,
                        context,
                        allowed_ids(p_id, existing_supports),
                    )
                    if context is not None
                    else ()
                )
                if not p_sups:
                    # Reject unsupported replacement prose. The original unit
                    # remains intact so final validation still sees its claims.
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue

                text = candidate_text
                sentences = _split_sentences_safe(text)
                from src.publication.article_models import _normalize_for_dedup

                seen_sn: set[str] = set()
                deduped_s: list[str] = []
                for sentence in sentences:
                    sn = _normalize_for_dedup(sentence)
                    if sn in seen_sn:
                        continue
                    seen_sn.add(sn)
                    deduped_s.append(sentence)
                if deduped_s and len(deduped_s) < len(sentences):
                    text = " ".join(deduped_s)
                    sentences = deduped_s
                claims = tuple(
                    ArticleClaimAtom(text=sentence, cited_support_ids=p_sups)
                    for sentence in (sentences or [text])
                )

                new_paragraphs.append(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=p_sups,
                        claims=claims,
                        generation_origin=para.generation_origin,
                    )
                )
                p_idx += 1

            if not new_paragraphs:
                continue

            sec_sups = sec.heading_support_ids or tuple(
                dict.fromkeys(sid for p in new_paragraphs for sid in p.cited_support_ids)
            )
            new_sections.append(
                ArticleSection(
                    heading=heading,
                    heading_support_ids=sec_sups,
                    heading_claims=heading_claims,
                    paragraphs=tuple(new_paragraphs),
                    cited_evidence_ids=sec.cited_evidence_ids,
                    heading_generation_origin=sec.heading_generation_origin,
                )
            )

        calc_words = (
            len(title.split())
            + len(lead.split())
            + sum(len(p.text.split()) for s in new_sections for p in s.paragraphs)
        )
        return StructuredArticleDraft(
            title=title,
            title_support_ids=title_sups,
            lead=lead,
            lead_support_ids=lead_sups,
            sections=tuple(new_sections),
            title_claims=title_claims,
            lead_claims=lead_claims,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=calc_words,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )
