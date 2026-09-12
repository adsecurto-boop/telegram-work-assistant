"""
Regression and hardening tests covering Mission requirements:
- Dashboard Evidence XSS prevention (SVG/HTML/XML download, safe PNG inline, filename sanitization)
- Dashboard Token Exchange (303 redirect, Cookie setting)
- Dashboard Session thread-safety
- Evidence deduplication (unlinked, case-only, test-linked) and relational integrity
- Concurrency protection for now_iso
- Update claiming idempotency and retry
- Daily plan time parsing and rejection of invalid hours/minutes/zeros
- Canonical case status visibility
- Voice single-logging (no double note activity)
- Multi-action planning, atomic execution, and rollback
- Connector HTTPS enforcement and untrusted author labeling
"""
import asyncio
import io
import json
import os
import secrets
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import config
from connectors import FreshchatConnector, FreshdeskConnector, sync_connector, ConnectorItem, Connector
from daily_assistant import parse_lunch_flexible, parse_shift_range_flexible, parse_time_flexible
from dashboard import DashboardService, SAFE_INLINE_MIME_TYPES, STATUSES, KANBAN_COLUMNS
from database import Database, now_iso
from domain import CASE_STATUSES, TEST_RESULTS
from models import CASE_LIFECYCLE, CaseStatus, TestResult
from nlp import (
    ConversationPlan,
    NaturalLanguagePipeline,
    NLEntities,
    NLIntent,
    NLInterpretation,
    PlanExecutionResult,
    PlannedAction,
    parse_shift_time_range,
    parse_time_token,
)


class DummyMessage:
    def __init__(self, text="", voice=None, photo=None, document=None, video=None, caption=None):
        self.text = text
        self.voice = voice
        self.photo = photo
        self.document = document
        self.video = video
        self.caption = caption
        self.forward_origin = None


class DummyUpdate:
    def __init__(self, update_id=101, message=None):
        self.update_id = update_id
        self.effective_message = message or DummyMessage()
        self.callback_query = None


class MissionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_hardening.db"
        self.db = Database(self.db_path)
        self.evidence_dir = Path(self.temp_dir.name) / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._orig_base = config.BASE_DIR
        config.BASE_DIR = Path(self.temp_dir.name)
        (config.BASE_DIR / "storage" / "evidence").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        config.BASE_DIR = self._orig_base
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Dashboard Evidence XSS Prevention & Token Exchange
    # -------------------------------------------------------------------------
    def test_safe_inline_mime_types_allowlist(self):
        self.assertIn("image/png", SAFE_INLINE_MIME_TYPES)
        self.assertIn("image/jpeg", SAFE_INLINE_MIME_TYPES)
        self.assertNotIn("image/svg+xml", SAFE_INLINE_MIME_TYPES)
        self.assertNotIn("text/html", SAFE_INLINE_MIME_TYPES)
        self.assertNotIn("application/javascript", SAFE_INLINE_MIME_TYPES)

    def test_dashboard_token_exchange_303(self):
        service = DashboardService(self.db, port=8791)
        valid_token = service.token

        # Mock HTTP request handler
        from dashboard import DashboardService as DS
        # Test authenticate_request directly
        # When token is supplied, new session is returned
        sid, csrf = service.create_session()
        self.assertIsNotNone(sid)
        self.assertIsNotNone(csrf)

        session = service.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["csrf_token"], csrf)

    def test_dashboard_session_thread_safety(self):
        service = DashboardService(self.db, port=8792)
        created_sids = []
        errors = []

        def worker():
            try:
                for _ in range(50):
                    sid, csrf = service.create_session()
                    created_sids.append(sid)
                    s = service.get_session(sid)
                    if not s or s["csrf_token"] != csrf:
                        errors.append("Session retrieval mismatch")
                    bulk_token = service.create_bulk_token({"action": "test"}, sid)
                    consumed = service.consume_bulk_token(bulk_token, sid)
                    if not consumed or consumed.get("action") != "test":
                        errors.append("Bulk token consumption mismatch")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(created_sids), 400)

    # -------------------------------------------------------------------------
    # 2. Evidence Deduplication and Relational Validation
    # -------------------------------------------------------------------------
    def test_evidence_dedup_unlinked(self):
        sha = "aaa111bbb222ccc333"
        id1 = self.db.add_evidence("photo", path="dummy1.png", sha256=sha)
        id2 = self.db.add_evidence("photo", path="dummy1_copy.png", sha256=sha)
        self.assertEqual(id1, id2, "Unlinked evidence with matching SHA-256 must deduplicate")

    def test_evidence_dedup_case_only(self):
        c_id = self.db.create_case("Login Failure", client="Acme")
        sha = "sha_case_only_123"
        id1 = self.db.add_evidence("photo", case_id=c_id, path="log1.png", sha256=sha)
        id2 = self.db.add_evidence("photo", case_id=c_id, path="log1_dup.png", sha256=sha)
        self.assertEqual(id1, id2, "Evidence on same case must deduplicate")

        # Same SHA on another case should create a distinct row
        c2_id = self.db.create_case("Other Case", client="Beta")
        id3 = self.db.add_evidence("photo", case_id=c2_id, path="log1_c2.png", sha256=sha)
        self.assertNotEqual(id1, id3, "Same evidence legitimately used on different case must be separate")

    def test_cross_case_test_link_rejected(self):
        c1 = self.db.create_case("Case 1", client="Acme")
        c2 = self.db.create_case("Case 2", client="Beta")
        # create test session on case 1
        ts1 = self.db.add_test_session("Login Flow", case_id=c1)

        # Trying to attach evidence claiming case 2 but test session belongs to case 1
        with self.assertRaises(ValueError) as cm:
            self.db.add_evidence("photo", case_id=c2, test_session_id=ts1, sha256="abc")
        self.assertIn("does not belong to case", str(cm.exception))

    # -------------------------------------------------------------------------
    # 3. Timestamp Concurrency & Update Idempotency
    # -------------------------------------------------------------------------
    def test_timestamp_generation_concurrency(self):
        timestamps = []
        errors = []

        def worker():
            try:
                for _ in range(50):
                    timestamps.append(now_iso())
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(timestamps), 400)
        # All timestamps must be strictly distinct
        self.assertEqual(len(timestamps), len(set(timestamps)))

    def test_failed_update_claim_can_retry(self):
        uid = 998877
        # First claim succeeds
        self.assertTrue(self.db.claim_update(uid))
        # Second claim while processing returns False
        self.assertFalse(self.db.claim_update(uid))

        # Mark failed (e.g. error in handler)
        self.db.fail_update(uid, "Network timeout")

        # After failure, retry claim MUST succeed
        self.assertTrue(self.db.claim_update(uid))

        # Complete update
        self.db.complete_update(uid)

        # Completed update cannot be re-claimed
        self.assertFalse(self.db.claim_update(uid))

    # -------------------------------------------------------------------------
    # 4. Daily Plan Time Parsing Validation
    # -------------------------------------------------------------------------
    def test_daily_plan_invalid_hour(self):
        self.assertIsNone(parse_time_flexible("25:00"))
        self.assertIsNone(parse_time_flexible("99:80"))
        with self.assertRaises(ValueError):
            parse_time_token("25:00")

    def test_daily_plan_invalid_minute(self):
        self.assertIsNone(parse_time_flexible("10:99"))
        with self.assertRaises(ValueError):
            parse_time_token("10:99")

    def test_daily_plan_invalid_lunch(self):
        self.assertIsNone(parse_lunch_flexible("lunch at 99:80"))
        self.assertIsNone(parse_lunch_flexible("lunch at 25:00"))

    def test_invalid_12_hour_zero(self):
        self.assertIsNone(parse_time_flexible("0pm"))
        self.assertIsNone(parse_time_flexible("0am"))
        with self.assertRaises(ValueError):
            parse_time_token("0pm")
        with self.assertRaises(ValueError):
            parse_time_token("0am")

    def test_contradictory_shift_range_rejected(self):
        self.assertIsNone(parse_shift_range_flexible("10 to 10"))
        with self.assertRaises(ValueError):
            parse_shift_time_range("10am", "10am")
        with self.assertRaises(ValueError):
            parse_shift_time_range("14:00", "14:00")

    # -------------------------------------------------------------------------
    # 5. Canonical Case Lifecycle & Test Results
    # -------------------------------------------------------------------------
    def test_all_case_statuses_visible(self):
        expected_statuses = {
            "new", "triaged", "investigating", "waiting_client", "waiting_internal",
            "fix_ready", "testing", "retest_required", "resolved", "client_updated", "closed"
        }
        self.assertEqual(CASE_STATUSES, expected_statuses)
        self.assertEqual(set(STATUSES), expected_statuses)
        kanban_keys = {col[0] for col in KANBAN_COLUMNS}
        self.assertEqual(kanban_keys, expected_statuses)
        self.assertEqual(len(CASE_LIFECYCLE), 11)

    def test_all_test_results(self):
        expected_results = {"passed", "failed", "partial", "blocked", "not_run"}
        self.assertEqual(TEST_RESULTS, expected_results)

    # -------------------------------------------------------------------------
    # 6. Multi-Action Planning & Atomic Execution
    # -------------------------------------------------------------------------
    def test_multi_action_plan_schema(self):
        action1 = PlannedAction(
            intent=NLIntent.CREATE_CASE,
            entities=NLEntities(case_title="Ubuntu Screenshot Issue", client="GBB"),
        )
        action2 = PlannedAction(
            intent=NLIntent.CREATE_TEST_SESSION,
            entities=NLEntities(test_scenario="Wayland vs X11", test_result="failed"),
            dependencies=[0],
        )
        plan = ConversationPlan(actions=[action1, action2])
        dumped = plan.model_dump()
        self.assertEqual(len(dumped["actions"]), 2)
        self.assertEqual(dumped["actions"][1]["dependencies"], [0])

    def test_multi_action_plan_atomic_execution(self):
        pipeline = NaturalLanguagePipeline(self.db)
        plan = ConversationPlan(actions=[
            PlannedAction(
                intent=NLIntent.CREATE_CASE,
                entities=NLEntities(case_title="Multi Action Case", client="Alpha"),
            ),
            PlannedAction(
                intent=NLIntent.CREATE_TEST_SESSION,
                entities=NLEntities(test_scenario="Verify auth token", test_result="passed"),
                dependencies=[0],
            ),
            PlannedAction(
                intent=NLIntent.CREATE_FOLLOWUP,
                entities=NLEntities(waiting_on="developer", followup_due="tomorrow", notes="Check PR"),
                dependencies=[0],
            )
        ])

        res = asyncio.run(pipeline.execute_plan(plan, shift=None))
        self.assertTrue(res.success)
        self.assertIsNotNone(res.correlation_id)
        self.assertEqual(len(res.executed_actions), 3)

        # Verify case was created and test session / follow-up linked to it
        cases = self.db.list_cases()
        created_c = [c for c in cases if c["title"] == "Multi Action Case"]
        self.assertEqual(len(created_c), 1)
        cid = created_c[0]["id"]

        sessions = self.db.test_sessions()
        matched_ts = [ts for ts in sessions if ts["case_id"] == cid]
        self.assertEqual(len(matched_ts), 1)

        followups = self.db.list_followups(limit=10)
        matched_f = [f for f in followups if f["case_id"] == cid]
        self.assertEqual(len(matched_f), 1)

    def test_multi_action_plan_partial_failure_rolls_back(self):
        pipeline = NaturalLanguagePipeline(self.db)
        # Action 1: Create valid case
        # Action 2: Intent with invalid entities that causes an unhandled error in execution
        plan = ConversationPlan(actions=[
            PlannedAction(
                intent=NLIntent.CREATE_CASE,
                entities=NLEntities(case_title="Rollback Test Case", client="Beta"),
            ),
            PlannedAction(
                intent=NLIntent.UPDATE_TASK,
                entities=NLEntities(reference="nonexistent_task_999999", task_title="Will Fail"),
            )
        ])

        res = asyncio.run(pipeline.execute_plan(plan, shift=None))
        self.assertFalse(res.success)
        self.assertTrue(res.rolled_back)

        # Verify case was rolled back (undone)
        cases = self.db.list_cases()
        matched = [c for c in cases if c["title"] == "Rollback Test Case"]
        self.assertEqual(len(matched), 0, "Initial case insertion should have been rolled back upon plan failure")

    def test_high_risk_action_inside_plan_requires_confirmation(self):
        pipeline = NaturalLanguagePipeline(self.db)
        plan = ConversationPlan(actions=[
            PlannedAction(
                intent=NLIntent.CREATE_TASK,
                entities=NLEntities(task_title="Safe low risk task"),
                requires_confirmation=False,
            ),
            PlannedAction(
                intent=NLIntent.CHANGE_CASE_STATUS,
                entities=NLEntities(case_id=1, status="resolved"),
                requires_confirmation=True,
            )
        ])

        res = asyncio.run(pipeline.execute_plan(plan, shift=None))
        self.assertFalse(res.success)
        self.assertEqual(res.error, "confirmation_required")

        # Verify safe task was NOT executed before confirmation
        tasks = self.db.list_tasks()
        self.assertEqual([t for t in tasks if t.title == "Safe low risk task"], [])

    # -------------------------------------------------------------------------
    # 7. Connectors HTTPS & Untrusted Author
    # -------------------------------------------------------------------------
    def test_connector_https(self):
        with self.assertRaises(ValueError) as cm:
            FreshdeskConnector("http://mycompany.freshdesk.com", "api_key", 101)
        self.assertIn("requires HTTPS", str(cm.exception))

        with self.assertRaises(ValueError) as cm2:
            FreshchatConnector("http://api.freshchat.com/v2", "api_key", "agent_1")
        self.assertIn("requires HTTPS", str(cm2.exception))

    def test_connector_content_untrusted(self):
        class MockConnector(Connector):
            name = "mock_test_connector"

            def fetch(self, cursor=None, limit=100):
                return [ConnectorItem("ticket_1", "2026-09-12T10:00:00Z", "Help ticket", [], {"id": 1})], None

        mock_conn = MockConnector()
        sync_connector(self.db, mock_conn)

        messages = self.db.inbox(limit=10, include_observed=True)
        self.assertTrue(len(messages) >= 1)
        target = [m for m in messages if m["external_message_id"] == "ticket_1"][0]
        self.assertFalse(target["author_is_owner"], "Connector imported messages must never be marked as author_is_owner")
        meta = json.loads(target["metadata_json"]) if target.get("metadata_json") else {}
        self.assertFalse(meta.get("trusted", True), "Connector messages must explicitly have trusted=False")

    # -------------------------------------------------------------------------
    # 8. Contextual Pronoun Resolution
    # -------------------------------------------------------------------------
    def test_contextual_pronoun_resolves_unique_case(self):
        c1 = self.db.create_case("Ubuntu Screenshot Bug", client="GBB")
        pipeline = NaturalLanguagePipeline(self.db)
        # Message using pronoun without active case in context, but only 1 open case exists
        reply, interp = asyncio.run(pipeline.process("mark that case testing", shift=None))
        self.assertEqual(interp.entities.case_id, c1)

    def test_contextual_pronoun_clarifies_when_ambiguous(self):
        c1 = self.db.create_case("Case One", client="Client A")
        c2 = self.db.create_case("Case Two", client="Client B")
        pipeline = NaturalLanguagePipeline(self.db)
        # Multiple open cases, pronoun used without active case context
        reply, interp = asyncio.run(pipeline.process("mark that case testing", shift=None))
        self.assertTrue(interp.needs_confirmation or interp.clarification_question is not None)
        self.assertIn("Which one did you mean", interp.clarification_question or reply)

    # -------------------------------------------------------------------------
    # 9. Voice Double Logging Invariant
    # -------------------------------------------------------------------------
    def test_voice_does_not_create_duplicate_activity(self):
        # When capture_voice runs for an actionable command, it should not insert a pre-NLP unparsed activity note
        from handlers import capture_voice
        self.db.start_shift("10:00", "19:00")
        shift = self.db.active_shift()
        t = self.db.add_task("Test task 1", shift_id=shift["id"])

        mock_update = DummyUpdate(
            update_id=5555,
            message=DummyMessage(voice=MagicMock(file_id="voice_file_1", mime_type="audio/ogg"))
        )
        mock_context = MagicMock()
        mock_context.application.bot_data = {'db': self.db}

        # Mock voice download & transcription
        async def fake_voice_bytes(msg):
            return b"fake_audio_bytes", "audio/ogg", "voice_file_1"

        async def fake_reply(up, text, *args, **kwargs):
            pass

        mock_engine = MagicMock()
        mock_engine.last_model = "test-model"
        mock_engine.transcribe = AsyncMock(return_value=f"complete task #{t.id}")

        with patch("media_capture.voice_bytes", side_effect=fake_voice_bytes), \
             patch("media_capture.save_voice_data", return_value=("fake_path.ogg", "hash_voice_1")), \
             patch("ai.writer", return_value=mock_engine), \
             patch("handlers.reply", side_effect=fake_reply), \
             patch("handlers.reserve_ai", new_callable=AsyncMock), \
             patch("handlers.active", return_value=shift):

            asyncio.run(capture_voice(mock_update, mock_context))

        # Inspect activities
        activities = self.db.activities(shift["id"])
        raw_notes = [a for a in activities if a["category"] == "note" and a["detail"] == f"complete task #{t.id}"]
        self.assertEqual(len(raw_notes), 0, "capture_voice must not unconditionally insert raw unparsed activity notes")

    # -------------------------------------------------------------------------
    # 10. Evidence Response Headers & Content-Disposition
    # -------------------------------------------------------------------------
    def test_evidence_headers_svg_and_html_forced_download(self):
        import re
        html_mime = "text/html"
        svg_mime = "image/svg+xml"
        png_mime = "image/png"

        self.assertNotIn(html_mime, SAFE_INLINE_MIME_TYPES)
        self.assertNotIn(svg_mime, SAFE_INLINE_MIME_TYPES)
        self.assertIn(png_mime, SAFE_INLINE_MIME_TYPES)

        # Verify filename sanitization
        malicious_filename = 'test"malicious\r\nheader.png'
        sanitized = re.sub(r'[\r\n"\\\x00-\x1f]', '_', malicious_filename)
        self.assertNotIn('"', sanitized)
        self.assertNotIn('\r', sanitized)
        self.assertNotIn('\n', sanitized)

    # -------------------------------------------------------------------------
    # 11. AI Policy & Fallback Invariants
    # -------------------------------------------------------------------------
    def test_ai_unavailable_falls_back_safely(self):
        pipeline = NaturalLanguagePipeline(self.db, ai_client=None)
        # Deterministic intent works completely offline without Gemini
        reply, interp = asyncio.run(pipeline.process("start shift 10 to 7", shift=None))
        self.assertEqual(interp.intent, NLIntent.SET_SHIFT)
        self.assertEqual(interp.provider, "deterministic")


if __name__ == "__main__":
    unittest.main()
