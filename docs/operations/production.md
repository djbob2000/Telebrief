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

### Configure Coolify auto-deploy

Use the existing Coolify resource for Telebrief. Do not create a second application or a separate GitHub Actions deployment.

1. Open [Coolify](https://githubify.duckdns.org), then open the Telebrief resource in the `production` environment.
2. Open **Git Source**. Select the connected Coolify GitHub App source for `djbob2000/Telebrief` and set the branch to `dev`. Do not select `main`.
3. Confirm the build settings: Docker Compose, Base Directory `/`, and Docker Compose Location `/docker-compose.yml`.
4. Open **Advanced → Build & deploy → Deployment**. Set **Auto deploy** to **Deploy on push (webhooks)**.
5. Confirm the GitHub App is installed only for `djbob2000/Telebrief` and has the `Push` webhook event enabled. It needs repository read access; pull request and GitHub Actions permissions are not part of this deploy path. Coolify manages the webhook; do not add a second webhook by hand.
6. If changing the Git source causes Coolify to show **Deploy again to apply** or **Rebuild required**, run one redeploy and wait for it to finish. This applies the selected source to the running resource.

### What happens after a push

1. Push a commit to `dev`. GitHub sends a push event to Coolify automatically.
2. Coolify creates a deployment for that commit and builds the Compose image on the production VM. The build is asynchronous; the push starts the deployment, but the new version is not live immediately.
3. Coolify runs the database migration service, then starts the app and workers. The new release is ready when the deployment reports **Success**, the resource reports **Running**, and PostgreSQL, the app, and all three workers are healthy.

In Coolify, use **Deployment Logs** to follow the build and **Deployment history** to check that the entry is marked `Webhook` and references the commit you pushed. A failed or still-running deployment does not confirm that the new commit is live. If it fails, inspect that deployment's logs before retrying.

**Last verified:** on 2026-11-10, pushing commit `3451ebd` to `dev` triggered a `Webhook` deployment. Coolify reported `Success`; PostgreSQL, the app, and all three workers reported healthy, and the app logs showed the bot polling and scheduler active.

`.github/workflows/deploy-dev.yml` is intentionally not used: Coolify builds the `dev` branch directly. `.github/workflows/docker-publish.yml` remains a separate release-image workflow for `v*` tags; it does not deploy production.

The Docker Compose file defines one `build: .` on `telebrief-app` and tags that local build as `telebrief:${IMAGE_TAG:-dev}`. Migration and worker services use the same image tag. Keep this shared-image configuration and the migration dependency intact. Coolify runs the build on the production VM, so stagger future Next.js builds and monitor memory and disk before changing PostgreSQL settings.

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

Use [Database Backup and Disaster Recovery](database-backup.md) for backup and restore commands. For server-to-server migration, use a PostgreSQL custom-format dump (`pg_dump -Fc`) streamed over SSH to a mode-0600 temporary file on the target. Do not route the dump through the workstation or Git.

Before restoring, verify the transfer checksum and that `pg_restore --list` can read the archive. Confirm the restore exit status and schema/table checks. Keep the old database volume intact until the new deployment is accepted. Never run a destructive `pg_restore --clean` against the only production copy.

## Routine deployment checklist

1. Follow **What happens after a push** above and confirm the deployed commit matches the commit you pushed to `dev`.
2. In **Deployment Logs**, confirm `telebrief-migrate` completed successfully. In **Runtime Logs** or service status, confirm PostgreSQL, the app, and all three workers are healthy. Review logs without exposing secrets.
3. For a production smoke check, use the bot's `/status` command. Do not trigger a real digest or channel publication just to test a deployment.
4. Keep the rollback VM stopped and its volume intact until the new deployment is accepted and rollback is no longer needed.

If a deployment fails before external production work occurs, stop the new app/workers and restore service from the old host. If the new system already processed jobs or published to Telegram, preserve its latest database state and reconcile it before restarting old workers to avoid duplicate side effects.

Coolify's documented paths: [GitHub App setup](https://coolify.io/docs/applications/sources/github/app), [GitHub auto deploy](https://coolify.io/docs/applications/sources/github/auto-deploy), [Docker Compose builds](https://coolify.io/docs/applications/builds/docker-compose), [instance settings](https://coolify.io/docs/core/instance-management/instance-settings), and [firewall requirements](https://coolify.io/docs/core/infrastructure/servers/firewall).
