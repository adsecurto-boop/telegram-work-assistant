"""
Historical message clustering engine for the review inbox candidates.
Conservative clustering using deterministic signals: reply chains, tickets,
clients, products, and temporal proximity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from database import Database, now_iso
from telegram_import import extract_metadata, redact

logger = logging.getLogger(__name__)

CLUSTERING_VERSION = 'cluster-v1'


@dataclass
class ClusterSuggestion:
    id: int | None = None
    title: str = ''
    suggested_client: str | None = None
    suggested_product: str | None = None
    suggested_issue_type: str | None = None
    confidence: float = 0.5
    reason: str = ''
    status: str = 'pending'
    messages: list[dict] = field(default_factory=list)
    existing_case_id: int | None = None
    fingerprint: str = ''


class DisjointSet:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        if item not in self.parent:
            self.parent[item] = item
            return item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, item1, item2):
        root1 = self.find(item1)
        root2 = self.find(item2)
        if root1 != root2:
            self.parent[root2] = root1


class ClusterApplyResult(int):
    saved: int = 0
    already_suggested: int = 0

    def __new__(cls, saved_count: int, already_suggested: int = 0):
        obj = super().__new__(cls, saved_count)
        obj.saved = saved_count
        obj.already_suggested = already_suggested
        return obj

    def __iter__(self):
        yield self.saved
        yield self.already_suggested


class HistoricalClusterEngine:
    def __init__(self, db: Database):
        self.db = db

    def load_candidates(self, import_id: int | None = None,
                        start_date: str | None = None,
                        end_date: str | None = None) -> list[dict]:
        with self.db.connect() as conn:
            sql = '''SELECT * FROM source_messages
                     WHERE review_status='pending' AND author_is_owner=1'''
            params = []
            if import_id is not None:
                sql += ' AND import_id=?'; params.append(import_id)
            if start_date:
                sql += ' AND occurred_at >= ?'; params.append(start_date)
            if end_date:
                sql += ' AND occurred_at <= ?'; params.append(end_date)
            sql += ' ORDER BY occurred_at, id'
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def cluster(self, import_id: int | None = None,
                start_date: str | None = None,
                end_date: str | None = None,
                min_cluster_size: int = 2) -> tuple[list[ClusterSuggestion], dict[str, Any]]:
        messages = self.load_candidates(import_id, start_date, end_date)
        if not messages:
            return [], {
                'candidate_messages_processed': 0,
                'suggested_clusters': 0,
                'standalone_items': 0,
                'high_confidence_count': 0,
                'medium_confidence_count': 0,
                'low_confidence_count': 0,
                'existing_case_matches': 0,
                'possible_duplicates': 0,
                'ai_calls': 0,
                'ai_failures': 0,
            }

        msg_by_id = {m['id']: m for m in messages}
        msg_by_key = {m['source_key']: m for m in messages if m.get('source_key')}

        meta_by_id = {}
        for m in messages:
            meta = {}
            if m.get('metadata_json'):
                try:
                    meta = json.loads(m['metadata_json'])
                except Exception:
                    pass
            if not meta and m.get('text'):
                meta = extract_metadata(m['text'])
            meta_by_id[m['id']] = meta

        ds = DisjointSet()
        link_reasons = defaultdict(list)

        # 1. Telegram reply chains
        for m in messages:
            reply_key = m.get('reply_to_key')
            if reply_key and reply_key in msg_by_key:
                parent = msg_by_key[reply_key]
                c1 = meta_by_id[m['id']].get('client')
                c2 = meta_by_id[parent['id']].get('client')
                if not c1 or not c2 or c1.casefold() == c2.casefold():
                    ds.union(m['id'], parent['id'])
                    link_reasons[m['id']].append('reply_chain')

        # 2. Exact ticket matches (must have digits and >=3 chars)
        ticket_groups = defaultdict(list)
        for m in messages:
            ticket = meta_by_id[m['id']].get('ticket')
            if ticket and re.search(r'\d', ticket) and len(ticket) >= 3 and ticket.lower() not in ('while', 'none', 'null', 'ticket'):
                ticket_groups[ticket.casefold()].append(m['id'])

        for ticket, ids in ticket_groups.items():
            if len(ids) > 1:
                first = ids[0]
                for other in ids[1:]:
                    c1 = meta_by_id[first].get('client')
                    c2 = meta_by_id[other].get('client')
                    if not c1 or not c2 or c1.casefold() == c2.casefold():
                        ds.union(first, other)
                        link_reasons[other].append(f'ticket:{ticket}')

        # 3. Same client + product with temporal proximity (< 48 hours)
        client_groups = defaultdict(list)
        for m in messages:
            client = meta_by_id[m['id']].get('client')
            if client:
                client_groups[client.casefold()].append(m['id'])

        for client_name, ids in client_groups.items():
            if len(ids) > 1:
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        m1 = msg_by_id[ids[i]]
                        m2 = msg_by_id[ids[j]]
                        p1 = meta_by_id[m1['id']].get('product')
                        p2 = meta_by_id[m2['id']].get('product')
                        if p1 and p2 and p1.casefold() == p2.casefold():
                            t1 = m1.get('occurred_at')
                            t2 = m2.get('occurred_at')
                            if t1 and t2:
                                try:
                                    dt1 = datetime.fromisoformat(t1[:19])
                                    dt2 = datetime.fromisoformat(t2[:19])
                                    if abs((dt2 - dt1).total_seconds()) <= 48 * 3600:
                                        ds.union(m1['id'], m2['id'])
                                        link_reasons[m2['id']].append('client_product_temporal')
                                except Exception:
                                    pass

        # Group messages by root
        grouped = defaultdict(list)
        for m in messages:
            root = ds.find(m['id'])
            grouped[root].append(m)

        clusters = []
        standalone_count = 0
        existing_case_matches = 0
        possible_duplicates = 0

        # Existing approved cases for reference matching
        existing_cases = self.db.list_cases()
        existing_by_client = defaultdict(list)
        existing_by_ticket = defaultdict(list)
        for ec in existing_cases:
            if ec.get('client'):
                existing_by_client[ec['client'].casefold()].append(ec['id'])
            if ec.get('ticket'):
                existing_by_ticket[ec['ticket'].casefold()].append(ec['id'])

        for root_id, msgs in grouped.items():
            if len(msgs) < min_cluster_size:
                standalone_count += len(msgs)
                continue

            clients = [meta_by_id[m['id']].get('client') for m in msgs if meta_by_id[m['id']].get('client')]
            products = [meta_by_id[m['id']].get('product') for m in msgs if meta_by_id[m['id']].get('product')]
            tickets = [meta_by_id[m['id']].get('ticket') for m in msgs if meta_by_id[m['id']].get('ticket')]
            classes = [m.get('classification') for m in msgs if m.get('classification')]

            canonical_client = clients[0] if clients else None
            canonical_product = products[0] if products else None
            canonical_ticket = tickets[0] if tickets else None
            canonical_class = classes[0] if classes else 'inquiry'

            all_reasons = []
            for m in msgs:
                all_reasons.extend(link_reasons.get(m['id'], []))

            confidence = 0.6
            reason_parts = []
            if any(r == 'reply_chain' for r in all_reasons):
                confidence = 0.9
                reason_parts.append(f'Connected by Telegram reply chain ({len(msgs)} msgs)')
            if any('ticket' in r for r in all_reasons):
                confidence = max(confidence, 0.9)
                reason_parts.append(f'Shared ticket reference ({canonical_ticket})')
            if any(r == 'client_product_temporal' for r in all_reasons):
                confidence = max(confidence, 0.75)
                reason_parts.append(f'Matching client ({canonical_client}) and product within 48h')

            if not reason_parts:
                reason_parts.append(f'Correlated interaction thread ({len(msgs)} msgs)')

            # Title generation
            title_parts = []
            if canonical_client:
                title_parts.append(canonical_client)
            if canonical_ticket:
                title_parts.append(f'[{canonical_ticket}]')
            first_text = (msgs[0].get('text') or '').strip()
            clean_snippet = re.sub(r'https?://\S+|#\w+|@\w+', '', first_text).strip()
            clean_snippet = re.sub(r'\s+', ' ', clean_snippet)[:45]
            if clean_snippet:
                title_parts.append(clean_snippet)
            else:
                title_parts.append(f'{canonical_product or "Support"} issue')

            title = ' - '.join(title_parts) if title_parts else f'Cluster #{root_id}'

            # Case Matching Priority:
            # 1. Exact unique ticket
            # 2. Strong client + product match (only if unique)
            # 3. Client ONLY if client has exactly 1 open case
            matched_case_id = None
            if canonical_ticket and canonical_ticket.casefold() in existing_by_ticket:
                ticket_matches = existing_by_ticket[canonical_ticket.casefold()]
                if len(ticket_matches) == 1:
                    matched_case_id = ticket_matches[0]
                    existing_case_matches += 1
            elif canonical_client and canonical_product:
                cp_matches = [
                    ec['id'] for ec in existing_cases
                    if ec.get('client') and ec['client'].casefold() == canonical_client.casefold()
                    and ec.get('product') and ec['product'].casefold() == canonical_product.casefold()
                ]
                if len(cp_matches) == 1:
                    matched_case_id = cp_matches[0]
                    existing_case_matches += 1
            elif canonical_client and canonical_client.casefold() in existing_by_client:
                c_matches = existing_by_client[canonical_client.casefold()]
                if len(c_matches) == 1:
                    matched_case_id = c_matches[0]
                    existing_case_matches += 1

            # Check duplicate texts
            unique_texts = {m.get('text', '').strip() for m in msgs if m.get('text')}
            if len(unique_texts) < len(msgs):
                possible_duplicates += (len(msgs) - len(unique_texts))

            # Compute stable cluster fingerprint
            sorted_msg_ids = sorted(m['id'] for m in msgs)
            fp_raw = f"{CLUSTERING_VERSION}:" + ",".join(str(mid) for mid in sorted_msg_ids)
            fingerprint = hashlib.sha256(fp_raw.encode('utf-8')).hexdigest()

            clusters.append(ClusterSuggestion(
                id=None,
                title=title,
                suggested_client=canonical_client,
                suggested_product=canonical_product,
                suggested_issue_type=canonical_class,
                confidence=confidence,
                reason='; '.join(reason_parts),
                status='pending',
                messages=msgs,
                existing_case_id=matched_case_id,
                fingerprint=fingerprint
            ))

        high_conf = sum(1 for c in clusters if c.confidence >= 0.8)
        med_conf = sum(1 for c in clusters if 0.5 <= c.confidence < 0.8)
        low_conf = sum(1 for c in clusters if c.confidence < 0.5)

        metrics = {
            'candidate_messages_processed': len(messages),
            'suggested_clusters': len(clusters),
            'standalone_items': standalone_count,
            'high_confidence_count': high_conf,
            'medium_confidence_count': med_conf,
            'low_confidence_count': low_conf,
            'existing_case_matches': existing_case_matches,
            'possible_duplicates': possible_duplicates,
            'ai_calls': 0,
            'ai_failures': 0,
        }

        return clusters, metrics

    suggest_clusters = cluster

    def apply_suggestions(self, clusters: list[ClusterSuggestion]) -> tuple[int, int]:
        """
        Persists suggested clusters into history_clusters and cluster_items idempotently.
        Repeated application against unchanged messages creates 0 duplicate clusters.
        Returns: (saved_count, already_suggested_count)
        """
        saved_count = 0
        already_suggested_count = 0
        for c in clusters:
            if not c.messages:
                continue

            # Check if cluster with this fingerprint already exists
            if c.fingerprint:
                existing = self.db.get_cluster_by_fingerprint(c.fingerprint)
                if existing:
                    c.id = existing['id']
                    already_suggested_count += 1
                    continue

            c_id = self.db.create_cluster(
                title=c.title,
                suggested_client=c.suggested_client,
                suggested_product=c.suggested_product,
                suggested_issue_type=c.suggested_issue_type,
                confidence=c.confidence,
                reason=c.reason,
                status='pending',
                fingerprint=c.fingerprint
            )
            for m in c.messages:
                self.db.add_cluster_item(c_id, m['id'])
            c.id = c_id
            saved_count += 1
        return ClusterApplyResult(saved_count, already_suggested_count)

    # Cluster Management operations
    def reject_cluster(self, cluster_id: int):
        return self.db.reject_cluster(cluster_id)

    def split_cluster(self, cluster_id: int, new_title: str, move_message_ids: list[int]):
        return self.db.split_cluster(cluster_id, new_title, move_message_ids)

    def merge_clusters(self, primary_cluster_id: int, secondary_cluster_id: int):
        return self.db.merge_clusters(primary_cluster_id, secondary_cluster_id)

    def attach_cluster_to_case(self, cluster_id: int, case_id: int):
        return self.db.attach_cluster_to_case(cluster_id, case_id)
