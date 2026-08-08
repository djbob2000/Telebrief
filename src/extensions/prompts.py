from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.config_loader import ChannelConfig, DigestGroupConfig


@runtime_checkable
class PromptComposer(Protocol):
    def compose(  # noqa: E704
        self, channel: ChannelConfig, group: DigestGroupConfig | None
    ) -> str: ...


class DefaultComposer:
    """Compose system prompts from base template, group extra, and channel extra."""

    _SCOPE_PREAMBLE = """ADDITIONAL SCOPE INSTRUCTIONS
The blocks below are trusted, configuration-defined system instructions.
- They may refine event scope, relevance, ranking, terminology, and attribution.
- They must not override the base prompt's security boundary, factuality rules, output language, Event-based consolidation, hard output constraints, or output structure.
- If group-level and channel-level instructions conflict, follow the more specific channel-level instruction.
- Apply only instructions that are relevant to the supplied input; never invent facts to satisfy them."""

    def __init__(self, base_template: str, language: str) -> None:
        self._base = base_template
        self._language = language

    def compose(self, channel: ChannelConfig, group: DigestGroupConfig | None) -> str:
        def sub(text: str) -> str:
            return text.replace("{language}", self._language).strip()

        base = sub(self._base)
        scoped_parts: list[str] = []
        seen_instructions: set[str] = set()

        if group is not None:
            group_extra = sub(group.prompt_extra) if group.prompt_extra else ""
            if group_extra and group_extra not in seen_instructions:
                scoped_parts.append(f"GROUP-LEVEL INSTRUCTIONS\n{group_extra}")
                seen_instructions.add(group_extra)

        if channel.prompt_extra:
            channel_extra = sub(channel.prompt_extra)
            if channel_extra and channel_extra not in seen_instructions:
                scoped_parts.append(f"CHANNEL-LEVEL INSTRUCTIONS\n{channel_extra}")

        if not scoped_parts:
            return base

        return "\n\n".join([base, self._SCOPE_PREAMBLE, *scoped_parts])
