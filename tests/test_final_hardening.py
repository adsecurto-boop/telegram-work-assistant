import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database import Database, SCHEMA_VERSION
from assistant_orchestrator import ExternalFactContinuationPlanner
from nlp import NLIntent
from mcp_manager import MAX_TOOL_DISCOVERY_PAGES, MCPManager, MCPServerConnection
from mcp_registry import RiskLevel


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
