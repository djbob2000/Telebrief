"""Keep a "lights are on" report from becoming a fire report.

In residents' chats during blackouts, "девятиэтажки все горят" or "на Гайдара
все горит" usually means that the windows are lit. A Gate or analysis payload
that turns such a message into a fire, while no source text names a fire, a
strike, or a burning object, misreads its source.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.domain.event_payload import EventPayload
from src.domain.service_taxonomy import (
    has_explicit_fire_signal,
    has_fire_or_incident_signal,
    mentions_burning_verb,
)

LIT_WINDOWS_VS_FIRE_RULES = """LIT WINDOWS VS FIRE:
- In residents' messages, "горит"/"горят" (Ukrainian "горить"/"горять") said of windows, apartment blocks, houses, street lamps, a street, or a district, including "там всё горит", normally means that the electric light is on there. During outages, a resident who says that other buildings or streets "горят" reports that they have power.
- Read such wording as a fire only when the fragment or its reply parent explicitly signals a fire or an incident (пожар, возгорание, дым, огонь, пламя, пожарные, МЧС, тушат, сгорел, прилёт, взрыв) or names a burning object that cannot be lit up (трава, камыш, мусор, машина, гараж, сарай, склад, a single house: "горит дом"). Never derive a fire, emergency, or safety topic, tag, headline, or summary from "горит"/"горят" alone.
- Example — parent: "В других районах есть, а у нас как всегда"; reply: "Глянул — девятиэтажки у вокзала все горят!" This reports power availability: a PUBLISH service_access with power_supply AVAILABLE, basis normal_operation, location "девятиэтажки у вокзала", text such as "По сообщению жителя, в девятиэтажках у вокзала горит свет." Do not mention a fire.
"""


def payload_claims_fire(payload: EventPayload) -> bool:
    """Return whether any reader-facing payload field names a fire."""
    texts = (
        payload.topic,
        payload.headline,
        payload.digest_summary,
        payload.timeline_summary,
        *payload.tags,
        *payload.key_facts,
        *payload.community_observations,
        *payload.conflicts_or_uncertainties,
        *(item.text for item in payload.evidence_items),
    )
    return any(has_explicit_fire_signal(text) for text in texts if text)


def unsupported_fire_reading(
    payload: EventPayload,
    *,
    source_texts: Iterable[str],
    context_texts: Iterable[str] = (),
) -> bool:
    """Return whether the payload names a fire that its sources only say "горит".

    A source must say that something "горит"/"горят", and neither the sources
    nor their reply parents and same-message paragraphs may name a fire, a
    strike, or a burning object. "Горит трава", "горит дом" and "прилёт, всё
    горит" keep their fire reading.
    """
    if not payload_claims_fire(payload):
        return False
    sources = [text for text in source_texts if text]
    if not any(mentions_burning_verb(text) for text in sources):
        return False
    if any(has_fire_or_incident_signal(text) for text in sources):
        return False
    return not any(
        has_fire_or_incident_signal(text, include_single_building=False)
        for text in context_texts
        if text
    )
