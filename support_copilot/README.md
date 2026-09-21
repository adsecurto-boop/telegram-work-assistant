# Support Copilot - Core API & Operations (Phases 0–7)

Support Copilot is a local desktop assistant for support professionals. It observes approved support contexts, retrieves approved knowledge, proposes grounded replies, records what was actually sent, and builds fact-checked daily work reports.

> [!IMPORTANT]
> **Core Architecture & Safety Guardrails**:
> - **Copilot, not autonomous agent**: The human operator sends all client replies. The copilot never autonomously sends messages, executes external actions, or modifies client data.
> - **Replies sent by human**: "Confirm Sent" records what the human sent through their support channel; the application has no outbound delivery capability.
> - **Telegram integration is inbound/read-only**: The Telegram adapter uses the official Bot API `getUpdates` endpoint and can only ingest inbound text into the Core API with `capture:write` scope.
> - **Meeting transcription**: Transcription is manual or adapter-supplied; no raw audio is claimed or stored. Starting a session requires explicit participant consent acknowledgement.
> - **Screen capture**: One-shot, explicitly selected window capture only. Local OCR and redaction run before any remote visual analysis. Sensitive screens (banking, passwords, credentials) are blocked. Ctrl/Cmd+Shift+P pauses and clears capture immediately.
> - **No computer control**: The application has no mouse or keyboard control IPC or API.
> - **n8n boundary**: n8n can only export finalized reports using HMAC-SHA256 signed requests. It has no database access, cannot finalize reports, and cannot send replies.
> - **Human-approved learning**: Nothing is learned automatically or merely because text was suggested. Learning candidates originate only from confirmed sent responses, require the operator to review and generalize the reusable content, reject obvious client identifiers, and require explicit approval with separate `knowledge:approve` capability.
> - **Remote-model privacy**: Client text and case/knowledge text are deterministically filtered for common email, phone/account, and credential patterns before leaving the local Core API.
> - **Controlled expansion**: Additional channels (Slack, Teams, WhatsApp) and outbound automations remain strictly deferred until usage evidence is established.

---

## 1. Dependencies & Reproducibility

- `support_copilot/requirements.txt`: Direct top-level dependencies.
- `support_copilot/requirements.lock.txt`: Pinned, deterministic lockfile for production and CI installations.

To install dependencies reproducibly:
```powershell
.\.venv\Scripts\pip.exe install -r support_copilot/requirements.lock.txt
```

To verify dependency consistency:
```powershell
.\.venv\Scripts\python.exe -m pip check
```

---

## 2. Environment Configuration

> [!CAUTION]
> Never commit real tokens, API keys, or HMAC secrets to version control.

Configure environment variables before starting:
```powershell
# Paths and Server
$env:COPILOT_DATA_DIR = "storage/support_copilot"
$env:COPILOT_DB_PATH = "sqlite:///./storage/support_copilot/copilot.sqlite3"
$env:COPILOT_API_HOST = "127.0.0.1"
$env:COPILOT_API_PORT = "8000"

# AI Provider (set to "disabled" for manual-only mode or "gemini" for generation)
$env:COPILOT_AI_PROVIDER = "gemini"
$env:COPILOT_GEMINI_API_KEY = "<YOUR-PRIVATE-GEMINI-API-KEY>"
$env:COPILOT_GEMINI_MODEL = "gemini-2.5-flash"

# Core API Tokens (Scoped capabilities)
$token = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object {[char]$_})
$env:COPILOT_API_TOKENS = "{`"$token`": [`"capture:write`", `"knowledge:read`", `"knowledge:write`", `"knowledge:approve`", `"suggestion:read`", `"suggestion:write`", `"response:confirm_sent`", `"activity:read`", `"activity:write`", `"report:read`", `"report:write`", `"case:read`", `"case:write`", `"meeting:read`", `"meeting:write`", `"meeting:approve`", `"screen:analyze`", `"operations:read`"]}"
$env:COPILOT_DESKTOP_API_TOKEN = $token

# Optional: n8n HMAC Signing Secret
$env:COPILOT_N8N_SHARED_SECRET = "<YOUR-PRIVATE-HMAC-SECRET>"
```

---

## 3. Database Migrations

Mutating database migrations are executed via `support_copilot.migration_runner`, which creates verified online SQLite backups before applying changes.

### Apply Migrations (Upgrade)
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner upgrade head
```

### Inspect Current Revision
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner current
```

### Safe Downgrade (Destructive)
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner downgrade 004_phase5 --allow-downgrade
```

---

## 4. Starting the Application

### Start Core API
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\uvicorn.exe support_copilot.main:create_production_app --factory --host 127.0.0.1 --port 8000
```

### Start Desktop Overlay
In a separate terminal:
```powershell
Set-Location support_copilot/desktop
npm install
npm start
```

---

## 5. Operations: Backup, Verification, and Restore

Support Copilot provides dedicated operational tools using the SQLite online backup API:

### Create an Online Backup
```powershell
uv run python -m support_copilot.operations backup --db storage/support_copilot/copilot.sqlite3 --out storage/support_copilot/backups
```
Generates a timestamped `.sqlite3` file and a companion `_manifest.json` containing SHA-256 checksum and schema revision.

### Verify a Backup
```powershell
uv run python -m support_copilot.operations verify-backup storage/support_copilot/backups/<backup-file>.sqlite3
```
Verifies checksum, SQLite PRAGMA integrity, and schema readiness.

### Restore a Backup
```powershell
uv run python -m support_copilot.operations restore storage/support_copilot/backups/<backup-file>.sqlite3 --db storage/support_copilot/copilot.sqlite3 --confirm-restore
```
Requires `--confirm-restore`. Verifies backup in isolation, takes a pre-restore backup of the active database, and performs an atomic replacement.

See [DISASTER_RECOVERY.md](../docs/support-copilot/DISASTER_RECOVERY.md) for full incident handling runbooks.

---

## 6. Retrieval Evaluation

Run the deterministic retrieval evaluation runner to measure Recall@1, Recall@3, and MRR against synthetic ground-truth cases:
```powershell
uv run python -m support_copilot.retrieval_evaluation --dataset support_copilot/evaluation/retrieval_cases.json --fixture-corpus support_copilot/evaluation/retrieval_knowledge.json --threshold 1.0
```

---

## 7. Signed n8n EOD Distribution

1. Configure `COPILOT_N8N_SHARED_SECRET` in both processes.
2. In n8n environment, ensure Node built-in crypto is permitted:
   `NODE_FUNCTION_ALLOW_BUILTIN=crypto`
3. Import `support_copilot/n8n/eod-distribution.workflow.json`.
4. The workflow signs requests with timestamp, unique event ID, and canonical payload, requesting only capability `report:export`.

---

## 8. Running Tests

Run full backend suite:
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m pytest support_copilot/tests -v
```

Run desktop suite:
```powershell
Set-Location support_copilot/desktop
npm test -- --run
npm run typecheck
npm run build
npm audit --omit=dev
```

Run Telegram assistant regression suite:
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
