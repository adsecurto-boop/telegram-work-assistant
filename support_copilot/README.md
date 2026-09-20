# Support Copilot - Core API (Phase 0 & Phase 1)

The Local Support Copilot observes approved support contexts, retrieves approved knowledge, proposes grounded replies, records what was actually sent, and builds fact-checked daily work reports.

## Dependencies & Reproducibility

- `support_copilot/requirements.txt`: Declares direct top-level dependencies with semantic version bounds. Use for development updates.
- `support_copilot/requirements.lock.txt`: Fully pinned, deterministic lockfile containing all resolved direct and transitive runtime and test dependencies. Use for production and CI installations.

To install dependencies reproducibly using the lockfile:
```powershell
.\.venv\Scripts\pip.exe install -r support_copilot/requirements.lock.txt
```

To verify dependency tree consistency:
```powershell
.\.venv\Scripts\python.exe -m pip check
```

## Environment Configuration

> [!CAUTION]
> `COPILOT_API_TOKENS` contains private authentication credentials (secrets). Never commit real tokens to version control.

Configure the environment before starting the application:
```powershell
# Set Support Copilot data directory (must be separate from Telegram assistant's storage)
$env:COPILOT_DATA_DIR = "storage/support_copilot"
$env:COPILOT_DB_PATH = "sqlite:///./storage/support_copilot/copilot.sqlite3"
$env:COPILOT_API_HOST = "127.0.0.1"
$env:COPILOT_API_PORT = "8000"
$env:COPILOT_AI_PROVIDER = "gemini"
$env:COPILOT_GEMINI_API_KEY = "<YOUR-PRIVATE-GEMINI-API-KEY>"
$env:COPILOT_GEMINI_MODEL = "gemini-2.5-flash"

# Generate a private random token (minimum 16 chars) without saving to disk:
$token = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object {[char]$_})
$env:COPILOT_API_TOKENS = "{`"$token`": [`"capture:write`", `"knowledge:read`", `"knowledge:write`", `"knowledge:approve`", `"suggestion:read`", `"suggestion:write`", `"response:confirm_sent`", `"activity:read`", `"activity:write`", `"report:read`", `"report:write`", `"case:read`", `"case:write`"]}"
$env:COPILOT_DESKTOP_API_TOKEN = $token
```

Documented placeholders such as `<GENERATE-A-PRIVATE-RANDOM-TOKEN>` are strictly rejected at startup.

## Safe Database Migrations

All mutating migration operations must be executed via the safe migration runner (`support_copilot.migration_runner`). The runner enforces pre-migration online SQLite backups, consistency checks, and automatic atomic rollback on failure.

### Applying Migrations (Upgrade)
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner upgrade head
```

### Inspecting Current Revision
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner current
```

### Safe Downgrade (Destructive)
Downgrading is a destructive operation that creates a verified pre-downgrade backup and requires an explicit `--allow-downgrade` confirmation flag:
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.migration_runner downgrade base --allow-downgrade
```

> [!NOTE]
> Direct `alembic` commands (e.g. `alembic -c support_copilot/alembic.ini upgrade head`) are reserved for internal testing and do not provide production backup guarantees.

## Starting the Core API

Start the Core API using the Uvicorn factory entry point:
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\uvicorn.exe support_copilot.main:create_production_app --factory --host 127.0.0.1 --port 8000
```

The application will fail closed if:
- Bind host is non-loopback.
- `COPILOT_API_TOKENS` is unconfigured, weak, or uses a known placeholder.
- Database schema is unmigrated or corrupt.
- Data directory is not writable or violates path isolation rules.
- Gemini is selected without a private API key.

Set `COPILOT_AI_PROVIDER=disabled` to run knowledge capture and activity
features without model generation. The application never silently substitutes
a canned response for a missing production provider.

## Health Check

Verify service health:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/health" -Method Get
```

## Starting the Desktop Overlay

With the Core API running and `COPILOT_DESKTOP_API_TOKEN` set to one of the
configured scoped tokens, start the local overlay in a second PowerShell:

```powershell
Set-Location support_copilot/desktop
npm install
npm start
```

The overlay captures messages only after you paste them and never sends a
response externally. “Confirm sent” records what you sent through another
support channel; it does not perform delivery.

## Optional Read-Only Telegram Capture

The Telegram adapter uses the official Bot API `getUpdates` endpoint and can
only forward inbound text into the Core API. It has no send-message operation
and no database access. Use a dedicated Core API token containing only
`capture:write`.

```powershell
$env:COPILOT_TELEGRAM_BOT_TOKEN = "<PRIVATE-BOT-TOKEN>"
$env:COPILOT_CHANNEL_API_TOKEN = "<PRIVATE-CAPTURE-ONLY-CORE-TOKEN>"
$env:COPILOT_TELEGRAM_ALLOWED_CHAT_IDS = "123456789"
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m support_copilot.telegram_adapter
```

Restarting or replaying Telegram updates is safe because the stable Telegram
update ID is enforced by the Core API idempotency registry. If the adapter is
stopped, manual message entry continues to work independently.

## Optional n8n EOD Distribution

Set a private HMAC secret in both processes:

```powershell
$env:COPILOT_N8N_SHARED_SECRET = "<GENERATE-A-PRIVATE-SIGNING-SECRET>"
```

Import the inactive workflow definitions from `support_copilot/n8n/`, complete
the signing Code node using an n8n credential/environment secret, and replace
the no-op target with an approved email, Slack, or Teams destination. The
integration can export only already-finalized reports. It cannot finalize a
report, send a client reply, or access SQLite.

## Running Tests

Run the complete Support Copilot test suite:
```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m pytest support_copilot/tests -v
```

Run existing Telegram Assistant regression tests:
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
