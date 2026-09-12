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
from application.workflow_service import WorkflowService
from application.member_service import MemberService
from workspace_view import render_workspace_view

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
        self.workflow_service = WorkflowService(database)
        self.member_service = MemberService(database)
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

                # In web preview or AI Studio iframe environment, auto-provision session
                if os.getenv('ALLOW_IFRAME', 'true').lower() in ('true', '1', 'yes') or os.getenv('ENV') == 'production':
                    new_sid, csrf = service.create_session()
                    return True, new_sid, csrf, False

                return False, None, None, False

            def send_html(self, body: str, status=200, set_sid: str | None = None):
                set_sid = set_sid or getattr(self, 'pending_sid', None)
                content = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                if os.getenv('ALLOW_IFRAME', 'true').lower() in ('true', '1', 'yes'):
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
                    self.send_header('Set-Cookie', f'dashboard_session={set_sid}; Path=/; HttpOnly; SameSite=Strict')
                self.end_headers()
                self.wfile.write(content)

            def page(self, content, title='Personal Work Assistant', csrf_token: str = '', route: str = '/'):
                route_path = (route or '/').split('?')[0]
                pillar_today = route_path in ('/', '/overview', '/today')
                pillar_work = route_path in ('/work', '/work-items', '/kanban', '/inbox', '/cases', '/case')
                pillar_testing = route_path in ('/tests', '/testing', '/evidence')
                pillar_workspace = route_path == '/workspace'
                pillar_ops = not (pillar_today or pillar_work or pillar_testing or pillar_workspace)

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
        <span class="brand-name">Personal Work Assistant</span>
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
                    self.send_header('Set-Cookie', f'dashboard_session={set_sid}; Path=/; HttpOnly; SameSite=Strict')
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

                # 1. KANBAN VIEW (Visual column display)
                if route == '/kanban':
                    cols_html = []
                    for col_key, col_title in KANBAN_COLUMNS:
                        col_cases = [c for c in cases if c['status'] == col_key]
                        items_html = []
                        for c in col_cases:
                            items_html.append(f'''<div class="kanban-item">
<strong><a href="/case?id={c['id']}">CASE-{c['id']}: {h(c['title'])}</a></strong>
<p class="muted">Client: {h(c.get('client') or 'General')} | Priority: P{c['priority']}</p>
<span class="tag">{h(c['status'])}</span>
<form method="post" action="/case/status"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="id" value="{c['id']}"><select name="status" onchange="this.form.submit()">
{''.join(f'<option value="{s}" {"selected" if s==c["status"] else ""}>{s}</option>' for s in STATUSES)}
</select></form></div>''')
                        cols_html.append(f'''<div class="kanban-col">
<h3>{col_title} <span class="tag">{len(col_cases)}</span></h3>
{''.join(items_html) or '<p class="muted" style="text-align:center;padding:20px 0;">Empty</p>'}
</div>''')
                    content = render_work_sub_nav('kanban', len(inbox), len(cases)) + f'''<div class="today-hero">
  <div>
    <h1>Work Operations · Kanban Board</h1>
    <p class="muted">Visual status tracking of active cases across operational stages.</p>
  </div>
</div>
<div class="kanban-board">{''.join(cols_html)}</div>'''

                # 2. REVIEW INBOX & BULK ACTIONS
                elif route == '/inbox':
                    client_filter = (params.get('client') or [''])[0]
                    product_filter = (params.get('product') or [''])[0]
                    category_filter = (params.get('classification') or [''])[0]
                    search_q = (params.get('q') or [''])[0]
                    review_status = (params.get('review_status') or ['pending'])[0]
                    date_from = (params.get('date_from') or [''])[0]
                    date_to = (params.get('date_to') or [''])[0]
                    min_conf_str = (params.get('min_confidence') or [''])[0]
                    cluster_id_str = (params.get('cluster_id') or [''])[0]
                    import_id_str = (params.get('import_id') or [''])[0]
                    has_media_str = (params.get('has_media') or [''])[0]
                    has_ticket_str = (params.get('has_ticket') or [''])[0]
                    ext_author = (params.get('external_author') or [''])[0]

                    page_num = max(1, int((params.get('page') or ['1'])[0]))
                    limit = 50
                    offset = (page_num - 1) * limit

                    status_query = review_status if review_status != 'all' else None
                    min_conf = float(min_conf_str) if min_conf_str else None
                    c_id = int(cluster_id_str) if cluster_id_str else None
                    imp_id = int(import_id_str) if import_id_str else None
                    h_media = True if has_media_str == '1' else None
                    h_ticket = True if has_ticket_str == '1' else None
                    author_owner = False if ext_author == '1' else None

                    filtered_items, total_count = service.database.filter_inbox(
                        review_status=status_query,
                        author_is_owner=author_owner,
                        import_id=imp_id,
                        start_date=date_from or None,
                        end_date=date_to or None,
                        client=client_filter or None,
                        product=product_filter or None,
                        classification=category_filter or None,
                        min_confidence=min_conf,
                        cluster_id=c_id,
                        has_media=h_media,
                        has_ticket=h_ticket,
                        search_text=search_q or None,
                        offset=offset,
                        limit=limit
                    )

                    filter_form = f'''<form class="filter-bar" method="get" action="/inbox" style="display:flex;gap:8px;flex-wrap:wrap;">
<input name="q" placeholder="Text search..." value="{h(search_q)}" style="width:140px">
<input name="client" placeholder="Client..." value="{h(client_filter)}" style="width:110px">
<input name="product" placeholder="Product..." value="{h(product_filter)}" style="width:110px">
<input type="date" name="date_from" value="{h(date_from)}" title="Date From">
<input type="date" name="date_to" value="{h(date_to)}" title="Date To">
<select name="classification">
<option value="">All Categories</option>
<option value="support" {"selected" if category_filter=="support" else ""}>Support</option>
<option value="testing" {"selected" if category_filter=="testing" else ""}>Testing</option>
<option value="note" {"selected" if category_filter=="note" else ""}>Note</option>
</select>
<select name="review_status">
<option value="pending" {"selected" if review_status=="pending" else ""}>Pending</option>
<option value="accepted" {"selected" if review_status=="accepted" else ""}>Accepted</option>
<option value="ignored" {"selected" if review_status=="ignored" else ""}>Ignored</option>
<option value="all" {"selected" if review_status=="all" else ""}>All Statuses</option>
</select>
<input name="min_confidence" placeholder="Min Conf (0.0-1.0)..." value="{h(min_conf_str)}" style="width:120px">
<input name="cluster_id" placeholder="Cluster ID..." value="{h(cluster_id_str)}" style="width:90px">
<button class="primary">Filter</button>
<a href="/inbox" class="button">Reset</a>
</form>'''

                    rows_html = []
                    for item in filtered_items:
                        txt = item.get('redacted_text') or item.get('text') or '(media only)'
                        meta = json.loads(item['metadata_json']) if item.get('metadata_json') else {}
                        rows_html.append(f'''<tr>
<td><input type="checkbox" name="message_ids" value="{item['id']}"></td>
<td><strong>#{item['id']}</strong></td>
<td><span class="tag">{h(item.get('classification') or 'note')}</span></td>
<td>{h(meta.get('client') or '—')}</td>
<td>{h(meta.get('product') or '—')}</td>
<td>{h(item.get('occurred_at', '')[:16].replace('T', ' '))}</td>
<td>{h(txt[:180])}</td>
</tr>''')

                    total_pages = max(1, (total_count + limit - 1) // limit)
                    active_query = {k: v[0] for k, v in params.items() if v and v[0] and k != 'page'}
                    prev_query = {**active_query, 'page': page_num - 1}
                    next_query = {**active_query, 'page': page_num + 1}
                    prev_link = f'<a href="/inbox?{urlencode(prev_query)}">« Previous</a>' if page_num > 1 else '<span class="muted">« Previous</span>'
                    next_link = f'<a href="/inbox?{urlencode(next_query)}">Next »</a>' if page_num < total_pages else '<span class="muted">Next »</span>'
                    pagination_html = f'<div style="margin-top:12px;display:flex;gap:16px;align-items:center;">{prev_link} <span>Page {page_num} of {total_pages}</span> {next_link}</div>'

                    bulk_actions = f'''<form method="post" action="/inbox/bulk/preview"><input type="hidden" name="csrf_token" value="{csrf_token}">
<div style="background:#fff;border:1px solid #dce2e8;border-radius:8px;padding:12px;margin-bottom:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
<strong>Bulk Actions on Selected:</strong>
<button name="action" value="accept_tasks">Accept as Tasks</button>
<span>or into Case ID: <input type="number" name="target_case_id" min="1" style="width:70px"></span>
<button name="action" value="accept_case_events">Attach as Case Events</button>
<button name="action" value="ignore">Ignore Selected</button>
<input name="assign_client" placeholder="Assign Client..." style="width:120px">
<button name="action" value="assign_client">Set Client</button>
</div>
<table class="card">
<tr><th><input type="checkbox" onclick="document.querySelectorAll('input[name=message_ids]').forEach(c=>c.checked=this.checked)"></th>
<th>ID</th><th>Type</th><th>Client</th><th>Product</th><th>When</th><th>Text Snippet</th></tr>
{''.join(rows_html) or '<tr><td colspan="7" class="muted" style="text-align:center;">No messages matching filter.</td></tr>'}
</table>
{pagination_html}
</form>'''

                    content = render_work_sub_nav('inbox', len(inbox), len(cases)) + f'''<div class="today-hero">
  <div>
    <h1>Work Operations · Review Inbox ({total_count} Messages)</h1>
    <p class="muted">Safe two-step bulk review with preview confirmation and atomic undo.</p>
  </div>
</div>
{filter_form}
{bulk_actions}'''

                # 3. HISTORICAL CLUSTERING
                elif route == '/clusters':
                    clusters = service.database.get_clusters()
                    cluster_cards = []
                    for cl in clusters:
                        c_id = cl['id']
                        status = cl.get('status', 'pending')
                        items = service.database.get_cluster_items(c_id)
                        snippet_html = ''.join(f"<li>#{m['id']} [{m.get('occurred_at','')[:16]}]: {h((m.get('text') or '')[:80])}</li>" for m in items[:3])

                        actions_html = ''
                        if status == 'pending':
                            actions_html = f'''<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">
<form method="post" action="/cluster/apply"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="cluster_id" value="{c_id}"><button class="primary">Accept as Case</button></form>
<form method="post" action="/cluster/reject"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="cluster_id" value="{c_id}"><button class="danger">Reject</button></form>
<form method="post" action="/cluster/attach" style="display:inline-flex;gap:4px;"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="cluster_id" value="{c_id}"><input type="number" name="case_id" placeholder="Case ID" style="width:80px" required><button>Attach to Case</button></form>
<form method="post" action="/cluster/split" style="display:flex;gap:4px;flex-wrap:wrap;"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="cluster_id" value="{c_id}"><input name="message_ids" placeholder="Message IDs: 12,14" required><input name="new_title" placeholder="New title"><button>Split selected</button></form>
<form method="post" action="/cluster/merge" style="display:flex;gap:4px;"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="source_cluster_id" value="{c_id}"><input type="number" name="target_cluster_id" placeholder="Target cluster ID" required><button>Merge</button></form>
</div>'''
                        else:
                            actions_html = f'<p class="tag tag-ok">Status: {h(status)}' + (f' (CASE-{cl.get("case_id")})' if cl.get('case_id') else '') + '</p>'

                        cluster_cards.append(f'''<div class="card" style="margin-bottom:14px;">
<div style="display:flex;justify-content:space-between;align-items:center;">
<h3>Cluster #{c_id}: {h(cl['title'])}</h3>
<div><span class="tag tag-ok">{round(cl.get('confidence',0)*100)}% Conf</span>
<span class="tag">{status}</span>
<span class="tag">{len(items)} messages</span></div>
</div>
<p><strong>Client:</strong> {h(cl.get('suggested_client') or 'General')} | <strong>Product:</strong> {h(cl.get('suggested_product') or '—')}</p>
<p class="muted">Reason: {h(cl.get('reason') or '')}</p>
<ul>{snippet_html}</ul>
{actions_html}
</div>''')

                    content = f'''<h1>Historical Message Clusters</h1>
<p class="muted">Deterministic clusters from reply chains, ticket IDs, and temporal proximity. No automated mutations.</p>
{''.join(cluster_cards) or '<p class="muted">No cluster suggestions generated yet. Run: python scripts/cluster_history.py --apply-suggestions</p>'}'''

                # 4. SHIFT CALENDAR & TEMPLATES
                elif route == '/shifts':
                    today_str = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m-%d')
                    templates = service.database.list_shift_templates()
                    upcoming = preview_calendar_week(service.database, reference_date=today_str, days=7)
                    missing = check_missing_shift_assignments(service.database, days_ahead=7)

                    tmpl_rows = ''.join(f"<tr><td><strong>{h(t['name'])}</strong></td><td>{h(t['start_time'])}–{h(t['end_time'])}</td><td>{h(t['lunch_time'] or 'Flexible')}</td><td>{h(t['timezone'])}</td></tr>" for t in templates)

                    preview_rows = ''.join(f'''<tr>
<td>{h(u['date'])} ({h(u.get('weekday_name',''))})</td>
<td>{'<span class="tag tag-warn">Day Off</span>' if u.get('is_day_off') else f"{h(u.get('start_time'))}–{h(u.get('end_time'))}"}</td>
<td>{h(u.get('lunch_time') or 'Flexible')}</td>
<td><span class="tag">{h(u.get('source'))}</span></td>
<td>{h(u.get('note') or '—')}</td>
</tr>''' for u in upcoming)

                    template_options = ''.join(f'<option value="{t["id"]}">{h(t["name"])} ({h(t["start_time"])}–{h(t["end_time"])})</option>' for t in templates)

                    content = f'''<h1>Shift Calendar & Templates</h1>
<div class="grid">
<div class="card">
<h2>Upcoming 7 Days</h2>
<table><tr><th>Date</th><th>Shift</th><th>Lunch</th><th>Source</th><th>Notes</th></tr>{preview_rows}</table>
{f'<p class="muted" style="color:#d9383a;margin-top:10px;">⚠️ Unassigned days: {", ".join(missing)}</p>' if missing else '<p class="muted tag-ok" style="display:inline-block;padding:4px 8px;margin-top:10px;">All upcoming 7 days assigned</p>'}
</div>
<div class="card">
<h2>Assign Template to Date Range</h2>
<form method="post" action="/shifts/assign-range"><input type="hidden" name="csrf_token" value="{csrf_token}">
<label>Template:<br><select name="template_id" style="width:100%">{template_options}</select></label><br><br>
<label>Start Date: <input type="date" name="start_date" required value="{today_str}"></label><br><br>
<label>End Date: &nbsp;<input type="date" name="end_date" required value="{today_str}"></label><br><br>
<button class="primary">Assign Range</button>
</form>
<hr style="margin:16px 0;border:none;border-top:1px solid #eee;">
<h2>Single Date Override</h2>
<form method="post" action="/shifts/override"><input type="hidden" name="csrf_token" value="{csrf_token}">
<label>Date: <input type="date" name="date" required value="{today_str}"></label><br><br>
<label>Start: <input type="time" name="start_time" value="10:00"></label>
<label>End: <input type="time" name="end_time" value="19:00"></label><br><br>
<label><input type="checkbox" name="is_day_off" value="1"> Mark as Day Off</label><br><br>
<input name="note" placeholder="Reason / note..." style="width:100%"><br><br>
<button>Save Override</button>
</form>
</div>
</div>
<div class="card" style="margin-top:16px;">
<h2>Shift Templates</h2>
<table><tr><th>Template Name</th><th>Shift Hours</th><th>Lunch Time</th><th>Timezone</th></tr>{tmpl_rows}</table>
</div>'''

                # 5. AUDIT LOG & SAFE UNDO
                elif route == '/audit':
                    audit_rows = service.database.get_audit_log(limit=50)
                    last_rev = service.database.get_last_reversible_audit()
                    rows_html = []
                    for a in audit_rows:
                        is_reverted = bool(a.get('reverted_at'))
                        rev_badge = '<span class="tag tag-warn">Reverted</span>' if is_reverted else '<span class="tag tag-ok">Active</span>'
                        rows_html.append(f'''<tr>
<td>{h(a['created_at'][:19].replace('T',' '))}</td>
<td><code>{h(a['correlation_id'])}</code></td>
<td><strong>{h(a['operation_type'])}</strong></td>
<td>{h(a['affected_table'])} #{a['record_id']}</td>
<td>{h(a['actor'])}</td>
<td>{rev_badge}</td>
</tr>''')
                    content = f'''<h1>Audit Log & Safe Undo</h1>
<div class="card" style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
<div>
<strong>Last Reversible Action:</strong> {h(last_rev['operation_type'] + ' on ' + last_rev['affected_table'] if last_rev else 'None')}
</div>
{f'<form method="post" action="/audit/undo"><input type="hidden" name="csrf_token" value="{csrf_token}"><button class="danger">Undo Last Action</button></form>' if last_rev else '<button disabled>Nothing to Undo</button>'}
</div>
<div class="card">
<table><tr><th>When</th><th>Batch ID</th><th>Operation</th><th>Target</th><th>Actor</th><th>Status</th></tr>
{''.join(rows_html) or '<tr><td colspan="6" class="muted" style="text-align:center;">No audit records.</td></tr>'}
</table></div>'''

                # 6. CASE DETAILS
                elif route == '/case':
                    case_id = int((params.get('id') or ['0'])[0])
                    item = service.database.case(case_id)
                    if not item:
                        self.send_html(self.page('<h2>Case not found</h2>', csrf_token=csrf_token), 404, set_sid=set_sid)
                        return
                    events = service.database.case_events(case_id)
                    case_tests = service.database.test_sessions(case_id=case_id)
                    evidence = service.database.evidence(case_id=case_id)
                    timeline = ''.join(f"<tr><td>{h(e['occurred_at'][:16].replace('T',' '))}</td><td>{h(e['event_type'])}</td><td>{h(e['detail'])}</td></tr>" for e in events)
                    evidence_rows = ''.join(f"<li><a href=\"/evidence?id={e['id']}\">Evidence #{e['id']} · {h(Path(e.get('path') or 'retained-metadata').name)}</a> — {h(e.get('caption'))}</li>" for e in evidence)
                    test_rows = ''.join(f"<li>TEST-{t['id']} [{h(t['result'])}] {h(t['scenario'])}</li>" for t in case_tests)
                    content = render_work_sub_nav('cases', len(inbox), len(cases)) + f'''<div class="today-hero">
  <div>
    <h1>Work Operations · CASE-{case_id}</h1>
    <p class="muted">Detailed operational case history, events, testing logs, and evidence.</p>
  </div>
</div>
{self.case_card(item, csrf_token)}
<section class="card"><h2>Timeline</h2><table><tr><th>When</th><th>Type</th><th>Detail</th></tr>{timeline}</table></section>
<div class="grid"><section class="card"><h2>Testing</h2><ul>{test_rows or '<li>None</li>'}</ul></section>
<section class="card"><h2>Evidence</h2><ul>{evidence_rows or '<li>None</li>'}</ul></section></div>'''

                # 7. CASES LIST
                elif route == '/cases':
                    cards = ''.join(self.case_card(item, csrf_token) for item in cases) or '<p>No approved cases.</p>'
                    content = render_work_sub_nav('cases', len(inbox), len(cases)) + f'''<div class="today-hero">
  <div>
    <h1>Work Operations · Cases & Issues ({len(cases)})</h1>
    <p class="muted">Approved operational cases, escalation tracking, and duplicate merge tools.</p>
  </div>
</div>
<section class="card"><h2>Merge duplicates</h2>
<form method="post" action="/cases/merge"><input type="hidden" name="csrf_token" value="{csrf_token}">
Source case <input name="source" type="number" min="1" required> into target <input name="target" type="number" min="1" required> <button>Merge</button></form></section>
<div class="grid">{cards}</div>'''

                # 7B. UNIFIED WORK ITEMS VIEW
                elif route == '/work-items':
                    type_filter = (params.get('type') or [''])[0]
                    status_filter = (params.get('status') or [''])[0]
                    q_filter = (params.get('q') or [''])[0].strip().lower()

                    items = service.work_item_service.list_work_items()
                    if type_filter:
                        items = [i for i in items if i['entity_type'] == type_filter]
                    if status_filter:
                        items = [i for i in items if i['operational_status'] == status_filter]
                    if q_filter:
                        items = [i for i in items if q_filter in i['title'].lower() or q_filter in (i.get('client') or '').lower() or q_filter in (i.get('product') or '').lower()]

                    all_blockers = service.work_item_service.get_blockers(status='active')
                    all_members = service.member_service.get_members()

                    filter_bar = f'''<div class="filter-bar">
<form method="get" action="/work-items" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;width:100%;">
<label>Type: <select name="type">
<option value="">All Types</option>
<option value="requirement" {'selected' if type_filter=='requirement' else ''}>Requirements</option>
<option value="task" {'selected' if type_filter=='task' else ''}>Tasks</option>
<option value="case" {'selected' if type_filter=='case' else ''}>Cases</option>
</select></label>
<label>Status: <select name="status">
<option value="">All Statuses</option>
<option value="active" {'selected' if status_filter=='active' else ''}>Active</option>
<option value="blocked" {'selected' if status_filter=='blocked' else ''}>Blocked</option>
<option value="completed" {'selected' if status_filter=='completed' else ''}>Completed</option>
</select></label>
<label>Search: <input type="search" name="q" value="{h(q_filter)}" placeholder="Title, client, product..."></label>
<button type="submit">Filter</button>
<a href="/work-items" style="margin-left:auto;">Reset</a>
</form>
</div>'''

                    create_req_card = f'''<section class="card" style="margin-bottom:16px;">
<h2>Create New Requirement</h2>
<form method="post" action="/requirements/create">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;">
<input name="title" placeholder="Requirement title *" required>
<input name="client" placeholder="Client / Product">
<input name="ticket" placeholder="Issue / Ticket #">
</div>
<div style="margin-top:8px;">
<textarea name="user_story" rows="2" style="width:100%;box-sizing:border-box;" placeholder="User Story: As a [role], I want [capability] so that [benefit]..."></textarea>
</div>
<div style="margin-top:8px;">
<textarea name="acceptance_criteria" rows="2" style="width:100%;box-sizing:border-box;" placeholder="Acceptance Criteria: Given [context], When [action], Then [outcome]..."></textarea>
</div>
<div style="margin-top:8px;display:flex;gap:10px;align-items:center;">
<label>Owner: <select name="owner_member_id"><option value="">Unassigned</option>
{''.join(f'<option value="{m["id"]}">{h(m["name"])}</option>' for m in all_members)}
</select></label>
<label>Priority: <select name="priority"><option value="1">P1 (Urgent)</option><option value="2" selected>P2 (Normal)</option><option value="3">P3 (Low)</option></select></label>
<button class="primary" style="margin-left:auto;">Create Requirement</button>
</div>
</form>
</section>'''

                    rows_html = []
                    for it in items:
                        op_status = it.get('operational_status') or 'active'
                        if op_status == 'blocked':
                            status_badge = '<span class="tag tag-err">Blocked</span>'
                        elif op_status in ('completed', 'closed', 'resolved'):
                            status_badge = '<span class="tag tag-ok">Completed</span>'
                        else:
                            status_badge = '<span class="tag">Active</span>'

                        disp_id = it.get('display_id') or f"{it['entity_type'].upper()}-{it['entity_id']}"
                        stage_name = it.get('current_stage_name') or it.get('status') or '—'
                        owner_name = it.get('owner_name') or 'Unassigned'
                        title_val = it.get('title') or 'Untitled'

                        actions_html = ''
                        if it['entity_type'] == 'requirement':
                            actions_html = f'<a href="/tests?req_id={it["entity_id"]}">Test Suite &rarr;</a>'
                        elif it['entity_type'] == 'case':
                            actions_html = f'<a href="/case?id={it["entity_id"]}">View Case &rarr;</a>'

                        rows_html.append(f'''<tr>
<td><strong>{h(disp_id)}</strong></td>
<td><span class="tag">{h(it['entity_type'].upper())}</span></td>
<td><strong>{h(title_val)}</strong><br><span class="muted">{h(it.get('client') or 'General')} {('· ' + h(it.get('product'))) if it.get('product') else ''}</span></td>
<td><span class="tag">{h(stage_name)}</span></td>
<td>{status_badge}</td>
<td>{h(owner_name)}</td>
<td>P{it.get('priority', 2)}</td>
<td>{actions_html}</td>
</tr>''')

                    table_html = f'''<div class="card" style="overflow-x:auto;"><table>
<tr><th>ID</th><th>Type</th><th>Title</th><th>Stage</th><th>Status</th><th>Owner</th><th>Priority</th><th>Actions</th></tr>
{''.join(rows_html) or '<tr><td colspan="8" class="muted" style="text-align:center;padding:20px;">No work items match filter.</td></tr>'}
</table></div>'''

                    blockers_summary = ''
                    if all_blockers:
                        b_rows = ''.join(f"<li><strong>{h(b['entity_type'].upper())} #{b['entity_id']}</strong>: {h(b['description'])} <span class=\"muted\">({h(b['dependency_type'])})</span></li>" for b in all_blockers)
                        blockers_summary = f'''<div class="card" style="margin-bottom:16px;background:#fff8f8;border-color:#ffd7d7;">
<h3 style="color:#cf222e;margin-top:0;">Active Blockers & Dependencies ({len(all_blockers)})</h3>
<ul style="margin-bottom:0;">{b_rows}</ul>
</div>'''

                    content = render_work_sub_nav('items', len(inbox), len(cases)) + f'''<div class="today-hero">
  <div>
    <h1>Work Operations · Items Hub ({len(items)} Items)</h1>
    <p class="muted">Unified operational view across Requirements, Tasks, and Support Cases with workflow stages, blockers, and assignments.</p>
  </div>
</div>
{blockers_summary}
{create_req_card}
{filter_bar}
{table_html}'''

                # 7C. WORKFLOW TEMPLATES & STAGES VIEW
                elif route == '/workflows':
                    templates = service.workflow_service.list_templates()
                    tmpl_cards = []
                    for t in templates:
                        stages_items = []
                        for s in t.get('stages', []):
                            desc_part = f'<p class="muted" style="margin:2px 0 0 0;">{h(s["description"])}</p>' if s.get('description') else ''
                            cat = s.get('stage_category') or ('waiting' if s.get('is_waiting') else 'active')
                            role_req = s.get('expected_role') or s.get('required_role_name') or 'Any'
                            stages_items.append(
                                f'<li style="margin-bottom:6px;"><strong>{s["stage_order"]}. {h(s["name"])}</strong> '
                                f'<span class="tag">{h(cat)}</span> '
                                f'<span class="muted">Role: {h(role_req)}</span>'
                                f'{desc_part}</li>'
                            )
                        stages_html = ''.join(stages_items)
                        tmpl_cards.append(f'''<section class="card">
<h2>{h(t['name'])} {'<span class="tag tag-ok">Default</span>' if t.get('is_default') else ''}</h2>
<p class="muted">Applies to: <strong>{h(t['work_type'].title())}</strong> · {len(t.get('stages', []))} defined stages</p>
<p>{h(t.get('description') or 'Standard lifecycle workflow.')}</p>
<h3>Stages Sequence</h3>
<ol style="padding-left:20px;line-height:1.6;">{stages_html or '<li>No stages defined</li>'}</ol>
</section>''')
                    content = f'''<h1>Workflow Templates & Lifecycles</h1>
<p class="muted">Standardized multi-stage pipelines ensuring rigorous separation of stages, clear handoffs, and verification gates.</p>
<div class="grid">{''.join(tmpl_cards)}</div>'''

                # 7D. TEAM & ROLE ASSIGNMENTS VIEW
                elif route == '/team':
                    members = service.member_service.get_members()
                    roles = service.member_service.get_roles()

                    create_member_card = f'''<section class="card" style="margin-bottom:16px;">
<h2>Add Team Member</h2>
<form method="post" action="/members/create">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<div style="display:grid;grid-template-columns:2fr 2fr 1fr 1fr;gap:10px;">
<input name="name" placeholder="Full name *" required>
<input name="email" type="email" placeholder="Email address">
<input name="telegram_handle" placeholder="@telegram_handle">
<select name="role"><option value="">Assign Role</option>
{''.join(f'<option value="{h(r["name"])}">{h(r["name"])}</option>' for r in roles)}
</select>
</div>
<div style="margin-top:8px;display:flex;justify-content:flex-end;">
<button class="primary">Add Member</button>
</div>
</form>
</section>'''

                    member_rows = []
                    for m in members:
                        m_roles = ', '.join(m.get('roles', [])) or '<span class="muted">None</span>'
                        member_rows.append(f'''<tr>
<td><strong>{h(m['name'])}</strong></td>
<td>{h(m.get('email') or '—')}</td>
<td>{h(m.get('telegram_handle') or '—')}</td>
<td>{m_roles}</td>
<td><span class="tag tag-ok">Active</span></td>
</tr>''')

                    team_table = f'''<div class="card"><table>
<tr><th>Name</th><th>Email</th><th>Telegram</th><th>Roles</th><th>Status</th></tr>
{''.join(member_rows) or '<tr><td colspan="5" class="muted" style="text-align:center;">No members recorded.</td></tr>'}
</table></div>'''

                    roles_list = ''.join(f'<div class="card"><h3>{h(r["name"])}</h3><p class="muted">{h(r.get("description") or "Standard team role.")}</p></div>' for r in roles)

                    content = f'''<h1>Team & Role Directory</h1>
<p class="muted">Operational staff profiles, functional roles, and work ownership matrix.</p>
{create_member_card}
<h2>Active Personnel</h2>
{team_table}
<h2 style="margin-top:24px;">Configured Operational Roles</h2>
<div class="grid">{roles_list}</div>'''

                # 8. TESTING WORKSPACE (Enhanced with Requirements, Conditions, Cases & Executions)
                elif route == '/tests':
                    test_sub = (params.get('sub') or ['cases'])[0]
                    req_id_param = (params.get('req_id') or [''])[0]
                    selected_req_id = int(req_id_param) if req_id_param.isdigit() else None

                    requirements = service.work_item_service.list_requirements()
                    test_cases = service.work_item_service.list_test_cases(requirement_id=selected_req_id)
                    all_members = service.member_service.get_members()

                    req_selector = f'''<div class="filter-bar">
<form method="get" action="/tests" style="display:flex;gap:10px;align-items:center;">
<label>Requirement Scope: <select name="req_id" onchange="this.form.submit()">
<option value="">All Requirements ({len(requirements)})</option>
{''.join(f'<option value="{r["id"]}" {"selected" if r["id"]==selected_req_id else ""}>REQ-{r["id"]}: {h(r["title"])}</option>' for r in requirements)}
</select></label>
<a href="/tests" style="margin-left:auto;">Clear Scope</a>
</form>
</div>'''

                    create_tc_card = f'''<section class="card" style="margin-bottom:16px;">
<h2>Add Test Case</h2>
<form method="post" action="/tests/cases/create">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;">
<input name="title" placeholder="Test case title *" required>
<select name="requirement_id">
<option value="">Associate with Requirement</option>
{''.join(f'<option value="{r["id"]}" {"selected" if r["id"]==selected_req_id else ""}>REQ-{r["id"]}: {h(r["title"])}</option>' for r in requirements)}
</select>
<select name="priority"><option value="1">P1 (Critical)</option><option value="2" selected>P2 (Normal)</option><option value="3">P3 (Minor)</option></select>
</div>
<div style="margin-top:8px;">
<textarea name="objective" rows="2" style="width:100%;box-sizing:border-box;" placeholder="Test Objective / Scenario description..."></textarea>
</div>
<div style="margin-top:8px;display:grid;grid-template-columns:1fr 1fr;gap:10px;">
<textarea name="steps" rows="3" style="width:100%;box-sizing:border-box;" placeholder="Step-by-step actions: 1. ... 2. ..."></textarea>
<textarea name="expected_result" rows="3" style="width:100%;box-sizing:border-box;" placeholder="Expected outcome / verification checkpoint..."></textarea>
</div>
<div style="margin-top:8px;display:flex;justify-content:flex-end;">
<button class="primary">Create Test Case</button>
</div>
</form>
</section>'''

                    tc_rows = []
                    for tc in test_cases:
                        last_res = tc.get('last_execution_result') or 'not_run'
                        if last_res == 'pass':
                            badge = '<span class="tag tag-ok">PASS</span>'
                        elif last_res == 'fail':
                            badge = '<span class="tag tag-err">FAIL</span>'
                        elif last_res == 'blocked':
                            badge = '<span class="tag tag-warn">BLOCKED</span>'
                        else:
                            badge = '<span class="tag">NOT RUN</span>'

                        exec_form = f'''<form method="post" action="/tests/executions/record" style="margin:0;display:flex;gap:4px;align-items:center;">
<input type="hidden" name="csrf_token" value="{csrf_token}">
<input type="hidden" name="test_case_id" value="{tc['id']}">
<select name="result" style="padding:3px 6px;font-size:12px;">
<option value="pass">Pass</option>
<option value="fail">Fail</option>
<option value="blocked">Blocked</option>
</select>
<input name="actual_result" placeholder="Actual result note" style="padding:3px 6px;font-size:12px;width:140px;">
<button style="padding:3px 8px;font-size:12px;">Log</button>
</form>'''

                        req_label = f"REQ-{tc['requirement_id']}" if tc.get('requirement_id') else '—'
                        tc_rows.append(f'''<tr>
<td><strong>TC-{tc['id']}</strong></td>
<td>{h(req_label)}</td>
<td><strong>{h(tc['title'])}</strong><br><span class="muted">{h(tc.get('objective') or '')}</span></td>
<td>P{tc.get('priority', 2)}</td>
<td>{badge}</td>
<td>{h(tc.get('last_execution_date') or 'Never')[:16].replace('T', ' ')}</td>
<td>{exec_form}</td>
</tr>''')

                    tc_table = f'''<div class="card" style="overflow-x:auto;">
<h3>Structured Test Cases & Executions</h3>
<table>
<tr><th>Case ID</th><th>Requirement</th><th>Title & Objective</th><th>Priority</th><th>Status</th><th>Last Run</th><th>Quick Execute</th></tr>
{''.join(tc_rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:20px;">No test cases defined.</td></tr>'}
</table>
</div>'''

                    session_cards = ''.join(self.test_card(item, csrf_token) for item in tests) or '<p class="muted">No exploratory test sessions recorded.</p>'
                    exploratory_section = f'''<div class="card" style="margin-top:20px;">
  <div class="card-header-row">
    <h2 class="card-title">Exploratory & Ad-hoc Test Sessions ({len(tests)})</h2>
    <span class="tag">{len(tests)} Recorded</span>
  </div>
  <div class="grid">{session_cards}</div>
</div>'''

                    if test_sub == 'sessions':
                        body_content = f'''{exploratory_section}
<div style="margin-top:24px;">
  <h2 style="font-size:16px;font-weight:600;margin-bottom:12px;">Requirements & Test Cases Reference</h2>
  {req_selector}
  {tc_table}
</div>'''
                    else:
                        body_content = f'''{req_selector}
{create_tc_card}
{tc_table}
{exploratory_section}'''

                    content = render_testing_sub_nav(test_sub) + f'''<div class="today-hero">
  <div>
    <h1>Testing Operations Workspace</h1>
    <p class="muted">End-to-end quality workspace: requirements tracing, test case definitions, test executions, and exploratory session logs.</p>
  </div>
</div>
{body_content}'''

                # 9. FOLLOW-UPS
                elif route == '/followups':
                    cards = ''.join(self.followup_card(item, csrf_token) for item in followups) or '<p>No pending follow-ups.</p>'
                    content = '<h1>Follow-ups</h1><div class="grid">' + cards + '</div>'

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

                # DEFAULT: TODAY OVERVIEW (Comprehensive Operational Command Center)
                else:
                    shift = service.database.active_shift()
                    work_items = service.work_item_service.list_work_items()
                    active_blockers = service.work_item_service.get_blockers(status='active')
                    test_cases = service.work_item_service.list_test_cases()
                    tc_passed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'pass')
                    tc_failed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'fail')
                    tc_blocked = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'blocked')
                    total_tc = len(test_cases)
                    pass_pct = int((tc_passed / total_tc) * 100) if total_tc > 0 else 0

                    active_cases = [c for c in cases if c['status'] not in ('closed', 'resolved')]
                    pending_followups = service.database.list_followups(status='pending', limit=5)
                    in_flight_items = [w for w in work_items if w.get('operational_status') in ('open', 'in_progress', 'blocked')][:6]
                    recent_audits = service.database.get_audit_log(limit=4)

                    # Hero shift action button
                    if shift:
                        shift_time = (shift.get('start') or '')[:16].replace('T', ' ')
                        shift_status_badge = f'<span class="tag tag-ok" style="font-size:13px;">● Shift Active</span> <span class="muted" style="margin-left:6px;">Clocked in at {shift_time}</span>'
                        shift_action_btn = f'''<form method="post" action="/shift/close" style="margin:0;display:inline;">
                          <input type="hidden" name="csrf_token" value="{csrf_token}">
                          <button class="danger" style="padding:6px 14px;font-size:12px;">Clock Out</button>
                        </form>'''
                    else:
                        shift_status_badge = '<span class="tag" style="font-size:13px;">Off Duty</span> <span class="muted" style="margin-left:6px;">No active shift session</span>'
                        shift_action_btn = f'''<form method="post" action="/shift/start" style="margin:0;display:inline;">
                          <input type="hidden" name="csrf_token" value="{csrf_token}">
                          <button class="primary" style="padding:6px 14px;font-size:12px;">Clock In (8h Shift)</button>
                        </form>'''

                    # 1. Today Hero Banner
                    hero_banner = f'''<div class="today-hero">
  <div>
    <h1>Operational Command Center</h1>
    <div style="display:flex;align-items:center;gap:10px;margin-top:4px;flex-wrap:wrap;">
      {shift_status_badge}
    </div>
  </div>
  <div class="today-hero-actions">
    {shift_action_btn}
    <a href="/work" class="btn-subtle">Work Items Hub &rarr;</a>
    <a href="/tests" class="btn-subtle">Testing Workspace &rarr;</a>
  </div>
</div>'''

                    # 2. Metric Band (4 responsive cards)
                    metric_band = f'''<div class="metric-band">
  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Shift State</span>
      <span class="tag {'tag-ok' if shift else ''}">{'Active' if shift else 'Standby'}</span>
    </div>
    <div class="metric-card-value">{'On Duty' if shift else 'Off Duty'}</div>
    <div class="metric-card-desc">{(shift.get('start') or '')[:16].replace('T', ' ') if shift else 'Clock in to log active operations'}</div>
    <div class="metric-card-footer">
      <a href="/shifts" class="muted" style="font-size:12px;">Shift calendar &rarr;</a>
      {shift_action_btn}
    </div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Work Items</span>
      <span class="tag tag-info">{len(work_items)} Total</span>
    </div>
    <div class="metric-card-value">{len(in_flight_items)} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">in-flight</span></div>
    <div class="metric-card-desc">{sum(1 for w in work_items if w.get('operational_status') == 'in_progress')} in progress · {len(active_blockers)} blocked</div>
    <div class="metric-card-footer">
      <a href="/work?sub=items" class="muted" style="font-size:12px;">Manage work &rarr;</a>
      <a href="/work?sub=kanban" class="tag" style="text-decoration:none;">Kanban</a>
    </div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Verification Posture</span>
      <span class="tag {'tag-ok' if pass_pct >= 80 else 'tag-warn'}">{pass_pct}% Pass</span>
    </div>
    <div class="metric-card-value">{total_tc} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">test cases</span></div>
    <div class="progress-bar-wrap">
      <div class="progress-bar-fill" style="width:{pass_pct}%;"></div>
    </div>
    <div class="metric-card-footer">
      <span style="font-size:12px;"><span style="color:var(--color-success);font-weight:600;">{tc_passed} Pass</span> · <span style="color:var(--color-danger);font-weight:600;">{tc_failed} Fail</span></span>
      <a href="/tests" class="muted" style="font-size:12px;">Testing suite &rarr;</a>
    </div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Review Inbox & Cases</span>
      <span class="tag {'tag-warn' if len(inbox) > 0 else 'tag-ok'}">{len(inbox)} Pending</span>
    </div>
    <div class="metric-card-value">{len(active_cases)} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">open cases</span></div>
    <div class="metric-card-desc">{len(cases)} approved total · {len(followups)} scheduled followups</div>
    <div class="metric-card-footer">
      <a href="/work?sub=inbox" class="muted" style="font-size:12px;">Triage inbox &rarr;</a>
      <a href="/work?sub=cases" class="tag" style="text-decoration:none;">Cases</a>
    </div>
  </div>
</div>'''

                    # 3. Main Bento Left: Blockers Banner
                    if active_blockers:
                        b_list = []
                        for b in active_blockers:
                            b_list.append(f'''<div class="today-blocker-item">
  <div class="today-blocker-header">
    <strong style="color:var(--color-danger);">{h(b['entity_type'].upper())} #{b['entity_id']}</strong>
    <span class="tag tag-err">{h(b.get('dependency_type') or 'Dependency')}</span>
  </div>
  <div style="font-size:13px;color:#7f1d1d;">{h(b['description'])}</div>
</div>''')
                        blocker_section = f'''<div class="card" style="border-left: 4px solid var(--color-danger);">
  <div class="card-header-row">
    <h2 class="card-title" style="color:var(--color-danger);">Active Blockers & Dependencies ({len(active_blockers)})</h2>
    <a href="/work-items?status=blocked" class="btn-subtle" style="font-size:12px;">Resolve in Work Items &rarr;</a>
  </div>
  {''.join(b_list)}
</div>'''
                    else:
                        blocker_section = '''<div class="card" style="border-left: 4px solid var(--color-success);padding:14px 18px;">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span class="tag tag-ok">✓ Operational Flow Clear</span>
      <span style="font-size:13px;color:var(--text-secondary);">No active blockers reported across requirements, tasks, or cases.</span>
    </div>
    <a href="/work?sub=items" class="muted" style="font-size:12px;">View all items &rarr;</a>
  </div>
</div>'''

                    # 4. Main Bento Left: In-Flight Priority Work
                    in_flight_rows = []
                    for it in in_flight_items:
                        st = it.get('operational_status') or 'open'
                        if st == 'in_progress':
                            badge = '<span class="tag tag-info">In Progress</span>'
                        elif st == 'blocked':
                            badge = '<span class="tag tag-err">Blocked</span>'
                        else:
                            badge = '<span class="tag">Open</span>'

                        disp_id = f"{it['entity_type'].upper()[:4]}-{it['entity_id']}"
                        in_flight_rows.append(f'''<tr>
  <td><strong>{disp_id}</strong></td>
  <td>
    <strong>{h(it['title'][:65])}</strong>
    <div class="muted" style="font-size:12px;">{h(it.get('client') or 'General')} {('· ' + h(it.get('product'))) if it.get('product') else ''}</div>
  </td>
  <td>{badge}</td>
  <td>P{it.get('priority', 2)}</td>
  <td><a href="/work?sub=items" class="btn-subtle">Open</a></td>
</tr>''')

                    in_flight_section = f'''<div class="card">
  <div class="card-header-row">
    <h2 class="card-title">Priority Work Queue</h2>
    <a href="/work?sub=items" class="muted" style="font-size:12px;">All {len(work_items)} items &rarr;</a>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr><th>ID</th><th>Title & Context</th><th>Status</th><th>Priority</th><th>Action</th></tr>
      </thead>
      <tbody>
        {''.join(in_flight_rows) or '<tr><td colspan="5" class="muted" style="text-align:center;padding:16px;">No in-flight work items. Queue is clear!</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

                    # 5. Main Bento Left: Scheduled Follow-ups
                    followup_rows = []
                    for f in pending_followups:
                        followup_rows.append(f'''<tr>
  <td><strong>#{f['id']}</strong></td>
  <td>{h(f.get('due_at', '')[:16].replace('T', ' '))}</td>
  <td>
    <div>{h(f.get('note') or 'Follow-up deliverable')}</div>
    <div class="muted" style="font-size:11px;">Case #{f.get('case_id')} · Waiting on: {h(f.get('waiting_on') or 'Team')}</div>
  </td>
  <td>
    <form method="post" action="/followup/complete" style="margin:0;display:inline;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="id" value="{f['id']}">
      <button style="padding:4px 8px;font-size:11px;">Complete</button>
    </form>
  </td>
</tr>''')

                    followup_section = f'''<div class="card">
  <div class="card-header-row">
    <h2 class="card-title">Scheduled Follow-ups ({len(followups)})</h2>
    <a href="/followups" class="muted" style="font-size:12px;">View all &rarr;</a>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead><tr><th>ID</th><th>Due</th><th>Deliverable & Context</th><th>Action</th></tr></thead>
      <tbody>
        {''.join(followup_rows) or '<tr><td colspan="4" class="muted" style="text-align:center;padding:16px;">No follow-ups due today.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

                    # 6. Bento Side Column: Google Workspace Quick Actions
                    workspace_card = f'''<div class="card">
  <div class="card-header-row">
    <h2 class="card-title">Google Workspace Hub</h2>
    <a href="/workspace" class="tag tag-info" style="text-decoration:none;">Connected</a>
  </div>
  <p class="muted" style="margin-top:0;">Access your linked Google Workspace applications:</p>
  <a href="/workspace" class="ws-quick-link">
    <div class="ws-icon-wrap" style="background:#e8f0fe;color:#1967d2;">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
    </div>
    <div>
      <div style="font-weight:600;font-size:13px;">Google Drive & Docs</div>
      <div class="muted" style="font-size:12px;">Browse docs, folders & retained evidence</div>
    </div>
  </a>
  <a href="/workspace" class="ws-quick-link">
    <div class="ws-icon-wrap" style="background:#e6f4ea;color:#137333;">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
    </div>
    <div>
      <div style="font-weight:600;font-size:13px;">Google Sheets</div>
      <div class="muted" style="font-size:12px;">Operational tracking, work items & test logs</div>
    </div>
  </a>
  <a href="/workspace" class="ws-quick-link">
    <div class="ws-icon-wrap" style="background:#fef7e0;color:#b06000;">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
    </div>
    <div>
      <div style="font-weight:600;font-size:13px;">Google Tasks</div>
      <div class="muted" style="font-size:12px;">Import tasks directly into Review Inbox</div>
    </div>
  </a>
</div>'''

                    # 7. Bento Side Column: Operational Shortcuts
                    shortcuts_card = f'''<div class="card">
  <h2 class="card-title" style="margin-bottom:12px;">Quick Operations</h2>
  <div style="display:flex;flex-direction:column;gap:8px;">
    <a href="/work?sub=inbox" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
      Review Pending Messages ({len(inbox)})
    </a>
    <a href="/work?sub=items" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      Add Requirement / Work Item
    </a>
    <a href="/tests?sub=cases" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
      Define Structured Test Case
    </a>
    <a href="/shifts" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
      Shift Schedule & Overrides
    </a>
  </div>
</div>'''

                    # 8. Bento Side Column: Recent Audit Trail
                    audit_rows = []
                    for a in recent_audits:
                        t = (a.get('occurred_at') or '')[11:16]
                        audit_rows.append(f'''<tr>
  <td class="muted">{t}</td>
  <td><strong>{h(a.get('operation_type') or 'action')}</strong></td>
  <td class="muted">{h(a.get('actor') or 'user')}</td>
</tr>''')

                    audit_card = f'''<div class="card">
  <div class="card-header-row">
    <h2 class="card-title">Recent Activity</h2>
    <a href="/audit" class="muted" style="font-size:12px;">Audit log &rarr;</a>
  </div>
  <table style="font-size:12px;">
    <tbody>
      {''.join(audit_rows) or '<tr><td colspan="3" class="muted">No recent events.</td></tr>'}
    </tbody>
  </table>
</div>'''

                    # Combine into Bento Layout
                    content = f'''{hero_banner}
{metric_band}
<div class="bento-split">
  <div class="bento-main">
    {blocker_section}
    {in_flight_section}
    {followup_section}
  </div>
  <div class="bento-side">
    {workspace_card}
    {shortcuts_card}
    {audit_card}
  </div>
</div>'''

                self.send_html(self.page(content, title='Operations Dashboard · Personal Work Assistant', csrf_token=csrf_token, route=route), set_sid=set_sid)

            def do_POST(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                length = min(int(self.headers.get('Content-Length', '0')), 65536)
                form = parse_qs(self.rfile.read(length).decode('utf-8'))

                # Authenticate and verify CSRF
                authed, set_sid, expected_csrf, _ = self.authenticate_request(query)
                if not authed:
                    self.send_html(self.page('<h2>Access denied</h2><p>Authentication required.</p>'), 403)
                    return

                submitted_csrf = (form.get('csrf_token') or [''])[0]
                if not submitted_csrf or not secrets.compare_digest(submitted_csrf, expected_csrf):
                    self.send_html(self.page('<h2>CSRF validation failed</h2><p>Form submission rejected for security.</p>'), 403)
                    return

                try:
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

                    # 14. MEMBERS CREATE
                    elif parsed.path == '/members/create':
                        name = form['name'][0]
                        email = (form.get('email') or [''])[0] or None
                        tg = (form.get('telegram_handle') or [''])[0] or None
                        role = (form.get('role') or [''])[0]
                        roles = [role] if role else []
                        service.member_service.add_member(name=name, email=email, telegram_handle=tg, roles=roles)

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
