"""Tests for delivery destinations, payloads, deliveries, and delivery constraints (Plan 4 Task 1)."""

import datetime as dt

import psycopg
import pytest

from src.publication.repository import (
    DeliveryRepository,
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition_id, config_hash="elig-hash-1", prompt_version="elig-v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-1", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-1", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


@pytest.mark.postgres
class TestDeliveryConstraints:
    """Tests DB constraints for destinations, delivery payloads, and deliveries."""

    async def test_delivery_payload_must_match_publication(
        self, conn: psycopg.AsyncConnection, edition
    ):
        pub_repo = PublicationRepository()
        deliv_repo = DeliveryRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run1 = await pub_repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-deliv-run-1",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        attempt1 = await pub_repo.insert_generation_attempt(
            conn, run_id=run1.id, attempt_no=1, kind="writer"
        )
        pub1 = await pub_repo.create_publication(
            conn,
            run_id=run1.id,
            winning_attempt_id=attempt1.id,
            publication_type="article",
            title="Заголовок 1",
            lead=None,
            body="Тело 1",
        )

        run2 = await pub_repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-deliv-run-2",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        attempt2 = await pub_repo.insert_generation_attempt(
            conn, run_id=run2.id, attempt_no=1, kind="writer"
        )
        pub2 = await pub_repo.create_publication(
            conn,
            run_id=run2.id,
            winning_attempt_id=attempt2.id,
            publication_type="article",
            title="Заголовок 2",
            lead=None,
            body="Тело 2",
        )

        dest = await deliv_repo.get_or_create_destination(
            conn,
            edition_id=edition.id,
            platform="telegram_channel",
            destination_key="@berdyansk_channel",
        )

        payload_pub1 = await deliv_repo.create_payload(
            conn,
            publication_id=pub1.id,
            destination_id=dest.id,
            payload_format="telegram_html",
            rendered_content={"text": "HTML content"},
            content_hash="hash-p1",
        )

        # Creating a delivery for pub2 using payload from pub1 must fail composite FK
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await deliv_repo.create_delivery(
                conn,
                publication_id=pub2.id,
                destination_id=dest.id,
                payload_id=payload_pub1.id,
                idempotency_key="deliv-key-mismatch",
            )


@pytest.mark.postgres
class TestPublicationDeliveryService:
    """Tests for PublicationDeliveryService execution, reconciliation, and attempt recording."""

    async def test_prepare_payloads_and_successful_delivery(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        from src.db.uow import DatabaseUnitOfWork
        from src.publication.delivery import PublicationDeliveryService
        from src.publication.repository import PublicationRepository

        uow = DatabaseUnitOfWork(pool)
        pub_repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # 1. Seed publication
        run = await pub_repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-deliv-service-run",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        att = await pub_repo.insert_generation_attempt(
            conn, run_id=run.id, attempt_no=1, kind="writer"
        )
        pub = await pub_repo.create_publication(
            conn,
            run_id=run.id,
            winning_attempt_id=att.id,
            publication_type="article",
            title="Заголовок для доставки",
            lead="Лид для доставки",
            body="Тело статьи для доставки",
        )

        service = PublicationDeliveryService(uow=uow)

        # 2. Prepare payloads
        deliveries = await service.prepare_payloads(pub.id)
        assert len(deliveries) == 1
        deliv = deliveries[0]
        assert deliv.status == "pending"

        # 3. Deliver
        deliv_result = await service.deliver(deliv.id)
        assert deliv_result.status == "succeeded"
        assert deliv_result.external_delivery_id == "ext-msg-12345"

        # 4. Check attempts recorded
        cur = await conn.execute(
            "SELECT id, attempt_no, status FROM publication_delivery_attempts WHERE publication_delivery_id = %s",
            (deliv.id,),
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == 1
        assert rows[0][2] == "succeeded"

    async def test_outcome_unknown_triggers_reconciliation(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        from src.db.uow import DatabaseUnitOfWork
        from src.publication.delivery import (
            PublicationDeliveryService,
        )
        from src.publication.repository import PublicationRepository

        uow = DatabaseUnitOfWork(pool)
        pub_repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run = await pub_repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-deliv-reconcile-run",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        att = await pub_repo.insert_generation_attempt(
            conn, run_id=run.id, attempt_no=1, kind="writer"
        )
        pub = await pub_repo.create_publication(
            conn,
            run_id=run.id,
            winning_attempt_id=att.id,
            publication_type="article",
            title="Заголовок для таймаута",
            lead=None,
            body="Тело для таймаута",
        )

        class ReconcilingClient:
            def __init__(self):
                self.send_count = 0
                self.reconcile_count = 0

            async def send_payload(self, destination, payload):
                self.send_count += 1
                raise TimeoutError("connection dropped while awaiting ack")

            async def reconcile_outcome(self, destination, payload, last_attempt):
                self.reconcile_count += 1
                return "succeeded"

        client = ReconcilingClient()
        service = PublicationDeliveryService(
            uow=uow,
            clients={"telegram_channel": client},
        )

        deliveries = await service.prepare_payloads(pub.id)
        deliv_id = deliveries[0].id

        # First delivery attempt: raises TimeoutError -> status outcome_unknown
        deliv1 = await service.deliver(deliv_id)
        assert deliv1.status == "outcome_unknown"
        assert client.send_count == 1

        # Second delivery attempt: reconciles without sending duplicate payload
        deliv2 = await service.deliver(deliv_id)
        assert deliv2.status == "succeeded"
        assert client.reconcile_count == 1
        assert client.send_count == 1  # did not re-send!
