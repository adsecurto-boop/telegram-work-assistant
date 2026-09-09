"""Deterministic, privacy-aware importer for Telegram Desktop HTML exports."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from database import Database


SENSITIVE_PATTERNS = (
    (re.compile(r"https?://\S+", re.I), "[LINK]"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"@[A-Za-z0-9_]+"), "[MENTION]"),
    (re.compile(r"\b(?:\+?\d[\d ()-]{7,}\d)\b"), "[NUMBER]"),
    (re.compile(r"\b(password|token|secret|api[_ -]?key|otp)\s*[:=]\s*\S+", re.I), r"\1: [REDACTED]"),
)


def redact(text: str) -> str:
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def classify(text: str) -> tuple[str, float]:
    low = normalized(text)
    rules = (
        ('system_notification', r'customer chats? waiting for reply|silent \d+ min|on-shift', .98),
        ('resolution', r'\b(resolved|fixed|solved|working now|issue is closed)\b', .90),
        ('testing', r'\b(test|tested|testing|retest|verify|verified|reproduce|passed|failed)\b', .88),
        ('pending', r'\b(pending|waiting|awaiting|follow[ -]?up|tomorrow)\b', .84),
        ('call', r'\b(call|meeting|demo|google meet|remote session|anydesk)\b', .84),
        ('assignment', r'\b(assign|assigned|kindly check|please check|can you check|handoff)\b', .82),
        ('investigation', r'\b(investigat|checked|checking|logs?|debug|observed|issue|error|problem)\b', .78),
        ('client_query', r'\b(client|customer)\b.*\b(query|requirement|asking|reported)\b|\bclient query\b', .82),
        ('evidence', r'\b(screenshot|screen recording|video|attachment|document)\b', .76),
    )
    for name, pattern, confidence in rules:
        if re.search(pattern, low, re.I):
            return name, confidence
    return 'note', .55


def extract_metadata(text: str) -> dict:
    low = text.casefold()
    metadata = {}
    client_match = re.search(r'\b(?:client name|client)\s*[:\-]\s*([^\n|]{2,80})', text, re.I)
    if client_match:
        candidate = client_match.group(1).strip(' :-')
        if not re.match(r'^(query|issue|requirement|reported|email|admin)\b', candidate, re.I):
            metadata['client'] = candidate[:80]
    for name, patterns in {
        'Freshchat': ('freshchat','freshworks'), 'WhatsApp': ('whatsapp',),
        'Teams': ('teams',), 'Telegram': ('telegram',), 'Email': ('email','mail'),
        'Call': ('call','meet')}.items():
        if any(re.search(rf'\b{re.escape(pattern)}\b', low) for pattern in patterns):
            metadata['channel'] = name
            break
    for platform in ('windows','macos','mac','linux','android','ios'):
        if re.search(rf'\b{platform}\b', low):
            metadata['platform'] = 'macOS' if platform in ('mac','macos') else platform.title()
            break
    if 'empmonitor' in low:
        metadata['product'] = 'EmpMonitor'
    ticket = re.search(r'\b(?:ticket|issue|bug|request)\s*(?:id|key|no\.?|#)?\s*[:#-]\s*([A-Z0-9][A-Z0-9_-]{2,30})',
                       text, re.I)
    if ticket:
        metadata['ticket'] = ticket.group(1)
    return metadata


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[object] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get('class', '').split())

    def descendants(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()

    def find(self, *, tag=None, classes=()):
        required = set(classes)
        for node in self.descendants():
            if (tag is None or node.tag == tag) and required.issubset(node.classes):
                return node
        return None

    def find_all(self, *, tag=None, classes=()):
        required = set(classes)
        return [node for node in self.descendants()
                if (tag is None or node.tag == tag) and required.issubset(node.classes)]

    def text(self) -> str:
        parts: list[str] = []
        def visit(node):
            if isinstance(node, str):
                parts.append(node)
            else:
                if node.tag == 'br':
                    parts.append('\n')
                for child in node.children:
                    visit(child)
        visit(self)
        value = ''.join(parts)
        value = re.sub(r'[ \t\r\f\v]+', ' ', value)
        value = re.sub(r' *\n *', '\n', value)
        return value.strip()


class TelegramHTMLParser(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@dataclass
class TelegramMessage:
    message_id: str
    occurred_at: str | None
    author: str
    text: str
    reply_to_id: str | None
    media: list[str]


def _message_number(value: str | None) -> str:
    return re.sub(r'\D+', '', value or '')


def parse_file(path: Path, last_sender: str | None = None) -> tuple[str, list[TelegramMessage], str | None]:
    parser = TelegramHTMLParser()
    parser.feed(path.read_text(encoding='utf-8-sig', errors='replace'))
    title_node = parser.root.find(tag='div', classes=('page_header',))
    title_text = title_node.find(tag='div', classes=('text','bold')).text() if title_node else path.parent.name
    messages = []
    for node in parser.root.find_all(tag='div', classes=('message','default')):
        body = node.find(tag='div', classes=('body',))
        if not body:
            continue
        sender_node = body.find(tag='div', classes=('from_name',))
        if sender_node and sender_node.text():
            last_sender = sender_node.text()
        author = last_sender or 'Unknown'
        date_node = body.find(tag='div', classes=('date','details'))
        occurred_at = None
        if date_node and date_node.attrs.get('title'):
            raw = date_node.attrs['title'].replace(' UTC+05:30', '')
            try:
                occurred_at = datetime.strptime(raw, '%d.%m.%Y %H:%M:%S').isoformat()
            except ValueError:
                occurred_at = raw
        text_node = body.find(tag='div', classes=('text',))
        text = text_node.text() if text_node else ''
        reply_node = body.find(tag='div', classes=('reply_to',))
        reply_to_id = None
        if reply_node:
            link = reply_node.find(tag='a')
            reply_to_id = _message_number(link.attrs.get('href')) if link else None
        media = []
        for descendant in body.descendants():
            if descendant.tag != 'a':
                continue
            href = descendant.attrs.get('href', '')
            if href and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', href) and not href.startswith('#'):
                candidate = (path.parent / href).resolve()
                if candidate.is_file():
                    media.append(str(candidate))
        messages.append(TelegramMessage(
            _message_number(node.attrs.get('id')), occurred_at, author, text,
            reply_to_id, sorted(set(media))))
    return title_text.strip(), messages, last_sender


def export_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    def order(item: Path):
        number = _message_number(item.stem)
        return int(number) if number else 1
    return sorted(path.glob('messages*.html'), key=order)


def fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode('utf-8'))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_key(chat_name: str, message_id: str) -> str:
    return hashlib.sha256(f'telegram|{normalized(chat_name)}|{message_id}'.encode('utf-8')).hexdigest()


def import_export(database: Database | None, path: Path, owner_aliases=(), apply=False, limit=None):
    files = export_files(path)
    if not files:
        raise ValueError(f'No messages*.html files found under {path}.')
    all_messages = []
    title = path.name
    last_sender = None
    for file_path in files:
        title, messages, last_sender = parse_file(file_path, last_sender)
        all_messages.extend(messages)
        if limit and len(all_messages) >= limit:
            all_messages = all_messages[:limit]
            break
    sender_counts = Counter(message.author for message in all_messages)
    aliases = {normalized(alias) for alias in owner_aliases if alias}
    if not aliases and sender_counts:
        sender, count = sender_counts.most_common(1)[0]
        if count / len(all_messages) >= .80:
            aliases.add(normalized(sender))
    stats = Counter()
    stats['files'] = len(files)
    stats['messages'] = len(all_messages)
    stats['senders'] = len(sender_counts)
    for message in all_messages:
        stats[classify(message.text)[0]] += 1
        stats['media'] += len(message.media)
        if normalized(message.author) in aliases:
            stats['owner_messages'] += 1
    result = {'chat': title, **dict(stats), 'owner_aliases_detected': len(aliases)}
    if not apply:
        return result
    if database is None:
        raise ValueError('A database is required with apply=True.')
    batch, created = database.create_import('telegram_html', title, str(path), fingerprint(files))
    if not created:
        return {**result, 'import_id': batch['id'], 'status': 'already_imported', 'inserted': 0}
    inserted = 0
    try:
        for message in all_messages:
            owner = normalized(message.author) in aliases
            category, confidence = classify(message.text)
            key = source_key(title, message.message_id)
            reply_key = source_key(title, message.reply_to_id) if message.reply_to_id else None
            kind = 'media' if message.media and not message.text else ('mixed' if message.media else 'text')
            _, was_inserted = database.add_source_message(
                import_id=batch['id'], source_type='telegram_html', source_key=key,
                chat_name=title, external_message_id=message.message_id,
                occurred_at=message.occurred_at, author_name=message.author,
                author_is_owner=owner, text=message.text, redacted_text=redact(message.text),
                reply_to_key=reply_key, message_kind=kind, media=message.media,
                classification=category, confidence=confidence,
                review_status='pending' if owner and category not in ('system_notification','note') else 'ignored',
                metadata=extract_metadata(message.text))
            inserted += int(was_inserted)
        result['inserted'] = inserted
        result['import_id'] = batch['id']
        result['status'] = 'completed'
        database.finish_import(batch['id'], 'completed', result)
        if aliases:
            database.set_setting('telegram_owner_aliases', json.dumps(sorted(aliases)))
        return result
    except Exception:
        database.finish_import(batch['id'], 'failed', result)
        raise


def backfill_metadata(database: Database) -> int:
    with database.connect() as connection:
        rows = connection.execute('''SELECT id,text FROM source_messages
            WHERE metadata_json IS NULL OR metadata_json='' ''').fetchall()
        for row in rows:
            connection.execute('UPDATE source_messages SET metadata_json=? WHERE id=?',
                               (json.dumps(extract_metadata(row['text'] or '')), row['id']))
        return len(rows)


def main():
    parser = argparse.ArgumentParser(description='Analyze or import Telegram Desktop HTML exports.')
    parser.add_argument('paths', nargs='*', type=Path)
    parser.add_argument('--database', type=Path, help='SQLite path; defaults to configured SQLITE_PATH.')
    parser.add_argument('--owner', action='append', default=[], help='Your exact Telegram display name; repeat for aliases.')
    parser.add_argument('--apply', action='store_true', help='Write reviewable source messages to SQLite.')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--backfill-metadata', action='store_true',
                        help='Populate structured metadata for already imported messages.')
    args = parser.parse_args()
    if args.apply and not args.database:
        import config
        args.database = Path(config.DB_PATH)
    database = Database(args.database) if args.apply else None
    if args.backfill_metadata:
        if not database:
            parser.error('--backfill-metadata requires --apply')
        print(json.dumps({'metadata_backfilled': backfill_metadata(database)}, indent=2))
        return
    if not args.paths:
        parser.error('provide at least one Telegram export path')
    aliases = list(args.owner)
    if database and not aliases:
        try:
            aliases = json.loads(database.get_setting('telegram_owner_aliases') or '[]')
        except json.JSONDecodeError:
            aliases = []
    outputs = []
    for path in args.paths:
        result = import_export(database, path, aliases, args.apply, args.limit)
        outputs.append(result)
        if database and result.get('owner_aliases_detected') and not aliases:
            aliases = json.loads(database.get_setting('telegram_owner_aliases') or '[]')
    print(json.dumps(outputs, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
