# Support Copilot — Disaster Recovery Runbook

This document defines standard operating procedures for detecting, containing, and recovering from operational failures in Support Copilot.

---

## 1. Principles of Incident Management

1. **Human Authority**: The copilot never autonomously takes corrective actions outside its sandbox. All disaster recovery operations require operator execution and explicit confirmation.
2. **Data Preservation**: Never delete active databases, audit logs, or previous backups during an incident.
3. **No Secret Leakage**: Incident reports and telemetry must never include API tokens, private keys, HMAC shared secrets, customer message content, or raw screenshots.
4. **Pre-Flight Safety**: Every restore operation takes an automatic pre-restore backup of the active database before atomic replacement.

---

## 2. Emergency Procedures

### 2.1 Detecting Database Corruption

Symptoms:
- Health check reports `"integrity_check": "failed"` or `"status": "unhealthy"`.
- SQLite errors such as `sqlite3.DatabaseError: file is not a database` or `database disk image is malformed`.
- Unhandled HTTP 500 errors during knowledge retrieval or event persistence.

Verification Command:
```powershell
# Run SQLite integrity check directly
uv run python -c "import sqlite3; conn = sqlite3.connect('storage/support_copilot/copilot.sqlite3'); print(conn.cursor().execute('PRAGMA integrity_check;').fetchall()); conn.close()"
```

If the output is anything other than `[('ok',)]`, corruption is present.

---

### 2.2 Stopping the Application Safely

To prevent writing corrupted state or compounding data loss:

```powershell
# Stop the desktop overlay process and background Python API service
Get-Process -Name "electron", "python" | Where-Object { $_.Path -like "*telegram-work-assistant*" } | Stop-Process
```

---

### 2.3 Creating an Emergency Backup

Before modifying or replacing any files, take an emergency snapshot:

```powershell
uv run python -m support_copilot.operations backup --db storage/support_copilot/copilot.sqlite3 --out storage/support_copilot/backups/emergency
```

If the online backup fails due to disk corruption, make a raw file copy:
```powershell
Copy-Item "storage/support_copilot/copilot.sqlite3" "storage/support_copilot/backups/emergency_corrupt_raw_$(Get-Date -Format 'yyyyMMdd_HHmmss').sqlite3"
```

---

### 2.4 Listing and Verifying Backups

List available backups:
```powershell
Get-ChildItem -Path "storage/support_copilot/backups" -Filter "*.sqlite3" -Recurse | Sort-Object LastWriteTime -Descending
```

Verify backup checksum, SQLite integrity, and schema readiness:
```powershell
uv run python -m support_copilot.operations verify-backup "storage/support_copilot/backups/<backup-file>.sqlite3"
```

A valid backup produces:
```
Backup verification PASSED:
  File:      data/backups/backup_20260921_120000.sqlite3
  Checksum:  <sha256_hash>
  Revision:  005_phase7
  Integrity: ok
```

---

### 2.5 Restoring from a Verified Backup

Restoring requires the mandatory `--confirm-restore` flag. The tool automatically verifies the backup in an isolated environment, creates a pre-restore backup of the current database, and replaces the database atomically.

```powershell
uv run python -m support_copilot.operations restore "storage/support_copilot/backups/<backup-file>.sqlite3" --db storage/support_copilot/copilot.sqlite3 --confirm-restore
```

Output:
```
Database restored successfully:
  Target DB:          C:\Projects\TELEBOT\telegram-work-assistant\data\support_copilot.sqlite3
  Pre-restore Backup: C:\Projects\TELEBOT\telegram-work-assistant\data\support_copilot.sqlite3.pre_restore_20260921_120500.bak
```

---

### 2.6 Rolling Back a Failed Migration

If an Alembic migration fails during deployment or update:

1. Identify the target revision (e.g., `004_phase5`):
```powershell
uv run python -m support_copilot.migration_runner current
```

2. Downgrade with explicit authorization:
```powershell
uv run python -m support_copilot.migration_runner downgrade 004_phase5 --allow-downgrade
```
*Note: A safety backup is automatically created prior to downgrade.*

3. Verify schema readiness:
```powershell
uv run python -m support_copilot.migration_runner current
```

---

### 2.7 Rebuilding the Knowledge Search Index (FTS5)

If knowledge search returns unexpected results or FTS5 indices are out of sync:

```powershell
uv run python -c "from support_copilot.database import create_db_engine, create_session_factory; from support_copilot.knowledge_service import KnowledgeService; engine = create_db_engine('sqlite:///storage/support_copilot/copilot.sqlite3'); db = create_session_factory(engine)(); count = KnowledgeService.rebuild_fts_index(db); print(f'Rebuilt FTS index with {count} versions'); db.close(); engine.dispose()"
```

---

### 2.8 Recovering when AI Providers are Unavailable

If Gemini / external AI API is unreachable or rate-limited:

1. Check detailed health:
```powershell
$healthHeaders = @{ Authorization = "Bearer $env:COPILOT_DESKTOP_API_TOKEN" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/health/detailed" -Headers $healthHeaders
```
Status will show `"status": "degraded"` with `"ai_provider": {"configured": true, "mode": "gemini"}`.

2. **Copilot Continues Operating**:
   - Knowledge retrieval remains 100% operational locally via SQLite FTS5.
   - Grounded suggestions safely fall back to `provider_unavailable` status.
   - Screen analysis returns approved knowledge sources without hallucinated steps.
   - Operators can manually compose and send replies.

3. To continue in local manual-only mode without model generation:
```powershell
$env:COPILOT_AI_PROVIDER = "disabled"
```

---

### 2.9 Continuing in Manual Mode when Telegram is Disconnected

If Telegram webhook or polling fails:
- The copilot architecture guarantees channel decoupling.
- The desktop overlay accepts manually pasted client messages via `/v1/captures/manual-message`.
- Daily activity recording, report finalization, and meeting notes continue functioning without Telegram.

---

### 2.10 Post-Recovery Health Verification

After completing any recovery procedure, verify system health:

```powershell
# Basic health check
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/health"

# Detailed component check
$healthHeaders = @{ Authorization = "Bearer $env:COPILOT_DESKTOP_API_TOKEN" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/health/detailed" -Headers $healthHeaders
```

Expected response:
```json
{
  "status": "healthy",
  "api_status": "healthy",
  "database": {
    "connected": true,
    "schema_revision": "005_phase7",
    "schema_ready": true,
    "integrity_check": "ok",
    "data_dir_writable": true
  },
  "ai_provider": {
    "configured": true,
    "mode": "gemini"
  },
  "backup": {
    "last_backup_timestamp": "2026-09-21T12:00:00Z"
  },
  "retention": {
    "status": "manual_cleanup_available"
  },
  "telegram": {
    "status": "external_adapter_unreported"
  },
  "n8n": {
    "configured": true
  }
}
```

---

## 3. Evidence Collection & Confidentiality

When filing an incident report:

### Safe to Collect:
- Health check output from `/v1/health/detailed`.
- Event and candidate IDs (UUIDs).
- SQLite integrity check output.
- Backup manifest files (`manifest.json`).
- Schema revision strings.

### Banned from Incident Artifacts:
- `COPILOT_API_TOKENS`, `COPILOT_GEMINI_API_KEY`, `COPILOT_N8N_SHARED_SECRET`.
- Raw client message text or client PII.
- Screenshot PNG bytes or raw OCR text.
- Customer names or identifying enterprise identifiers.
- Untracked `bot.log.1` or active database files.

---

## 4. Human Escalation Triggers

Immediately escalate to engineering lead / security team if:
1. SQLite corruption cannot be resolved by restoring any verified backup.
2. Tampering is detected during backup verification (`Checksum verification failed!`).
3. An unconfirmed response was somehow emitted to an external channel.
4. PII or credentials are found unredacted in audit logs.
