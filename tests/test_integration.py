import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from datetime import datetime,timezone,timedelta
import config
import handlers
from bot import gate
from database import Database
from scheduler import tick
from telegram.ext import ApplicationHandlerStop
from ai import Suggestion

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'work.sqlite3')
        self.owner=patch.object(config,'OWNER_ID',123); self.owner.start(); self.addCleanup(self.owner.stop)
        self.context=SimpleNamespace(application=SimpleNamespace(bot_data={'db':self.db}),bot=SimpleNamespace(send_message=AsyncMock()))

    def update(self,text,user=123,chat=123,kind='private',uid=1):
        message=SimpleNamespace(text=text,reply_text=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=user),effective_chat=SimpleNamespace(id=chat,type=kind),
            update_id=uid,callback_query=None,effective_message=message)

    async def test_gate_blocks_other_users_groups_and_duplicates(self):
        for update in [self.update('/task X',user=456),self.update('/task X',kind='group')]:
            with self.assertRaises(ApplicationHandlerStop): await gate(update,self.context)
        update=self.update('/help')
        await gate(update,self.context)
        with self.assertRaises(ApplicationHandlerStop): await gate(update,self.context)

    async def test_workflow_and_aliases(self):
        for text in ['/shift 12:00 21:00','/schedule 21:00 16:00','/task Test Linux','/progress 1',
                     '/support Pace | Teams | investigated | Installation issue','/learning Received KT','/pl','/done 1','/eod']:
            await handlers.handle(self.update(text),self.context)
        self.assertEqual(len(self.db.history()),2)
        report=self.db.report(2)
        self.assertIn('Test Linux',report['text'])
        self.assertIn('[investigated]',report['text'])
        self.assertEqual(self.db.active_shift()['lunch'][11:16],'16:00')

    async def test_rich_commands_and_report_preferences(self):
        commands=[
            '/shift 12:00 21:00',
            '/task Test login | urgent | tomorrow | EmpMonitor | Pace | REQ-1 | Retest | linux',
            '/support Pace | Teams | resolved | Fixed install | EmpMonitor | installation | Send guide | SUP-1 | 2 | pace-install',
            '/testing Web blocking | failed | EmpMonitor | Rocky Linux | 4.2 | Two issues | BUG-1 | yes',
            '/learning Silent install | KT | EmpMonitor | Use flags | practise',
            '/reportstyle detailed', '/privacy clients on', '/eod']
        for uid,text in enumerate(commands,100):
            await handlers.handle(self.update(text,uid=uid),self.context)
        task=self.db.get_task(1)
        self.assertEqual(task.priority,3)
        self.assertEqual(task.project,'EmpMonitor')
        report=self.db.report(1)['text']
        self.assertIn('Explicit resolved queries: 2.',report)
        self.assertIn('Rocky Linux',report)
        self.assertIn('Client 1',report)
        self.assertNotIn('Pace (Teams)',report)

    async def test_catchup_is_combined_and_survives_restart(self):
        now=datetime.now(timezone.utc)
        sid=self.db.start_shift((now-timedelta(hours=10)).isoformat(),(now-timedelta(hours=1)).isoformat(),(now-timedelta(hours=5)).isoformat())
        with patch.object(config,'REMINDERS_ENABLED',True):
            await tick(self.context)
            self.context.bot.send_message.assert_awaited_once()
            text=self.context.bot.send_message.call_args.kwargs['text']
            for kind in ('tod','pl','eod'): self.assertIn('/'+kind,text)
            self.context.application.bot_data['db']=Database(self.db.path)
            await tick(self.context)
            self.context.bot.send_message.assert_awaited_once()

    async def test_failed_delivery_retries(self):
        now=datetime.now(timezone.utc)
        sid=self.db.start_shift((now-timedelta(hours=1)).isoformat(),(now+timedelta(hours=8)).isoformat())
        self.context.bot.send_message.side_effect=RuntimeError('offline')
        with patch.object(config,'REMINDERS_ENABLED',True):
            with self.assertRaises(RuntimeError): await tick(self.context)
        self.assertFalse(self.db.delivered(sid,'tod'))

    async def test_ai_failure_preserves_report(self):
        sid=self.db.start_shift('2026-09-08T12:00:00+05:30','2026-09-08T21:00:00+05:30')
        rid=self.db.save_report(sid,'eod','Original facts')
        with patch('ai.writer',return_value=SimpleNamespace(draft=AsyncMock(side_effect=TimeoutError))):
            await handlers.ai_report(self.update('/ai 1'),self.context,rid)
        self.assertEqual(len(self.db.history()),1)
        self.assertEqual(self.db.report(rid)['text'],'Original facts')

    async def test_proposal_atomic_single_application_and_stale_rejection(self):
        sid=self.db.start_shift('2026-09-08T12:00:00+05:30','2026-09-08T21:00:00+05:30')
        aid=self.db.add_activity(sid,'note','Plan to test Linux')
        payload=Suggestion.model_validate({'entries':[{'category':'plan','detail':'Test Linux'}]}).model_dump()['entries']
        pid=self.db.propose(aid,'Plan to test Linux',payload)
        self.db.apply_proposal(pid,sid)
        with self.assertRaises(ValueError): self.db.apply_proposal(pid,sid)
        self.assertEqual(len(self.db.list_tasks()),1)
        self.assertEqual(len(self.db.activities(sid)),1)
        aid=self.db.add_activity(sid,'note','Before')
        pid=self.db.propose(aid,'Before',payload)
        self.db.correct_activity(sid,aid,'After')
        with self.assertRaises(ValueError): self.db.apply_proposal(pid,sid)

    async def test_unicode_splitting(self):
        update=self.update('')
        text='😀'*5000
        await handlers.reply(update,text)
        parts=[call.args[0] for call in update.effective_message.reply_text.call_args_list]
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=3500 for p in parts))

class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_offline_with_isolated_paths(self):
        import bot
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.multiple(config,BOT_TOKEN='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',OWNER_ID=123,
                DB_PATH=str(root/'work.sqlite3'),LEGACY_PATH=str(root/'missing.json'),BASE_DIR=root):
                app=bot.build_application()
                self.assertIsNotNone(app.job_queue)
                self.assertIsNotNone(app.handlers[-1])
                self.assertEqual(len(list((root/'storage/backups').glob('*.sqlite3'))),1)
                await app.bot.shutdown()

class AiFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_primary_failure_uses_fallback(self):
        from ai import GeminiWriter
        from google.genai import errors
        calls=[]
        class Models:
            async def generate_content(self,model,contents,config):
                calls.append(model)
                if model=='primary':
                    raise errors.ServerError(503,{'error':{'message':'busy'}})
                return SimpleNamespace(text='ok')
        class Aio:
            async def __aenter__(self): return SimpleNamespace(models=Models())
            async def __aexit__(self,*args): return False
        engine=object.__new__(GeminiWriter)
        engine.client=SimpleNamespace(aio=Aio())
        engine.model='primary'; engine.fallback_model='fallback'; engine.last_model=None
        response=await engine._generate('facts',None)
        self.assertEqual(response.text,'ok')
        self.assertEqual(calls,['primary','fallback'])
        self.assertEqual(engine.last_model,'fallback')

    async def test_authentication_failure_does_not_fallback(self):
        from ai import GeminiWriter
        from google.genai import errors
        calls=[]
        class Models:
            async def generate_content(self,model,contents,config):
                calls.append(model)
                raise errors.ClientError(401,{'error':{'message':'bad key'}})
        class Aio:
            async def __aenter__(self): return SimpleNamespace(models=Models())
            async def __aexit__(self,*args): return False
        engine=object.__new__(GeminiWriter)
        engine.client=SimpleNamespace(aio=Aio())
        engine.model='primary'; engine.fallback_model='fallback'; engine.last_model=None
        with self.assertRaises(errors.ClientError): await engine._generate('facts',None)
        self.assertEqual(calls,['primary'])
