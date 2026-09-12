"""Read-only work-source connectors. Imported items always enter the review inbox."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]  # Only needed for HTTP-based connectors

import config
from telegram_import import classify, extract_metadata, redact


@dataclass
class ConnectorItem:
    external_id: str
    occurred_at: str | None
    text: str
    media: list[str]
    metadata: dict


class Connector:
    name = 'base'

    def fetch(self, cursor=None, limit=100) -> tuple[list[ConnectorItem], str | None]:
        raise NotImplementedError


class FreshdeskConnector(Connector):
    name = 'freshdesk'

    def __init__(self, domain, api_key, agent_id):
        if not domain or not api_key or not agent_id:
            raise ValueError('Set FRESHDESK_DOMAIN, FRESHDESK_API_KEY, and FRESHDESK_AGENT_ID.')
        if domain.startswith('http://'):
            raise ValueError('Insecure HTTP protocol rejected. Freshdesk requires HTTPS.')
        self.base = domain.rstrip('/') if domain.startswith('https://') else f'https://{domain}.freshdesk.com'
        self.agent_id = int(agent_id)
        token = base64.b64encode(f'{api_key}:X'.encode()).decode()
        self.headers = {'Authorization': f'Basic {token}', 'Accept': 'application/json'}

    def fetch(self, cursor=None, limit=100):
        items = []
        page = 1
        while len(items) < limit:
            batch_size = min(limit - len(items), 100)
            params = {'page': page, 'per_page': batch_size, 'order_by': 'updated_at', 'order_type': 'asc'}
            if cursor:
                params['updated_since'] = cursor
            response = requests.get(self.base + '/api/v2/tickets', headers=self.headers,
                                    params=params, timeout=25)
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows = [row for row in batch if row.get('responder_id') == self.agent_id]
            for row in rows:
                description = row.get('description_text') or row.get('subject') or 'Freshdesk ticket update'
                text = f"{row.get('subject') or 'Ticket'} — {description} — status {row.get('status')}"
                items.append(ConnectorItem(str(row['id']), row.get('updated_at'), text, [], {
                    'ticket': str(row['id']), 'status': row.get('status'), 'priority': row.get('priority')}))
                if len(items) >= limit:
                    break
            if len(batch) < batch_size:
                break
            page += 1
        next_cursor = max((item.occurred_at for item in items if item.occurred_at), default=cursor)
        return items, next_cursor


class FreshchatConnector(Connector):
    name = 'freshchat'

    def __init__(self, base_url, api_key, agent_id):
        if not base_url or not api_key or not agent_id:
            raise ValueError('Set FRESHCHAT_BASE_URL, FRESHCHAT_API_KEY, and FRESHCHAT_AGENT_ID.')
        if base_url.startswith('http://'):
            raise ValueError('Insecure HTTP protocol rejected. Freshchat requires HTTPS.')
        self.base = base_url.rstrip('/')
        self.agent_id = str(agent_id)
        self.headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}

    def fetch(self, cursor=None, limit=100):
        response = requests.get(self.base + '/conversations', headers=self.headers,
                                params={'items_per_page': min(limit, 100)}, timeout=25)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get('conversations', payload if isinstance(payload, list) else [])
        items = []
        for row in rows:
            assigned = row.get('assigned_agent_id') or row.get('assigned_agent', {}).get('id')
            if assigned is not None and str(assigned) != self.agent_id:
                continue
            updated = row.get('updated_time') or row.get('updated_at') or row.get('created_time')
            if cursor and updated and updated <= cursor:
                continue
            preview = row.get('last_message', {})
            if isinstance(preview, dict):
                preview = preview.get('message') or preview.get('text') or ''
            text = row.get('subject') or preview or 'Freshchat conversation update'
            external_id = str(row.get('conversation_id') or row.get('id'))
            items.append(ConnectorItem(external_id, updated, text, [], {'conversation_id': external_id}))
            if len(items) >= limit:
                break
        next_cursor = max((item.occurred_at for item in items if item.occurred_at), default=cursor)
        return items, next_cursor


class CSVConnector(Connector):
    name = 'csv'

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise ValueError(f'CSV file not found: {self.path}')

    def fetch(self, cursor=None, limit=1000):
        items = []
        with self.path.open('r', encoding='utf-8-sig', newline='') as handle:
            for index, row in enumerate(csv.DictReader(handle), 1):
                external_id = str(row.get('id') or row.get('ticket') or index)
                text = row.get('text') or row.get('detail') or row.get('subject') or json.dumps(row)
                occurred = row.get('updated_at') or row.get('created_at') or row.get('timestamp')
                items.append(ConnectorItem(external_id, occurred, text, [], row))
                if len(items) >= limit:
                    break
        return items, cursor


def configured_connector(name, csv_path=None):
    name = name.casefold()
    if name == 'freshdesk':
        return FreshdeskConnector(config.FRESHDESK_DOMAIN, config.FRESHDESK_API_KEY,
                                  os.getenv('FRESHDESK_AGENT_ID', ''))
    if name == 'freshchat':
        return FreshchatConnector(config.FRESHCHAT_BASE_URL, config.FRESHCHAT_API_KEY,
                                  os.getenv('FRESHCHAT_AGENT_ID', ''))
    if name == 'csv':
        if not csv_path:
            raise ValueError('Provide a CSV path.')
        return CSVConnector(csv_path)
    raise ValueError('Connector must be freshdesk, freshchat, or csv.')


def sync_connector(database, connector: Connector, limit=100):
    state = database.connector_state(connector.name) or {}
    try:
        items, cursor = connector.fetch(state.get('cursor'), limit)
        inserted = 0
        for item in items:
            key = hashlib.sha256(
                f'{connector.name}|{item.external_id}'.encode('utf-8')).hexdigest()
            category, confidence = classify(item.text)
            _, created = database.add_source_message(
                source_type=connector.name, source_key=key, chat_name=connector.name,
                external_message_id=item.external_id, occurred_at=item.occurred_at,
                author_name=connector.name, author_is_owner=False, text=item.text,
                redacted_text=redact(item.text), message_kind='connector', media=item.media,
                classification=category, confidence=confidence, review_status='pending',
                metadata={**extract_metadata(item.text), **item.metadata, 'trusted': False})
            inserted += int(created)
        database.save_connector_state(connector.name, cursor, None,
                                      {'fetched': len(items), 'inserted': inserted})
        return {'connector': connector.name, 'fetched': len(items), 'inserted': inserted,
                'cursor': cursor}
    except Exception as exc:
        database.save_connector_state(connector.name, state.get('cursor'), type(exc).__name__)
        raise
