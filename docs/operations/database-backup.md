# Database Backup and Disaster Recovery

Telebrief uses PostgreSQL 18 with `pgvector` for multisource news, editorial drafts, archives, and task queues. This document explains how database backups work, how to configure automated off-site cloud uploads to **Mega.nz** via `rclone`, and how to perform a disaster recovery restore.

---

## 🏗 Overview

The automated backup system ([scripts/backup_to_mega.sh](file:///Users/air/develop/Telebrief/scripts/backup_to_mega.sh)):

1. **Dumps PostgreSQL** using PostgreSQL's binary custom format (`-Fc`) with **`zstd:7` compression**.
   - `zstd` provides 3–5× faster compression than `gzip` with a smaller archive footprint.
2. **Connects to Mega.nz on-the-fly** via `rclone` (no interactive wizard or permanent background daemon required).
3. **Uploads the single integral dump** to your Mega account with automatic retry logic.
4. **Performs retention cleanup** by purging backups older than $N$ days (default: 14 days) both from Mega.nz and local storage.
5. *(Optional)* **Sends a Telegram alert** to your private chat or channel with file size and execution time.

---

## 📋 Prerequisites

### 1. Install `rclone`

- **macOS:**
  ```bash
  brew install rclone
  ```
- **Linux / Production Server:**
  ```bash
  curl https://rclone.org/install.sh | sudo bash
  ```

### 2. Configure `.env`

Add your Mega.nz account credentials to your `.env` file:

```env
# Required for backups
MEGA_USER=your_mega_email@example.com
MEGA_PASSWORD=your_mega_password

# Optional configurations (defaults shown below)
MEGA_BACKUP_DIR=telebrief-backups      # Remote folder name inside Mega.nz
BACKUP_RETENTION_DAYS=14             # Number of days to retain backups
BACKUP_LOCAL_DIR=./data/backups      # Local directory for temporary dumps
BACKUP_ALERT_CHAT_ID=123456789       # Telegram chat ID for backup status alerts
```

---

## 🚀 Running Backups

### Manual Execution

You can run the backup manually at any time:

```bash
./scripts/backup_to_mega.sh
```

Or via Makefile:

```bash
make backup
```

### Scheduled Automatic Backups (Cron)

To automate daily backups at 03:00 AM server time, add a job to `crontab`:

```bash
crontab -e
```

Add the following entry:

```cron
0 3 * * * cd /path/to/Telebrief && ./scripts/backup_to_mega.sh >> /var/log/telebrief_backup.log 2>&1
```

---

## 🔄 Disaster Recovery / Restoring a Backup

If you need to restore your database on a new server or recover from accidental data loss:

### Step 1: Download the Backup from Mega.nz

```bash
# Configure temporary dynamic backend or use rclone copy directly:
export RCLONE_CONFIG_MEGABACKUP_TYPE="mega"
export RCLONE_CONFIG_MEGABACKUP_USER="your_mega_email@example.com"
export RCLONE_CONFIG_MEGABACKUP_PASS="$(rclone obscure 'your_mega_password')"

# List available backups
rclone lsf MEGABACKUP:telebrief-backups/

# Download the desired backup dump
rclone copy "MEGABACKUP:telebrief-backups/telebrief_db_YYYYMMDD_HHMMSS.dump" ./data/backups/
```

### Step 2: Restore into PostgreSQL

Run `pg_restore` inside the `telebrief-postgres` container:

```bash
docker exec -i telebrief-postgres pg_restore \
    -U telebrief \
    -d telebrief \
    --clean \
    --if-exists < ./data/backups/telebrief_db_YYYYMMDD_HHMMSS.dump
```

> **Note:** The `--clean --if-exists` flags drop existing database objects before recreating them, ensuring a clean state without collisions.

---

## 🔒 Security Best Practices

1. **Keep `.env` Protected:** Ensure `.env` file permissions are restricted:
   ```bash
   chmod 600 .env
   ```
2. **Back up Auth Sessions:** In addition to PostgreSQL, preserve:
   - `sessions/` directory (Telegram MTProto session string / sqlite session)
   - `telebrief_auth` volume (Facebook profile / session storage)
