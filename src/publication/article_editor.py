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
    ArticleReaderQualityFinding,
    ArticleReaderQualityReport,
    diagnose_article_quality,
)
from src.publication.article_validator import ArticleValidationResult, validate_article_draft
from src.publication.article_writer_context import sanitize_writer_source_text

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _reground_support_ids(text: str, context: ArticleEditorialContext) -> tuple[str, ...]:
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
    for support in context.supports:
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
            quality_issues = list(current_quality.repair_findings)
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
                    preserve_unmatched_supports=material_projection is None,
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
                    logger.warning(
                        "ArticleEditor pass %d left remaining issues: %s",
                        attempt,
                        list(current_val.violations)[:5],
                    )

            except Exception as exc:
                logger.warning("ArticleEditor pass %d encountered error: %s", attempt, exc)
                if attempt_observer is not None:
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        "failed",
                        error_kind=type(exc).__name__,
                        metadata={"error": str(exc)},
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
        def unit_supports(unit_ids: list[str], unit_issues: list[Any]) -> list[str]:
            # Required support IDs from a quality finding must appear first so
            # the prompt's bounded five-support display cannot hide the exact
            # evidence that the editor is asked to restore.
            ids: list[str] = []
            for issue in unit_issues:
                ids.extend(getattr(issue, "support_ids", ()) or ())
            ids.extend(unit_ids)
            return list(dict.fromkeys(ids))

        if "TITLE" in issues_by_unit:
            t_sups = unit_supports(list(draft.title_support_ids), issues_by_unit["TITLE"])
            if not t_sups:
                t_sups = list(draft.lead_support_ids) or (
                    list(draft.sections[0].heading_support_ids) if draft.sections else []
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
            lead_sups = unit_supports(list(draft.lead_support_ids), issues_by_unit["LEAD"])
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
                h_sups = unit_supports(list(sec.heading_support_ids), issues_by_unit[h_id])
                unit_data.append(
                    {
                        "unit_id": h_id,
                        "unit_type": "heading",
                        "text": sec.heading,
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

            for p in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                if p_id in issues_by_unit:
                    p_sups = unit_supports(list(p.cited_support_ids), issues_by_unit[p_id])
                    if not p_sups:
                        p_sups = list(sec.heading_support_ids)
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
                for s_text in supports[:5]:
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
        preserve_unmatched_supports: bool = True,
    ) -> StructuredArticleDraft:
        """Apply targeted text patches while preserving structure and provenance.

        Projected validation contexts intentionally omit suppressed material.  In
        that mode an editor patch that cannot be re-grounded must not inherit the
        old citation IDs, because doing so would reattach filtered evidence.
        """
        if not patches:
            return draft

        title_sups = draft.title_support_ids
        if not title_sups:
            title_sups = draft.lead_support_ids or (
                draft.sections[0].heading_support_ids if draft.sections else ()
            )

        title = draft.title
        title_claims = draft.title_claims
        if "TITLE" in patches:
            raw_t = patches["TITLE"].strip()
            if raw_t.upper() not in ("", "[DELETE]", "DELETE", "NONE", "NULL", "[УДАЛИТЬ]"):
                title = _normalize_homoglyphs(_strip_internal_handles(raw_t))
                if context:
                    regrounded = _reground_support_ids(title, context)
                    if regrounded:
                        title_sups = regrounded
                    else:
                        filtered_title_sups = tuple(
                            sid
                            for sid in title_sups
                            if sid in context.support_by_id
                            and context.support_by_id[sid].publication_use == "PUBLISH"
                            and context.support_by_id[sid].temporal_role == "CURRENT_WINDOW"
                        )
                        title_sups = (
                            filtered_title_sups
                            if not preserve_unmatched_supports
                            else filtered_title_sups or title_sups
                        )
                title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_sups),)

        lead = draft.lead
        lead_claims = draft.lead_claims
        lead_sups = draft.lead_support_ids
        if "LEAD" in patches:
            raw_l = patches["LEAD"]
            lead = _normalize_homoglyphs(_strip_internal_handles(raw_l))
            lead_sups = _reground_support_ids(lead, context) if context else draft.lead_support_ids
            if not lead_sups and context:
                filtered_lead_sups = tuple(
                    sid
                    for sid in draft.lead_support_ids
                    if sid in context.support_by_id
                    and context.support_by_id[sid].publication_use == "PUBLISH"
                )
                lead_sups = (
                    filtered_lead_sups
                    if not preserve_unmatched_supports
                    else filtered_lead_sups or draft.lead_support_ids
                )
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
                heading = _normalize_homoglyphs(_strip_internal_handles(patches[h_id]))
                h_sups = (
                    _reground_support_ids(heading, context) if context else sec.heading_support_ids
                )
                if not h_sups:
                    h_sups = sec.heading_support_ids if preserve_unmatched_supports else ()
                heading_claims = (ArticleClaimAtom(text=heading, cited_support_ids=h_sups),)

            new_paragraphs: list[ArticleParagraph] = []
            for para in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                text = para.text
                claims = para.claims
                p_sups = para.cited_support_ids or sec.heading_support_ids
                if p_id in patches:
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
                        if context and p_sups:
                            sup_texts = [
                                context.support_by_id[s_id].text
                                for s_id in p_sups
                                if s_id in context.support_by_id
                                and context.support_by_id[s_id].text
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
                                        ArticleClaimAtom(text=s, cited_support_ids=p_sups)
                                        for s in (sentences or [safe_text])
                                    )
                                    new_paragraphs.append(
                                        ArticleParagraph(
                                            text=safe_text,
                                            cited_support_ids=p_sups,
                                            claims=claims,
                                        )
                                    )
                                p_idx += 1
                                continue
                        p_idx += 1
                        continue
                    text = _normalize_homoglyphs(_strip_internal_handles(raw_patch))
                    # A patch replaces the prose; recompute its provenance
                    # instead of retaining citations from the old paragraph.
                    p_sups = _reground_support_ids(text, context) if context else p_sups
                    if not p_sups and context and not preserve_unmatched_supports:
                        p_sups = ()
                    sentences = _split_sentences_safe(text)
                    from src.publication.article_models import _normalize_for_dedup

                    seen_sn: set[str] = set()
                    deduped_s: list[str] = []
                    for s in sentences:
                        sn = _normalize_for_dedup(s)
                        if sn in seen_sn:
                            continue
                        seen_sn.add(sn)
                        deduped_s.append(s)
                    if deduped_s and len(deduped_s) < len(sentences):
                        text = " ".join(deduped_s)
                        sentences = deduped_s
                    claims = tuple(
                        ArticleClaimAtom(text=s, cited_support_ids=p_sups)
                        for s in (sentences or [text])
                    )

                if not p_sups:
                    # If paragraph lacks supports, try to find matching supports from context
                    if context and getattr(context, "supports", None):
                        matched = []
                        tok_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
                        p_words = {w.lower() for w in tok_re.findall(text) if len(w) >= 3}
                        p_nums = set(re.findall(r"\b\d+\b", text))
                        for sup in context.supports:
                            if sup.publication_use == "PUBLISH" and sup.text:
                                s_words = {
                                    w.lower() for w in tok_re.findall(sup.text) if len(w) >= 3
                                }
                                s_nums = set(re.findall(r"\b\d+\b", sup.text))
                                if len(p_words & s_words) >= 2 or (
                                    p_nums and s_nums and (p_nums & s_nums)
                                ):
                                    matched.append(sup.support_id)
                        if matched:
                            p_sups = tuple(dict.fromkeys(matched))
                            sentences = _split_sentences_safe(text)
                            claims = tuple(
                                ArticleClaimAtom(text=s, cited_support_ids=p_sups)
                                for s in (sentences or [text])
                            )

                if not p_sups:
                    # Paragraph has zero evidence in source material; omit to uphold Evidence Boundary
                    p_idx += 1
                    continue

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
