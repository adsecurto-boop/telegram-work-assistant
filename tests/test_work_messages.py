import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from database import Database
from work_messages import DraftStore, render, classify, handle_work_message, extract


class WorkMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'work.db')
        self.store = DraftStore(self.db, 123)
        self.fields = dict(scenario='requirement', recipients='@dev sir', platform='Teams',
                           client='Example + TECH TEAM', requirements='Show download and upload speed.',
                           admin_email='admin@example.com', cc='@sales')

    def test_exact_template(self):
        self.assertEqual(render(self.fields), "hello @dev sir,\n\nkindly check this client requirement over Teams,\nExample + TECH TEAM\n\nRequirement ::\n\nShow download and upload speed.\n\nclient admin mail id - admin@example.com\nCC: @sales")

    def test_replay_revision_and_owner(self):
        draft = self.store.create('original', self.fields, '123:1')
        self.assertEqual(self.store.create('original', self.fields, '123:1')['id'], draft['id'])
        new = self.store.edit(draft['id'], 1, {'requirements': '43 instead of 35 licenses'})
        self.assertEqual(new['revision'], 2)
        with self.assertRaises(ValueError):
            self.store.action(draft['id'], 1, 'shared')
        with self.assertRaises(ValueError):
            DraftStore(self.db, 999).get(draft['id'])
        self.assertEqual(DraftStore(Database(self.db.path), 123).get(draft['id'])['revision'], 2)

    def test_shared_event_once_and_draft_not_completed_work(self):
        sid = self.db.start_shift('2026-09-10T12:00:00+05:30', '2026-09-10T21:00:00+05:30')
        draft = self.store.create('original', self.fields, '123:2')
        self.assertEqual(self.db.activities(sid), [])
        self.store.action(draft['id'], 1, 'shared')
        self.store.action(draft['id'], 1, 'shared')
        self.assertEqual(len(self.db.activities(sid)), 1)

    def test_missing_fields_cannot_be_shared(self):
        draft = self.store.create('incomplete', {}, '123:3')
        with self.assertRaises(ValueError):
            self.store.action(draft['id'], 1, 'shared')

    def test_migration_preserves_tasks_and_backup(self):
        task = self.db.add_task('Preserve me')
        with self.db.connect() as conn:
            conn.execute('PRAGMA user_version=7')
            conn.execute('DROP TABLE work_draft_events')
            conn.execute('DROP TABLE work_drafts')
            conn.execute('DROP TABLE work_contacts')
            conn.execute('DROP TABLE work_client_profiles')
        migrated = Database(self.db.path)
        self.assertEqual(migrated.get_task(task.id).title, 'Preserve me')
        self.assertEqual(migrated.integrity(), 'ok')
        self.assertTrue(list((self.db.path.parent / 'backups').glob('pre-migration-v7-*')))

    async def test_status_and_followup_are_explicit_and_idempotent(self):
        draft = self.store.create('original', self.fields, '123:4')
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={'db': self.db}))
        msg = SimpleNamespace(text=f"/workstatus {draft['id']} fix_reported Developer says fixed; retest pending", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123,type='private'),
                                 effective_message=msg, callback_query=None, update_id=10)
        with patch.object(config, 'OWNER_ID', 123):
            await handle_work_message(update, ctx)
            await handle_work_message(update, ctx)
            self.assertEqual(self.store.get(draft['id'])['status'], 'fix_reported')
            msg.text = f"/draftfollowup {draft['id']} 2099-01-01T12:00"
            await handle_work_message(update, ctx)
            await handle_work_message(update, ctx)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_draft_events').fetchone()[0], 2)

    async def test_offline_preserves_note(self):
        with patch.object(config, 'AI_KEY', ''):
            result = await extract('Client request over WhatsApp for admin@example.com: download speed', self.db)
        self.assertEqual(result['platform'], 'WhatsApp')
        self.assertNotIn('latency', result['requirements'])

    async def test_handler_and_reply_edit(self):
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={'db': self.db}))
        msg = SimpleNamespace(text='/draft scenario=requirement | client=Example', reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123,type='private'),
                                 effective_message=msg, callback_query=None, update_id=9)
        with patch.object(config, 'OWNER_ID', 123):
            self.assertTrue(await handle_work_message(update, ctx))
            original = msg.reply_text.call_args.args[0]
            msg.reply_to_message = SimpleNamespace(text=original)
            msg.text = 'platform=Teams | cc=@sales'
            self.assertTrue(await handle_work_message(update, ctx))
        self.assertEqual(self.store.get(1)['fields']['platform'], 'Teams')

    def test_scenarios(self):
        for text, expected in [('any update on this?', 'followup'), ('43 instead of 35 licenses','modification'),
                               ('client confirmed working','resolution'), ('okay sir','acknowledgment'),
                               ('developer fixed it','fix'), ('on-premise setup','deployment')]:
            self.assertEqual(classify(text), expected)
