# Design Spec: Dev Branch CI/CD and Server Deployment

**Date:** 2026-09-06  
**Status:** Approved by User  
**Target Branch:** `dev`  
**Target Server:** `92.5.58.200` (`opc` user, `/home/opc/Telebrief`)

---

## 1. Goal

Set up an automated, secure, and robust DevOps pipeline to build, test, package, and deploy the Telebrief `dev` branch to the server (`92.5.58.200`) on every push, with full traceability and rollback capability.

---

## 2. Architecture & Workflow

```
[Push to dev] / [workflow_dispatch]
           │
           ▼
     ┌───────────┐
     │  Job: CI  │ (Lint + Pytest)
     └─────┬─────┘
           │ (Pass)
           ▼
 ┌───────────────────┐
 │ Job: Build & Push │ (Docker Buildx + GHA Cache)
 └─────────┬─────────┘
           │ Pushes:
           │ - ghcr.io/djbob2000/telebrief:dev
           │ - ghcr.io/djbob2000/telebrief:dev-<sha>
           ▼
    ┌──────────────┐
    │ Job: Deploy  │ (SSH via deploy key)
    └──────┬───────┘
           │ SSH to 92.5.58.200:
           │ 1. Sync updated docker-compose.yml
           │ 2. docker compose pull
           │ 3. Run migrations (scripts/migrate.py)
           │ 4. docker compose up -d --remove-orphans
           │ 5. Health verification (docker compose ps)
           ▼
    [Running on Server]
```

---

## 3. Detailed Specifications

### 3.1 Docker Compose Parameterization (`docker-compose.yml`)

The image reference will be parameterized to default to `:dev`:
```yaml
services:
  telebrief-app:
    image: ghcr.io/djbob2000/telebrief:${IMAGE_TAG:-dev}
    container_name: telebrief-app
    ...

  telebrief-worker:
    image: ghcr.io/djbob2000/telebrief:${IMAGE_TAG:-dev}
    container_name: telebrief-worker
    ...

  postgres:
    image: pgvector/pgvector:pg18
    container_name: telebrief-postgres
    ...
```

This allows:
- Running `dev` by default in CI/CD and staging.
- Overriding `IMAGE_TAG=dev-<sha>` for pinpoint deployment or rolling back to a specific commit.

### 3.2 GitHub Actions Workflow (`.github/workflows/deploy-dev.yml`)

- **Name:** `Deploy Dev to Server`
- **Triggers:**
  - `push` on branch `dev`
  - `workflow_dispatch` (manual trigger with optional rollback `tag` input)
- **Permissions:**
  - `contents: read`
  - `packages: write`
- **Jobs:**
  1. **`test`**:
     - Fast lint check (`ruff`).
     - Unit tests (`pytest`).
  2. **`build-and-push`**:
     - Depends on: `test`.
     - Sets up Docker Buildx and logs into GHCR (`ghcr.io`).
     - Tags:
       - `ghcr.io/djbob2000/telebrief:dev`
       - `ghcr.io/djbob2000/telebrief:dev-${{ github.sha }}`
     - Uses GitHub Actions layer cache (`cache-from: type=gha`, `cache-to: type=gha,mode=max`).
  3. **`deploy`**:
     - Depends on: `build-and-push`.
     - Runs on `ubuntu-latest`.
     - Connects via SSH to `${{ secrets.SSH_HOST }}` as `${{ secrets.SSH_USER }}` using `${{ secrets.SSH_PRIVATE_KEY }}`.
     - Copies updated `docker-compose.yml` to `${{ secrets.WORK_DIR }}/docker-compose.yml`.
     - Runs server deployment commands:
       ```bash
       cd ${{ secrets.WORK_DIR }}
       docker compose pull
       docker compose run --rm telebrief-app python scripts/migrate.py
       docker compose up -d --remove-orphans
       docker compose ps
       ```

### 3.3 Server State & Migration Strategy

- **Existing State:**
  - Running legacy container `telebrief` (monolithic `python main.py`).
  - Persistent directories:
    - `/home/opc/Telebrief/.env` (contains secrets and API keys).
    - `/home/opc/Telebrief/config.yaml` (channel and edition settings).
    - `/home/opc/Telebrief/sessions/` (SQLite Telethon user session).
    - `/home/opc/Telebrief/data/` (published articles, editorial cache).
- **Target State:**
  - `postgres` container manages `postgres_data` volume with `pgvector:pg18`.
  - `telebrief-app` container runs scheduler and bot handler.
  - `telebrief-worker` container runs Procrastinate job workers.
  - Existing legacy `telebrief` container stopped and removed via `--remove-orphans`.
  - `.env`, `config.yaml`, `sessions/`, and `data/` remain untouched and mounted.

### 3.4 Security & Secrets

- Secrets stored in GitHub repository:
  - `SSH_PRIVATE_KEY`: ed25519 private key.
  - `SSH_HOST`: `92.5.58.200`
  - `SSH_USER`: `opc`
  - `WORK_DIR`: `/home/opc/Telebrief`
- Strict permissions: private key never exposed in logs or commits.
- Host key checking handled securely via known hosts or ssh-keyscan in the action runner.

---

## 4. Verification Plan

1. **Local Lint & Validation:**
   - Validate YAML syntax of `.github/workflows/deploy-dev.yml` and `docker-compose.yml`.
   - Verify `docker-compose config` locally.
2. **Server Pre-flight Check:**
   - Check available disk space and memory on `92.5.58.200`.
3. **Commit & Push to `dev`:**
   - Push changes to `origin/dev`.
4. **Pipeline Execution:**
   - Monitor GitHub Actions run in browser / CLI.
   - Verify image build and push in GHCR.
   - Verify SSH deployment step succeeds and containers are healthy.
5. **Post-deploy Verification on Server:**
   - `docker compose ps` shows `telebrief-app`, `telebrief-worker`, and `telebrief-postgres` as healthy/running.
   - `docker compose logs` shows successful startup.
