# Oracle Coolify Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the live Telebrief deployment to an Always Free eligible Ubuntu A1 2 OCPU / 12 GB instance, managed by Coolify, with production data preserved and the old E2 instance retained stopped for rollback.

**Architecture:** Coolify will manage a Git-based Docker Compose resource on the canonical `dev` branch. GitHub Actions will build and publish a dual-architecture GHCR image; an operator will start deployment manually through Coolify over an SSH tunnel after the image build succeeds. The old direct SSH deployment will be removed. Database and filesystem state will be transferred securely, with PostgreSQL restored from a logical dump and Compose migrations gated before app and worker startup.

**Tech Stack:** Oracle Cloud Infrastructure, Ubuntu ARM64, Coolify, Docker Compose, GitHub Actions, Docker Buildx/QEMU, GHCR, PostgreSQL 18 with pgvector, SSH.

**Spec:** [docs/superpowers/specs/2026-11-08-oracle-coolify-migration-design.md](../specs/2026-11-08-oracle-coolify-migration-design.md)

## Global Constraints

- Always Free A1 allowance: **2 OCPUs / 12 GB memory**; use exactly this A1 shape.
- Always Free boot and block-volume allowance: **200 GB total** in the home region.
- Existing old boot volume: **47 GB**. Proposed new boot volume: **150 GB**. Combined total: **197 GB**, leaving **3 GB** for any other volumes.
- The existing `free-tier-a1` is actually a running `VM.Standard.E2.1.Micro`, not an A1 instance.
- OCI documents Always Free A1 reclamation when CPU, network, and memory each stay below 20% for seven days; do not generate artificial traffic to avoid this.
- `dev` is the canonical working and running branch; `main` is stale and must not be used as a source or branch point.
- Preserve unrelated uncommitted changes in `src/publication/rubrics.py` and `tests/test_digest_rubrics.py`.
- Never run the complete old and new Telebrief stacks concurrently with the same Telegram credentials or session.
- Do not print, commit, or log secrets, session contents, database dumps, Coolify tokens, or unredacted secret-bearing logs.
- Use only OCI resources that the Console marks Always Free eligible; stop before creation if the exact quota or storage arithmetic does not fit.
- Stop the old VM only after the new deployment passes its checks; do not terminate it or delete its 47 GB boot volume.

## Review Focus

1. **Hidden or unattached OCI volumes and idle-reclaim terms:** verify the tenancy-wide total before launch; if other volumes use more than 3 GB, do not create the 150 GB target disk. Record the documented seven-day idle-reclaim rule and do not simulate load to avoid it.
2. **Wrong architecture in GHCR:** inspect the published manifest and require both `linux/amd64` and `linux/arm64` before deploying to A1 or relying on E2 rollback.
3. **Broken Coolify Compose parsing or persistence:** validate the resolved Compose file, env-file path, and all bind/volume destinations before starting the production stack.
4. **Corrupt or incomplete PostgreSQL transfer:** verify a source/target SHA-256, inspect the custom archive, restore a rehearsal copy, and compare schema/version and selected table counts before cutover.
5. **Competing deployments or premature Telegram side effects:** keep Coolify Git auto-deploy disabled, avoid starting new app/workers before cutover, and verify GitHub Actions no longer uses SSH to the old host.

---

### Task 1: Read-only OCI and source preflight

**Files:** None.

**Interfaces:**
- Consumes: The OCI Console and SSH access to the current E2 VM.
- Produces: A recorded preflight with exact quotas, source revision, service inventory, database size, and state paths. No secret values are recorded.

- [ ] **Step 1: Verify tenancy storage and compute headroom.** In OCI, inventory every boot and block volume in the home region, including detached volumes, and record the aggregate allocated GB. Confirm the existing 47 GB boot volume and confirm A1 Always Free limits and availability in Frankfurt.
- [ ] **Step 2: Apply the storage gate.** Calculate `47 GB + 150 GB + all other boot/block volumes`. Continue only if the total is at most 200 GB and the A1 entitlement remains within 2 OCPUs / 12 GB. If either the Always Free label or the required headroom is absent, stop and report the exact issue before provisioning.
- [ ] **Step 3: Inspect the source host without changing it.** Record running services, Compose directory, container image reference/digest/architecture, database schema version and size, disk usage, network dependencies, and presence/permissions of `.env`, `sessions/`, `data/`, `logs/`, and `telebrief_auth`. Do not print `.env` or session contents.
- [ ] **Step 4: Check the target network path.** Confirm whether `free-tier-vcn` has a public subnet and Internet Gateway suitable for the new VNIC; identify existing network rules without changing them.
- [ ] **Step 5: Confirm the source snapshot target.** Record the current running image tag/digest and compare the server's config/profile hashes to the canonical `dev` commit. If the live config differs, preserve the live copy for migration rather than substituting local workspace files.
- [ ] **Step 6: Record the Free Tier availability limitation.** Confirm and note Oracle's current A1 idle-reclaim criteria (CPU, network, and memory each below 20% for seven days). Do not create synthetic load; include this availability risk in the final handoff.

**Check:** Preflight confirms room for the new 150 GB volume, current A1 quota/capacity, exact source revision, and sufficient target disk space for Coolify, the database restore, and one temporary rehearsal copy.

---

### Task 2: Prepare Compose and deployment workflow on `dev`

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.github/workflows/deploy-dev.yml`
- Preserve: all unrelated repository files and the user's existing local edits.

**Interfaces:**
- Consumes: Task 1 source/runtime facts.
- Produces: A Coolify-compatible Compose definition and a `dev` workflow that publishes a multi-architecture image for a later manual Coolify deployment.

- [ ] **Step 1: Fetch and inspect the canonical branch.** Fetch only `origin/dev`; compare it with the current `dev` tip so the approved design-document commits are accounted for. Do not fetch, branch from, or rebase onto `main`.
- [ ] **Step 2: Create the isolated worktree.** Create branch `codex/oracle-coolify-migration` in a separate worktree from the current `dev` tip, rebased onto the latest `origin/dev` only if that remote branch has advanced. Keep the original workspace and its uncommitted files untouched.
- [ ] **Step 3: Make the application env-file path selectable while preserving old-host behavior.** Change each of the four Telebrief app/worker `env_file` entries to `${TELEBRIEF_ENV_FILE:-.env}`. The old host continues to resolve `.env`; Coolify will set `TELEBRIEF_ENV_FILE=/etc/telebrief/telebrief.env`.
- [ ] **Step 4: Add a one-shot migration service.** Add `telebrief-migrate` using the same `ghcr.io/djbob2000/telebrief:${IMAGE_TAG:-dev}` image, env file, Compose environment, and required config mount as the app. Set `command: ["python", "scripts/migrate.py"]`, `restart: "no"`, and a healthy-PostgreSQL dependency. Make the app and all three workers depend on both healthy PostgreSQL and `telebrief-migrate` completing successfully.
- [ ] **Step 5: Add QEMU and build both image platforms.** In `.github/workflows/deploy-dev.yml`, add `docker/setup-qemu-action@v3` before Buildx and set the build action platforms to `linux/amd64,linux/arm64`. Keep the `dev` and `dev-${{ github.sha }}` tags so A1 and the E2 rollback host can use the same release.
- [ ] **Step 6: Replace the old SSH deployment block.** Remove SSH key setup, `scp` to the old server, remote migrations, remote Compose startup, and old-host pruning. Do not trigger app redeploy from Coolify's direct Git auto-deploy.
- [ ] **Step 7: Verify the published image manifest.** After image push, inspect `ghcr.io/djbob2000/telebrief:dev` and fail unless both AMD64 and ARM64 are present.
- [ ] **Step 8: Keep the workflow scoped to the canonical branch.** Keep `push` on `dev`, remove the obsolete manual image-tag input, and add an early guard so manual workflow runs are allowed only for `refs/heads/dev`.
- [ ] **Step 9: Validate Compose without production secrets.** Run `TELEBRIEF_ENV_FILE=/dev/null docker compose --env-file /dev/null -f docker-compose.yml config -q`. Expected: exit status 0 and no secret values emitted.
- [ ] **Step 10: Review the workflow diff.** Confirm the resulting workflow has no SSH, `scp`, `docker compose` remote access, old-host address/secrets, or Coolify deploy webhook; confirm build platforms and manifest validation are present. Run `actionlint .github/workflows/deploy-dev.yml` if `actionlint` is installed; do not install a new tool solely for this check.
- [ ] **Step 11: Commit the scoped implementation changes.** Commit only `docker-compose.yml` and `.github/workflows/deploy-dev.yml` from the isolated worktree. Do not stage the original workspace's unrelated edits. Keep this commit on the isolated branch until the target VM and Coolify are ready.

**Check:** Compose validation exits 0, workflow diff is free of old-host SSH deployment and Coolify webhook access, and the commit contains only intended migration files. The first `dev` workflow run later must publish a manifest with both architectures.

---

### Task 3: Provision the Always Free A1 VM and network

**Files:** None.

**Interfaces:**
- Consumes: Task 1 quota gate and source network survey.
- Produces: A running Ubuntu ARM64 A1 instance with restricted management access and no added paid storage or services.

- [ ] **Step 1: Create a dedicated NSG.** Allow inbound SSH on TCP 22 only from the operator workstation's current public IP. Do not expose Coolify ports 8000/6001/6002 or public application ports 80/443; outbound HTTPS remains available for image and package pulls.
- [ ] **Step 2: Create one VM in `eu-frankfurt-1`.** Name it `telebrief-coolify`; select `VM.Standard.A1.Flex`, exactly 2 OCPUs and 12 GB RAM, Ubuntu LTS ARM64 marked Always Free eligible, and a 150 GB boot volume marked Always Free eligible. Reuse the existing public VCN/subnet only if Task 1 confirms route suitability; attach the dedicated NSG.
- [ ] **Step 3: Verify the created resource.** In OCI, confirm Running state, exact shape/memory/image/boot-disk values, Always Free eligibility, assigned public IP, and no extra block volumes or paid services. Recalculate storage as 197 GB plus any other existing volume allocations.
- [ ] **Step 4: Verify SSH access.** Connect using the already-authorized SSH key and the Ubuntu image's documented default username. Confirm `uname -m` reports `aarch64` and `df -h /` reports the expected 150 GB boot filesystem capacity.

**Check:** The new VM is Running with the exact Always Free configuration; management ports are not public, and storage stays at or below 200 GB.

---

### Task 4: Install Coolify and establish restricted dashboard access

**Files:** None.

**Interfaces:**
- Consumes: Task 3 SSH and public IP.
- Produces: Running Coolify control plane, SSH-tunnel-only dashboard access, and a user-owned initial administrator account.

- [ ] **Step 1: Install Coolify from its official self-hosted installer.** Connect by SSH and run the official install script as a sudo-capable account. Keep installer output free of secrets in chat/logs.
- [ ] **Step 2: Establish an SSH tunnel.** Forward local ports 8000, 6001, and 6002 to the corresponding loopback ports on the VM over SSH. Open the dashboard at `http://127.0.0.1:8000`; do not open those ports in the OCI NSG.
- [ ] **Step 3: Hand off initial account creation to the user.** Pause when Coolify presents its first-admin registration. The user creates the Coolify account/password in the browser. Do not create, view, or type a new password on the user's behalf.
- [ ] **Step 4: Verify the Coolify control plane.** After the user confirms registration, inspect Coolify's dashboard and server readiness; confirm the tunnel works for dashboard updates and terminal access without public Coolify ingress.

**Check:** Coolify dashboard and server status load through the SSH tunnel, first-admin account is user-owned, and OCI still exposes only the restricted SSH management rule.

---

### Task 5: Configure Coolify and rehearse data restore without starting production workers

**Files:**
- Coolify resource: Git-based Docker Compose application from `djbob2000/Telebrief`, branch `dev`.
- Host paths: `/etc/telebrief/telebrief.env` and Coolify-managed persistent storage for Telebrief bind mounts/volumes.

**Interfaces:**
- Consumes: Tasks 1–4, the dual-architecture GHCR image, and the user-owned Coolify admin account.
- Produces: Saved Coolify application configuration, protected target state, and a successful database restore rehearsal; production app/worker services remain stopped.

- [ ] **Step 1: Integrate and publish the reviewed `dev` changes.** Once the target VM and Coolify are ready, integrate the reviewed migration commit into canonical `dev` and push it. This first `dev` workflow run must build/push `dev` and `dev-${sha}`, inspect the `dev` manifest, and report both `linux/amd64` and `linux/arm64`. Confirm the workflow does not start or connect to either server; the old SSH deployment has already been removed in the pushed revision.
- [ ] **Step 2: Create the Coolify Compose resource.** Connect the public Git repository at branch `dev`; set `IMAGE_TAG=dev`, `TELEBRIEF_ENV_FILE=/etc/telebrief/telebrief.env`, and the source database's `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` values in Coolify. Set auto-deploy to Manual so a Git push cannot pull the previous `dev` image before CI publishes the new one. Confirm GHCR allows the target to pull the image; if the package is private, configure pull credentials in Coolify's secret store. Deployment will be initiated from the Coolify dashboard through the SSH tunnel.
- [ ] **Step 3: Review Coolify's Deployable Compose.** Confirm the five long-running services (PostgreSQL, app, and three workers) plus the one-shot migration service are represented, the migration service gates all app/workers on healthy PostgreSQL and successful migration, the target env file path is correct, no production port is exposed, and `config.yaml`/city profiles remain available from the repository. Enable Coolify's Preserve repository option if required for the tracked `config.yaml` file bind. Confirm relative `./data`, `./logs`, and `./sessions` mounts resolve to a stable resource directory on the server and persist through redeploys.
- [ ] **Step 4: Transfer the production env file without displaying it.** Stream the old `.env` to `/etc/telebrief/telebrief.env` on the new server over SSH; set owner-only permissions. Do not write its contents to terminal output, workflow logs, repo files, or chat.
- [ ] **Step 5: Prepare filesystem state.** Create the target mount directories and transfer the non-secret `data/` and `logs/` content as required. Do not copy the active Telethon SQLite session while the old app is using it; final session/auth state is transferred after quiescing the old app in Task 6. Compare tracked `config.yaml` and city-profile hashes against the running server and the active image revision. If they disagree, pause and reconcile the source with the user before deploying; never silently replace live config with local workspace content.
- [ ] **Step 6: Take a rehearsal database dump while the old application remains active.** Run `pg_dump -Fc` from the source PostgreSQL container and stream it over SSH to a mode-0600 temporary archive on the new VM. Keep the database port private and do not save the archive on the workstation or in Git.
- [ ] **Step 7: Verify and rehearse the archive.** Compare the source-stream and target-file SHA-256; run `pg_restore --list`; restore the rehearsal into a separate temporary `pgvector/pgvector:pg18` container/volume without a published port. Verify migration schema version and selected row counts against the source. Create a harmless sentinel under each target runtime bind mount, redeploy the stopped resource, and verify the sentinels and tracked config/profile files remain available. Remove the temporary test container/volume/archive only after the rehearsal passes.

**Check:** Coolify config parses correctly, target environment/files have correct permissions, target architecture can pull the ARM64 image, and the rehearsal PostgreSQL archive restores successfully. The production app and all Telebrief workers remain stopped on the new VM.

---

### Task 6: Perform final cutover and verify Telebrief on the A1 host

**Files:** None.

**Interfaces:**
- Consumes: Task 5 verified Coolify resource and rehearsed restore procedure.
- Produces: One active production Telebrief deployment on A1, with database and persisted files restored.

- [ ] **Step 1: Quiesce the old deployment.** Stop `telebrief-app`, `telebrief-worker`, `telebrief-processing-worker`, and `telebrief-authority-worker` gracefully; leave PostgreSQL running. Wait for worker shutdown and confirm no Telebrief app/worker container remains active on the old host.
- [ ] **Step 2: Take the final source snapshot.** Run final `pg_dump -Fc` after app/workers stop; stream over SSH to the target and verify SHA-256 plus `pg_restore --list`. Transfer the Telethon session, `telebrief_auth` volume, and any remaining mutable filesystem state only after the old writers are stopped. Leave the old PostgreSQL volume untouched.
- [ ] **Step 3: Restore the target production database.** Initialize the Coolify Compose PostgreSQL volume, restore the final archive, and confirm successful exit, expected schema version, and selected table counts. Run Coolify Deploy manually; its `telebrief-migrate` service must finish before the app/worker containers start.
- [ ] **Step 4: Verify target runtime.** Confirm all app/worker/PostgreSQL containers run on ARM64 using the expected image revision; check migration logs and startup logs with secrets redacted; confirm Postgres is healthy; send `/status` to the Telegram bot and confirm it reports the expected configuration/schedule. Do not run `/digest` or create a production delivery as a test.
- [ ] **Step 5: Keep the old VM as rollback until target checks pass.** If validation fails before new external processing begins, stop the new app/workers, restart the old VM and stack, and document the old host as active. If new processing has started, preserve the latest target database and fix forward; if emergency rollback is unavoidable, restore that latest database to old PostgreSQL before restarting old workers and note the at-least-once Telegram side-effect risk.
- [ ] **Step 6: Stop the old OCI VM after success.** Use Stop, not Terminate. Verify the old instance is Stopped and its 47 GB boot volume remains attached/present for rollback.

**Check:** The target responds to Telegram `/status`, all services start in migration order, and the old VM is stopped only after the target passes validation. No production digest was manually generated as a test.

---

### Task 7: Document manual Coolify deployments and clean temporary migration artifacts

**Files:** None.

**Interfaces:**
- Consumes: Successful Task 6 deployment and user-created Coolify admin account.
- Produces: A verified operator runbook for future `dev` releases and a final OCI resource audit; no workflow targets either server.

- [ ] **Step 1: Record the future release procedure.** Document that a `dev` push builds/publishes the dual-architecture image; after the build succeeds, open Coolify through the SSH tunnel and select Deploy. Confirm Coolify Git auto-deploy remains Manual.
- [ ] **Step 2: Remove temporary archives.** After the successful Coolify deployment and target DB checks, remove temporary dump files and the rehearsal PostgreSQL volume. Keep only the production PostgreSQL and auth/session volumes on the new VM; keep the old boot volume intact.
- [ ] **Step 3: Final resource audit.** Recheck the Console: A1 usage is 2 OCPU / 12 GB, old E2 is Stopped, and combined boot/block storage is at most 200 GB. Confirm no 80/443 or Coolify dashboard ports were accidentally opened; note the idle-reclaim limitation for the handoff.

**Check:** The deployment is initiated only from Coolify after a successful GHCR build; no workflow-driven SSH deployment or public webhook remains, and OCI resources remain within the Always Free envelope.

---

## Execution notes

- The local workspace's uncommitted `src/publication/rubrics.py` and `tests/test_digest_rubrics.py` edits are unrelated and must remain untouched.
- The deployment branch is `dev`; do not use `main` for fetch, rebase, branching, or deployment.
- The initial Coolify admin account is a user handoff. Continue only after the user confirms account creation.
- A pre-cutover rehearsal restore checks the procedure; only the final quiesced dump is authoritative for the production target.
- Oracle Always Free and Coolify current networking requirements are documented in the approved design and its official references.
