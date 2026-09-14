import tempfile
import unittest
from pathlib import Path

from application.project_service import ProjectService
from application.task_service import TaskService
from database import Database, SCHEMA_VERSION


class ProjectVerticalSliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'work.sqlite3')
        self.projects = ProjectService(self.db)

    def tearDown(self): self.temp.cleanup()

    def test_project_task_link_and_progress(self):
        created = self.projects.create_project('Project Alpha', code='ALPHA', status='active')
        task = TaskService(self.db).create_task('Verify dashboard parity', project_id=created.entity_id,
                                                due_date='2026-10-30', priority=2)
        detail = self.projects.project_detail(created.entity_id)
        self.assertEqual(detail['code'], 'ALPHA')
        self.assertEqual(detail['tasks'][0]['id'], task.entity_id)
        self.db.mark_status(task.entity_id, 'completed')
        self.assertEqual(self.projects.list_projects(status='active')[0]['completed_count'], 1)

    def test_project_member_and_schema(self):
        member = self.db.add_member('Priya')
        project = self.projects.create_project('Migration work', code='MIG')
        self.projects.add_member(project.entity_id, member, 'manager')
        self.assertEqual(self.projects.project_detail(project.entity_id)['members'][0]['project_role'], 'manager')
        self.assertEqual(SCHEMA_VERSION, 19)
