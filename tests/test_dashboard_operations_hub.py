"""Unit tests verifying Dashboard routes and handlers for Tester + Support Work Operations Hub."""
import http.client
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from database import Database
from dashboard import DashboardService
from application.work_item_service import WorkItemService
from application.member_service import MemberService
from application.workflow_service import WorkflowService


class TestDashboardOperationsHub(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_hub_dash.db"
        self.db = Database(self.db_path)
        self.service = DashboardService(self.db, host="127.0.0.1", port=0)
        self.service.start()
        time.sleep(0.1)
        self.port = self.service.server.server_address[1]

    def tearDown(self):
        self.service.stop()
        self.temp_dir.cleanup()

    def _get(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Cookie": cookie} if cookie else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode('utf-8')
        return resp.status, resp.headers, body

    def _post(self, path, form_data, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body_encoded = urlencode(form_data)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body_encoded)),
        }
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST", path, body=body_encoded, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode('utf-8')
        return resp.status, resp.headers, body

    def test_dashboard_routes_render_cleanly(self):
        # 1. Access with token to get session cookie
        status, headers, _ = self._get(f"/?token={self.service.token}")
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie")
        self.assertIsNotNone(cookie)

        # 2. /work-items
        status, _, body = self._get("/work-items", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Work Items Hub", body)
        self.assertIn("Create New Requirement", body)

        # 3. /workflows
        status, _, body = self._get("/workflows", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Workflow Templates & Lifecycles", body)
        self.assertIn("Standard Feature Delivery", body)

        # 4. /team
        status, _, body = self._get("/team", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Team & Role Directory", body)
        self.assertIn("Add Team Member", body)

        # 5. /tests
        status, _, body = self._get("/tests", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Testing Operations Workspace", body)
        self.assertIn("Add Test Case", body)

        # 6. / (Overview)
        status, _, body = self._get("/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Today Operations Overview", body)
        self.assertIn("Work Items Hub", body)
        self.assertIn("Testing Posture", body)

    def test_post_creation_and_execution(self):
        # 1. Establish session
        status, headers, _ = self._get(f"/?token={self.service.token}")
        cookie = headers.get("Set-Cookie")
        # Extract CSRF token from a rendered form
        _, _, page_body = self._get("/work-items", cookie=cookie)
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', page_body)
        self.assertIsNotNone(m)
        csrf_token = m.group(1)

        # 2. Create requirement via POST
        req_post_data = {
            'csrf_token': csrf_token,
            'title': 'Automated Checkout Verification',
            'client': 'Shopify Client',
            'ticket': 'SHOP-101',
            'user_story': 'As a shopper, I want to checkout seamlessly',
            'acceptance_criteria': 'Payment gateway callback returns 200 OK',
            'priority': '1',
        }
        res_code, _, _ = self._post("/requirements/create", req_post_data, cookie=cookie)
        self.assertEqual(res_code, 303)

        # Verify requirement exists
        reqs = self.service.work_item_service.list_requirements()
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        self.assertEqual(req['title'], 'Automated Checkout Verification')

        # 3. Create test case via POST
        tc_post_data = {
            'csrf_token': csrf_token,
            'requirement_id': str(req['id']),
            'title': 'Verify credit card processing with 3DS',
            'objective': 'Ensure 3DS modal opens and succeeds',
            'steps': '1. Enter card\n2. Authorize 3DS OTP',
            'expected_result': 'Order confirmed screen displayed',
            'priority': '1',
        }
        res_code, _, _ = self._post("/tests/cases/create", tc_post_data, cookie=cookie)
        self.assertEqual(res_code, 303)

        tcs = self.service.work_item_service.list_test_cases(requirement_id=req['id'])
        self.assertEqual(len(tcs), 1)
        tc = tcs[0]

        # 4. Record test execution via POST
        exec_post_data = {
            'csrf_token': csrf_token,
            'test_case_id': str(tc['id']),
            'result': 'pass',
            'actual_result': '3DS OTP succeeded and order created',
        }
        res_code, _, _ = self._post("/tests/executions/record", exec_post_data, cookie=cookie)
        self.assertEqual(res_code, 303)

        tcs_after = self.service.work_item_service.list_test_cases(requirement_id=req['id'])
        self.assertEqual(tcs_after[0]['latest_result'], 'pass')

    def _post_json(self, path, json_data, cookie=None):
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body_encoded = json.dumps(json_data)
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body_encoded)),
        }
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST", path, body=body_encoded, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode('utf-8')
        return resp.status, resp.headers, json.loads(body) if body else {}

    def test_api_chat_proposal_routes_workspace_external_write(self):
        import unittest.mock
        import re
        status, headers, _ = self._get(f"/?token={self.service.token}")
        cookie = headers.get("Set-Cookie")

        _, _, page_body = self._get("/work-items", cookie=cookie)
        m = re.search(r'name="csrf_token" value="([^"]+)"', page_body)
        self.assertIsNotNone(m)
        csrf_token = m.group(1)

        proposal = self.service.workspace_service.propose_action('owner', 'drive_delete_file', {'file_id': 'f1', 'name': 'test.pdf'})
        prop_id = proposal['proposal_id']

        with unittest.mock.patch.object(self.service.workspace_service, 'execute_action') as mock_exec:
            mock_exec.return_value = {'success': True, 'action': 'drive_delete_file', 'status': 'success'}
            status, _, body = self._post_json(
                '/api/chat/proposal',
                {'proposal_id': prop_id, 'action': 'confirm', 'csrf_token': csrf_token},
                cookie=cookie
            )
            self.assertEqual(status, 200)
            self.assertTrue(body.get('success'))
            self.assertIn('drive_delete_file', body.get('reply', ''))
            mock_exec.assert_called_once()


if __name__ == '__main__':
    unittest.main()

