"""Policy service for publication eligibility, editorial selection, and writer versions."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import psycopg

from src.publication.digest_contracts import DIGEST_PUBLICATION_TYPES
from src.publication.editorializer import DIGEST_EDITORIALIZER_PROMPT_VERSION
from src.publication.models import (
    PublicationPolicySet,
)
from src.publication.repository import PublicationPolicyRepository

logger = logging.getLogger(__name__)

DEFAULT_ELIGIBILITY_CONFIG_HASH = "elig-cfg-default"
DEFAULT_ELIGIBILITY_PROMPT_VERSION = "elig-prompt-v1"

DEFAULT_SELECTION_CONFIG_HASH = "selection-cfg-default"
DEFAULT_SELECTION_PROMPT_VERSION = "selection-prompt-v1"

DEFAULT_WRITER_CONFIG_HASH = "writer-cfg-default"
DEFAULT_WRITER_PROMPT_VERSION = "writer-prompt-v1"


def compute_config_hash(payload: dict[str, Any] | str) -> str:
    if isinstance(payload, dict):
        raw = json.dumps(payload, sort_keys=True)
    else:
        raw = str(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class PublicationPolicyService:
    """Service to ensure and load active publication policy sets."""

    def __init__(self, repo: PublicationPolicyRepository | None = None) -> None:
        self._repo = repo or PublicationPolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
        config: Any | None = None,
        lookback_hours_override: int | None = None,
        eligibility_config_hash: str = DEFAULT_ELIGIBILITY_CONFIG_HASH,
        eligibility_prompt_version: str = DEFAULT_ELIGIBILITY_PROMPT_VERSION,
        selection_config_hash: str = DEFAULT_SELECTION_CONFIG_HASH,
        selection_prompt_version: str = DEFAULT_SELECTION_PROMPT_VERSION,
        writer_config_hash: str = DEFAULT_WRITER_CONFIG_HASH,
        writer_prompt_version: str = DEFAULT_WRITER_PROMPT_VERSION,
        triage_version: str | None = None,
        scope_version: str | None = None,
        scope_config_hash: str | None = None,
    ) -> PublicationPolicySet:
        lookback_hours = 24
        excluded_platforms: list[str] = []
        if lookback_hours_override is not None:
            lookback_hours = int(lookback_hours_override)
        elif config is not None:
            is_article = publication_type in ("daily_article", "article")
            if (
                is_article
                and hasattr(config, "settings")
                and hasattr(config.settings, "article")
                and hasattr(config.settings.article, "lookback_hours")
            ):
                lookback_hours = int(config.settings.article.lookback_hours)
            elif (
                is_article
                and hasattr(config, "article")
                and hasattr(config.article, "lookback_hours")
            ):
                lookback_hours = int(config.article.lookback_hours)
            elif hasattr(config, "settings") and hasattr(config.settings, "lookback_hours"):
                lookback_hours = int(config.settings.lookback_hours)
            elif isinstance(config, dict):
                art_dict = config.get("article") or (
                    config.get("settings", {}).get("article")
                    if isinstance(config.get("settings"), dict)
                    else None
                )
                if is_article and isinstance(art_dict, dict) and "lookback_hours" in art_dict:
                    lookback_hours = int(art_dict["lookback_hours"])
                else:
                    lookback_hours = int(
                        config.get(
                            "lookback_hours",
                            config.get("settings", {}).get("lookback_hours", 24)
                            if isinstance(config.get("settings"), dict)
                            else 24,
                        )
                    )

        if config is not None:
            fb_cfg = getattr(config, "facebook", None)
            if fb_cfg is not None:
                if not getattr(fb_cfg, "editorial_enabled", True):
                    excluded_platforms.append("facebook")
            elif isinstance(config, dict):
                fb_dict = config.get("facebook")
                if isinstance(fb_dict, dict) and not fb_dict.get("editorial_enabled", True):
                    excluded_platforms.append("facebook")

        if triage_version is None:
            triage_version = "v9"
        if scope_version is None:
            scope_version = "v1"
        if scope_config_hash is None and config is not None:
            try:
                from src.processing.edition_scope import resolve_edition_scope
                from src.processing.edition_scope import scope_config_hash as calc_scope_hash

                _, scope_obj = await resolve_edition_scope(conn, config, edition_id)
                scope_config_hash = calc_scope_hash(scope_obj)
            except Exception as exc:
                logger.debug(
                    "Could not resolve scope_config_hash for edition %s: %s", edition_id, exc
                )

        eligibility_config = {
            "lookback_hours": lookback_hours,
            "excluded_platforms": sorted(set(excluded_platforms)),
            "triage_version": triage_version,
            "scope_version": scope_version,
            "scope_config_hash": scope_config_hash,
        }
        if eligibility_config_hash == DEFAULT_ELIGIBILITY_CONFIG_HASH:
            eligibility_config_hash = compute_config_hash(eligibility_config)

        selection_config: dict[str, Any] = {}
        if selection_config_hash == DEFAULT_SELECTION_CONFIG_HASH:
            selection_config_hash = compute_config_hash(selection_config)

        is_digest = publication_type in DIGEST_PUBLICATION_TYPES
        writer_config: dict[str, Any] = {}
        if is_digest:
            ai_prov = ""
            ai_mod = ""
            if config is not None:
                settings_obj = getattr(config, "settings", None)
                if settings_obj is not None:
                    ai_prov = str(getattr(settings_obj, "ai_provider", ""))
                    ai_mod = str(
                        getattr(settings_obj, "ai_model", "")
                        or getattr(settings_obj, "openai_model", "")
                    )
            writer_config = {
                "editorializer_prompt_version": DIGEST_EDITORIALIZER_PROMPT_VERSION,
                "ai_provider": ai_prov,
                "ai_model": ai_mod,
            }
            if writer_prompt_version == DEFAULT_WRITER_PROMPT_VERSION:
                writer_prompt_version = DIGEST_EDITORIALIZER_PROMPT_VERSION

        if writer_config_hash == DEFAULT_WRITER_CONFIG_HASH:
            writer_config_hash = compute_config_hash(writer_config)

        elig = await self._repo.get_or_create_eligibility_policy(
            conn,
            edition_id=edition_id,
            config_hash=eligibility_config_hash,
            prompt_version=eligibility_prompt_version,
            config=eligibility_config,
        )
        sel = await self._repo.get_or_create_selection_policy(
            conn,
            edition_id=edition_id,
            config_hash=selection_config_hash,
            prompt_version=selection_prompt_version,
            config=selection_config,
        )
        wri = await self._repo.get_or_create_writer_policy(
            conn,
            edition_id=edition_id,
            config_hash=writer_config_hash,
            prompt_version=writer_prompt_version,
            config=writer_config,
        )
        return PublicationPolicySet(eligibility=elig, selection=sel, writer=wri)
