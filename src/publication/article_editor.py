"""Targeted editorial copy-editor and fact-checker for structured article drafts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from src.ai_providers import AIProvider
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_homoglyphs,
    _strip_internal_handles,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
    validate_article_draft,
)

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
        max_output_tokens: int = 8192,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

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
            user_prompt = self._build_user_prompt(prompt_data)

            obs_att_id = 0
            if attempt_observer is not None:
                obs_att_id = await attempt_observer.attempt_started(
                    "article_editor",
                    provider=self.provider.__class__.__name__,
                    model=self.model,
                    metadata={
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

                current_draft = self.apply_patches(current_draft, patches)
                current_val = validate_article_draft(
                    current_draft, context, config=config, length_profile=length_profile
                )

                if attempt_observer is not None:
                    status = "succeeded" if current_val.is_valid else "partial"
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        status,
                        metadata={
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

        # Index units across draft
        # 1. Title
        if "TITLE" in issues_by_unit:
            unit_data.append(
                {
                    "unit_id": "TITLE",
                    "unit_type": "title",
                    "text": draft.title,
                    "support_ids": list(draft.title_support_ids),
                    "supports": [
                        context.support_by_id[sid].text
                        for sid in draft.title_support_ids
                        if sid in context.support_by_id
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
                        context.support_by_id[sid].text
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
                            context.support_by_id[sid].text
                            for sid in sec.heading_support_ids
                            if sid in context.support_by_id
                        ],
                        "issues": issues_by_unit[h_id],
                    }
                )

            for p in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                if p_id in issues_by_unit:
                    unit_data.append(
                        {
                            "unit_id": p_id,
                            "unit_type": "paragraph",
                            "text": p.text,
                            "support_ids": list(p.cited_support_ids),
                            "supports": [
                                context.support_by_id[sid].text
                                for sid in p.cited_support_ids
                                if sid in context.support_by_id
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
            "   - Переведите фразу в естественную косвенную речь БЕЗ КАВЫЧЕК (например: «как отметили жители...», «горожане призывают...»).\n\n"
            "2. ИМЕНА СОБСТВЕННЫЕ И НАЗВАНИЯ (UNSUPPORTED_PROPER_NAME / UNSUPPORTED_LOCATION):\n"
            "   - Если слово с заглавной буквы отмечено как неподтвержденное (название ведомства, организации, улица, имя), замените его на описательную формулировку (например: «профильное ведомство», «местные службы», «городские специалисты», «в одном из районов города») либо исключите упоминание.\n\n"
            "3. КОНКРЕТНЫЕ ФАКТЫ И ПРИЧИНЫ (UNSUPPORTED_CONCRETE_CLAIM):\n"
            "   - Не утверждайте причинно-следственные связи («из-за аварии», «вследствие отключения»), если точная причина не указана прямо в подтверждениях. Используйте нейтральное описание: «на фоне проблем со светом...», «в этот же период...».\n"
            "   - Числа, даты и интервалы должны строго соответствовать предоставленным фактам. Если факт не ясен, используйте качественное описание.\n\n"
            "4. СТИЛЬ И СОХРАННОСТЬ:\n"
            "   - Сохраняйте естественный журналистский стиль, грамматику и связность с остальным текстом статьи.\n"
            "   - Не добавляйте никаких новых фактов или деталей, которых нет в предоставленных подтверждениях.\n"
            "   - Отредактируйте ТОЛЬКО запрошенные фрагменты.\n\n"
            "ФОРМАТ ОТВЕТА (строго валидный JSON):\n"
            "{\n"
            '  "units": {\n'
            '    "<unit_id>": "Исправленный текст фрагмента...",\n'
            "    ...\n"
            "  }\n"
            "}"
        )

    def _build_user_prompt(self, unit_contexts: list[dict[str, Any]]) -> str:
        blocks: list[str] = ["ФРАГМЕНТЫ ДЛЯ РЕДАКТИРОВАНИЯ И ЗАМЕЧАНИЯ ФАКТ-ЧЕКИНГА:\n"]
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
            for iss in issues:
                blocks.append(f"  • [{iss.code}] {iss.message}")

            if supports:
                blocks.append("\nПодтверждающие факты (источники):")
                for s_text in supports[:5]:
                    blocks.append(f"  - {s_text}")
            else:
                blocks.append(
                    "\n(Прямых цитат нет — опирайтесь на общий контекст и снимите категоричные утверждения)"
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
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                patches[k.strip()] = v.strip()
            elif isinstance(k, str) and isinstance(v, dict) and "text" in v:
                patches[k.strip()] = str(v["text"]).strip()

        return patches

    def apply_patches(
        self, draft: StructuredArticleDraft, patches: Mapping[str, str]
    ) -> StructuredArticleDraft:
        """Apply targeted text patches to StructuredArticleDraft while preserving structure."""
        if not patches:
            return draft

        title = draft.title
        if "TITLE" in patches:
            raw_t = patches["TITLE"]
            title = _normalize_homoglyphs(_strip_internal_handles(raw_t))

        lead = draft.lead
        if "LEAD" in patches:
            raw_l = patches["LEAD"]
            lead = _normalize_homoglyphs(_strip_internal_handles(raw_l))

        p_idx = 1
        new_sections: list[ArticleSection] = []
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            heading = sec.heading
            if h_id in patches:
                heading = _normalize_homoglyphs(_strip_internal_handles(patches[h_id]))

            new_paragraphs: list[ArticleParagraph] = []
            for para in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                text = para.text
                if p_id in patches:
                    text = _normalize_homoglyphs(_strip_internal_handles(patches[p_id]))

                new_paragraphs.append(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=para.cited_support_ids,
                        claims=para.claims,
                        generation_origin=para.generation_origin,
                    )
                )
                p_idx += 1

            new_sections.append(
                ArticleSection(
                    heading=heading,
                    heading_support_ids=sec.heading_support_ids,
                    heading_claims=sec.heading_claims,
                    paragraphs=tuple(new_paragraphs),
                    cited_evidence_ids=sec.cited_evidence_ids,
                    heading_generation_origin=sec.heading_generation_origin,
                )
            )

        return StructuredArticleDraft(
            title=title,
            title_support_ids=draft.title_support_ids,
            lead=lead,
            lead_support_ids=draft.lead_support_ids,
            sections=tuple(new_sections),
            title_claims=draft.title_claims,
            lead_claims=draft.lead_claims,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=0,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )
