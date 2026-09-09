"""Configure optional read-only support connectors."""
import getpass
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credential_store import write_secret


def main():
    path = Path(__file__).resolve().parents[1] / '.env'
    values = dotenv_values(path) if path.exists() else {}
    print('Optional connector setup. Enter leaves that connector unchanged.')
    freshdesk_domain = input('Freshdesk domain or full base URL: ').strip()
    freshdesk_agent = input('Your Freshdesk agent ID: ').strip()
    freshdesk_key = getpass.getpass('Freshdesk API key (hidden): ').strip()
    freshchat_url = input('Freshchat API base URL (ending in /v2): ').strip()
    freshchat_agent = input('Your Freshchat agent ID: ').strip()
    freshchat_key = getpass.getpass('Freshchat API key (hidden): ').strip()
    for name, value in (
        ('FRESHDESK_DOMAIN', freshdesk_domain), ('FRESHDESK_AGENT_ID', freshdesk_agent),
        ('FRESHCHAT_BASE_URL', freshchat_url), ('FRESHCHAT_AGENT_ID', freshchat_agent)):
        if value:
            set_key(str(path), name, value)
    if freshdesk_key:
        try:
            write_secret('FRESHDESK_API_KEY', freshdesk_key)
        except OSError:
            set_key(str(path), 'FRESHDESK_API_KEY', freshdesk_key)
            print('Credential Manager unavailable; Freshdesk key saved in local .env.')
    if freshchat_key:
        try:
            write_secret('FRESHCHAT_API_KEY', freshchat_key)
        except OSError:
            set_key(str(path), 'FRESHCHAT_API_KEY', freshchat_key)
            print('Credential Manager unavailable; Freshchat key saved in local .env.')
    print('Connector configuration saved. Imports remain read-only and require review.')


if __name__ == '__main__':
    main()
