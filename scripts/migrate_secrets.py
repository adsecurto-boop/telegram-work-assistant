"""Move supported secrets from .env to Windows Credential Manager."""
import sys
from pathlib import Path

from dotenv import dotenv_values, unset_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credential_store import write_secret


def main():
    path = Path(__file__).resolve().parents[1] / '.env'
    values = dotenv_values(path) if path.exists() else {}
    moved = []
    for name in ('BOT_TOKEN','GEMINI_API_KEY','FRESHDESK_API_KEY','FRESHCHAT_API_KEY'):
        value = values.get(name)
        if value:
            write_secret(name, value)
            unset_key(str(path), name)
            moved.append(name)
    print('Moved to Windows Credential Manager: ' + (', '.join(moved) if moved else 'none'))


if __name__ == '__main__':
    main()
