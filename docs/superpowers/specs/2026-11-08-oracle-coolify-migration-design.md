# Oracle A1 and Coolify migration design

**Status:** Awaiting user approval
**Date:** 2026-11-08

## Intent

Move the running Telebrief deployment from the existing Oracle VM to a new Ubuntu VM.Standard.A1.Flex instance managed through self-hosted Coolify. The new server must run alongside the old server during preparation, preserve Telebrief's persisted data and operating configuration, and become the sole active deployment after a verified cutover. The old instance may be stopped after the new deployment is working; it must remain available as a rollback copy.

## Current state and evidence

- The Oracle Console instance named `free-tier-a1` is currently **VM.Standard.E2.1.Micro**, running Oracle Linux 9, with 1 GB RAM and a 47 GB boot volume. It has no attached block volume. Its name does not describe its shape.
- The selected tenancy region is `eu-frankfurt-1` and the existing instance is in `free-tier-vcn`.
- Current Oracle Always Free documentation lists 1,500 A1 OCPU-hours and 9,000 GB-memory-hours monthly, equivalent to a continuous 2 OCPU / 12 GB A1 instance. It lists 200 GB total for boot and block volumes in the home region. The proposed 150 GB target boot volume plus the existing 47 GB boot volume uses 197 GB, leaving 3 GB, provided no other boot or block volumes consume the quota.
- The repository's `docker-compose.yml` runs the Telebrief app, three worker services, and pgvector/PostgreSQL. It relies on `.env`, `config.yaml`, `sessions/`, `data/`, `logs/`, the `telebrief_auth` named volume, and the `postgres_data` named volume.
- `config.yaml` enables the database. The app includes Telegram bot polling, scheduled publication work, and a Telethon user session. Running both complete deployments with the same credentials/session can conflict or duplicate external work.
- The current SSH deployment runs `scripts/migrate.py` after stopping processing/authority workers and before recreating application containers. Coolify must preserve migration-before-runtime ordering for the initial deployment and future updates.
- `.github/workflows/deploy-dev.yml` builds/pushes `dev` images on `dev` pushes and then directly deploys to the current server over SSH. The workflow does not specify image platforms, so its current build is tied to the GitHub runner architecture; the new A1 server requires ARM64 while the old E2 rollback host requires AMD64. `dev` is the canonical working and running branch; `main` is stale and must not be used as a source or branch point. The current local `dev` checkout has unrelated uncommitted edits in `src/publication/rubrics.py` and `tests/test_digest_rubrics.py`. Migration work must preserve those edits and must not use them as deployment content by accident.

## Goals

1. Create one Ubuntu LTS VM.Standard.A1.Flex with 2 OCPUs, 12 GB RAM, and a 150 GB boot volume in `eu-frankfurt-1`, using only Oracle resources visibly marked Always Free eligible.
2. Install Coolify from its official self-hosted install route, create its initial administrator through a user-controlled browser handoff, and deploy Telebrief through Coolify.
3. Preserve the production configuration, PostgreSQL contents, Telegram session, and other persisted application files without displaying secrets in chat or writing them into the repository.
4. Keep production processing single-active: prepare and inspect the new stack while the old app is active, then quiesce the old app before enabling the new app with copied production Telegram credentials.
5. Verify the new deployment's database, containers, logs, Telegram bot response, and scheduled-worker readiness. After successful verification, stop (do not terminate or resize) the old E2 instance and retain it and its boot volume for rollback.
6. Make Coolify the ongoing deployment authority so later repository updates cannot unexpectedly reactivate the old server.
7. Publish one GHCR image tag for both `linux/amd64` and `linux/arm64`, so the current old host remains a usable rollback target and the new A1 host can run the same revision.

## Non-goals

- No editorial, ingestion, or application behavior changes unrelated to deployment.
- No public web application or custom domain is required for Telebrief itself; it is a Telegram service. Coolify's administrative dashboard is the only web UI in this migration.
- No deletion, termination, or resizing of the old instance or its boot volume.
- No broad cleanup of OCI resources, DNS, networking, Git history, or repository configuration outside what is needed for this migration.
- No transfer of database dumps, credentials, or session files into a committed artifact.

## Anti-goals and failure conditions

- Do not launch the A1 instance if the account's actual boot/block-volume total would exceed 200 GB, if the Console does not mark the target shape/image/volume Always Free eligible, or if the 2/12 allocation is not available under the tenancy's current entitlement.
- Do not create extra paid volumes, paid images, load balancers, or other billable resources.
- Do not run both complete Telebrief deployments against the same Telegram bot token and user session.
- Do not start the new production stack from an unverified database restore or discard the old copy before successful validation.
- Do not publish or print `.env`, Telegram keys, API keys, session contents, SSH private keys, raw database dumps, or unredacted secret-bearing logs.
- Do not overwrite the user's unrelated working-tree edits or deploy them unless the live server is confirmed to be running that exact revision.

## Approaches considered

### 1. Coolify-managed deployment with state migration — selected

Create the new VM, install Coolify, connect the existing GitHub repository/configuration to Coolify, and transfer the old deployment's persisted state. Keep the app services inactive on the new VM while they are prepared. At cutover, stop the old app/workers, make a final database snapshot, restore/verify it on the new VM, start the new app/workers, validate external behavior, then stop the old VM while retaining its disk.

**Trade-off:** The cutover includes a short service pause so a single Telegram poller/scheduler owns production work. It preserves state and puts the new deployment under Coolify.

### 2. Fresh repository deployment with an empty database — rejected

This avoids transferring database state but loses production history and requires re-authenticating the Telegram user session. It does not satisfy a complete migration.

### 3. Full machine image/disk clone — rejected

This might preserve the host more literally, but it carries forward the old OS/runtime layout and bypasses the requested Coolify deployment model. It also adds unnecessary storage and recovery complexity.

## Design decisions

### OCI resource envelope

- Target shape: `VM.Standard.A1.Flex`, 2 OCPUs, 12 GB memory.
- Target OS: an Ubuntu LTS ARM64 image explicitly marked Always Free eligible in the Frankfurt Console.
- Boot disk: 150 GB, not a second 150 GB block volume. With the observed 47 GB old boot volume, this totals 197 GB of the documented 200 GB shared allocation.
- Before creation, inspect all boot and block volumes in the relevant tenancy/home region, including unattached volumes, and verify the live Console quota/Always Free label. If any other volume exists, recalculate before proceeding; do not assume the current instance's storage tab proves the tenancy total.
- Oracle may reclaim Always Free compute it considers idle when A1 CPU, network, and memory utilization all remain below 20% for seven days. Telebrief may be quiet enough to approach those thresholds; do not generate artificial traffic to evade reclamation. Treat this as an availability limitation of the free tier and report it during handoff.
- Reuse the existing public VCN/subnet only if route and subnet choices are suitable. Add a dedicated NSG for the new VM rather than broadening shared ingress rules. Keep SSH access restricted to the operator's current source IP where possible. Do not expose Coolify's direct dashboard/realtime/web-terminal ports publicly. Because no domain is specified, use an SSH tunnel to reach the dashboard during first-admin setup and later administration. Coolify's first administrator must be claimed immediately by the user; the assistant will not set or enter a new password.
- Do not open public 80/443 unless a public application route or certificate is later required. Telebrief itself has no inbound web service requirement.

### Source and deployment authority

- Inspect the old server's running image digest/tag, work directory, compose state, and actual configuration before choosing the deployment revision. The target must reproduce that known running revision, not assume the current local `dev` checkout is production.
- The source of application configuration remains `djbob2000/Telebrief`; Coolify manages the Compose deployment. Configure a Git-based Compose resource on branch `dev` to consume `ghcr.io/djbob2000/telebrief:dev`. Keep the GitHub Actions image build/publish and add `linux/amd64` and `linux/arm64` with QEMU. Replace the current direct SSH deployment with image publication only. Disable Coolify's Git-push auto-deploy so it cannot pull `dev` before CI updates the tag. After CI succeeds, the operator starts deployment manually from the Coolify dashboard through the SSH tunnel. This avoids exposing Coolify publicly, does not require a custom domain or GitHub-held deployment token, and does not build on the 2-OCPU VM.
- There must be one defined deployment path after cutover; do not leave the old SSH target able to redeploy the old host unexpectedly. Future changes are deployed by running the GitHub Actions build, then selecting Deploy in Coolify after confirming the image build succeeded.
- Keep the old host's `.env` resolution unchanged while allowing the new host to use a protected absolute env file. Change the four Telebrief service `env_file` entries to `${TELEBRIEF_ENV_FILE:-.env}`. In Coolify, set `TELEBRIEF_ENV_FILE=/etc/telebrief/telebrief.env`, write the migrated file there with owner-only permissions over SSH, and set the Compose interpolation values (including the database credentials and `IMAGE_TAG=dev`) in Coolify. This keeps application secrets out of Git, deployment output, and the browser form.
- Preserve the existing migration-before-runtime gate in Compose. Add one `telebrief-migrate` service using the same image and environment as the app, with command `python scripts/migrate.py`, `restart: "no"`, and a healthy-PostgreSQL dependency. Make the app and all three workers depend on both healthy PostgreSQL and `telebrief-migrate` completing successfully. This makes every Coolify deployment run the idempotent schema migration before new runtime code can start.
- Preserve repository working-tree changes. Create deployment workflow/config changes in an isolated worktree based on current `origin/dev`, never `main`; keep unrelated local modifications out of migration commits and review changes before pushing.

### State migration and cutover

1. Read-only source preflight: identify compose project path, running services, image revision, volume mounts, database schema, network/DNS assumptions, and all required state locations. Inspect secret presence without emitting values.
2. Prepare the target VM and Coolify; verify OS architecture, Docker/Compose, disk space, Coolify dashboard, and restricted access.
3. Configure a Coolify-managed resource using the verified code/image revision and persistent volumes. Transfer `config.yaml`, the `.env` file to `/etc/telebrief/telebrief.env`, `sessions/`, `data/`, `logs/` where operationally useful, and `telebrief_auth` directly to the target over SSH. The target Compose app uses that owner-only env file through `TELEBRIEF_ENV_FILE`; the old server keeps the default relative `.env`. Transfer PostgreSQL as a logical custom-format dump (`pg_dump -Fc`) streamed from the source PostgreSQL container over SSH into a mode-0600 temporary file on the target; do not copy the raw PostgreSQL volume and do not route the dump through the workstation. Keep the database port private. Verify the dump's SHA-256 and archive readability (`pg_restore --list`) before restoring it into the target PostgreSQL volume, then verify restore exit status, schema compatibility, and key table counts. Keep temporary data owner-readable only and remove migration-only temporary files after restore verification. Do not expose production credentials in a build log or frontend form beyond the deployment's necessary secret store.
4. During parallel preparation, do not start the new Telegram bot polling and production workers with copied credentials. Build/pull images, start/verify Coolify and PostgreSQL, restore a pre-cutover snapshot for a rehearsal if needed, and keep production app/worker services stopped.
5. At cutover, quiesce/stop old app and workers to prevent writes and external side effects; take a final consistent `pg_dump -Fc` and stream it over SSH to the target; verify checksum/archive readability and restore it. The Coolify deployment's `telebrief-migrate` dependency then applies the same migration script used by the current deploy workflow before starting app/workers. The old database volume remains untouched as the rollback source until the target has passed validation.
6. Verify container readiness, database connectivity and schema, startup logs with secrets redacted, Telegram `/status` or an equivalent bot check, and that the configured schedule/work queues are available. Do not trigger a production digest just to prove delivery unless the user explicitly asks for that behavior.
7. Before the new app or workers create external side effects, a failed validation can roll back by stopping the target stack and restarting the old stack. After the new deployment begins Telegram delivery or other production work, treat it as the active source of truth: preserve a fresh target database dump and fix forward. If an emergency rollback is unavoidable after that point, stop the target, restore its latest database into the old host before restarting old workers, and note the residual at-least-once delivery risk for any action that may have succeeded externally before its database commit.
8. On success, stop the old OCI VM only; keep it and its boot volume intact. Document which deployment is active.

### Completion criteria

- Oracle confirms Always Free eligibility for the A1 shape, Ubuntu image, and 150 GB boot volume; aggregate storage is at most 200 GB and compute is within the current 2/12 A1 limit.
- Coolify is running and the user has claimed its initial admin account.
- The Coolify-managed Telebrief release matches the verified production revision and restored state.
- Telebrief application, database, and worker services run stably on the new server, and Telegram `/status` responds from the new deployment.
- The old VM is stopped (not deleted) only after the user and operator can observe the successful new deployment; its boot volume remains present for rollback.
- A push to `dev` publishes an image manifest containing both AMD64 and ARM64; the successful release is then deployed manually through Coolify over the SSH tunnel. No workflow step targets either server.
- Coolify's `telebrief-migrate` service exits successfully before app/workers start on each deployment; a migration error prevents new runtime services from starting.

## Remaining builder calls

- Verify current Oracle volume totals, actual service limits, regional A1 capacity, current Ubuntu ARM64 image eligibility, and whether 150 GB is accepted as a boot volume in the live create form. If the requested configuration cannot be kept Always Free, stop and report the exact safe alternatives before creating anything.
- Determine from the old VM whether the running release is the `dev` tag or an immutable GHCR tag and align Coolify with it. Confirm the target can pull the ARM64 manifest entry and the old host can still pull AMD64.
- The app is Telegram-only, so no custom domain is assumed. Use SSH-tunnel access for Coolify and do not broaden public dashboard access to make setup easier. If tunneling from the user's workstation is unavailable, pause before selecting another exposure method.
- The actual running server environment is authoritative for source data; the local uncommitted checkout is not a migration source unless explicitly verified.

## External references

- Oracle Always Free resource limits: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Coolify self-hosted setup: https://coolify.io/docs/start-with-self-hosted
- Coolify firewall ports: https://coolify.io/docs/core/infrastructure/servers/firewall
