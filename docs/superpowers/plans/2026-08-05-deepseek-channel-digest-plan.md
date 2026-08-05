# DeepSeek Channel Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure Telebrief to summarize `@berdiansk_me` with DeepSeek `deepseek-v4-flash` and publish to `@berdiansk_news`.

**Architecture:** Reuse the existing OpenAI-compatible provider with an optional `OPENAI_BASE_URL` override for DeepSeek. Add a separate destination chat setting so owner authorization remains independent from the channel receiving digests. Keep backward compatibility by defaulting the destination to the owner user ID.

**Tech Stack:** Python 3.14, Telethon, python-telegram-bot, PyYAML, OpenAI Python SDK, pytest, Docker Compose, OrbStack.

## Global Constraints

- Use `OPENAI_BASE_URL=https://api.deepseek.com/v1` and model `deepseek-v4-flash`.
- Use timezone `Europe/Kyiv` and Russian output.
- Never print or commit API keys, bot tokens, Telegram sessions, databases, or logs.
- Keep `target_user_id` as the sole owner authorization identity.
- Preserve legacy configs by defaulting `target_chat_id` to `target_user_id`.
- Use the locally built arm64 image because the published image lacks an arm64 manifest.

### Task 1: Add OpenAI-compatible endpoint configuration

**Files:**
- Modify: `src/ai_providers.py:OpenAIProvider/create_provider`
- Modify: `src/config_loader.py:Config/_load_and_validate_env_vars/load_config`
- Modify: `config.yaml.example`
- Modify: `.env.example`
- Test: `tests/test_ai_providers.py`
- Test: `tests/test_config_loader.py`

**Interfaces:**
- `create_provider(..., openai_base_url: str = "")` passes a non-empty URL to `AsyncOpenAI(base_url=...)`.
- `Config.openai_base_url` contains `OPENAI_BASE_URL` or an empty string.

- [ ] **Step 1: Write failing tests** for `AsyncOpenAI` receiving the DeepSeek base URL and config loading the optional env var.
- [ ] **Step 2: Run targeted tests** with `npx` not applicable; run `pytest tests/test_ai_providers.py tests/test_config_loader.py -q` and confirm the new assertions fail.
- [ ] **Step 3: Implement the optional base URL** and pass it through summarizer and grouper provider construction.
- [ ] **Step 4: Add example env/config values** documenting `OPENAI_BASE_URL` and model `deepseek-v4-flash`.
- [ ] **Step 5: Run the targeted tests** and confirm they pass.

### Task 2: Separate owner authorization from destination chat

**Files:**
- Modify: `src/config_loader.py:Settings/load_config`
- Modify: `src/sender.py:DigestSender`
- Modify: `src/utils.py:message ID storage type hints`
- Modify: `src/core.py:generate_and_send_digest`
- Modify: `config.yaml.example`
- Test: `tests/test_sender.py`
- Test: `tests/test_config_loader.py`

**Interfaces:**
- `Settings.target_chat_id: str | int` identifies the Telegram destination.
- Public sender methods keep their existing `user_id` parameter as the requesting owner and send to `target_chat_id`.

- [ ] **Step 1: Write failing tests** proving an authorized owner request sends to `@berdiansk_news`, an unauthorized request is rejected, and legacy configs default destination to the owner ID.
- [ ] **Step 2: Run the sender/config tests** and confirm the new destination assertions fail.
- [ ] **Step 3: Implement destination routing** for all send/delete operations and track message IDs under the destination key.
- [ ] **Step 4: Run the sender/config tests** and confirm they pass without breaking legacy tests.

### Task 3: Create the user configuration and local secret file

**Files:**
- Create: `config.yaml` (ignored local runtime configuration)
- Create: `.env` (ignored local secrets)
- Create: `sessions/`, `logs/`, `data/` (ignored runtime directories)

- [ ] **Step 1: Write `config.yaml`** with source `@berdiansk_me`, destination `@berdiansk_news`, schedule `09:00`, timezone `Europe/Kyiv`, Russian output, and the four requested topic groups.
- [ ] **Step 2: Store the user-provided DeepSeek key** only in `.env` as `OPENAI_API_KEY`; add `OPENAI_BASE_URL` and leave unavailable Telegram values clearly empty without printing them.
- [ ] **Step 3: Validate YAML** with the local image or PyYAML and verify the secret-bearing files are ignored by Git.

### Task 4: Build, authorize, and launch

**Files:**
- Modify: `docker-compose.yml` to use local arm64 image without changing secret handling.
- Runtime: `sessions/user.session`, `logs/`, `data/`.

- [ ] **Step 1: Run `docker compose config`** and fix any missing-value or mount errors.
- [ ] **Step 2: Create the Telethon session** using `create_session.sh`, entering the user's phone, Telegram code, and 2FA only in the interactive terminal.
- [ ] **Step 3: Ensure the bot is an administrator** of `@berdiansk_news` and the channel exists with the requested username.
- [ ] **Step 4: Start the local Compose service** and inspect logs, container restart count, and health state.
- [ ] **Step 5: Send `/status` and then `/digest`** from the authorized Telegram account; verify a real message appears in `@berdiansk_news`.

### Task 5: Final verification

- [ ] **Step 1: Run targeted pytest tests** for config, provider, sender, and core paths.
- [ ] **Step 2: Run `docker compose config --quiet`** and inspect `docker compose ps`.
- [ ] **Step 3: Confirm no secrets, session files, logs, or data are tracked** with `git status --short` and ignore checks.
- [ ] **Step 4: Report only non-sensitive results**, including image architecture, model, source/destination, and any remaining Telegram action required.
