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
        self.clients = clients or {"telegram_channel": MockDestinationClient()}

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
                # Get or create default destination for edition
                run = await self.pub_repo.get_run_by_id(conn, pub.publication_run_id)
                edition_id = run.edition_id if run else 1
                default_dest = await self.delivery_repo.get_or_create_destination(
                    conn,
                    edition_id=edition_id,
                    platform="telegram_channel",
                    destination_key="@telebrief_default",
                )
                dests = [default_dest]

            created_deliveries: list[PublicationDelivery] = []
            for dest in dests:
                # Format immutable destination payload
                raw_text = f"{pub.title}\n\n{pub.body}"
                payload_bytes = raw_text.encode("utf-8")
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()

                payload = await self.delivery_repo.create_payload(
                    conn,
                    publication_id=pub.id,
                    destination_id=dest.id,
                    payload_format="telegram_html",
                    rendered_content={"text": raw_text},
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
                await self._defer_deliver_payload(conn, delivery.id)

            return created_deliveries

    async def deliver(self, delivery_id: int) -> PublicationDelivery:
        async with self.uow.transaction() as conn:
            delivery = await self.delivery_repo.get_delivery_by_id(conn, delivery_id)
            if delivery is None:
                raise ValueError(f"delivery {delivery_id} not found")

            if delivery.status in ("succeeded", "failed_terminal"):
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

        client = self.clients.get(destination.platform, MockDestinationClient())

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
                    return await self.delivery_repo.get_delivery_by_id(conn, delivery.id)  # type: ignore

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
                return await self.delivery_repo.get_delivery_by_id(conn, delivery.id)  # type: ignore

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
                return await self.delivery_repo.get_delivery_by_id(conn, delivery.id)  # type: ignore

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
                    status="failed_retryable",
                )
                return await self.delivery_repo.get_delivery_by_id(conn, delivery.id)  # type: ignore

    async def _defer_deliver_payload(self, conn: psycopg.AsyncConnection, delivery_id: int) -> None:
        try:
            from src.jobs.publication import deliver_publication_payload

            await deliver_publication_payload.configure(connection=conn).defer_async(
                delivery_id=delivery_id
            )
        except Exception as err:
            logger.warning(
                "could not defer deliver_publication_payload for delivery %s: %s",
                delivery_id,
                err,
            )
