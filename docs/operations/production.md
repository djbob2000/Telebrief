# Production operations: Oracle and Coolify

This runbook describes Telebrief production on Oracle Cloud Infrastructure (OCI) through Coolify. OCI and Coolify settings can change, so check the live consoles before deploying, stopping, or restoring resources. Do not store credentials, Telegram sessions, database dumps, private SSH keys, or account-specific connection details here.

For VM provisioning and migration history, see [Server migration runbook](server-migration.md). For the budget and Free Tier guardrail, see [Oracle Cloud budget guide](oracle-cloud-budget.md).

## Servers and access

- The production VM is an Ubuntu ARM64 `VM.Standard.A1.Flex` in `eu-frankfurt-1`, configured with 2 OCPUs and 12 GB RAM. Find its current public IP, SSH login, and key in OCI Console → Compute → Instances.
- The rollback VM `free-tier-a1` is an `VM.Standard.E2.1.Micro`; it is stopped and its boot volume is retained. Do not terminate it or delete its volume while it is the rollback copy.
- Coolify dashboard: [https://githubify.duckdns.org](https://githubify.duckdns.org). The dashboard uses HTTPS. SSH access and a local tunnel remain available for administration if needed.
- OCI ingress: TCP/22 is restricted to the administrator's current IP `/32`; TCP/80 and TCP/443 are public for the Coolify proxy, HTTPS, and GitHub webhook delivery. Keep TCP/8000, TCP/6001, and TCP/6002 closed to the public internet.

## Deployment path

The production source is the public repository's `dev` branch. `main` is stale for this service and must not be used for production deployments.

```text
push to dev
  → Coolify GitHub App receives the repository webhook
  → Coolify clones the pushed dev commit
  → Docker Compose builds the Telebrief image from the repository Dockerfile on the ARM VM
  → telebrief-migrate runs, then the application and workers start
```

The Coolify instance URL and webhook endpoint are `https://githubify.duckdns.org`. The Telebrief resource uses the GitHub App source for only `djbob2000/Telebrief`, branch `dev`, Docker Compose build strategy, base directory `/`, and `/docker-compose.yml`. In **Advanced → Deployment**, **Auto deploy** must be **Deploy on push (webhooks)**. GitHub App installation is limited to this repository; pull request updates, GitHub Actions runners, and GitHub deployment statuses are disabled.

`.github/workflows/deploy-dev.yml` is intentionally not used: Coolify builds the `dev` branch directly. `.github/workflows/docker-publish.yml` remains a separate release-image workflow for `v*` tags; it does not deploy production.

The Docker Compose file defines `build: .` for the migration, app, and worker services and tags that local build as `telebrief:${IMAGE_TAG:-dev}`. Keep these build definitions and the migration dependency intact. Coolify runs the build on the production VM, so stagger future Next.js builds and monitor memory and disk before changing PostgreSQL settings.

Keep the existing Telegram destination: bot `@berdiansk_news_digest_bot` publishes to channel `@berdiansk_news`. Do not create or substitute another channel or bot.

## Services and persistent state

The Compose stack contains:

- `postgres`: PostgreSQL 18 with pgvector;
- `telebrief-migrate`: one-shot `python scripts/migrate.py` after PostgreSQL is healthy;
- `telebrief-app` and three workers: `telebrief-worker`, `telebrief-processing-worker`, and `telebrief-authority-worker`.

All app and worker services wait for the migration service to complete successfully. Verify this dependency after Compose changes.

| Data | Compose mount | Handling |
| --- | --- | --- |
| Database | `postgres_data` named volume | Production records and job state; back up before restore or destructive changes. |
| Telegram/Facebook auth | `telebrief_auth` named volume and `./sessions` | Sensitive authentication state; transfer only over a protected channel. |
| Application files | `./data` and `./logs` | Persistent app data and logs. |
| Runtime config | `./config.yaml` mounted read-only | Confirm the deployed revision and config before changing it. |
| Environment | `${TELEBRIEF_ENV_FILE:-.env}` | The server-side env file is `/etc/telebrief/telebrief.env`, owner-readable only. Coolify Environment Variables also supply the required application values. Never print them. |

Coolify may resolve relative bind mounts under its resource directory. Check rendered Compose and actual host mounts before assuming where `./data`, `./sessions`, or `./logs` live.

## Database backup and migration

Use [Database Backup and Disaster Recovery](../database_backup.md) for backup and restore commands. For server-to-server migration, use a PostgreSQL custom-format dump (`pg_dump -Fc`) streamed over SSH to a mode-0600 temporary file on the target. Do not route the dump through the workstation or Git.

Before restoring, verify the transfer checksum and that `pg_restore --list` can read the archive. Confirm the restore exit status and schema/table checks. Keep the old database volume intact until the new deployment is accepted. Never run a destructive `pg_restore --clean` against the only production copy.

## Deploy and verify

1. Push the intended change to `dev`; verify that Coolify receives the GitHub webhook and starts a deployment for that commit.
2. In Coolify Deployment Logs, confirm it cloned the intended `dev` commit, built the Compose application image on the VM, and that `telebrief-migrate` succeeded.
3. Confirm PostgreSQL is healthy and the app plus all three workers are running. Review logs without exposing secrets.
4. Use the bot's `/status` command for a production smoke check. Do not trigger a real digest or channel publication just to test a deployment.
5. Keep the old VM stopped and its volume intact until the new deployment is accepted and rollback is no longer needed.

If a deployment fails before external production work occurs, stop the new app/workers and restore service from the old host. If the new system already processed jobs or published to Telegram, preserve its latest database state and reconcile it before restarting old workers to avoid duplicate side effects.

Coolify's documented paths: [GitHub App setup](https://coolify.io/docs/applications/sources/github/app), [GitHub auto deploy](https://coolify.io/docs/applications/sources/github/auto-deploy), [Docker Compose builds](https://coolify.io/docs/applications/builds/docker-compose), [instance settings](https://coolify.io/docs/core/instance-management/instance-settings), and [firewall requirements](https://coolify.io/docs/core/infrastructure/servers/firewall).
