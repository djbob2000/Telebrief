"""Policy service for publication eligibility, editorial selection, and writer versions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg

from src.publication.models import (
    PublicationPolicySet,
)
from src.publication.repository import PublicationPolicyRepository

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
        eligibility_config_hash: str = DEFAULT_ELIGIBILITY_CONFIG_HASH,
        eligibility_prompt_version: str = DEFAULT_ELIGIBILITY_PROMPT_VERSION,
        selection_config_hash: str = DEFAULT_SELECTION_CONFIG_HASH,
        selection_prompt_version: str = DEFAULT_SELECTION_PROMPT_VERSION,
        writer_config_hash: str = DEFAULT_WRITER_CONFIG_HASH,
        writer_prompt_version: str = DEFAULT_WRITER_PROMPT_VERSION,
    ) -> PublicationPolicySet:
        elig = await self._repo.get_or_create_eligibility_policy(
            conn,
            edition_id=edition_id,
            config_hash=eligibility_config_hash,
            prompt_version=eligibility_prompt_version,
        )
        sel = await self._repo.get_or_create_selection_policy(
            conn,
            edition_id=edition_id,
            config_hash=selection_config_hash,
            prompt_version=selection_prompt_version,
        )
        wri = await self._repo.get_or_create_writer_policy(
            conn,
            edition_id=edition_id,
            config_hash=writer_config_hash,
            prompt_version=writer_prompt_version,
        )
        return PublicationPolicySet(eligibility=elig, selection=sel, writer=wri)
