# CollectFlow Backup & Disaster Recovery Architecture

This document defines the production backup, encryption, retention, and disaster recovery procedures for **CollectFlow**, operating on SQLite 3.45+ with Write-Ahead Logging (WAL) enabled.

---

## 1. Objectives & SLAs

| Metric | Target | Rationale |
|---|---|---|
| *
*Recovery Point Objective (RPO)** | **< 15 minutes** | Maximum allowable data loss window under sudden infrastructure failure or storage corruptions. |
| **Recovery Time Objective (RTO)** | **< 10 minutes** | Total time required to provision a target host, retrieve the latest verified backup, decrypt, verify integrity, and restart the Uvicorn application service. |

---

## 2. Backup Mechanisms & Concurrency

Standard file copying (`cp data/collectflow.db ...`) while SQLite is under active write transactions risks creating a corrupted snapshot (torn page writes across WAL and main database files).

CollectFlow mandates one of two non-blocking snapshot mechanisms:

### A. Online SQLite Backup API (Recommended)
Python's built-in `sqlite3.Connection.backup()` copies active database pages incrementally without acquiring exclusive file locks, allowing read and write operations to proceed without blocking.

### B. `VACUUM INTO` (Atomic Snapshot)
SQLite provides atomic snapshot creation via:
```sql
VACUUM INTO '/var/backups/collectflow/collectflow_snapshot_YYYYMMDD_HHMMSS.db';
```
This produces an independent, non-fragmented, compacted SQLite database file while active queries proceed in WAL mode.

---

## 3. Automation Script (`scripts/backup.py`)

A production-grade backup script using Python's native `sqlite3.backup`:

```python
#!/usr/bin/env python3
"""
CollectFlow Automated Backup Script
Performs atomic non-blocking online SQLite backup with integrity check and gzip compression.
"""
import os
import sys
import time
import sqlite3
import gzip
import shutil
from datetime import datetime

DB_PATH = os.getenv("COLLECTFLOW_DB_PATH", "data/collectflow.db")
BACKUP_DIR = os.getenv("COLLECTFLOW_BACKUP_DIR", "backups")
TIMESTAMP = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
TEMP_BACKUP = os.path.join(BACKUP_DIR, f"temp_{TIMESTAMP}.db")
FINAL_BACKUP_GZ = os.path.join(BACKUP_DIR, f"collectflow_backup_{TIMESTAMP}.db.gz")

def perform_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        print(f"Error: Database {DB_PATH} does not exist.")
        sys.exit(1)

    print(f"[{datetime.utcnow().isoformat()}] Starting backup of {DB_PATH}...")
    source_conn = sqlite3.connect(DB_PATH)
    dest_conn = sqlite3.connect(TEMP_BACKUP)

    try:
        # Non-blocking page-by-page online backup
        source_conn.backup(dest_conn, pages=250, sleep=0.01)
        dest_conn.commit()
    finally:
        source_conn.close()
        dest_conn.close()

    # Step 2: Run PRAGMA integrity_check on the snapshot
    verify_conn = sqlite3.connect(TEMP_BACKUP)
    cur = verify_conn.cursor()
    cur.execute("PRAGMA integrity_check;")
    result = cur.fetchone()[0]
    verify_conn.close()

    if result != "ok":
        os.remove(TEMP_BACKUP)
        raise RuntimeError(f"Integrity check failed on backup copy: {result}")

    # Step 3: Compress with Gzip
    print(f"[{datetime.utcnow().isoformat()}] Integrity check verified ('ok'). Compressing...")
    with open(TEMP_BACKUP, "rb") as f_in, gzip.open(FINAL_BACKUP_GZ, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)

    os.remove(TEMP_BACKUP)
    print(f"[{datetime.utcnow().isoformat()}] Backup successfully saved to {FINAL_BACKUP_GZ}")

if __name__ == "__main__":
    perform_backup()
```

---

## 4. Encryption Requirements

All backups at rest must be encrypted before transmission to off-site cloud storage (AWS S3, Google Cloud Storage, or Azure Blob Storage).

### Encryption at Rest
- **Algorithm**: AES-256 (via OpenSSL or GnuPG).
- **Key Storage**: Encryption keys must be stored in an enterprise Key Management Service (AWS KMS, GCP Cloud KMS, or HashiCorp Vault), decoupled from the application host.
- **Example GPG Command**:
  ```bash
  gpg --batch --yes --passphrase-file /etc/collectflow/backup_key.secret \
      --symmetric --cipher-algo AES256 \
      -o /backups/collectflow_backup_YYYYMMDD_HHMMSS.db.gz.enc \
      /backups/collectflow_backup_YYYYMMDD_HHMMSS.db.gz
  ```

---

## 5. Frequency & Retention Schedule

| Schedule | Frequency | Retention Window | Storage Tier |
|---|---|---|---|
| **Intraday Incremental/Snapshot** | Every 15 minutes (or hourly) | 24 Hours | Local NVMe / Fast S3 Standard |
| **Daily Full** | 02:00 UTC Daily | 30 Days | S3 Standard-IA (Infrequent Access) |
| **Monthly Archive** | 1st of every month | 12 Months | S3 Glacier Flexible Retrieval |
| **Fiscal Year Close** | April 1 00:00 UTC | 7 Years (statutory requirement) | S3 Glacier Deep Archive (Locked Vault) |

### Automatic Pruning (Cron / Lifecycle Policy)
```bash
# Delete local hourly snapshots older than 24 hours
find /backups -name "collectflow_backup_*.db.gz" -mtime +1 -delete
```

---

## 6. Step-by-Step Restoration Procedure

When recovering from disaster or hardware failure, follow this exact sequence:

### Step 1: Stop Application Service
Prevent concurrent write attempts during restore:
```bash
systemctl stop collectflow
```

### Step 2: Retrieve & Decrypt Latest Backup
```bash
# Decrypt archive
gpg --batch --yes --passphrase-file /etc/collectflow/backup_key.secret \
    --decrypt -o /tmp/collectflow_restore.db.gz \
    /backups/collectflow_backup_LATEST.db.gz.enc

# Decompress
gzip -d /tmp/collectflow_restore.db.gz
```

### Step 3: Verify Integrity of Restored Database
Never replace the live database with an unverified image:
```bash
sqlite3 /tmp/collectflow_restore.db "PRAGMA integrity_check;"
# MUST output: ok
sqlite3 /tmp/collectflow_restore.db "PRAGMA foreign_key_check;"
# MUST output empty (no FK violations)
```

### Step 4: Archive Existing Corrupt/Old Database
```bash
mv /data/collectflow.db /data/collectflow.db.corrupted.$(date +%s)
rm -f /data/collectflow.db-wal /data/collectflow.db-shm
```

### Step 5: Atomic Cutover
```bash
cp /tmp/collectflow_restore.db /data/collectflow.db
chmod 660 /data/collectflow.db
chown collectflow:collectflow /data/collectflow.db
```

### Step 6: Restart & Validate Service Probes
```bash
systemctl start collectflow

# Verify readiness probe
curl -f http://127.0.0.1:8000/ready
# Expected: {"status":"ready","database":"connected","wal_mode":"wal"}
```

---

## 7. Disaster Recovery Drills

- **Schedule**: Bi-monthly scheduled recovery drills in staging environments.
- **Verification**: Run automated test suite (`pytest`) against restored snapshot to ensure zero regression in customer balances and invoice states.
