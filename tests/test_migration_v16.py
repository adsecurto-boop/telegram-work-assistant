"""Unit tests verifying Schema v16 and new operational hub services."""
import tempfile
import unittest
from pathlib import Path

from database import Database, SCHEMA_VERSION
from application.workflow_service import WorkflowService
from application.member_service import MemberService
from application.work_item_service import WorkItemService


class TestMigrationV16(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_v16.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_version_and_tables(self):
        self.assertEqual(SCHEMA_VERSION, 19)
        with self.db.connect() as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(ver, 19)
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            table_names = {r['name'] for r in rows}
            expected_new_tables = {
                'workflow_templates', 'workflow_stages', 'members', 'roles',
                'member_roles', 'work_item_meta', 'stage_history',
                'blockers_dependencies', 'requirements', 'test_conditions',
                'test_cases', 'test_executions', 'artifacts'
            }
            for tbl in expected_new_tables:
                self.assertIn(tbl, table_names, f"Table {tbl} must exist in schema v16")

    def test_default_seeded_templates_and_roles(self):
        roles = self.db.get_roles()
        role_names = {r['name'] for r in roles}
        self.assertIn('Lead Tester / QA', role_names)
        self.assertIn('Developer', role_names)
        self.assertIn('Support Engineer', role_names)

        templates = self.db.get_workflow_templates()
        self.assertGreaterEqual(len(templates), 2)
        req_tmpl = next((t for t in templates if t['work_type'] == 'requirement'), None)
        self.assertIsNotNone(req_tmpl)
        self.assertEqual(req_tmpl['name'], 'Standard Feature Delivery')
        self.assertGreaterEqual(len(req_tmpl['stages']), 7)

    def test_member_and_role_service(self):
        member_svc = MemberService(self.db)
        res = member_svc.add_member("Jane Doe", email="jane@example.com", roles=["Lead Tester / QA"])
        self.assertTrue(res.success)
        member_id = res.entity_id

        member = member_svc.get_member(member_id)
        self.assertEqual(member['name'], "Jane Doe")
        self.assertIn("Lead Tester / QA", member['roles'])

    def test_work_item_requirement_lifecycle_and_blockers(self):
        work_svc = WorkItemService(self.db)
        wf_svc = WorkflowService(self.db)

        # 1. Create requirement
        res = work_svc.create_requirement(
            title="User Profile Management",
            description="Allow users to edit profile and avatar",
            acceptance_criteria="Users can upload avatar and change display name",
            priority=2
        )
        self.assertTrue(res.success)
        req_id = res.entity_id

        req = work_svc.get_requirement(req_id)
        self.assertEqual(req['title'], "User Profile Management")
        self.assertEqual(req['current_stage_name'], "Requirement Received")
        self.assertEqual(req['operational_status'], "active")

        # 2. Add test condition
        c_res = work_svc.add_test_condition(
            requirement_id=req_id,
            title="Profile upload file size validation",
            category="validation",
            risk_level="high"
        )
        self.assertTrue(c_res.success)

        # 3. Add test case
        tc_res = work_svc.add_test_case(
            requirement_id=req_id,
            test_condition_id=c_res.entity_id,
            title="Verify 15MB avatar is rejected",
            expected_result="Validation error displayed"
        )
        self.assertTrue(tc_res.success)
        tc_id = tc_res.entity_id

        # 4. Record test execution
        exec_res = work_svc.record_test_execution(
            test_case_id=tc_id,
            result="pass",
            build="v1.2.0-rc1",
            actual_result="Error banner appeared as expected"
        )
        self.assertTrue(exec_res.success)

        # 5. Add blocker
        b_res = work_svc.add_blocker(
            entity_type="requirement",
            entity_id=req_id,
            dependency_type="blocked_by_dependency",
            description="Waiting for auth token backend patch"
        )
        self.assertTrue(b_res.success)

        # Verify operational status is now blocked
        req_after_block = work_svc.get_requirement(req_id)
        self.assertEqual(req_after_block['operational_status'], "blocked")

        # 6. Resolve blocker
        work_svc.resolve_blocker(b_res.entity_id, resolution_notes="Auth patch merged")
        req_after_resolve = work_svc.get_requirement(req_id)
        self.assertEqual(req_after_resolve['operational_status'], "active")

        # 7. Transition stage
        templates = wf_svc.list_templates()
        req_tmpl = next(t for t in templates if t['id'] == req['workflow_template_id'])
        stage_2 = req_tmpl['stages'][1] # Test Design & Preparation
        t_res = wf_svc.transition_stage("requirement", req_id, stage_2['id'], note="Analysis complete")
        self.assertTrue(t_res.success)

        req_updated = work_svc.get_requirement(req_id)
        self.assertEqual(req_updated['current_stage_name'], stage_2['name'])

        # 8. Check unified work items list
        all_items = work_svc.list_work_items()
        item = next((i for i in all_items if i['entity_type'] == 'requirement' and i['entity_id'] == req_id), None)
        self.assertIsNotNone(item)
        self.assertEqual(item['display_id'], f"REQ-{req_id}")

    def test_testing_lifecycle_defects_posture_and_artifacts(self):
        work_svc = WorkItemService(self.db)

        # 1. Create requirement
        r_res = work_svc.create_requirement(
            title="Payment Processing Integration",
            user_story="As a shopper, I want to checkout using Visa and Mastercard",
            acceptance_criteria="Returns 200 OK and generates receipt"
        )
        self.assertTrue(r_res.success)
        req_id = r_res.entity_id

        # 2. Propose test conditions (AI drafting)
        proposals = work_svc.propose_test_conditions(req_id)
        self.assertGreaterEqual(len(proposals), 3)

        # 3. Add & approve test condition
        cond_res = work_svc.add_test_condition(
            requirement_id=req_id,
            title=proposals[0]['title'],
            description=proposals[0]['description'],
            category=proposals[0]['category'],
            risk_level=proposals[0]['risk_level'],
            status='approved'
        )
        self.assertTrue(cond_res.success)
        cond_id = cond_res.entity_id

        # 4. Add test case linked to condition
        tc_res = work_svc.add_test_case(
            requirement_id=req_id,
            test_condition_id=cond_id,
            title="Verify 3DS transaction authorization",
            expected_result="200 OK and receipt"
        )
        self.assertTrue(tc_res.success)
        tc_id = tc_res.entity_id

        # 5. Execute test with failure
        ex_res = work_svc.record_test_execution(
            test_case_id=tc_id,
            result="fail",
            build="v1.0.0-rc1",
            actual_result="500 Gateway Timeout"
        )
        self.assertTrue(ex_res.success)

        # 6. Create defect linked to execution
        def_res = work_svc.create_defect(
            title="500 Timeout on 3DS authorization",
            severity="critical",
            priority=1,
            requirement_id=req_id,
            test_case_id=tc_id,
            execution_id=ex_res.entity_id,
            build_found="v1.0.0-rc1"
        )
        self.assertTrue(def_res.success)
        defect_id = def_res.entity_id

        # 7. Check posture (should be Blocked due to critical defect)
        posture = work_svc.get_testing_posture(req_id)
        self.assertEqual(posture['readiness'], 'Blocked')
        self.assertEqual(posture['critical_defects'], 1)
        self.assertEqual(posture['failed'], 1)

        # 8. Retest defect with pass (closes defect)
        retest_res = work_svc.retest_defect(
            defect_id=defect_id,
            result="pass",
            build_fixed="v1.0.0-rc2",
            retest_notes="Fixed timeout setting in payment gateway proxy"
        )
        self.assertTrue(retest_res.success)

        defect = work_svc.get_defect(defect_id)
        self.assertEqual(defect['status'], 'verified')

        # 9. Record passing execution on test case
        work_svc.record_test_execution(
            test_case_id=tc_id,
            result="pass",
            build="v1.0.0-rc2",
            actual_result="Authorized in 320ms"
        )

        # Posture should now be Ready for Release
        posture_ready = work_svc.get_testing_posture(req_id)
        self.assertEqual(posture_ready['readiness'], 'Ready for Release')
        self.assertEqual(posture_ready['passed'], 1)
        self.assertEqual(posture_ready['open_defects'], 0)

        # 10. Link Google Workspace Artifact
        art_res = work_svc.add_artifact(
            entity_type="requirement",
            entity_id=req_id,
            artifact_type="doc",
            name="Payment QA Test Plan",
            url="https://docs.google.com/document/d/xyz"
        )
        self.assertTrue(art_res.success)
        art_id = art_res.entity_id

        artifacts = work_svc.list_artifacts("requirement", req_id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]['name'], "Payment QA Test Plan")

        # 11. Timeline should contain aggregated events
        timeline = work_svc.get_timeline("requirement", req_id)
        self.assertGreaterEqual(len(timeline), 3)

        # 12. Completion Report
        report = work_svc.generate_completion_report(req_id)
        self.assertEqual(report['requirement']['id'], req_id)
        self.assertEqual(report['posture']['readiness'], 'Ready for Release')
        self.assertEqual(len(report['artifacts']), 1)


if __name__ == '__main__':
    unittest.main()
