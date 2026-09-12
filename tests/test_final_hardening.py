import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database import Database, SCHEMA_VERSION
from assistant_orchestrator import AssistantOrchestrator, ExternalFactContinuationPlanner
from agent_orchestrator import AgentAuthorization
from nlp import ConversationPlan, NLIntent, NLEntities, PlannedAction
from mcp_manager import MAX_TOOL_DISCOVERY_PAGES, MCPManager, MCPServerConnection, ToolResult
from mcp_registry import (RiskLevel, ToolDescriptor, ToolRegistry, effective_risk_for_call,
                          potential_capabilities_for_tool, required_capabilities_for_call)


class _PagedClient:
    def __init__(self, pages):
        self.pages = pages
        self.cursors = []

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        return self.pages[cursor]


def _page(names, next_cursor=None):
    tools = [SimpleNamespace(name=name, description=name, input_schema={}, annotations=None)
             for name in names]
    return SimpleNamespace(tools=tools, next_cursor=next_cursor)


class MCPDiscoveryHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_discovery_pages_are_drained_in_order(self):
        server = MCPServerConnection("paged", {"enabled": True})
        server.client = _PagedClient({
            None: _page(["a", "b"], "two"),
            "two": _page(["c", "d"], "three"),
            "three": _page(["e"]),
        })
        self.assertEqual([tool.name for tool in await server.list_tools()], ["a", "b", "c", "d", "e"])
        self.assertEqual(server.client.cursors, [None, "two", "three"])

    async def test_cursor_cycle_degrades_without_partial_registry(self):
        manager = MCPManager()
        server = MCPServerConnection("paged", {"enabled": True})
        server.status = "ready"
        server.client = _PagedClient({None: _page(["a"], "loop"), "loop": _page(["b"], "loop")})
        manager.servers["paged"] = server
        await manager.discover_server_tools("paged")
        self.assertEqual(server.status, "degraded")
        self.assertIn("cursor cycle", server.last_error)
        self.assertEqual(manager.registry.list_tools("paged"), [])

    async def test_page_cap_degrades_without_pretending_complete(self):
        pages = {None: _page(["first"], "0")}
        for index in range(MAX_TOOL_DISCOVERY_PAGES):
            pages[str(index)] = _page([f"t{index}"], str(index + 1))
        manager = MCPManager()
        server = MCPServerConnection("paged", {"enabled": True})
        server.status = "ready"
        server.client = _PagedClient(pages)
        manager.servers["paged"] = server
        await manager.discover_server_tools("paged")
        self.assertEqual(server.status, "degraded")
        self.assertIn("exceeded", server.last_error)
        self.assertEqual(manager.get_health_status()["paged"]["tool_count"], 0)

    def test_annotations_require_operator_flag_and_exact_endpoint(self):
        exact = MCPServerConnection("anything", {
            "transport": "streamable_http", "url": "https://official.invalid/mcp",
            "trusted_endpoint": "https://official.invalid/mcp", "trust_tool_annotations": True})
        renamed = MCPServerConnection("github", {
            "transport": "streamable_http", "url": "http://localhost/mcp",
            "trusted_endpoint": "https://official.invalid/mcp", "trust_tool_annotations": True})
        self.assertTrue(MCPManager._annotations_are_trusted(exact))
        self.assertFalse(MCPManager._annotations_are_trusted(renamed))

    async def test_rediscovery_replaces_stale_tools_without_duplicates(self):
        manager = MCPManager()
        server = MCPServerConnection("github", {"enabled": True, "read_only_tools": ["get_issue"]})
        server.status = "ready"
        server.client = _PagedClient({None: _page(["old_tool", "get_issue"])})
        manager.servers["github"] = server
        await manager.discover_server_tools("github")
        server.client = _PagedClient({None: _page(["get_issue", "new_tool"])})
        await manager.discover_server_tools("github")
        self.assertEqual([t.original_name for t in manager.registry.list_tools("github")],
                         ["get_issue", "new_tool"])


class AuthorizationAndCapabilityTests(unittest.TestCase):
    def test_read_questions_never_grant_write_authority(self):
        for text in ("Is issue #61 closed?", "What labels does it have?",
                     "Who assigned it?", "Was it merged?"):
            with self.subTest(text=text):
                auth = AgentAuthorization.from_user_message(text)
                self.assertFalse(auth.external_write_allowed)
                self.assertEqual(auth.requested_capabilities, frozenset())

    def test_explicit_writes_are_capability_scoped(self):
        expected = {
            "Close issue #61": "github.issue.update",
            "Add the bug label": "github.issue.label",
            "Merge PR #8": "github.pr.merge",
        }
        for text, capability in expected.items():
            with self.subTest(text=text):
                auth = AgentAuthorization.from_user_message(text)
                self.assertTrue(auth.external_write_allowed)
                self.assertIn(capability, auth.requested_capabilities)

    def test_negation_wins_globally(self):
        for text in ("Don't close it; just tell me whether it is closed.",
                     "Do not add a label, only show the labels.",
                     "Never merge the PR; check its status."):
            with self.subTest(text=text):
                self.assertFalse(AgentAuthorization.from_user_message(text).external_write_allowed)

    def test_clause_level_negation_preserves_an_independent_authorized_action(self):
        auth = AgentAuthorization.from_user_message("Don't close issue #61, but add the bug label.")
        self.assertEqual(auth.requested_capabilities, frozenset({"github.issue.label"}))
        comment = AgentAuthorization.from_user_message(
            "Don't merge the PR. Just add a comment saying the retest passed.")
        self.assertEqual(comment.requested_capabilities, frozenset({"github.issue.comment"}))

    def test_official_issue_write_uses_required_call_capabilities(self):
        tool = ToolDescriptor("github.issue_write", "mcp__github__issue_write", "github", "issue_write",
                              "Create or update GitHub issue", {}, RiskLevel.EXTERNAL_WRITE)
        self.assertTrue({"github.issue.create", "github.issue.update", "github.issue.label", "github.issue.assign"}
                        <= potential_capabilities_for_tool(tool))
        label_only = {"method": "update", "owner": "owner", "repo": "repo", "issue_number": 61,
                      "labels": ["bug"]}
        self.assertEqual(required_capabilities_for_call(tool, label_only),
                         frozenset({"github.issue.label"}))
        label_auth = AgentAuthorization.from_user_message("Add the bug label to issue #61.")
        self.assertTrue(label_auth.allows(tool, label_only))
        combined = {**label_only, "state": "closed", "assignees": ["someone"]}
        self.assertEqual(required_capabilities_for_call(tool, combined), frozenset({
            "github.issue.update", "github.issue.label", "github.issue.assign"}))
        self.assertFalse(label_auth.allows(tool, combined))

    def test_label_write_is_repository_crud_and_delete_is_destructive(self):
        tool = ToolDescriptor("github.label_write", "mcp__github__label_write", "github", "label_write",
                              "Repository label CRUD", {}, RiskLevel.EXTERNAL_WRITE)
        delete_args = {"method": "delete", "owner": "owner", "repo": "repo", "name": "bug"}
        self.assertEqual(required_capabilities_for_call(tool, delete_args),
                         frozenset({"github.label.delete"}))
        self.assertEqual(effective_risk_for_call(tool, delete_args), RiskLevel.DESTRUCTIVE)
        self.assertFalse(AgentAuthorization.from_user_message("Update the bug label description.").allows(tool, delete_args))

    def test_capability_index_is_cached_and_invalidated(self):
        registry = ToolRegistry()
        registry.register_tool("github", "get_issue", "Get an issue", {}, RiskLevel.READ_ONLY)
        self.assertTrue(registry.supports("github.read"))
        registry.clear_server_tools("github")
        self.assertFalse(registry.supports("github.read"))
        registry.register_tool("mail", "send_email", "Send email", {}, RiskLevel.EXTERNAL_WRITE)
        self.assertTrue(registry.supports("gmail.message.send"))


class FTSV15HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "assistant.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_database_is_v15_and_integrity_is_ok(self):
        Database(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v14_upgrade_backs_up_and_repairs_case_event_and_summary(self):
        db = Database(self.path)
        case_id = db.create_case("quasarcase_unique", client="Acme", product="GBB")
        db.add_case_event(case_id, "finding", "nebulaevent_unique")
        db.save_memory_summary(1, "rolling", "orbitsummary_unique")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM fts_work_memory")
            connection.execute("PRAGMA user_version=14")
            connection.commit()
        migrated = Database(self.path)
        self.assertEqual(migrated.search_historical_memory("quasarcase_unique")[0]["source_type"], "case")
        self.assertEqual(migrated.search_historical_memory("nebulaevent_unique")[0]["source_type"], "case_event")
        self.assertEqual(migrated.search_historical_memory("orbitsummary_unique")[0]["source_type"], "conversation_summary")
        self.assertTrue(list((self.path.parent / "backups").glob("pre-migration-v14-*.sqlite3")))

    def test_rebuild_is_idempotent_and_task_update_removes_obsolete_text(self):
        db = Database(self.path)
        task = db.add_task("obsoletealpha")
        db.rebuild_work_memory_index()
        db.rebuild_work_memory_index()
        with db.connect() as connection:
            count = connection.execute("SELECT count(*) FROM fts_work_memory WHERE source_type='task' AND source_id=?",
                                       (str(task.id),)).fetchone()[0]
        self.assertEqual(count, 1)
        db.update_task(task.id, "title", "currentbeta")
        self.assertEqual(db.search_historical_memory("obsoletealpha"), [])
        self.assertEqual(db.search_historical_memory("currentbeta")[0]["source_id"], str(task.id))

    def test_all_five_source_types_are_singleton_and_updates_replace_text(self):
        db = Database(self.path)
        task = db.add_task("task_oldtoken")
        case_id = db.create_case("case_oldtoken", detail="event_uniquetoken")
        test_id = db.add_test_session("test_oldtoken")
        summary_id = db.save_memory_summary(1, "rolling", "summary_oldtoken")
        db.update_task(task.id, "title", "task_newtoken")
        db.update_case(case_id, "title", "case_newtoken", detail="case renamed")
        db.update_test_session(test_id, "scenario", "test_newtoken")
        db.update_memory_summary(summary_id, "summary_newtoken")
        for old in ("task_oldtoken", "case_oldtoken", "test_oldtoken", "summary_oldtoken"):
            self.assertEqual(db.search_historical_memory(old), [])
        db.rebuild_work_memory_index()
        with db.connect() as connection:
            counts = connection.execute('''SELECT source_type, source_id, count(*) AS n
                FROM fts_work_memory GROUP BY source_type, source_id''').fetchall()
        self.assertTrue(counts)
        self.assertTrue(all(row["n"] == 1 for row in counts))
        self.assertEqual({row["source_type"] for row in counts},
                         {"task", "case", "case_event", "test_session", "conversation_summary"})

    def test_migration_failure_rolls_back_and_keeps_backup(self):
        Database(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=14")
            connection.commit()
        with patch.object(Database, "_seed_v15_defaults", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                Database(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue(list((self.path.parent / "backups").glob("pre-migration-v14-*.sqlite3")))


class ExternalContinuationHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_planner_is_invoked_and_validated(self):
        class SemanticPlanner:
            def __init__(self):
                self.calls = []

            async def interpret_external_continuation(self, text, facts, context, allowed):
                self.calls.append((text, facts, context, allowed))
                return ConversationPlan(actions=[PlannedAction(
                    intent=NLIntent.CREATE_TASK,
                    entities=NLEntities(task_title="Verified retest"))])

        semantic = SemanticPlanner()
        plan = await ExternalFactContinuationPlanner(semantic).plan(
            "Create a local retest task", [{"success": True, "data": {"state": "open"}}],
            {"active_case": {"id": 3}}, 3)
        self.assertEqual(plan.actions[0].intent, NLIntent.CREATE_TASK)
        self.assertEqual(len(semantic.calls), 1)

    async def test_semantic_planner_rejects_non_allowlisted_intent(self):
        class UnsafePlanner:
            async def interpret_external_continuation(self, *_args):
                return ConversationPlan(actions=[PlannedAction(
                    intent=NLIntent.SET_SHIFT, entities=NLEntities(
                        shift_start="10:00", shift_end="19:00"))])

        with self.assertRaises(ValueError):
            await ExternalFactContinuationPlanner(UnsafePlanner()).plan(
                "Change my shift", [{"success": True, "data": {"state": "open"}}], {}, 1)

    def test_operational_audits_exclude_payloads_and_secrets(self):
        temp = tempfile.TemporaryDirectory()
        try:
            db = Database(Path(temp.name) / "audit.sqlite3")
            db.record_agent_run_audit("agent_1", 77, {
                "route": "gemini_mcp_agent", "authorization_scope": ["github.read"],
                "tool_ids": ["github.get_issue"], "tool_count": 1,
                "final_status": "success", "duration_ms": 12.3,
                "error_reference": None, "raw_api_key": "SECRET",
            })
            row = db.get_audit_log(1)[0]
            self.assertEqual(row["operation_type"], "external_agent_run")
            self.assertNotIn("SECRET", row["after_state_json"])
            self.assertNotIn("raw_api_key", row["after_state_json"])
        finally:
            temp.cleanup()

    def test_structured_external_reference_is_canonical_and_fresh(self):
        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        result = ToolResult("github.get_issue", "mcp__github__get_issue", True, data={
            "number": 61, "repository": "owner/repo", "state": "open",
            "html_url": "https://github.com/owner/repo/issues/61", "title": "Wayland",
            "user": {"login": "alice"}, "labels": [{"name": "bug"}],
        })
        ref = orchestrator._external_refs_from_results({}, [result], "")["github_issue"]
        self.assertEqual(ref["canonical_id"], "github:owner/repo:issue:61")
        self.assertEqual(ref["creator"], "alice")
        self.assertEqual(ref["labels"], ["bug"])
        self.assertTrue(datetime.fromisoformat(ref["fetched_at"]).tzinfo)

    async def test_verified_state_can_only_produce_allowlisted_local_actions(self):
        planner = ExternalFactContinuationPlanner()
        facts = [{"success": True, "data": {"number": 61, "state": "open"}}]
        plan = await planner.plan(
            "Add its current state to the active case and create a retest task if it is still open.",
            facts, {}, 14)
        self.assertEqual([action.intent for action in plan.actions],
                         [NLIntent.ADD_CASE_EVENT, NLIntent.CREATE_TASK])
        self.assertTrue(all(action.intent in planner.ALLOWED_INTENTS for action in plan.actions))

    async def test_missing_case_clarifies_instead_of_selecting_arbitrary_case(self):
        plan = await ExternalFactContinuationPlanner().plan(
            "Add its current state to the active case.",
            [{"success": True, "data": {"state": "open"}}], {}, None)
        self.assertEqual(plan.actions, [])
        self.assertIn("don't know which local case", plan.clarification_question)

    async def test_tomorrow_uses_configured_local_calendar_date(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = datetime(2026, 9, 12, 19, 0, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        with patch("domain.datetime", FrozenDateTime), patch("config.TIMEZONE", "Asia/Kolkata"):
            plan = await ExternalFactContinuationPlanner().plan(
                "Create a retest task tomorrow if it is still open.",
                [{"success": True, "data": {"state": "open"}}], {}, None)
        self.assertEqual(plan.actions[0].entities.date, "2026-09-14")

if __name__ == "__main__":
    unittest.main()
