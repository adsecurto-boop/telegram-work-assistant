"""Unit tests for historical message clustering."""
import tempfile
import unittest
from pathlib import Path

from database import Database
from clustering import HistoricalClusterEngine


class TestHistoricalClustering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'work.sqlite3')
        self.engine = HistoricalClusterEngine(self.db)
        self.sid = self.db.start_shift('2026-09-09T10:00:00+05:30', '2026-09-09T19:00:00+05:30')

    def test_reply_chain_clustering(self):
        # Insert 3 messages where msg2 replies to msg1, and msg3 is standalone
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='key-1', text='First message',
            redacted_text='First message', author_is_owner=True, review_status='pending',
            shift_id=self.sid, metadata={'client': 'Acme', 'product': 'EmpMonitor'}
        )
        m2, _ = self.db.add_source_message(
            source_type='telegram', source_key='key-2', reply_to_key='key-1',
            text='Reply to first', redacted_text='Reply to first', author_is_owner=True,
            review_status='pending', shift_id=self.sid, metadata={'client': 'Acme', 'product': 'EmpMonitor'}
        )
        m3, _ = self.db.add_source_message(
            source_type='telegram', source_key='key-3', text='Standalone message',
            redacted_text='Standalone message', author_is_owner=True, review_status='pending',
            shift_id=self.sid, metadata={'client': 'BetaCorp'}
        )

        clusters, metrics = self.engine.cluster()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(metrics['candidate_messages_processed'], 3)
        self.assertEqual(metrics['suggested_clusters'], 1)
        self.assertEqual(metrics['standalone_items'], 1)
        self.assertEqual(len(clusters[0].messages), 2)
        self.assertIn('reply chain', clusters[0].reason)

    def test_shared_ticket_clustering(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='t-key-1', text='Ticket 101 report',
            redacted_text='Ticket 101 report', author_is_owner=True, review_status='pending',
            shift_id=self.sid, metadata={'ticket': 'SUP-101', 'client': 'Acme'}
        )
        m2, _ = self.db.add_source_message(
            source_type='telegram', source_key='t-key-2', text='Ticket 101 update',
            redacted_text='Ticket 101 update', author_is_owner=True, review_status='pending',
            shift_id=self.sid, metadata={'ticket': 'SUP-101', 'client': 'Acme'}
        )

        clusters, metrics = self.engine.cluster()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(metrics['suggested_clusters'], 1)
        self.assertEqual(clusters[0].suggested_client, 'Acme')

    def test_apply_suggestions_persists_pending_clusters(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='p-1', text='Parent',
            redacted_text='Parent', author_is_owner=True, review_status='pending',
            shift_id=self.sid
        )
        m2, _ = self.db.add_source_message(
            source_type='telegram', source_key='p-2', reply_to_key='p-1', text='Child',
            redacted_text='Child', author_is_owner=True, review_status='pending',
            shift_id=self.sid
        )

        clusters, _ = self.engine.cluster()
        saved = self.engine.apply_suggestions(clusters)
        self.assertEqual(saved, 1)

        db_clusters = self.db.get_clusters()
        self.assertEqual(len(db_clusters), 1)
        self.assertEqual(db_clusters[0]['status'], 'pending')
        self.assertEqual(db_clusters[0]['message_count'], 2)


if __name__ == '__main__':
    unittest.main()
