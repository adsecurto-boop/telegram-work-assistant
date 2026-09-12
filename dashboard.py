"""Token-protected localhost dashboard using only the Python standard library."""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
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
                self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline'")
                if set_sid:
                    self.send_header('Set-Cookie', f'dashboard_session={set_sid}; Path=/; HttpOnly; SameSite=Strict')
                self.end_headers()
                self.wfile.write(content)

            def page(self, content, title='Work Operations Hub', csrf_token: str = ''):
                return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title>
<meta name="description" content="Personal Work Assistant and Operations Hub with Telegram integration, tracking, and diagnostics.">
<meta property="og:title" content="{h(title)}">
<meta property="og:description" content="Personal Work Assistant and Operations Hub with Telegram integration, tracking, and diagnostics.">
<style>
body{{font:14px system-ui,-apple-system,sans-serif;margin:0;background:#f4f6f8;color:#17212b}}
header{{background:#17212b;color:white;padding:14px 4%;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
main{{max-width:1300px;margin:20px auto;padding:0 16px}}
nav{{margin-top:10px}}nav a{{color:#bfe1ff;margin-right:16px;text-decoration:none;font-weight:500}}
nav a:hover{{text-decoration:underline;color:white}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}}
.card{{background:white;border:1px solid #dce2e8;border-radius:8px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,0.04)}}
.muted{{color:#637282;font-size:13px}}
.tag{{display:inline-block;background:#e9f3ff;color:#0969da;padding:2px 8px;border-radius:12px;margin:2px;font-size:12px;font-weight:500}}
.tag-warn{{background:#fff8c5;color:#9a6700}}
.tag-err{{background:#ffebe9;color:#cf222e}}
.tag-ok{{background:#dafbe1;color:#1a7f37}}
button,select,input,textarea{{padding:7px 10px;border:1px solid #d0d7de;border-radius:6px;font-size:13px}}
button{{background:#f6f8fa;cursor:pointer;font-weight:500}}
button:hover{{background:#f3f4f6}}
button.primary{{background:#2da44e;color:white;border-color:#2c974b}}
button.primary:hover{{background:#2c974b}}
button.danger{{background:#cf222e;color:white;border-color:#b62324}}
button.danger:hover{{background:#b62324}}
form{{margin-top:8px}}pre{{white-space:pre-wrap;background:#f8f9fa;padding:10px;border-radius:6px}}
a{{color:#0969da;text-decoration:none}}a:hover{{text-decoration:underline}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
td,th{{text-align:left;padding:8px 10px;border-bottom:1px solid #eee;font-size:13px}}
th{{background:#f8f9fa;font-weight:600}}
.kanban-board{{display:flex;gap:12px;overflow-x:auto;padding-bottom:16px}}
.kanban-col{{flex:0 0 280px;background:#ebf0f5;border-radius:8px;padding:12px;min-height:500px}}
.kanban-col h3{{margin:0 0 10px 0;font-size:14px;color:#333;display:flex;justify-content:space-between}}
.kanban-item{{background:white;border:1px solid #dce2e8;border-radius:6px;padding:12px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}}
.filter-bar{{background:white;border:1px solid #dce2e8;border-radius:8px;padding:12px;margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
</style></head><body><header><strong>Personal Work Assistant · Telegram Work Assistant · Work Operations Hub</strong><nav>
<a href="/">Overview</a><a href="/work-items">Work Items</a><a href="/kanban">Kanban</a>
<a href="/tests">Testing</a><a href="/workflows">Workflows</a><a href="/team">Team</a>
<a href="/cases">Cases</a><a href="/inbox">Review Inbox</a>
<a href="/clusters">Clusters</a><a href="/followups">Follow-ups</a><a href="/shifts">Shifts</a>
<a href="/reports">Reports</a><a href="/audit">Audit Log</a>
<a href="/ai/history">AI History</a><a href="/connectors">Connectors</a>
<a href="/settings">Settings & Diagnostics</a></nav></header><main>{content}</main></body></html>'''

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
                    content = f'''<h1>Kanban Board</h1>
<p class="muted">Status tracking of active cases across operational stages.</p>
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

                    content = f'''<h1>Review Inbox ({total_count} Messages)</h1>
<p class="muted">Safe two-step bulk review with preview confirmation and atomic undo.</p>
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
                    content = f'''<h1>CASE-{case_id}</h1>{self.case_card(item, csrf_token)}
<section class="card"><h2>Timeline</h2><table><tr><th>When</th><th>Type</th><th>Detail</th></tr>{timeline}</table></section>
<div class="grid"><section class="card"><h2>Testing</h2><ul>{test_rows or '<li>None</li>'}</ul></section>
<section class="card"><h2>Evidence</h2><ul>{evidence_rows or '<li>None</li>'}</ul></section></div>'''

                # 7. CASES LIST
                elif route == '/cases':
                    cards = ''.join(self.case_card(item, csrf_token) for item in cases) or '<p>No approved cases.</p>'
                    content = f'''<h1>Cases</h1><section class="card"><h2>Merge duplicates</h2>
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

                    content = f'''<h1>Work Items Hub</h1>
<p class="muted">Unified operational view across Requirements, Tasks, and Support Cases with workflow stages, blockers, and assignments.</p>
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
                    exploratory_section = f'''<h2 style="margin-top:24px;">Exploratory & Ad-hoc Test Sessions</h2><div class="grid">{session_cards}</div>'''

                    content = f'''<h1>Testing Operations Workspace</h1>
<p class="muted">End-to-end quality workspace: requirements tracing, test case definitions, test executions, and exploratory session logs.</p>
{req_selector}
{create_tc_card}
{tc_table}
{exploratory_section}'''

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

                # DEFAULT: TODAY OVERVIEW
                else:
                    shift = service.database.active_shift()
                    work_items = service.work_item_service.list_work_items()
                    active_blockers = service.work_item_service.get_blockers(status='active')
                    test_cases = service.work_item_service.list_test_cases()
                    tc_passed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'pass')
                    tc_failed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'fail')

                    blocker_banner = ''
                    if active_blockers:
                        blocker_banner = f'''<div class="card" style="margin-bottom:16px;background:#fff8f8;border-color:#ffd7d7;">
<h3 style="color:#cf222e;margin-top:0;">Attention: {len(active_blockers)} Active Blocker(s) Need Resolution</h3>
<p class="muted">Items are paused waiting on internal/external dependencies. Check <a href="/work-items?status=blocked">Work Items</a> to review details.</p>
</div>'''

                    content = f'''<h1>Today Operations Overview</h1>
<p class="muted">Live summary of shift status, work items, verification posture, and blockers.</p>
{blocker_banner}
<div class="grid">
<div class="card"><h2>Active Shift</h2><p>{h(shift['start'] if shift else 'No active shift clocked')}</p><p>{h(shift['end'] if shift else '')}</p></div>
<div class="card"><h2>Work Items Hub</h2><p><strong>{len(work_items)}</strong> total items tracked</p><p class="muted"><a href="/work-items">View Work Items &rarr;</a></p></div>
<div class="card"><h2>Testing Posture</h2><p><strong>{len(test_cases)}</strong> test cases defined</p><p><span class="tag tag-ok">{tc_passed} Pass</span> <span class="tag tag-err">{tc_failed} Fail</span></p><p class="muted"><a href="/tests">Testing Workspace &rarr;</a></p></div>
<div class="card"><h2>Cases & Inbox</h2><p>{len(cases)} approved · {sum(c['status'] not in ('closed', 'resolved') for c in cases)} active open</p><p>{len(inbox)} inbox pending</p></div>
<div class="card"><h2>Follow-ups</h2><p>{len(followups)} scheduled</p></div>
</div>'''

                self.send_html(self.page(content, csrf_token=csrf_token), set_sid=set_sid)

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
                    # 1. CASE STATUS
                    if parsed.path == '/case/status':
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
