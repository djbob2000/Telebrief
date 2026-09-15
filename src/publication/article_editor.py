"""Targeted editorial copy-editor and fact-checker for structured article drafts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from src.ai_providers import AIProvider
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_homoglyphs,
    _split_sentences_safe,
    _strip_internal_handles,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
    validate_article_draft,
)
from src.publication.article_writer_context import sanitize_writer_source_text

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ArticleEditor:
    """Targeted fact-checking editor that fixes isolated validation issues without full draft rewrite."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 16384,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = min(max_output_tokens, 16384)

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
    ) -> tuple[StructuredArticleDraft, ArticleValidationResult]:
        """Apply targeted editorial corrections to units with blocking validation issues."""
        current_draft = draft
        current_val = validation_result

        for attempt in range(1, max_attempts + 1):
            blocking_issues = [
                iss
                for iss in current_val.issues
                if iss.blocking and iss.unit_id not in ("DRAFT", "")
            ]
            if not blocking_issues:
                break

            # Group blocking issues by unit_id
            issues_by_unit: dict[str, list[ArticleValidationIssue]] = {}
            for iss in blocking_issues:
                issues_by_unit.setdefault(iss.unit_id, []).append(iss)

            logger.info(
                "ArticleEditor pass %d/%d targeting %d problematic unit(s): %s",
                attempt,
                max_attempts,
                len(issues_by_unit),
                list(issues_by_unit.keys()),
            )

            prompt_data = self._build_unit_contexts(current_draft, issues_by_unit, context)
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
                        "violations": [f"{iss.code}:{iss.unit_id}" for iss in blocking_issues],
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

                current_draft = self.apply_patches(current_draft, patches, context=context)
                current_val = validate_article_draft(
                    current_draft, context, config=config, length_profile=length_profile
                )

                if attempt_observer is not None:
                    status = "succeeded" if current_val.is_valid else "failed"
                    error_kind = None if current_val.is_valid else "remaining_violations"
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        status,
                        error_kind=error_kind,
                        metadata={
                            "editor_status": "succeeded" if current_val.is_valid else "partial",
                            "patched_units": list(patches.keys()),
                            "remaining_violations": list(current_val.violations),
                        },
                    )

                if current_val.is_valid:
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
        issues_by_unit: Mapping[str, list[ArticleValidationIssue]],
        context: ArticleEditorialContext,
    ) -> list[dict[str, Any]]:
        """Collect current text, cited supports, and issues for each target unit."""
        unit_data: list[dict[str, Any]] = []

        def support_text(support_id: str) -> str:
            support = context.support_by_id.get(support_id)
            if support is None:
                return ""
            fact = (support.text or "").strip()
            source = sanitize_writer_source_text((support.source_text or "").strip())
            if source and source != fact:
                return f"{fact}\nПервичный источник: {source}" if fact else source
            return fact or source

        # Index units across draft
        # 1. Title
        if "TITLE" in issues_by_unit:
            t_sups = list(draft.title_support_ids)
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
                        support_text(sid) for sid in t_sups if sid in context.support_by_id
                    ],
                    "issues": issues_by_unit["TITLE"],
                }
            )

        # 2. Lead
        if "LEAD" in issues_by_unit:
            unit_data.append(
                {
                    "unit_id": "LEAD",
                    "unit_type": "lead",
                    "text": draft.lead,
                    "support_ids": list(draft.lead_support_ids),
                    "supports": [
                        support_text(sid)
                        for sid in draft.lead_support_ids
                        if sid in context.support_by_id
                    ],
                    "issues": issues_by_unit["LEAD"],
                }
            )

        # 3. Sections (Headings and Paragraphs)
        p_idx = 1
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            if h_id in issues_by_unit:
                unit_data.append(
                    {
                        "unit_id": h_id,
                        "unit_type": "heading",
                        "text": sec.heading,
                        "support_ids": list(sec.heading_support_ids),
                        "supports": [
                            support_text(sid)
                            for sid in sec.heading_support_ids
                            if sid in context.support_by_id
                        ],
                        "issues": issues_by_unit[h_id],
                    }
                )

            for p in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                if p_id in issues_by_unit:
                    p_sups = list(p.cited_support_ids)
                    if not p_sups:
                        p_sups = list(sec.heading_support_ids)
                    unit_data.append(
                        {
                            "unit_id": p_id,
                            "unit_type": "paragraph",
                            "text": p.text,
                            "support_ids": p_sups,
                            "supports": [
                                support_text(sid) for sid in p_sups if sid in context.support_by_id
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
            "8. ОБЪЕМ, СТИЛЬ И СОХРАННОСТЬ:\n"
            "   - Сохраняйте естественный журналистский стиль и связность с остальным текстом статьи.\n"
            "   - Не добавляйте никаких новых фактов или деталей, которых нет в предоставленных подтверждениях.\n"
            "   - Не переписывайте заново весь абзац, если нужно исправить только одну фразу или слово.\n"
            "   - Отредактируйте ТОЛЬКО запрошенные фрагменты.\n\n"
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
                msg = f"  • [{iss.code}] {iss.message}"
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
    ) -> StructuredArticleDraft:
        """Apply targeted text patches to StructuredArticleDraft while preserving structure."""
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
                title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_sups),)

        lead = draft.lead
        lead_claims = draft.lead_claims
        if "LEAD" in patches:
            raw_l = patches["LEAD"]
            lead = _normalize_homoglyphs(_strip_internal_handles(raw_l))
            lead_sentences = _split_sentences_safe(lead)
            lead_claims = tuple(
                ArticleClaimAtom(text=s, cited_support_ids=draft.lead_support_ids)
                for s in (lead_sentences or [lead])
            )

        p_idx = 1
        new_sections: list[ArticleSection] = []
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            heading = sec.heading
            heading_claims = sec.heading_claims
            if h_id in patches:
                heading = _normalize_homoglyphs(_strip_internal_handles(patches[h_id]))
                heading_claims = (
                    ArticleClaimAtom(text=heading, cited_support_ids=sec.heading_support_ids),
                )

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
                    sentences = _split_sentences_safe(text)
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
                        for s in context.supports:
                            if s.publication_use == "PUBLISH" and s.text:
                                s_words = {w.lower() for w in tok_re.findall(s.text) if len(w) >= 3}
                                s_nums = set(re.findall(r"\b\d+\b", s.text))
                                if len(p_words & s_words) >= 2 or (
                                    p_nums and s_nums and (p_nums & s_nums)
                                ):
                                    matched.append(s.support_id)
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
            lead_support_ids=draft.lead_support_ids,
            sections=tuple(new_sections),
            title_claims=title_claims,
            lead_claims=lead_claims,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=calc_words,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )
