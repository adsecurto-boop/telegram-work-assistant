"""Project workflow service built on the shared SQLite/audit boundary."""
from __future__ import annotations

import re
import uuid

from database import now_iso
from application.task_service import MutationResult


PROJECT_STATUSES = {'planned', 'active', 'on_hold', 'completed', 'cancelled'}
PROJECT_HEALTH = {'on_track', 'at_risk', 'off_track'}


class ProjectService:
    def __init__(self, db):
        self.db = db

    def create_project(self, name, *, code=None, description=None, owner_member_id=None,
                       start_date=None, due_date=None, status='planned', health='on_track', actor='owner'):
        if status not in PROJECT_STATUSES or health not in PROJECT_HEALTH:
            raise ValueError('Invalid project status or health.')
        code = code or re.sub(r'[^A-Z0-9]+', '-', name.upper()).strip('-')[:24]
        if not code:
            raise ValueError('Project code is required.')
        stamp, corr = now_iso(), f'project-{uuid.uuid4().hex}'
        with self.db.connect() as conn:
            project_id = conn.execute('''INSERT INTO projects
                (code,name,description,owner_member_id,status,health,start_date,due_date,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (code, name.strip(), description, owner_member_id, status, health, start_date, due_date, stamp, stamp)).lastrowid
            row = dict(conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone())
            self.db._record_audit_in_connection(conn, corr, 'create_project', actor, 'projects', project_id, None, row)
        return MutationResult(True, corr, reversible=True, entity_type='project', entity_id=project_id,
                              summary=f'Created project {code}: {name}')

    def add_member(self, project_id, member_id, role='member', actor='owner'):
        if role not in {'owner', 'manager', 'member'}:
            raise ValueError('Project role must be owner, manager, or member.')
        with self.db.connect() as conn:
            if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
                raise ValueError('Project not found.')
            if not conn.execute('SELECT 1 FROM members WHERE id=?', (member_id,)).fetchone():
                raise ValueError('Member not found.')
            conn.execute('''INSERT INTO project_members(project_id,member_id,role,created_at) VALUES(?,?,?,?)
                ON CONFLICT(project_id,member_id) DO UPDATE SET role=excluded.role''',
                (project_id, member_id, role, now_iso()))

    def list_projects(self, status=None, search=None):
        where, args = [], []
        if status:
            where.append('p.status=?'); args.append(status)
        if search:
            where.append('(p.name LIKE ? OR p.code LIKE ?)'); args.extend([f'%{search}%', f'%{search}%'])
        sql = '''SELECT p.*, COUNT(t.id) task_count,
            SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) completed_count,
            SUM(CASE WHEN t.status='blocked' THEN 1 ELSE 0 END) blocked_count
            FROM projects p LEFT JOIN tasks t ON t.project_id=p.id '''
        if where: sql += ' WHERE ' + ' AND '.join(where)
        sql += ' GROUP BY p.id ORDER BY p.due_date IS NULL, p.due_date, p.name'
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def project_detail(self, project_id):
        with self.db.connect() as conn:
            project = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
            if not project: return None
            data = dict(project)
            data['tasks'] = [dict(r) for r in conn.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY due_date, id', (project_id,))]
            data['members'] = [dict(r) for r in conn.execute('''SELECT m.*, pm.role AS project_role FROM project_members pm
                JOIN members m ON m.id=pm.member_id WHERE pm.project_id=? ORDER BY m.name''', (project_id,))]
            return data
