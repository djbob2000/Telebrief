# Local Regional Digest Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Настроить промпты Telebrief так, чтобы дайджест сохранял локально полезные новости Бердянска, Мелитополя и Якимовки и отбрасывал нерелевантный общенациональный шум.

**Architecture:** Изменения ограничиваются runtime-конфигурацией `config.yaml`: общий `prompt_extra` задаёт географию и критерий практической ценности, а описание группы `Другое` дополнительно фильтрует остаточные пункты. Конфигурация синхронизируется с Oracle и применяется перезапуском контейнера; код форматирования не меняется.

**Tech Stack:** YAML, Docker Compose, SSH, Telethon/DeepSeek через существующий Telebrief runtime.

## Global Constraints

- Основной фокус — Бердянск, Мелитополь, Якимовка и связанные с ними районы, службы и инфраструктурные сети.
- Общенациональные и международные события включаются только при прямом последствии для целевого региона.
- Прогнозы, сообщения жителей, слухи и пересказы допускаются при наличии локального факта или практического последствия; степень подтверждённости сохраняется в тексте.
- Не изменять код форматирования и структуру сообщения дайджеста.
- Секреты, `.env` и Telegram-сессии не выводить в терминал и не коммитить.

---

### Task 1: Update regional relevance prompts

**Files:**
- Modify: `config.yaml` (ignored runtime configuration; update `channels[0].prompt_extra` and `settings.digest_groups[name=Другое].description`)

**Interfaces:**
- Consumes: existing four channel entries and five existing digest groups.
- Produces: prompt text that the existing summarizer and topic grouper consume without code changes.

- [ ] **Step 1: Replace the general channel prompt**

  Keep the existing requirement to extract practical, verifiable value and source links, and add the approved regional scope: Бердянск, Мелитополь, Якимовка, nearby districts, regional services, and dependent infrastructure. Explicitly exclude Kyiv/other regions, international politics, military aid, and unrelated business damage unless a direct local consequence is stated.

- [ ] **Step 2: Add the uncertainty-quality rule**

  State that forecasts, resident reports, rumors, and retellings are not automatically excluded. Include them only when they contain a local fact or practical consequence, preserving qualifiers such as `предварительно`, `по сообщениям жителей`, `по данным сотрудников`, or `официального подтверждения нет`. Exclude opinions, panic, emotional assessments, political commentary, and repetitions without a new fact.

- [ ] **Step 3: Narrow the `Другое` group description**

  Require local importance, exclude general Kyiv/other-region news and unrelated international or business items, and omit the group when no suitable point remains.

- [ ] **Step 4: Validate the edited YAML structurally**

  Run:

  ```bash
  docker run --rm -v "$PWD:/app" -w /app telebrief:local \
    python -c 'import pathlib, sys; sys.path.insert(0, "/app"); import yaml; yaml.safe_load(pathlib.Path("config.yaml").read_text()); print("config_ok")'
  ```

  Expected: `config_ok`.

- [ ] **Step 5: Confirm the runtime-only change is not staged**

  Run `git status --short --ignored config.yaml` and confirm that `config.yaml` remains ignored. Do not force-add it: the file contains deployment-specific runtime settings and is intentionally excluded from version control.

### Task 2: Deploy and verify on Oracle

**Files:**
- Deploy: `/home/opc/Telebrief/config.yaml` on the Oracle instance

**Interfaces:**
- Consumes: the prompt configuration from Task 1.
- Produces: a healthy Telebrief container using the new prompt and the existing schedule/output settings.

- [ ] **Step 1: Copy only the configuration file**

  ```bash
  scp -q -i /Users/air/Downloads/ssh-key-2026-08-05.key \
    config.yaml opc@92.5.58.200:/home/opc/Telebrief/config.yaml
  ```

- [ ] **Step 2: Restart the service**

  ```bash
  ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200 \
    'chmod 600 /home/opc/Telebrief/config.yaml && cd /home/opc/Telebrief && sudo /usr/local/bin/docker compose restart telebrief'
  ```

- [ ] **Step 3: Verify startup and schedule**

  ```bash
  ssh -i /Users/air/Downloads/ssh-key-2026-08-05.key opc@92.5.58.200 \
    'sudo /usr/local/bin/docker inspect --format "status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}" telebrief; sudo /usr/local/bin/docker logs --since=2m telebrief'
  ```

  Expected: `status=running`, `health=healthy`, zero new restarts, four configured sources, and the next scheduled digest at 09:00 Europe/Kyiv.

- [ ] **Step 4: Review the next digest for the approved examples**

  Confirm that a regional blackout affecting Melitopol/Berdyansk/Yakymovka is eligible, while Kyiv-only attacks, unrelated business damage, and international statements without regional consequences are excluded.
