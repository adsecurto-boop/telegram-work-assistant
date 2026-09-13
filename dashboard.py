"""Token-protected localhost dashboard using only the Python standard library."""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import secrets
import hashlib
import threading
import time
import asyncio
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import config
from domain import CASE_STATUSES
from shifts import assign_template_range, check_missing_shift_assignments, format_shift_preview, preview_calendar_week
from application.work_item_service import WorkItemService
from application.task_service import TaskService
from application.case_service import CaseService
from application.message_service import AssistantMessageService
from application.workflow_service import WorkflowService
from application.member_service import MemberService
from application.workspace_service import WorkspaceActionService, WorkspaceActionError
from workspace_view import render_workspace_view
from dashboard_views.today_view import render_today_view
from dashboard_views.work_view import render_work_view
from dashboard_views.testing_view import render_testing_view
from dashboard_views.workflows_view import render_workflows_view
from dashboard_views.team_view import render_team_view
from dashboard_views.followups_view import render_followups_view
from dashboard_views.chat_view import render_chat_view
from gemini_chat_service import GeminiChatService

STATUSES = (
    'new', 'triaged', 'investigating', 'waiting_client', 'waiting_internal',
    'fix_ready', 'testing', 'retest_required', 'resolved', 'client_updated', 'closed'
)

KANBAN_COLUMNS = [
    ('new', 'New'),
    ('triaged', 'Triaged'),
    ('investigating', 'Investigating'),
    ('waiting_client', 'Waiting Client'),
    ('waiting_internal', 'Waiting Internal'),
    ('fix_ready', 'Fix Ready'),
    ('testing', 'Testing'),
    ('retest_required', 'Retest Required'),
    ('resolved', 'Resolved'),
    ('client_updated', 'Client Updated'),
    ('closed', 'Closed'),
]

SAFE_INLINE_MIME_TYPES = {
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'audio/ogg', 'audio/mpeg', 'audio/wav',
    'video/mp4', 'video/webm'
}


def h(value):
    return html.escape('' if value is None else str(value))


def render_work_sub_nav(active_sub: str, inbox_count: int, case_count: int) -> str:
    return f'''<div class="sub-nav-wrapper">
  <div class="sub-nav-bar">
    <a href="/work?sub=kanban" class="sub-nav-pill {'active' if active_sub == 'kanban' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="18" rx="1"/><rect x="14" y="3" width="7" height="10" rx="1"/></svg>
      Kanban Board
    </a>
    <a href="/work?sub=items" class="sub-nav-pill {'active' if active_sub == 'items' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
      Work Items Hub
    </a>
    <a href="/work?sub=inbox" class="sub-nav-pill {'active' if active_sub == 'inbox' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
      Review Inbox <span class="nav-counter">{inbox_count}</span>
    </a>
    <a href="/work?sub=cases" class="sub-nav-pill {'active' if active_sub == 'cases' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      Cases & Issues <span class="nav-counter">{case_count}</span>
    </a>
  </div>
</div>'''


def render_testing_sub_nav(active_sub: str = 'cases') -> str:
    return f'''<div class="sub-nav-wrapper">
  <div class="sub-nav-bar">
    <a href="/tests?sub=cases" class="sub-nav-pill {'active' if active_sub == 'cases' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
      Test Cases & Requirements
    </a>
    <a href="/tests?sub=sessions" class="sub-nav-pill {'active' if active_sub == 'sessions' else ''}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      Exploratory Sessions
    </a>
  </div>
</div>'''


class DashboardService:
    def __init__(self, database, host='127.0.0.1', port=8765):
        self.database = database
        self.host = host
        self.port = int(port)
        self.token = database.get_setting('dashboard_token') or secrets.token_urlsafe(24)
        database.set_setting('dashboard_token', self.token)
        self.work_item_service = WorkItemService(database)
        self.task_service = TaskService(database)
        self.case_service = CaseService(database)
        self.workflow_service = WorkflowService(database)
        self.member_service = MemberService(database)
        self.workspace_service = WorkspaceActionService(database)
        self.chat_service = GeminiChatService(database)
        self.message_service = AssistantMessageService(database)
        self.server = None
        self.thread = None
        self._lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.bulk_tokens: dict[str, dict] = {}
        self.session_ttl_seconds = 8 * 60 * 60
        self.bulk_token_ttl_seconds = 10 * 60

    @property
    def url(self):
        return f'http://{self.host}:{self.port}/?token={self.token}'

    def create_session(self) -> tuple[str, str]:
        with self._lock:
            self.cleanup_tokens()
            sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            now = time.time()
            self.sessions[sid] = {
                'csrf_token': csrf, 'created_at': now,
                'expires_at': now + self.session_ttl_seconds
            }
            return sid, csrf

    def get_session(self, sid: str) -> dict | None:
        with self._lock:
            session = self.sessions.get(sid)
            if session and session.get('expires_at', 0) > time.time():
                return session
            self.sessions.pop(sid, None)
            return None

    def cleanup_tokens(self):
        with self._lock:
            now = time.time()
            self.sessions = {
                key: value for key, value in self.sessions.items()
                if value.get('expires_at', 0) > now
            }
            self.bulk_tokens = {
                key: value for key, value in self.bulk_tokens.items()
                if value.get('expires_at', 0) > now
            }

    def create_bulk_token(self, data: dict, session_id: str) -> str:
        with self._lock:
            self.cleanup_tokens()
            bulk_token = secrets.token_urlsafe(16)
            self.bulk_tokens[bulk_token] = {
                **data,
                'session_id': session_id,
                'expires_at': time.time() + self.bulk_token_ttl_seconds
            }
            return bulk_token

    def consume_bulk_token(self, bulk_token: str, session_id: str) -> dict | None:
        with self._lock:
            bulk_data = self.bulk_tokens.pop(bulk_token, None)
            if not bulk_data:
                return None
            if bulk_data.get('session_id') != session_id or bulk_data.get('expires_at', 0) <= time.time():
                return None
            return bulk_data

    def start(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def get_cookie_sid(self) -> str | None:
                cookie_header = self.headers.get('Cookie')
                if not cookie_header:
                    return None
                try:
                    cookie = SimpleCookie(cookie_header)
                    m = cookie.get('dashboard_session')
                    return m.value if m else None
                except Exception:
                    return None

            def authenticate_request(self, params: dict) -> tuple[bool, str | None, str | None, bool]:
                """
                Returns (is_authenticated, session_id, csrf_token, is_token_exchange).
                Exchanges access token query param for local session cookie.
                """
                sid = self.get_cookie_sid()
                if sid and service.get_session(sid):
                    session = service.get_session(sid)
                    return True, sid, session['csrf_token'], False

                token_hdr = self.headers.get('X-Dashboard-Token')
                if token_hdr and secrets.compare_digest(token_hdr, service.token):
                    new_sid, csrf = service.create_session()
                    return True, new_sid, csrf, False

                supplied_token = (params.get('token') or [''])[0]
                if supplied_token and secrets.compare_digest(supplied_token, service.token):
                    new_sid, csrf = service.create_session()
                    return True, new_sid, csrf, True

                return False, None, None, False

            def send_json(self, data: dict, status: int = 200):
                content = json.dumps(data).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(content)

            def send_html(self, body: str, status=200, set_sid: str | None = None):
                set_sid = set_sid or getattr(self, 'pending_sid', None)
                content = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                if os.getenv('ALLOW_IFRAME', 'false').lower() in ('true', '1', 'yes'):
                    self.send_header('X-Frame-Options', 'SAMEORIGIN')
                else:
                    self.send_header('X-Frame-Options', 'DENY')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'same-origin')
                csp = (
                    "default-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://apis.google.com https://accounts.google.com; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src 'self' https://fonts.gstatic.com data:; "
                    "connect-src 'self' https://*.googleapis.com https://*.firebaseio.com https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://accounts.google.com; "
                    "frame-src 'self' https://*.firebaseapp.com https://accounts.google.com; "
                    "img-src 'self' data: https:;"
                )
                self.send_header('Content-Security-Policy', csp)
                if set_sid:
                    self.send_header('Set-Cookie', f'dashboard_session={set_sid}; Path=/; HttpOnly; SameSite=Lax')
                self.end_headers()
                self.wfile.write(content)

            def page(self, content, title='Personal Work Assistant', csrf_token: str = '', route: str = '/'):
                route_path = (route or '/').split('?')[0]
                pillar_today = route_path in ('/', '/overview', '/today')
                pillar_work = route_path in ('/work', '/work-items', '/kanban', '/inbox', '/cases', '/case')
                pillar_testing = route_path in ('/tests', '/testing', '/evidence')
                pillar_workspace = route_path == '/workspace'
                pillar_chat = route_path in ('/chat', '/assistant')
                pillar_ops = not (pillar_today or pillar_work or pillar_testing or pillar_workspace or pillar_chat)

                shift = service.database.active_shift()
                if shift:
                    st = (shift.get('start') or '')[:16].replace('T', ' ')
                    time_part = st[11:] if len(st) >= 16 else st
                    shift_chip_html = f'''<div class="shift-chip active" title="Active Shift started at {st}">
                      <span class="pulse-dot"></span>
                      <span class="shift-chip-text">Shift Active · {time_part}</span>
                    </div>'''
                else:
                    shift_chip_html = '''<div class="shift-chip inactive" title="No active shift currently clocked">
                      <span class="static-dot"></span>
                      <span class="shift-chip-text">Off Duty</span>
                    </div>'''

                return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{h(title)}</title>
<meta name="description" content="Personal Work Assistant and Operations Hub with Telegram and Google Workspace (Drive, Docs, Sheets, Tasks) integrations for tasks, documents, and test workflows.">
<meta property="og:title" content="{h(title)}">
<meta property="og:description" content="Personal Work Assistant and Operations Hub with Telegram and Google Workspace (Drive, Docs, Sheets, Tasks) integrations for tasks, documents, and test workflows.">
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
:root {{
  --bg-canvas: #f8fafc;
  --bg-card: #ffffff;
  --header-bg: #0f172a;
  --header-border: #1e293b;
  --text-primary: #0f172a;
  --text-secondary: #475569;
  --text-muted: #64748b;
  --border-subtle: #e2e8f0;
  --color-primary: #2563eb;
  --color-primary-hover: #1d4ed8;
  --color-success: #16a34a;
  --color-warning: #d97706;
  --color-danger: #dc2626;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  margin: 0;
  background: var(--bg-canvas);
  color: var(--text-primary);
  -webkit-font-smoothing: antialiased;
}}

/* Top App Header */
.app-header {{
  position: sticky;
  top: 0;
  z-index: 1000;
  background: var(--header-bg);
  border-bottom: 1px solid var(--header-border);
  color: #ffffff;
}}
.header-inner {{
  max-width: 1360px;
  margin: 0 auto;
  padding: 0 20px;
  height: 58px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}}
.brand-group {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.brand-link {{
  display: flex;
  align-items: center;
  gap: 8px;
  color: #f8fafc;
  text-decoration: none;
  font-weight: 700;
  font-size: 15px;
  letter-spacing: -0.01em;
  white-space: nowrap;
}}
.brand-link:hover {{ color: #ffffff; text-decoration: none; }}
.brand-icon {{ color: #60a5fa; flex-shrink: 0; }}

/* Shift status chip in header */
.shift-chip {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}}
.shift-chip.active {{
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
  border: 1px solid rgba(74, 222, 128, 0.3);
}}
.shift-chip.inactive {{
  background: rgba(148, 163, 184, 0.12);
  color: #94a3b8;
  border: 1px solid rgba(148, 163, 184, 0.2);
}}
.pulse-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #4ade80;
  box-shadow: 0 0 0 2px rgba(74, 222, 128, 0.4);
}}
.static-dot {{
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #94a3b8;
}}

/* Segmented Primary Navigation */
.primary-nav {{
  display: flex;
  align-items: center;
  background: #1e293b;
  padding: 3px;
  border-radius: 24px;
  gap: 2px;
}}
.nav-segment {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 20px;
  color: #94a3b8;
  text-decoration: none;
  font-weight: 500;
  font-size: 13px;
  white-space: nowrap;
  transition: all 0.15s ease;
}}
.nav-segment:hover {{
  color: #f8fafc;
  background: rgba(255, 255, 255, 0.06);
  text-decoration: none;
}}
.nav-segment.active {{
  background: var(--color-primary);
  color: #ffffff;
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}}
.nav-icon {{ flex-shrink: 0; }}

/* Secondary Tools Dropdown */
.header-actions {{
  display: flex;
  align-items: center;
}}
.ops-menu-container {{
  position: relative;
}}
.ops-menu-btn {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #1e293b;
  border: 1px solid #334155;
  color: #cbd5e1;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}}
.ops-menu-btn:hover {{
  background: #334155;
  color: #ffffff;
}}
.ops-dropdown-menu {{
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  width: 210px;
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  box-shadow: 0 10px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.05);
  display: none;
  flex-direction: column;
  padding: 6px 0;
  z-index: 1010;
}}
.ops-dropdown-menu.open {{
  display: flex;
}}
.dropdown-group-title {{
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  padding: 6px 14px 2px 14px;
}}
.ops-dropdown-menu a {{
  color: var(--text-primary);
  padding: 7px 14px;
  text-decoration: none;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}}
.ops-dropdown-menu a:hover {{
  background: #f1f5f9;
  color: var(--color-primary);
  text-decoration: none;
}}

/* Sub-Navigation Bar (for Work & Testing pillars) */
.sub-nav-wrapper {{
  background: #ffffff;
  border-bottom: 1px solid var(--border-subtle);
  margin: -20px -20px 24px -20px;
  padding: 10px 20px;
  position: sticky;
  top: 58px;
  z-index: 900;
}}
.sub-nav-bar {{
  max-width: 1360px;
  margin: 0 auto;
  display: flex;
  gap: 8px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  padding: 2px 0;
}}
.sub-nav-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 20px;
  background: #f1f5f9;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  border: 1px solid transparent;
  transition: all 0.15s ease;
}}
.sub-nav-pill:hover {{
  background: #e2e8f0;
  color: var(--text-primary);
  text-decoration: none;
}}
.sub-nav-pill.active {{
  background: #eff6ff;
  color: var(--color-primary);
  border-color: #bfdbfe;
  font-weight: 600;
}}
.nav-counter {{
  display: inline-block;
  background: rgba(0,0,0,0.08);
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 11px;
}}
.sub-nav-pill.active .nav-counter {{
  background: #dbeafe;
  color: var(--color-primary);
}}

/* Main Canvas Content */
main {{
  max-width: 1360px;
  margin: 20px auto;
  padding: 0 20px 60px 20px;
}}

/* Responsive Metric Band (4 Cards) */
.metric-band {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}}
.metric-card {{
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
  transition: border-color 0.15s;
}}
.metric-card:hover {{
  border-color: #cbd5e1;
}}
.metric-card-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}}
.metric-card-title {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}}
.metric-card-value {{
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.2;
  margin-bottom: 4px;
}}
.metric-card-desc {{
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.4;
}}
.metric-card-footer {{
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

/* Bento Grid (2:1 Desktop Split) */
.bento-split {{
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 20px;
  align-items: start;
}}
.bento-main {{
  display: flex;
  flex-direction: column;
  gap: 20px;
}}
.bento-side {{
  display: flex;
  flex-direction: column;
  gap: 20px;
}}

/* General UI Card */
.card {{
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 20px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
}}
.card-header-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}}
.card-title {{
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: var(--text-primary);
}}

/* Typography */
h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 6px 0; letter-spacing: -0.01em; }}
h2 {{ font-size: 17px; font-weight: 600; margin: 0 0 12px 0; }}
h3 {{ font-size: 15px; font-weight: 600; margin: 0 0 8px 0; }}
.muted {{ color: var(--text-muted); font-size: 13px; }}

/* Tags & Badges */
.tag {{
  display: inline-flex;
  align-items: center;
  background: #f1f5f9;
  color: #334155;
  padding: 3px 9px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}}
.tag-ok {{ background: #dcfce7; color: #15803d; }}
.tag-warn {{ background: #fef3c7; color: #b45309; }}
.tag-err {{ background: #fee2e2; color: #b91c1c; }}
.tag-info {{ background: #dbeafe; color: #1d4ed8; }}

/* Buttons */
button, input, select, textarea {{
  font-family: inherit;
  font-size: 13px;
}}
button {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-weight: 500;
  border: 1px solid var(--border-subtle);
  background: #ffffff;
  color: var(--text-primary);
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
  min-height: 36px;
}}
button:hover {{ background: #f8fafc; border-color: #cbd5e1; }}
button.primary, .btn-primary-action {{
  background: var(--color-primary);
  color: #ffffff;
  border-color: var(--color-primary);
}}
button.primary:hover, .btn-primary-action:hover {{
  background: var(--color-primary-hover);
  border-color: var(--color-primary-hover);
  color: #ffffff;
  text-decoration: none;
}}
button.danger {{
  background: var(--color-danger);
  color: #ffffff;
  border-color: var(--color-danger);
}}
button.danger:hover {{
  background: #b91c1c;
}}
.btn-subtle {{
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: #ffffff;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}}
.btn-subtle:hover {{
  background: #f1f5f9;
  color: var(--text-primary);
  text-decoration: none;
}}

/* Form Elements */
input, select, textarea {{
  padding: 8px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: #ffffff;
  color: var(--text-primary);
}}
input:focus, select:focus, textarea:focus {{
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}}
pre {{ white-space: pre-wrap; background: #f8fafc; padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); }}

/* Responsive Table Wrappers */
.table-scroll-wrap {{
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin-top: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}}
th {{
  background: #f8fafc;
  color: var(--text-secondary);
  font-weight: 600;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  white-space: nowrap;
}}
td {{
  padding: 10px 14px;
  border-bottom: 1px solid #f1f5f9;
  vertical-align: middle;
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: #fafafa; }}

/* Progress Bar */
.progress-bar-wrap {{
  height: 8px;
  background: #fee2e2;
  border-radius: 4px;
  overflow: hidden;
  display: flex;
  margin: 6px 0;
}}
.progress-bar-fill {{
  height: 100%;
  background: var(--color-success);
  transition: width 0.3s ease;
}}

/* Kanban Board */
.kanban-board {{
  display: flex;
  gap: 16px;
  overflow-x: auto;
  padding-bottom: 16px;
  -webkit-overflow-scrolling: touch;
}}
.kanban-col {{
  flex: 0 0 300px;
  background: #f1f5f9;
  border-radius: var(--radius-md);
  padding: 14px;
  min-height: 540px;
}}
.kanban-col h3 {{
  margin: 0 0 12px 0;
  font-size: 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.kanban-item {{
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 12px;
  margin-bottom: 10px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}}

/* Filter Bar */
.filter-bar {{
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  margin-bottom: 20px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}}

/* Today View Components */
.today-hero {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 12px;
}}
.today-hero-actions {{
  display: flex;
  align-items: center;
  gap: 10px;
}}

/* Attention Strip */
.attention-strip {{
  border-radius: var(--radius-md);
  margin-bottom: 20px;
  border: 1px solid var(--border-subtle);
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,0.03);
}}
.attention-strip.clear {{
  background: #f0fdf4;
  border-color: #bbf7d0;
}}
.attention-strip.alert {{
  background: #fff8f8;
  border-color: #fecaca;
}}
.attention-strip-inner {{
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}}
.attention-status-left {{
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}}
.attention-icon-ok {{
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #dcfce7;
  color: #16a34a;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
}}
.attention-alert-icon {{
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #fee2e2;
  color: #dc2626;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
}}
.attention-desc {{
  font-size: 13px;
  color: var(--text-secondary);
}}
.attention-actions {{
  display: flex;
  align-items: center;
  gap: 8px;
}}

/* Attention Counter Badges */
.attn-badge {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
}}
.attn-badge:hover {{ text-decoration: none; }}
.attn-badge-dot {{
  width: 6px;
  height: 6px;
  border-radius: 50%;
}}
.attn-err {{
  background: #fee2e2;
  color: #b91c1c;
}}
.attn-err .attn-badge-dot {{ background: #dc2626; }}
.attn-warn {{
  background: #fef3c7;
  color: #b45309;
}}
.attn-warn .attn-badge-dot {{ background: #d97706; }}
.attn-info {{
  background: #e0f2fe;
  color: #0369a1;
}}
.attn-info .attn-badge-dot {{ background: #0284c7; }}

/* Operational Status Indicators */
.status-indicator {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
}}
.status-indicator::before {{
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}}
.status-indicator.active::before {{ background: #2563eb; }}
.status-indicator.blocked::before {{ background: #dc2626; }}
.status-indicator.waiting::before {{ background: #d97706; }}
.status-indicator.done::before {{ background: #16a34a; }}
.status-indicator.pending::before {{ background: #94a3b8; }}

/* Priority Badges */
.p-badge {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}}
.p-badge.p1 {{ background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }}
.p-badge.p2 {{ background: #ffedd5; color: #c2410c; border: 1px solid #fed7aa; }}
.p-badge.p3 {{ background: #fef9c3; color: #a16207; border: 1px solid #fef08a; }}
.p-badge.p4 {{ background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0; }}

/* Entity Type Badges */
.type-tag {{
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}}
.type-tag.req {{ background: #ede9fe; color: #6d28d9; }}
.type-tag.task {{ background: #e0f2fe; color: #0369a1; }}
.type-tag.case {{ background: #fce7f3; color: #be185d; }}
.type-tag.defect {{ background: #fee2e2; color: #b91c1c; }}
.type-tag.test {{ background: #ccfbf1; color: #0f766e; }}

/* Stage Pill */
.stage-pill {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
  background: #f8fafc;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}}

/* Pipeline Stepper */
.pipeline-bar {{
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  padding: 8px 0;
  -webkit-overflow-scrolling: touch;
}}
.pipeline-step {{
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  background: #f8fafc;
  border: 1px solid var(--border-subtle);
  font-size: 12px;
  white-space: nowrap;
}}
.pipeline-step.step-done {{
  background: #f0fdf4;
  border-color: #bbf7d0;
  color: #16a34a;
}}
.pipeline-step.step-current {{
  background: #eff6ff;
  border-color: #3b82f6;
  color: #1d4ed8;
  font-weight: 600;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
}}
.pipeline-step.step-waiting {{
  background: #fefce8;
  border-color: #fde047;
  color: #a16207;
}}
.pipeline-step.step-upcoming {{
  color: var(--text-muted);
}}
.step-indicator {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.step-icon {{
  font-weight: bold;
}}
.step-meta {{
  font-size: 11px;
  color: var(--text-muted);
}}
.pipeline-arrow {{
  color: #cbd5e1;
  font-size: 12px;
}}

/* Detail Drawer (Side-Panel Flyout) */
.drawer-overlay {{
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(15, 23, 42, 0.4);
  backdrop-filter: blur(2px);
  z-index: 9999;
  display: flex;
  justify-content: flex-end;
}}
.drawer-panel {{
  background: #ffffff;
  width: 100%;
  max-width: 620px;
  height: 100%;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.15);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: slideDrawer 0.2s ease-out;
}}
@keyframes slideDrawer {{
  from {{ transform: translateX(100%); }}
  to {{ transform: translateX(0); }}
}}
.drawer-header {{
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #f8fafc;
}}
.drawer-display-id {{
  font-size: 13px;
  font-weight: 700;
  color: var(--text-muted);
}}
.drawer-close-btn {{
  background: none;
  border: none;
  font-size: 20px;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px 8px;
  min-height: auto;
}}
.drawer-close-btn:hover {{ color: var(--text-primary); }}
.drawer-body {{
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}}
.drawer-section {{
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #f1f5f9;
}}
.drawer-section:last-child {{
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}}
.drawer-section-title {{
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin-bottom: 10px;
}}
.drawer-subform {{
  background: #f8fafc;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 12px;
  margin-top: 10px;
}}

/* History Timeline */
.history-timeline {{
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-left: 10px;
}}
.history-item {{
  display: flex;
  gap: 10px;
  position: relative;
}}
.history-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  margin-top: 5px;
  flex-shrink: 0;
}}
.history-content {{
  font-size: 13px;
}}

/* Filter Chips */
.filter-chip {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #f1f5f9;
  border: 1px solid var(--border-subtle);
  padding: 4px 10px;
  border-radius: 16px;
  font-size: 12px;
  color: var(--text-secondary);
  text-decoration: none;
}}
.filter-chip.active {{
  background: #eff6ff;
  border-color: #bfdbfe;
  color: var(--color-primary);
  font-weight: 600;
}}
.active-filter-chips {{
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 8px;
}}

/* Work Hub Header */
.work-hub-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}}

/* Team Grid */
.team-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}}
.team-card {{
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.02);
}}
.team-avatar {{
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: #eff6ff;
  color: #2563eb;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 13px;
  flex-shrink: 0;
}}

/* Blockers in UI */
.blocker-item-box {{
  background: #fff8f8;
  border: 1px solid #fecaca;
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  margin-bottom: 8px;
}}
.blocker-card-drawer {{
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin-bottom: 8px;
}}

.item-link {{
  color: var(--color-primary);
  text-decoration: none;
  font-weight: 600;
}}
.item-link:hover {{
  text-decoration: underline;
}}

.today-blocker-item {{
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: var(--radius-sm);
  padding: 14px;
  margin-bottom: 10px;
}}
.today-blocker-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}}
.ws-quick-link {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  text-decoration: none;
  color: var(--text-primary);
  margin-bottom: 8px;
  transition: all 0.15s ease;
}}
.ws-quick-link:hover {{
  background: #f8fafc;
  border-color: #cbd5e1;
  text-decoration: none;
}}
.ws-icon-wrap {{
  width: 34px;
  height: 34px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}}

/* Google Workspace Hub specific styles */
.gsi-material-button{{user-select:none;background-color:#131314;border:1px solid #747775;border-radius:20px;box-sizing:border-box;color:#e3e3e3;cursor:pointer;font-family:system-ui,-apple-system,sans-serif;font-size:14px;height:40px;letter-spacing:0.25px;outline:none;overflow:hidden;padding:0 14px;position:relative;text-align:center;vertical-align:middle;white-space:nowrap;width:auto;display:inline-flex;align-items:center;transition:background-color .2s}}
.gsi-material-button:hover{{background-color:#202124}}
.gsi-material-button-icon{{height:20px;margin-right:10px;min-width:20px;width:20px}}
.gsi-material-button-content-wrapper{{align-items:center;display:flex;flex-direction:row;flex-wrap:nowrap;height:100%;justify-content:space-between;position:relative;width:100%}}
.gsi-material-button-contents{{flex-grow:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;vertical-align:top}}
.ws-tabs{{display:flex;gap:8px;border-bottom:1px solid #dce2e8;margin-bottom:16px;padding-bottom:8px}}
.ws-tab-btn{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:8px 14px;cursor:pointer;font-weight:600}}
.ws-tab-btn.active{{background:#0969da;color:white;border-color:#0969da}}
.ws-panel{{display:none}}
.ws-panel.active{{display:block}}
.ws-item-card{{border:1px solid #e1e4e8;border-radius:6px;padding:12px;margin-bottom:8px;background:#fff;display:flex;justify-content:space-between;align-items:center}}
.ws-action-btn{{padding:5px 10px;font-size:12px;border-radius:4px;margin-left:6px}}
.modal-overlay{{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:none;align-items:center;justify-content:center;z-index:9999}}
.modal-card{{background:white;border-radius:8px;max-width:480px;width:90%;padding:20px;box-shadow:0 8px 24px rgba(0,0,0,0.18)}}

/* Mobile Bottom Navigation Bar */
.mobile-bottom-nav {{
  display: none;
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: 60px;
  background: #0f172a;
  border-top: 1px solid #1e293b;
  z-index: 1000;
  justify-content: space-around;
  align-items: center;
  padding-bottom: env(safe-area-inset-bottom);
}}
.mobile-nav-item {{
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: #94a3b8;
  text-decoration: none;
  font-size: 11px;
  font-weight: 500;
  min-width: 54px;
  min-height: 48px;
}}
.mobile-nav-item:hover {{ color: #ffffff; text-decoration: none; }}
.mobile-nav-item.active {{
  color: #60a5fa;
  font-weight: 600;
}}

/* Responsive Breakpoints */
@media (max-width: 1024px) {{
  .metric-band {{ grid-template-columns: repeat(2, 1fr); }}
  .bento-split {{ grid-template-columns: 1fr; }}
}}

@media (max-width: 768px) {{
  .primary-nav {{ display: none; }}
  .mobile-bottom-nav {{ display: flex; }}
  main {{ padding: 0 16px 80px 16px; margin: 16px auto; }}
  .sub-nav-wrapper {{ margin: -16px -16px 16px -16px; padding: 10px 16px; top: 58px; }}
  .metric-band {{ grid-template-columns: 1fr; }}
  .metric-card {{ padding: 14px 16px; }}
  .header-inner {{ padding: 0 16px; }}
  .card {{ padding: 16px; }}
  button, .btn-primary-action {{ min-height: 44px; padding: 10px 16px; }}
}}
</style>
</head>
<body>
<header class="app-header">
  <div class="header-inner">
    <div class="brand-group">
      <a href="/" class="brand-link">
        <svg class="brand-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
        <span class="brand-name">Telegram Work Assistant</span>
      </a>
      {shift_chip_html}
    </div>

    <!-- Desktop Primary Segmented Navigation -->
    <nav class="primary-nav" aria-label="Main Navigation">
      <a href="/" class="nav-segment {'active' if pillar_today else ''}">
        <svg class="nav-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
        Today
      </a>
      <a href="/work" class="nav-segment {'active' if pillar_work else ''}">
        <svg class="nav-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
        Work
      </a>
      <a href="/tests" class="nav-segment {'active' if pillar_testing else ''}">
        <svg class="nav-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        Testing
      </a>
      <a href="/workspace" class="nav-segment {'active' if pillar_workspace else ''}">
        <svg class="nav-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>
        Workspace Hub
      </a>
      <a href="/chat" class="nav-segment {'active' if pillar_chat else ''}">
        <svg class="nav-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        AI Chat
      </a>
    </nav>

    <!-- Operations Secondary Tools Dropdown -->
    <div class="header-actions">
      <div class="ops-menu-container">
        <button type="button" class="ops-menu-btn" onclick="document.getElementById('ops-dropdown').classList.toggle('open')" aria-haspopup="true">
          <span>Operations Tools</span>
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div id="ops-dropdown" class="ops-dropdown-menu">
          <div class="dropdown-group-title">Workflows & Team</div>
          <a href="/workflows">Workflows</a>
          <a href="/team">Team Roster</a>
          <a href="/shifts">Shift Calendar</a>
          <div class="dropdown-group-title">Intelligence & Analysis</div>
          <a href="/chat">AI Chatbot</a>
          <a href="/followups">Follow-ups</a>
          <a href="/clusters">Clusters</a>
          <a href="/reports">Reports</a>
          <a href="/audit">Audit Log</a>
          <a href="/ai/history">AI History</a>
          <div class="dropdown-group-title">Configuration</div>
          <a href="/connectors">Connectors</a>
          <a href="/settings">Settings & Diagnostics</a>
        </div>
      </div>
    </div>
  </div>
</header>

<main>
{content}
</main>

<!-- Persistent Mobile Navigation -->
<nav class="mobile-bottom-nav" aria-label="Mobile Navigation">
  <a href="/" class="mobile-nav-item {'active' if pillar_today else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
    <span>Today</span>
  </a>
  <a href="/work" class="mobile-nav-item {'active' if pillar_work else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
    <span>Work</span>
  </a>
  <a href="/tests" class="mobile-nav-item {'active' if pillar_testing else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>
    <span>Testing</span>
  </a>
  <a href="/chat" class="mobile-nav-item {'active' if pillar_chat else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    <span>AI Chat</span>
  </a>
  <a href="/workspace" class="mobile-nav-item {'active' if pillar_workspace else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>
    <span>Workspace</span>
  </a>
  <a href="/settings" class="mobile-nav-item {'active' if pillar_ops else ''}">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    <span>Tools</span>
  </a>
</nav>

<script>
document.addEventListener('click', function(e) {{
  var menu = document.getElementById('ops-dropdown');
  var btn = document.querySelector('.ops-menu-btn');
  if (menu && btn && !btn.contains(e.target) && !menu.contains(e.target)) {{
    menu.classList.remove('open');
  }}
}});
</script>
</body>
</html>'''

            def send_file(self, item):
                path = Path(item.get('path') or '').resolve()
                allowed = (config.BASE_DIR / 'storage' / 'evidence').resolve()
                try:
                    path.relative_to(allowed)
                except ValueError:
                    self.send_error(403)
                    return
                if not path.is_file():
                    self.send_error(404)
                    return
                raw_mime = item.get('mime_type') or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                mime = raw_mime.lower().split(';')[0].strip()
                safe_filename = re.sub(r'[\r\n"\\\x00-\x1f]', '_', path.name)

                if mime in SAFE_INLINE_MIME_TYPES:
                    disposition = f'inline; filename="{safe_filename}"'
                else:
                    disposition = f'attachment; filename="{safe_filename}"'

                try:
                    file_size = path.stat().st_size
                except OSError:
                    self.send_error(404)
                    return

                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Content-Disposition', disposition)
                self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()

                with path.open('rb') as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                authed, set_sid, csrf_token, is_token_exchange = self.authenticate_request(params)
                if not authed:
                    self.send_html(self.page('<h2>Access denied</h2><p>Please use your authorized dashboard link with token parameter.</p>'), 403)
                    return

                self.pending_sid = set_sid if not is_token_exchange else None

                if is_token_exchange:
                    clean_params = {k: v for k, v in params.items() if k != 'token'}
                    redirect_path = parsed.path
                    if clean_params:
                        redirect_path += '?' + urlencode(clean_params, doseq=True)

                    self.send_response(303)
                    self.send_header('Location', redirect_path)
                    self.send_header('Set-Cookie', f'dashboard_session={set_sid}; Path=/; HttpOnly; SameSite=Lax')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    return

                route = parsed.path
                if route in ('/today', '/overview'):
                    route = '/'
                elif route == '/work':
                    sub = (params.get('sub') or ['kanban'])[0]
                    if sub == 'items':
                        route = '/work-items'
                    elif sub == 'inbox':
                        route = '/inbox'
                    elif sub == 'cases':
                        route = '/cases'
                    else:
                        route = '/kanban'
                elif route == '/testing':
                    route = '/tests'

                if route == '/evidence':
                    item = service.database.evidence_item(int((params.get('id') or ['0'])[0]))
                    if not item:
                        self.send_error(404)
                        return
                    self.send_file(item)
                    return

                cases = service.database.list_cases(limit=150)
                inbox = service.database.inbox(limit=150)
                followups = service.database.list_followups(limit=100)
                tests = service.database.test_sessions(limit=100)

                # 1. WORK OPERATIONS (Unified Hub, Kanban, Inbox, Cases)
                if route in ('/work', '/work-items', '/kanban', '/inbox', '/cases'):
                    sub_map = {'/kanban': 'kanban', '/inbox': 'inbox', '/cases': 'cases', '/work-items': 'items'}
                    sub_val = sub_map.get(route) or (params.get('sub') or ['items'])[0]
                    content = render_work_view(service, csrf_token, params, sub=sub_val)

                # 2. TESTING WORKSPACE
                elif route in ('/tests', '/testing'):
                    test_sub = (params.get('sub') or (params.get('test_sub') or ['cases']))[0]
                    content = render_testing_view(service, csrf_token, params, test_sub=test_sub)

                # 3. WORKFLOW TEMPLATES & LIFECYCLES
                elif route == '/workflows':
                    content = render_workflows_view(service, csrf_token, params)

                # 4. TEAM & ROLE ASSIGNMENTS
                elif route == '/team':
                    content = render_team_view(service, csrf_token, params)

                # 5. FOLLOW-UPS
                elif route == '/followups':
                    content = render_followups_view(service, csrf_token, params)

                # 10. REPORTS & FACTUAL VALIDATION
                elif route == '/reports':
                    report_cards = []
                    for summary in service.database.history():
                        report = service.database.report(summary['id'])
                        val_res = service.database.get_report_validation(report['id'])
                        val_badge = ''
                        val_warnings_html = ''
                        if val_res:
                            if val_res.get('is_valid'):
                                val_badge = '<span class="tag tag-ok">Validated</span>'
                            else:
                                val_badge = '<span class="tag tag-err">Validation Warnings</span>'
                            warns = val_res.get('warnings') or []
                            if warns:
                                val_warnings_html = '<div style="background:#fff8c5;padding:8px;border-radius:6px;margin:8px 0;"><strong>Quality Warnings:</strong><ul>' + ''.join(f"<li>[{w.get('severity','').upper()}] {h(w.get('message',''))}</li>" for w in warns) + '</ul></div>'

                        report_cards.append(f'''<section class="card"><h2>Report #{report['id']} · {h(report['kind'])} {val_badge}</h2>
<p class="muted">{h(report['created_at'])} · {h(report['style'])} · {'final' if report['finalized'] else 'draft'}</p>
{val_warnings_html}
<form method="post" action="/report/edit"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{report['id']}">
<textarea name="text" rows="10" style="width:100%">{h(report['text'])}</textarea><br>
<button>Save revised draft</button></form>
{f'<form method="post" action="/report/finalize" style="margin-top:6px;"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{report["id"]}"><label><input type="checkbox" name="acknowledge_errors" value="1"> Acknowledge validation warnings</label> <button class="primary">Finalize Report</button></form>' if not report['finalized'] else '<span class="tag tag-ok">Finalized</span>'}
</section>''')
                    content = '<h1>Reports & Quality Verification</h1><div class="grid">' + (''.join(report_cards) or '<p>No reports.</p>') + '</div>'

                # 11. AI ACTION HISTORY
                elif route == '/ai/history':
                    with service.database.connect() as conn:
                        ai_rows = conn.execute("SELECT * FROM ai_events ORDER BY id DESC LIMIT 50").fetchall()
                    rows_html = []
                    for r in ai_rows:
                        status_tag = '<span class="tag tag-ok">Success</span>' if r['status'] == 'success' else '<span class="tag tag-err">Failed</span>'
                        rows_html.append(f'''<tr>
<td>{h(r['occurred_at'][:19].replace('T',' '))}</td>
<td><strong>{h(r['feature'])}</strong></td>
<td>{h(r['provider'])}</td>
<td>{h(r['model'] or '—')}</td>
<td>{status_tag}</td>
<td>{h(r.get('error_type') or '—')}</td>
</tr>''')
                    content = f'''<h1>AI Action History & Auditing</h1>
<p class="muted">Safe telemetry log of AI invocations. Raw user content and prompts are strictly never stored.</p>
<div class="card"><table><tr><th>When</th><th>Feature</th><th>Provider</th><th>Model</th><th>Status</th><th>Error</th></tr>
{''.join(rows_html) or '<tr><td colspan="6" class="muted" style="text-align:center;">No AI events logged.</td></tr>'}
</table></div>'''

                # 12. CONNECTOR HEALTH
                elif route == '/connectors':
                    integrity = service.database.integrity()
                    backups = sorted((config.BASE_DIR / 'storage' / 'backups').glob('*.sqlite3'))
                    with service.database.connect() as conn:
                        source_count = conn.execute("SELECT COUNT(*) as c FROM source_messages").fetchone()['c']
                        task_count = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()['c']
                        case_count = conn.execute("SELECT COUNT(*) as c FROM work_cases").fetchone()['c']
                    content = f'''<h1>System & Connector Health</h1>
<div class="grid">
<div class="card"><h2>Database Connector</h2>
<p><strong>Engine:</strong> SQLite 3 (WAL mode)</p>
<p><strong>Path:</strong> <code>{h(config.DB_PATH)}</code></p>
<p><strong>Integrity:</strong> <span class="tag tag-ok">{h(integrity)}</span></p>
<p><strong>Source Messages:</strong> {source_count} retained</p>
<p><strong>Tasks / Cases:</strong> {task_count} / {case_count}</p>
</div>
<div class="card"><h2>Backup Connector</h2>
<p><strong>Directory:</strong> <code>storage/backups</code></p>
<p><strong>Total Backups:</strong> {len(backups)} retained</p>
<p><strong>Latest Backup:</strong> {h(backups[-1].name if backups else 'None')}</p>
</div>
<div class="card"><h2>AI Provider Status</h2>
<p><strong>Configured:</strong> {h(bool(config.AI_KEY and config.AI_MODEL))}</p>
<p><strong>Primary Model:</strong> <code>{h(config.AI_MODEL or 'Not set')}</code></p>
<p><strong>Fallback Model:</strong> <code>{h(config.AI_FALLBACK_MODEL or 'None')}</code></p>
<p><strong>Daily Limit:</strong> {config.AI_DAILY_LIMIT} operations</p>
</div></div>'''

                # 13. SETTINGS & DIAGNOSTICS
                elif route == '/settings':
                    masked = service.database.get_setting('mask_client_names') == 'true'
                    integrity = service.database.integrity()
                    backups = sorted((config.BASE_DIR / 'storage' / 'backups').glob('*.sqlite3'))
                    content = f'''<h1>Settings & Diagnostics</h1><div class="grid">
<section class="card"><h2>Privacy Controls</h2>
<form method="post" action="/settings"><input type="hidden" name="csrf_token" value="{csrf_token}"><label><input type="checkbox" name="mask_clients" value="on" {'checked' if masked else ''}> Mask client names in AI outbound payloads and drafts</label><br><br><button>Save Settings</button></form>
</section>
<section class="card"><h2>Diagnostics</h2>
<p><strong>Database Integrity:</strong> <span class="tag tag-ok">{h(integrity)}</span></p>
<p><strong>Backups Retained:</strong> {len(backups)}</p>
<p><strong>Dashboard Host:</strong> {h(service.host)}:{service.port} (Loopback Only)</p>
<p><strong>Timezone:</strong> {h(config.TIMEZONE)}</p>
</section></div>'''

                # 14. GOOGLE WORKSPACE
                elif route == '/workspace':
                    content = render_workspace_view(service, csrf_token)

                # 15. GEMINI AI CHATBOT
                elif route in ('/chat', '/assistant'):
                    owner_id = config.OWNER_ID or 1
                    turns = service.chat_service.get_conversation_history(owner_id=owner_id, limit=50)
                    content = render_chat_view(
                        conversation_turns=turns,
                        current_role=(params.get('role') or ['general_assistant'])[0],
                        current_mode=(params.get('mode') or ['auto'])[0],
                        csrf_token=csrf_token
                    )

                elif route == '/api/chat/history':
                    owner_id = config.OWNER_ID or 1
                    turns = service.chat_service.get_conversation_history(owner_id=owner_id, limit=50)
                    self.send_json({'success': True, 'turns': turns})
                    return

                # DEFAULT: TODAY COCKPIT (Primary Operational Hub)
                else:
                    content = render_today_view(service, csrf_token, params)

                self.send_html(self.page(content, title='Operations Dashboard · Personal Work Assistant', csrf_token=csrf_token, route=route), set_sid=set_sid)

            def do_POST(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                length = min(int(self.headers.get('Content-Length', '0')), 262144)
                raw_body = self.rfile.read(length)
                content_type = self.headers.get('Content-Type', '')

                json_body = {}
                form = {}
                if 'application/json' in content_type:
                    try:
                        json_body = json.loads(raw_body.decode('utf-8'))
                    except Exception:
                        json_body = {}
                else:
                    form = parse_qs(raw_body.decode('utf-8'))

                # Authenticate and verify CSRF
                authed, set_sid, expected_csrf, _ = self.authenticate_request(query)
                if not authed:
                    if 'application/json' in content_type:
                        self.send_json({'error': 'Authentication required.'}, 403)
                    else:
                        self.send_html(self.page('<h2>Access denied</h2><p>Authentication required.</p>'), 403)
                    return

                submitted_csrf = (form.get('csrf_token') or [''])[0] or json_body.get('csrf_token') or self.headers.get('X-CSRF-Token', '')
                if not submitted_csrf or not secrets.compare_digest(submitted_csrf, expected_csrf):
                    if 'application/json' in content_type:
                        self.send_json({'error': 'CSRF validation failed.'}, 403)
                    else:
                        self.send_html(self.page('<h2>CSRF validation failed</h2><p>Form submission rejected for security.</p>'), 403)
                    return

                try:
                    # 00. GEMINI MULTI-TURN CHAT INTERFACE
                    if parsed.path == '/api/chat':
                        msg = json_body.get('message') or (form.get('message') or [''])[0]
                        role_key = json_body.get('role_key') or (form.get('role_key') or ['general_assistant'])[0]
                        task_mode = json_body.get('task_mode') or (form.get('task_mode') or ['auto'])[0]
                        custom_prompt = json_body.get('custom_system_instruction') or (form.get('custom_system_instruction') or [''])[0]
                        owner_id = config.OWNER_ID or 1

                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        try:
                            result = new_loop.run_until_complete(service.message_service.process_user_message(
                                owner_id=owner_id, text=msg, source_channel='web',
                                client_message_id=json_body.get('client_message_id'),
                                metadata={'role_key': role_key, 'task_mode': task_mode}))
                        finally:
                            new_loop.close()

                        self.send_json(result if isinstance(result, dict) else {'success': result.success, 'reply': result.reply_text, 'proposal_id': result.proposal_id})
                        return

                    elif parsed.path == '/api/chat/clear':
                        owner_id = config.OWNER_ID or 1
                        ok = service.chat_service.clear_conversation_history(owner_id=owner_id)
                        self.send_json({'success': ok})
                        return

                    elif parsed.path == '/api/chat/proposal':
                        proposal_id = json_body.get('proposal_id')
                        action = json_body.get('action')
                        owner_id = config.OWNER_ID or 1
                        if action == 'cancel':
                            service.database.cancel_nl_proposal(proposal_id, owner_id)
                            self.send_json({'success': True, 'reply': 'Confirmation cancelled.'})
                            return
                        # Workspace execution owns its atomic claim.  Do not
                        # pre-claim here: that would convert pending ->
                        # executing and make the service's replay protection
                        # reject the same proposal as already in progress.
                        proposal = service.database.get_nl_proposal(proposal_id)
                        if proposal and proposal['action_type'] == 'workspace_external_write':
                            try:
                                result = service.workspace_service.execute_action(
                                    proposal_id=proposal_id, actor='chat', owner_id=owner_id)
                                self.send_json({
                                    'success': result['success'],
                                    'reply': f"Executed workspace action '{result.get('action', 'workspace')}': success."
                                })
                            except WorkspaceActionError as exc:
                                self.send_json({'success': False, 'reply': str(exc), 'error': str(exc)})
                            return
                        claimed = service.database.claim_nl_proposal(proposal_id, owner_id)
                        if claimed['action_type'] == 'compound_plan':
                            from nlp import NaturalLanguagePipeline, ConversationPlan
                            plan = ConversationPlan.model_validate(claimed['proposal'])
                            result = asyncio.run(NaturalLanguagePipeline(service.database).execute_plan(
                                plan, service.database.active_shift()))
                            service.database.finish_nl_proposal(
                                proposal_id, 'executed' if result.success else 'failed')
                            self.send_json({'success': result.success, 'reply': result.reply})
                            return

                        if claimed['action_type'] != 'mcp_external_write':
                            # Local NLP proposals and clarifications use the same durable
                            # proposal record as Telegram; choice indices are never trusted
                            # beyond the persisted choice list.
                            from nlp import NLActionExecutor, NLInterpretation, ContextResolver
                            interpretation = NLInterpretation.model_validate(claimed['proposal'])
                            if action == 'choose':
                                index = json_body.get('choice_index')
                                if not isinstance(index, int) or not 0 <= index < len(interpretation.choices):
                                    raise ValueError('That clarification choice is no longer available.')
                                choice = interpretation.choices[index].model_dump()
                                if choice.get('case_id'):
                                    interpretation.entities.case_id = choice['case_id']
                                if choice.get('task_id'):
                                    interpretation.entities.reference = f"#{choice['task_id']}"
                                    interpretation = ContextResolver(service.database).resolve(interpretation)
                                if choice.get('test_result'):
                                    interpretation.entities.test_result = choice['test_result']
                            elif action != 'confirm':
                                raise ValueError('Unknown proposal action.')
                            interpretation.needs_confirmation = False
                            interpretation.clarification_question = None
                            try:
                                reply, _ = asyncio.run(NLActionExecutor(service.database).execute(
                                    interpretation, service.database.active_shift()))
                            except Exception:
                                service.database.finish_nl_proposal(proposal_id, 'failed')
                                raise
                            service.database.finish_nl_proposal(proposal_id, 'executed')
                            self.send_json({'success': True, 'reply': reply})
                            return
                        payload = claimed['proposal']
                        manager = service.message_service.orchestrator.mcp_manager
                        if not manager:
                            raise ValueError('MCP integration is unavailable.')
                        tool = payload.get('gemini_name') or payload.get('tool_id')
                        args = payload.get('arguments', {})
                        desc = manager.validate_tool_call(tool, args)
                        from mcp_policy import ToolPolicy, PolicyDecision
                        from mcp_registry import effective_risk_for_call, required_capabilities_for_call
                        if ToolPolicy.evaluate(desc, args).decision != PolicyDecision.CONFIRMATION_REQUIRED:
                            raise ValueError('Proposal policy changed; create a new proposal.')
                        required = sorted(required_capabilities_for_call(desc, args))
                        if required != sorted(payload.get('required_capabilities') or []):
                            raise ValueError('Proposal capability semantics changed; create a new proposal.')
                        if not set(required) <= set(payload.get('authorization_family') or []):
                            raise ValueError('Proposal authority no longer covers this exact operation.')
                        actual_hash = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
                        if payload.get('arguments_hash') != actual_hash:
                            raise ValueError('Proposal arguments changed; create a new proposal.')
                        if payload.get('risk_level') != effective_risk_for_call(desc, args).value:
                            raise ValueError('Proposal risk classification changed; create a new proposal.')
                        result = asyncio.run(manager.call_tool(tool, args))
                        service.database.record_external_write_audit(proposal_id, owner_id, {
                            'server': desc.server_name, 'canonical_tool_id': desc.canonical_id,
                            'arguments_hash': hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest(),
                            'risk': effective_risk_for_call(desc, args).value,
                            'authorization_family': payload.get('authorization_family', []),
                            'required_capabilities': required, 'execution_timestamp': datetime.now(timezone.utc).isoformat(),
                            'status': 'success' if result.success else 'failed'})
                        service.database.finish_nl_proposal(proposal_id, 'executed' if result.success else 'failed')
                        self.send_json({'success': result.success, 'reply': result.text if result.success else result.error})
                        return

                    # 0A. WORKSPACE SERVER-SIDE MUTATIONS (PROPOSE & EXECUTE)
                    elif parsed.path == '/workspace/action/propose':
                        act = json_body.get('action') or (form.get('action') or [''])[0]
                        args_data = json_body.get('args')
                        if args_data is None:
                            args_raw = (form.get('args') or ['{}'])[0]
                            args_data = json.loads(args_raw) if args_raw else {}
                        proposal = service.workspace_service.propose_action(
                            actor='dashboard', action=act, args=args_data
                        )
                        self.send_json({'success': True, 'proposal': proposal})
                        return

                    elif parsed.path == '/workspace/action/execute':
                        proposal_id = json_body.get('proposal_id') or (form.get('proposal_id') or [''])[0]
                        if json_body.get('google_access_token') or self.headers.get('Authorization'):
                            raise WorkspaceActionError('Browser-supplied OAuth tokens are not accepted.')
                        result = service.workspace_service.execute_action(
                            proposal_id=proposal_id,
                            actor='dashboard',
                            owner_id=config.OWNER_ID or None
                        )
                        self.send_json({'success': True, 'result': result})
                        return
                    # 0. QUICK SHIFT START/CLOSE FROM TODAY HERO
                    if parsed.path == '/shift/start':
                        now = datetime.now(ZoneInfo(config.TIMEZONE))
                        start_iso = now.isoformat()
                        end_iso = (now + timedelta(hours=8)).isoformat()
                        service.database.start_shift(start=start_iso, end=end_iso, actor='dashboard')
                        self.send_response(303)
                        self.send_header('Location', '/')
                        self.end_headers()
                        return

                    elif parsed.path == '/shift/close':
                        shift = service.database.active_shift()
                        if shift:
                            service.database.close_shift(shift['id'])
                        self.send_response(303)
                        self.send_header('Location', '/')
                        self.end_headers()
                        return

                    # 0B. WORKSPACE TASK IMPORT
                    elif parsed.path == '/workspace/import-task':
                        task_id = (form.get('task_id') or [''])[0]
                        title = (form.get('title') or ['Untitled Task'])[0]
                        notes = (form.get('notes') or [''])[0]
                        due = (form.get('due') or [''])[0]
                        if not task_id:
                            raise ValueError('Missing Google Task ID.')
                        key = hashlib.sha256(f'google_tasks|{task_id}'.encode('utf-8')).hexdigest()
                        text = f"Google Task: {title}"
                        if notes:
                            text += f" — {notes}"
                        now_str = datetime.now(timezone.utc).isoformat()
                        service.database.add_source_message(
                            source_type='google_tasks',
                            source_key=key,
                            chat_name='Google Tasks',
                            external_message_id=task_id,
                            occurred_at=now_str,
                            author_name='Google Workspace',
                            author_is_owner=False,
                            text=text,
                            redacted_text=text,
                            message_kind='connector',
                            media=[],
                            classification='task',
                            confidence=0.95,
                            review_status='pending',
                            metadata={'source': 'google_tasks', 'due': due, 'task_id': task_id, 'trusted': True}
                        )
                        self.send_html(self.page(f'''<h1>Task Imported to Review Inbox</h1>
<div class="card">
<p class="tag tag-ok">Successfully imported into Review Inbox</p>
<p><strong>{h(title)}</strong></p>
<p class="muted">This item is now queued in your review inbox for triage and case assignment.</p>
<p><a href="/inbox">Open Review Inbox &rarr;</a> &nbsp;|&nbsp; <a href="/workspace">Back to Google Workspace</a></p>
</div>''', csrf_token=expected_csrf))
                        return

                    # 1. CASE STATUS
                    elif parsed.path == '/case/status':
                        service.database.update_case(int(form['id'][0]), 'status', form['status'][0])

                    # 2. MERGE CASES
                    elif parsed.path == '/cases/merge':
                        service.database.merge_cases(int(form['source'][0]), int(form['target'][0]))

                    # 3. INBOX BULK PREVIEW
                    elif parsed.path == '/inbox/bulk/preview':
                        action = form.get('action', [''])[0]
                        m_ids = [int(x) for x in form.get('message_ids', []) if x.isdigit()]
                        if not m_ids:
                            raise ValueError('No messages selected.')
                        bulk_token = service.create_bulk_token({
                            'action': action,
                            'message_ids': m_ids,
                            'target_case_id': (form.get('target_case_id') or [''])[0],
                            'assign_client': (form.get('assign_client') or [''])[0],
                        }, set_sid)
                        preview_html = f'''<h1>Confirm Bulk Action</h1>
<div class="card">
<p><strong>Action:</strong> {h(action.replace('_', ' ').title())}</p>
<p><strong>Affected Records:</strong> {len(m_ids)} message(s)</p>
<p class="muted">All changes run in a single atomic transaction with rollback and undo support.</p>
<form method="post" action="/inbox/bulk/confirm">
<input type="hidden" name="csrf_token" value="{expected_csrf}">
<input type="hidden" name="bulk_token" value="{bulk_token}">
<button class="primary">Confirm and Execute</button>
<a href="/inbox" style="margin-left:12px;">Cancel</a>
</form>
</div>'''
                        self.send_html(self.page(preview_html, csrf_token=expected_csrf))
                        return

                    # 4. INBOX BULK CONFIRM
                    elif parsed.path == '/inbox/bulk/confirm':
                        bulk_token = form.get('bulk_token', [''])[0]
                        bulk_data = service.consume_bulk_token(bulk_token, set_sid)
                        if not bulk_data:
                            raise ValueError('Invalid or expired bulk confirmation token.')
                        action = bulk_data['action']
                        m_ids = bulk_data['message_ids']
                        if action == 'accept_tasks':
                            service.database.bulk_accept_as_tasks(m_ids)
                        elif action == 'accept_case_events':
                            target_c = int(bulk_data.get('target_case_id') or 0)
                            if not target_c:
                                raise ValueError('Specify a valid Target Case ID.')
                            service.database.bulk_accept_as_case_events(m_ids, target_c)
                        elif action == 'ignore':
                            service.database.bulk_ignore(m_ids)
                        elif action == 'assign_client':
                            service.database.bulk_assign_metadata(m_ids, client=bulk_data.get('assign_client'))
                        else:
                            raise ValueError('Unsupported bulk action.')

                    # 5. CLUSTER ACCEPTANCE (Defect 4 fixed!)
                    elif parsed.path == '/cluster/apply':
                        c_id = int(form['cluster_id'][0])
                        cluster = service.database.get_cluster(c_id)
                        if not cluster:
                            raise ValueError('Cluster not found.')
                        case_title = (form.get('case_title') or [None])[0] or cluster['title']
                        service.database.bulk_accept_cluster_as_case(cluster_id=c_id, case_title=case_title)

                    # 6. CLUSTER REJECT
                    elif parsed.path == '/cluster/reject':
                        c_id = int(form['cluster_id'][0])
                        service.database.reject_cluster(c_id)

                    # 7. CLUSTER ATTACH
                    elif parsed.path == '/cluster/attach':
                        c_id = int(form['cluster_id'][0])
                        target_case = int(form['case_id'][0])
                        service.database.attach_cluster_to_case(c_id, target_case)

                    elif parsed.path == '/cluster/split':
                        c_id = int(form['cluster_id'][0])
                        raw_ids = (form.get('message_ids') or [''])[0]
                        message_ids = [int(value.strip()) for value in raw_ids.split(',') if value.strip().isdigit()]
                        if not message_ids:
                            raise ValueError('Enter one or more valid message IDs to split.')
                        service.database.split_cluster(
                            c_id, message_ids, (form.get('new_title') or [None])[0] or None)

                    elif parsed.path == '/cluster/merge':
                        service.database.merge_clusters(
                            int(form['source_cluster_id'][0]),
                            int(form['target_cluster_id'][0]))

                    # 8. SHIFTS
                    elif parsed.path == '/shifts/assign-range':
                        t_id = int(form['template_id'][0])
                        s_date = form['start_date'][0]
                        e_date = form['end_date'][0]
                        assign_template_range(service.database, t_id, s_date, e_date, skip_existing_overrides=True)
                    elif parsed.path == '/shifts/override':
                        d_str = form['date'][0]
                        is_off = bool(form.get('is_day_off'))
                        service.database.set_shift_calendar_override(
                            date_str=d_str,
                            start_time=form.get('start_time', [''])[0] if not is_off else None,
                            end_time=form.get('end_time', [''])[0] if not is_off else None,
                            is_day_off=1 if is_off else 0,
                            is_explicit_override=1,
                            note=form.get('note', [''])[0] or None
                        )

                    # 9. UNDO
                    elif parsed.path == '/audit/undo':
                        last = service.database.get_last_reversible_audit()
                        if last:
                            service.database.undo_audit_record(last['id'])

                    # 10. FOLLOW-UPS & TESTS
                    elif parsed.path == '/followup/complete':
                        service.database.complete_followup(int(form['id'][0]))
                    elif parsed.path == '/followup/snooze':
                        f_id = int(form['id'][0])
                        days = int((form.get('days') or ['1'])[0])
                        service.work_item_service.snooze_followup(f_id, days=days)
                    elif parsed.path == '/test/update':
                        session_id = int(form['id'][0])
                        service.database.update_test_session(session_id, 'result', form['result'][0])
                        service.database.update_test_session(session_id, 'actual', form.get('actual', [''])[0])
                        service.database.update_test_session(session_id, 'defects', form.get('defects', [''])[0])
                        service.database.update_test_session(session_id, 'retest_required', 'on' if 'retest_required' in form else 'off')

                    # 11. REPORTS
                    elif parsed.path == '/report/edit':
                        report = service.database.report(int(form['id'][0]))
                        if not report:
                            raise ValueError('Report not found.')
                        text = form['text'][0]
                        new_id = service.database.save_report(
                            report['shift_id'], report['kind'], text, report['style'],
                            source_report_id=report['id'])
                        shift = service.database.shift(report['shift_id']) or {}
                        activities = service.database.activities(report['shift_id'])
                        tasks = service.database.tasks_for_shift(report['shift_id'])
                        cases = service.database.cases_for_shift(report['shift_id'])
                        tests = service.database.test_sessions(report['shift_id'])
                        followups = service.database.list_followups(limit=50)
                        from report_validator import ReportValidator
                        validation = ReportValidator.validate(
                            report['kind'], text, shift, activities, tasks, cases, tests, followups)
                        service.database.save_report_validation(
                            new_id, validation.is_valid,
                            [warning.__dict__ for warning in validation.warnings],
                            validation.verified_metrics)
                        for item in validation.provenance_links:
                            service.database.record_report_provenance(
                                new_id, item['section_name'], item['record_type'],
                                item['record_id'], item.get('detail'))
                    elif parsed.path == '/report/finalize':
                        r_id = int(form['id'][0])
                        val_res = service.database.get_report_validation(r_id)
                        acknowledged = bool(form.get('acknowledge_errors'))
                        if val_res and not val_res.get('is_valid') and not acknowledged:
                            raise ValueError('Report contains error-level validation warnings. Acknowledge warnings to finalize.')
                        service.database.finalize(
                            r_id, acknowledge_errors=acknowledged, require_validation=True)

                    # 12. SETTINGS
                    elif parsed.path == '/settings':
                        service.database.set_setting('mask_client_names', 'true' if 'mask_clients' in form else 'false')

                    # 13. REQUIREMENTS CREATE
                    elif parsed.path == '/requirements/create':
                        title = form['title'][0]
                        owner_raw = (form.get('owner_member_id') or [''])[0]
                        owner_id = int(owner_raw) if owner_raw.isdigit() else None
                        service.work_item_service.create_requirement(
                            title=title,
                            client=(form.get('client') or [''])[0] or None,
                            ticket=(form.get('ticket') or [''])[0] or None,
                            user_story=(form.get('user_story') or [''])[0] or None,
                            acceptance_criteria=(form.get('acceptance_criteria') or [''])[0] or None,
                            owner_member_id=owner_id,
                            priority=int((form.get('priority') or ['2'])[0])
                        )

                    # 14. MEMBERS & ROLES CREATE
                    elif parsed.path in ('/team/members/create', '/members/create'):
                        name = form['name'][0]
                        email = (form.get('email') or [''])[0] or None
                        tg = (form.get('telegram_handle') or [''])[0] or None
                        roles_raw = form.get('roles') or form.get('role') or []
                        roles = [r for r in roles_raw if r]
                        service.member_service.add_member(name=name, email=email, telegram_handle=tg, roles=roles)

                    elif parsed.path in ('/team/roles/create', '/roles/create'):
                        name = form['name'][0]
                        description = (form.get('description') or [''])[0] or None
                        service.member_service.add_role(name=name, description=description)

                    # 14B. WORKFLOWS & STAGES
                    elif parsed.path == '/workflows/create':
                        name = form['name'][0]
                        work_type = (form.get('work_type') or ['requirement'])[0]
                        is_default = (form.get('is_default') or ['0'])[0] == '1'
                        description = (form.get('description') or [''])[0] or None
                        service.workflow_service.create_template(
                            name=name, description=description, work_type=work_type, is_default=is_default
                        )

                    elif parsed.path == '/workflows/stages/create':
                        t_id = int(form['template_id'][0])
                        name = form['name'][0]
                        order = int((form.get('stage_order') or ['1'])[0])
                        role = (form.get('expected_role') or [''])[0] or ''
                        dur = float((form.get('expected_duration_hours') or ['0'])[0])
                        is_w = (form.get('is_waiting') or ['0'])[0] == '1'
                        desc = (form.get('description') or [''])[0] or ''
                        service.workflow_service.add_stage(
                            template_id=t_id, name=name, stage_order=order,
                            description=desc, expected_role=role,
                            expected_duration_hours=dur, is_waiting=is_w
                        )

                    elif parsed.path == '/workflow/stage/transition':
                        e_type = form['entity_type'][0]
                        raw_id = (form.get('entity_id') or form.get('id') or ['0'])[0]
                        e_id = int(raw_id) if raw_id.isdigit() else 0
                        raw_st = (form.get('new_stage_id') or form.get('stage_id') or ['0'])[0]
                        st_id = int(raw_st) if raw_st.isdigit() else 0
                        note = (form.get('note') or [''])[0] or None
                        service.workflow_service.transition_stage(
                            entity_type=e_type, entity_id=e_id, new_stage_id=st_id, note=note
                        )

                    # 14C. TASKS & CASES & WORK ITEMS
                    elif parsed.path == '/tasks/create':
                        title = form['title'][0]
                        client = (form.get('client') or [''])[0] or None
                        ticket = (form.get('ticket') or [''])[0] or None
                        due = (form.get('due_date') or [''])[0] or None
                        prio = int((form.get('priority') or ['2'])[0])
                        project = (form.get('product') or form.get('project') or [''])[0] or None
                        service.task_service.create_task(title=title, priority=prio, client=client, ticket=ticket, due_date=due, project=project, actor='dashboard')

                    elif parsed.path == '/cases/create':
                        title = form['title'][0]
                        client = (form.get('client') or [''])[0] or None
                        ticket = (form.get('ticket') or [''])[0] or None
                        channel = (form.get('channel') or [''])[0] or None
                        prio = int((form.get('priority') or ['1'])[0])
                        service.case_service.create_case(title=title, client=client, ticket=ticket, channel=channel, priority=prio)

                    elif parsed.path == '/work/item/update':
                        e_type = form['entity_type'][0]
                        e_id = int((form.get('entity_id') or form.get('id') or ['0'])[0])
                        kwargs = {}
                        for k, v in form.items():
                            if k not in ('csrf_token', 'entity_type', 'entity_id', 'id'):
                                val = v[0] if v else ''
                                if val != '':
                                    kwargs[k] = val
                        service.work_item_service.update_work_item(e_type, e_id, **kwargs)

                    elif parsed.path == '/work/item/delete':
                        e_type = form['entity_type'][0]
                        e_id = int((form.get('entity_id') or form.get('id') or ['0'])[0])
                        service.work_item_service.delete_work_item(e_type, e_id)

                    # 15. TEST CASES CREATE
                    elif parsed.path == '/tests/cases/create':
                        title = form['title'][0]
                        req_raw = (form.get('requirement_id') or [''])[0]
                        req_id = int(req_raw) if req_raw.isdigit() else None
                        service.work_item_service.add_test_case(
                            title=title,
                            requirement_id=req_id,
                            objective=(form.get('objective') or [''])[0] or None,
                            steps=(form.get('steps') or [''])[0] or None,
                            expected_result=(form.get('expected_result') or [''])[0] or None,
                            priority=int((form.get('priority') or ['2'])[0])
                        )

                    # 16. TEST EXECUTIONS RECORD
                    elif parsed.path == '/tests/executions/record':
                        tc_id = int(form['test_case_id'][0])
                        result = form['result'][0]
                        actual = (form.get('actual_result') or [''])[0] or None
                        service.work_item_service.record_test_execution(
                            test_case_id=tc_id,
                            result=result,
                            actual_result=actual
                        )

                    # 17. BLOCKERS CREATE
                    elif parsed.path == '/blockers/create':
                        ent_type = form['entity_type'][0]
                        ent_id = int(form['entity_id'][0])
                        dep_type = form['dependency_type'][0]
                        desc = form['description'][0]
                        service.work_item_service.add_blocker(
                            entity_type=ent_type,
                            entity_id=ent_id,
                            dependency_type=dep_type,
                            description=desc
                        )

                    # 18. BLOCKERS RESOLVE
                    elif parsed.path == '/blockers/resolve':
                        b_id = int(form['id'][0])
                        notes = (form.get('resolution_notes') or [''])[0] or None
                        service.work_item_service.resolve_blocker(b_id, resolution_notes=notes)

                    # 19. TEST CONDITIONS
                    elif parsed.path == '/tests/conditions/create':
                        req_id = int(form['requirement_id'][0])
                        title = form['title'][0]
                        category = (form.get('category') or ['functional'])[0]
                        risk_level = (form.get('risk_level') or ['medium'])[0]
                        desc = (form.get('description') or [''])[0] or None
                        service.work_item_service.add_test_condition(
                            requirement_id=req_id, title=title, description=desc,
                            category=category, risk_level=risk_level, status='approved'
                        )

                    elif parsed.path == '/tests/conditions/approve':
                        cond_id = int(form['id'][0])
                        service.work_item_service.update_test_condition(cond_id, status='approved')

                    elif parsed.path == '/tests/conditions/delete':
                        cond_id = int(form['id'][0])
                        service.work_item_service.delete_test_condition(cond_id)

                    # 20. DEFECTS
                    elif parsed.path == '/tests/defects/create':
                        title = form['title'][0]
                        severity = (form.get('severity') or ['major'])[0]
                        priority = int((form.get('priority') or ['2'])[0])
                        req_raw = (form.get('requirement_id') or [''])[0]
                        req_id = int(req_raw) if req_raw.isdigit() else None
                        tc_raw = (form.get('test_case_id') or [''])[0]
                        tc_id = int(tc_raw) if tc_raw.isdigit() else None
                        steps = (form.get('steps_to_reproduce') or [''])[0] or None
                        actual = (form.get('actual_result') or [''])[0] or None
                        expected = (form.get('expected_result') or [''])[0] or None
                        build = (form.get('build_found') or [''])[0] or None
                        env = (form.get('environment') or [''])[0] or None
                        service.work_item_service.create_defect(
                            title=title, severity=severity, priority=priority,
                            requirement_id=req_id, test_case_id=tc_id,
                            steps_to_reproduce=steps, actual_result=actual,
                            expected_result=expected, build_found=build, environment=env
                        )

                    elif parsed.path == '/tests/defects/retest':
                        def_id = int(form['defect_id'][0])
                        res = form['result'][0]
                        build_fixed = (form.get('build_fixed') or [''])[0] or None
                        notes = (form.get('retest_notes') or [''])[0] or None
                        service.work_item_service.retest_defect(
                            defect_id=def_id, result=res, build_fixed=build_fixed, retest_notes=notes
                        )

                    elif parsed.path == '/tests/defects/reopen':
                        def_id = int(form['defect_id'][0])
                        reason = (form.get('reason') or [''])[0] or None
                        service.work_item_service.reopen_defect(defect_id=def_id, reason=reason)

                    # 21. ARTIFACTS
                    elif parsed.path == '/artifacts/create':
                        ent_type = form['entity_type'][0]
                        ent_id = int(form['entity_id'][0])
                        a_type = (form.get('artifact_type') or ['doc'])[0]
                        name = form['name'][0]
                        url = (form.get('url') or [''])[0] or None
                        path = (form.get('path') or [''])[0] or None
                        service.work_item_service.add_artifact(
                            entity_type=ent_type, entity_id=ent_id, artifact_type=a_type,
                            name=name, url=url, path=path
                        )

                    elif parsed.path == '/artifacts/delete':
                        art_id = int(form['id'][0])
                        service.work_item_service.delete_artifact(art_id)
                    else:
                        raise ValueError('Unknown action.')
                except (ValueError, KeyError) as exc:
                    self.send_html(self.page(f'<h2>Could not update</h2><p>{h(exc)}</p>', csrf_token=expected_csrf), 400)
                    return

                target = self.headers.get('Referer') or '/inbox'
                self.send_response(303)
                self.send_header('Location', target)
                self.end_headers()

            def case_card(self, item, csrf_token=''):
                options = ''.join(f'<option value="{s}" {"selected" if s == item["status"] else ""}>{s}</option>' for s in STATUSES)
                return f'''<section class="card"><h2><a href="/case?id={item['id']}">CASE-{item['id']}: {h(item['title'])}</a></h2>
<p><span class="tag">{h(item['status'])}</span><span class="tag">{h(item['participation'])}</span></p>
<p>{h(item.get('client') or 'Client unspecified')} · {h(item.get('channel') or 'Channel unspecified')}</p>
<p class="muted">Next: {h(item.get('next_action') or 'not recorded')}<br>Waiting on: {h(item.get('waiting_on') or 'nobody recorded')}</p>
<form method="post" action="/case/status"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{item['id']}"><select name="status">{options}</select> <button>Update</button></form></section>'''

            def followup_card(self, item, csrf_token=''):
                return f'''<section class="card"><h2>Follow-up #{item['id']}</h2><p>CASE-{item['case_id']}: {h(item['title'])}</p><p>Due {h(item['due_at'])}</p><p>{h(item.get('note'))}</p>
<form method="post" action="/followup/complete"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{item['id']}"><button>Complete</button></form></section>'''

            def test_card(self, item, csrf_token=''):
                options = ''.join(f'<option value="{value}" {"selected" if value == item["result"] else ""}>{value}</option>'
                                  for value in ('not_run', 'passed', 'failed', 'partial', 'blocked', 'inconclusive'))
                return f'''<section class="card"><h2>TEST-{item['id']}: {h(item['scenario'])}</h2>
<p>{h(item.get('environment'))} · build {h(item.get('build'))} · CASE-{h(item.get('case_id'))}</p>
<form method="post" action="/test/update"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{item['id']}">
<label>Result <select name="result">{options}</select></label><br>
<label>Actual<br><textarea name="actual" rows="3" style="width:100%">{h(item.get('actual'))}</textarea></label><br>
<label>Defects<br><textarea name="defects" rows="2" style="width:100%">{h(item.get('defects'))}</textarea></label><br>
<label><input type="checkbox" name="retest_required" {'checked' if item.get('retest_required') else ''}> Retest required</label><br><button>Save test</button></form></section>'''

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name='local-dashboard', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)
            self.server = None
            self.thread = None


def main():
    import argparse
    import os
    import sys
    from database import Database
    import config

    parser = argparse.ArgumentParser(description='Work Operations Hub Dashboard')
    parser.add_argument('--host', default=os.getenv('DASHBOARD_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('DASHBOARD_PORT', '8765')))
    args = parser.parse_args()

    db = Database(config.DB_PATH)
    service = DashboardService(db, host=args.host, port=args.port)
    service.start()
    print(f"DASHBOARD_TOKEN={service.token}", flush=True)
    print(f"Dashboard running on http://{args.host}:{args.port}/?token={service.token}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()


if __name__ == '__main__':
    main()
