"""Publication delivery service managing immutable platform payloads and delivery attempts (Plan 4 Task 7)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from typing import Any, Protocol

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.publication.models import (
    DeliveryDestination,
    PublicationDelivery,
    PublicationDeliveryAttempt,
    PublicationDeliveryPayload,
)
from src.publication.repository import DeliveryRepository, PublicationRepository

logger = logging.getLogger(__name__)


def _default_telegram_destination() -> str | None:
    """Return the configured default Telegram chat for publications, if any."""
    try:
        from src.config_loader import load_config

        config = load_config()
    except Exception:
        logger.debug("full config unavailable; no default Telegram destination", exc_info=True)
        return None
    target = config.settings.target_chat_id or config.settings.target_user_id
    return str(target) if target else None


def _render_payload(platform: str, pub: Any) -> tuple[str, dict[str, Any]]:
    """Render the immutable payload content for a destination platform."""
    body_text = (pub.body or "").strip()
    title_text = (pub.title or "").strip()
    lead_text = (pub.lead or "").strip()

    if platform == "telegram_channel":
        if body_text.startswith(f"# {title_text}") or body_text.startswith(title_text):
            raw_text = body_text
        elif title_text:
            raw_text = f"{title_text}\n\n{body_text}"
        else:
            raw_text = body_text
        return "telegram_html", {"text": raw_text}

    if platform == "telegraph":
        if body_text.startswith("# "):
            body = body_text
        elif title_text:
            if lead_text and not body_text.startswith(lead_text):
                body = f"# {title_text}\n\n{lead_text}\n\n{body_text}"
            else:
                body = f"# {title_text}\n\n{body_text}"
        else:
            body = body_text
        return "telegraph_nodes", {"title": title_text, "body_markdown": body}

    raise ValueError(f"no delivery payload renderer for platform {platform!r}")


class DestinationClient(Protocol):
    """Protocol for platform-specific delivery clients."""

    async def send_payload(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
    ) -> dict[str, Any]:
        """Send payload to platform and return result metadata including external_message_id."""
        ...

    async def reconcile_outcome(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
        last_attempt: PublicationDeliveryAttempt,
    ) -> str:
        """Check if an uncertain attempt actually succeeded on the remote platform.

        Returns 'succeeded', 'not_delivered', or 'unknown'.
        """
        ...


class MockDestinationClient:
    """Default mock destination client for tests and offline operations."""

    async def send_payload(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
    ) -> dict[str, Any]:
        return {"external_message_id": "ext-msg-12345", "status": "sent"}

    async def reconcile_outcome(
        self,
        *,
        destination: DeliveryDestination,
        payload: PublicationDeliveryPayload,
        last_attempt: PublicationDeliveryAttempt,
    ) -> str:
        return "succeeded"


class PublicationDeliveryService:
    """Service to prepare immutable destination payloads and execute resilient deliveries."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        delivery_repo: DeliveryRepository | None = None,
        pub_repo: PublicationRepository | None = None,
        clients: dict[str, DestinationClient] | None = None,
    ) -> None:
        self.uow = uow
        self.delivery_repo = delivery_repo or DeliveryRepository()
        self.pub_repo = pub_repo or PublicationRepository()
        # Default to real platform adapters; tests inject explicit client
        # mappings (including MockDestinationClient) to control outcomes.
        if clients is not None:
            self.clients = clients
        else:
            from src.publication.adapters import build_default_clients

            self.clients = build_default_clients()

    async def prepare_payloads(
        self,
        publication_id: int,
        *,
        destinations: list[DeliveryDestination] | None = None,
    ) -> list[PublicationDelivery]:
        async with self.uow.transaction() as conn:
            pub = await self.pub_repo.get_publication_by_id(conn, publication_id)
            if pub is None:
                raise ValueError(f"publication {publication_id} not found")

            dests = destinations
            if not dests:
                # Resolve the configured default destination for the edition.
                run = await self.pub_repo.get_run_by_id(conn, pub.publication_run_id)
                if run is None:
                    raise ValueError(
                        f"publication run {pub.publication_run_id} not found "
                        f"for publication {publication_id}"
                    )
                destination_key = _default_telegram_destination()
                if not destination_key:
                    raise ValueError(
                        "no delivery destinations passed and no default Telegram "
                        "chat configured (settings.target_chat_id / target_user_id)"
                    )
                default_dest = await self.delivery_repo.get_or_create_destination(
                    conn,
                    edition_id=run.edition_id,
                    platform="telegram_channel",
                    destination_key=destination_key,
                )
                dests = [default_dest]

            created_deliveries: list[PublicationDelivery] = []
            for dest in dests:
                # Format immutable destination payload per platform
                payload_format, rendered_content = _render_payload(dest.platform, pub)
                payload_bytes = repr(rendered_content).encode("utf-8")
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()

                payload = await self.delivery_repo.create_payload(
                    conn,
                    publication_id=pub.id,
                    destination_id=dest.id,
                    payload_format=payload_format,
                    rendered_content=rendered_content,
                    content_hash=payload_hash,
                )

                idempotency_key = f"deliv:{pub.id}:{dest.id}:{uuid.uuid4().hex}"
                delivery = await self.delivery_repo.create_delivery(
                    conn,
                    publication_id=pub.id,
                    destination_id=dest.id,
                    payload_id=payload.id,
                    idempotency_key=idempotency_key,
                )
                created_deliveries.append(delivery)
                if delivery.status not in ("succeeded", "failed"):
                    await self._defer_deliver_payload(conn, delivery.id)

            return created_deliveries

    async def deliver(self, delivery_id: int) -> PublicationDelivery:
        async with self.uow.transaction() as conn:
            delivery = await self.delivery_repo.get_delivery_by_id(conn, delivery_id)
            if delivery is None:
                raise ValueError(f"delivery {delivery_id} not found")

            if delivery.status in ("succeeded", "failed"):
                return delivery

            payload = await self.delivery_repo.get_payload(conn, delivery.payload_id)
            if payload is None:
                raise ValueError(
                    f"payload {delivery.payload_id} not found for delivery {delivery_id}"
                )

            destination = await self.delivery_repo.get_destination_by_id(
                conn, delivery.destination_id
            )
            if destination is None:
                raise ValueError(f"destination {delivery.destination_id} not found")

            cursor = await conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM publication_delivery_attempts WHERE publication_delivery_id = %s",
                (delivery_id,),
            )
            ver_row = await cursor.fetchone()
            attempt_no = ver_row[0] if ver_row is not None else 1

        client = self.clients.get(destination.platform)
        if client is None:
            raise RuntimeError(
                f"no destination client configured for platform {destination.platform!r} "
                f"(delivery {delivery_id})"
            )

        # Check if previous attempt was outcome_unknown -> reconcile first
        if delivery.status == "outcome_unknown":
            dummy_attempt = PublicationDeliveryAttempt(
                id=0,
                publication_delivery_id=delivery.id,
                attempt_no=attempt_no - 1,
                status="failed",
                error_kind="outcome_unknown",
                error_message=None,
                response={},
                started_at=dt.datetime.now(dt.timezone.utc),
                completed_at=dt.datetime.now(dt.timezone.utc),
            )
            reconciliation = await client.reconcile_outcome(
                destination=destination,
                payload=payload,
                last_attempt=dummy_attempt,
            )
            if reconciliation == "succeeded":
                async with self.uow.transaction() as conn:
                    await self.delivery_repo.record_delivery_attempt(
                        conn,
                        delivery_id=delivery.id,
                        attempt_no=attempt_no,
                        status="succeeded",
                        response={"reconciled": True},
                    )
                    await self.delivery_repo.update_delivery_status(
                        conn,
                        delivery.id,
                        status="succeeded",
                        external_delivery_id="reconciled-msg-id",
                    )
                    res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                    if res is None:
                        raise RuntimeError(f"delivery {delivery.id} not found after reconciliation")
                    return res
            if reconciliation == "unknown":
                # The platform cannot prove whether the earlier attempt
                # delivered; resending risks a duplicate publication. Keep
                # outcome_unknown and require explicit operator resolution.
                logger.error(
                    "delivery %s outcome cannot be reconciled; manual resolution "
                    "required (no automatic resend)",
                    delivery_id,
                )
                async with self.uow.transaction() as conn:
                    await self.delivery_repo.record_delivery_attempt(
                        conn,
                        delivery_id=delivery.id,
                        attempt_no=attempt_no,
                        status="failed",
                        error_kind="outcome_unknown",
                        error_message="reconciliation returned unknown; manual resolution required",
                        response={"reconciliation": "unknown"},
                    )
                    await self.delivery_repo.update_delivery_status(
                        conn,
                        delivery.id,
                        status="outcome_unknown",
                    )
                    res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                    if res is None:
                        raise RuntimeError(f"delivery {delivery.id} not found after reconciliation")
                    return res
            elif reconciliation == "not_delivered":
                # reconciliation == "not_delivered": definitive proof the payload
                # never arrived; resending the same immutable payload is safe.
                pass
            else:
                logger.error(
                    "delivery %s unexpected reconciliation status %r; halting resend",
                    delivery_id,
                    reconciliation,
                )
                async with self.uow.transaction() as conn:
                    res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                    if res is None:
                        raise RuntimeError(f"delivery {delivery.id} not found")
                    return res

        # Execute remote delivery
        try:
            result = await client.send_payload(destination=destination, payload=payload)
            ext_id = result.get("external_message_id", "sent")

            async with self.uow.transaction() as conn:
                await self.delivery_repo.record_delivery_attempt(
                    conn,
                    delivery_id=delivery.id,
                    attempt_no=attempt_no,
                    status="succeeded",
                    response=result,
                )
                await self.delivery_repo.update_delivery_status(
                    conn,
                    delivery.id,
                    status="succeeded",
                    external_delivery_id=ext_id,
                )
                res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                if res is None:
                    raise RuntimeError(f"delivery {delivery.id} not found after success")
                return res

        except TimeoutError as exc:
            logger.warning("delivery %s timed out with unknown outcome: %s", delivery_id, exc)
            async with self.uow.transaction() as conn:
                await self.delivery_repo.record_delivery_attempt(
                    conn,
                    delivery_id=delivery.id,
                    attempt_no=attempt_no,
                    status="failed",
                    error_kind="outcome_unknown",
                    error_message=str(exc),
                )
                await self.delivery_repo.update_delivery_status(
                    conn,
                    delivery.id,
                    status="outcome_unknown",
                )
                res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                if res is None:
                    raise RuntimeError(f"delivery {delivery.id} not found after timeout") from exc
                return res

        except Exception as exc:
            logger.error("delivery %s failed: %s", delivery_id, exc)
            async with self.uow.transaction() as conn:
                await self.delivery_repo.record_delivery_attempt(
                    conn,
                    delivery_id=delivery.id,
                    attempt_no=attempt_no,
                    status="failed",
                    error_kind=type(exc).__name__,
                    error_message=str(exc),
                )
                await self.delivery_repo.update_delivery_status(
                    conn,
                    delivery.id,
                    status="failed",
                )
                res = await self.delivery_repo.get_delivery_by_id(conn, delivery.id)
                if res is None:
                    raise RuntimeError(f"delivery {delivery.id} not found after failure") from exc
                return res

    async def _defer_deliver_payload(self, conn: psycopg.AsyncConnection, delivery_id: int) -> None:
        try:
            from src.jobs.publication import deliver_publication_payload

            await deliver_publication_payload.configure(connection=conn).defer_async(
                delivery_id=delivery_id
            )
        except Exception as err:
            # Re-raise so the surrounding transaction (including the delivery
            # rows just created) rolls back instead of stranding a pending
            # delivery that no job will ever pick up.
            logger.error(
                "could not defer deliver_publication_payload for delivery %s: %s",
                delivery_id,
                err,
            )
            raise
