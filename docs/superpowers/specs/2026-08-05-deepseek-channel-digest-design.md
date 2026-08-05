# DeepSeek Channel Digest Design

## Goal

Configure Telebrief to collect the public Telegram source `@berdiansk_me`, summarize it in Russian with DeepSeek model `deepseek-v4-flash`, and publish the digest to the public Telegram channel `@berdiansk_news`.

## Architecture

DeepSeek will use Telebrief's existing OpenAI-compatible provider. An optional `OPENAI_BASE_URL` environment variable will point the provider at `https://api.deepseek.com/v1`; the existing provider name remains `openai` so summarization and fallback behavior stay unchanged.

Delivery will separate the owner identity from the destination chat. `target_user_id` remains the authorized user for bot commands, while a new `target_chat_id` controls where scheduled and manual digest messages are sent. For this setup the destination is the public username `@berdiansk_news`, which Telegram Bot API accepts as a chat ID when the bot is an administrator of the channel.

## Configuration

- Source: `@berdiansk_me`.
- Destination: `@berdiansk_news`.
- Provider: existing `openai` adapter with `OPENAI_BASE_URL=https://api.deepseek.com/v1`.
- Model: `deepseek-v4-flash`.
- Timezone: `Europe/Kyiv`.
- Output language: Russian.
- Digest mode: topic groups for practical advice, recommendations, useful links, and warnings.
- Secrets remain only in local `.env`; no secret is committed or logged.

## Error handling and compatibility

Existing configurations without `target_chat_id` continue sending to `target_user_id`. Existing OpenAI configurations without `OPENAI_BASE_URL` continue using the SDK default endpoint. Message tracking and cleanup are keyed by the destination chat, while authorization checks remain keyed by the owner user.

## Verification

Add focused tests for OpenAI-compatible base URL propagation, config parsing/defaults, and sender destination-versus-authorization behavior. Validate the generated YAML and Compose configuration, build the local arm64 image, then run the container after Telegram credentials and session are available. A real `/status` or `/digest` response and container logs are required before declaring the deployment complete.
