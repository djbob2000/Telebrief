-- Canonical digest artifacts are delivered to Telegram with explicit entities.
-- Keep the database payload-format contract aligned with the delivery adapter.
ALTER TABLE publication_delivery_payloads
    DROP CONSTRAINT IF EXISTS publication_delivery_payloads_payload_format_check;

ALTER TABLE publication_delivery_payloads
    ADD CONSTRAINT publication_delivery_payloads_payload_format_check
    CHECK (
        payload_format IN (
            'telegram_html',
            'telegraph_nodes',
            'facebook_post',
            'telegram_photo_post',
            'telegram_entities'
        )
    );
