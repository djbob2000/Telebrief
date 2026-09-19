-- Allow 'telegram_photo_post' payload format for article publications with cover photos
ALTER TABLE publication_delivery_payloads
    DROP CONSTRAINT IF EXISTS publication_delivery_payloads_payload_format_check;

ALTER TABLE publication_delivery_payloads
    ADD CONSTRAINT publication_delivery_payloads_payload_format_check
    CHECK (payload_format IN ('telegram_html', 'telegraph_nodes', 'facebook_post', 'telegram_photo_post'));
