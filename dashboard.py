"""Token-protected localhost dashboard using only the Python standard library."""
from __future__ import annotations

import html
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import config


STATUSES = ('new','triaged','investigating','waiting_client','waiting_internal','fix_ready',
            'testing','retest_required','resolved','client_updated','closed')


def h(value):
    return html.escape('' if value is None else str(value))


class DashboardService:
    def __init__(self, database, host='127.0.0.1', port=8765):
        self.database = database
        self.host = host
        self.port = int(port)
        self.token = database.get_setting('dashboard_token') or secrets.token_urlsafe(24)
        database.set_setting('dashboard_token', self.token)
        self.server = None
        self.thread = None

    @property
    def url(self):
        return f'http://{self.host}:{self.port}/?token={self.token}'

    def start(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def token_ok(self, params):
                supplied = (params.get('token') or [''])[0]
                return secrets.compare_digest(supplied, service.token)

            def send_html(self, body, status=200):
                content = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(content)

            def page(self, content, title='Work Assistant'):
                token = urlencode({'token': service.token})
                return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title><style>
body{{font:15px system-ui;margin:0;background:#f4f6f8;color:#17212b}}header{{background:#17212b;color:white;padding:16px 5%}}
main{{max-width:1180px;margin:24px auto;padding:0 18px}}nav a{{color:#bfe1ff;margin-right:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}}
.card{{background:white;border:1px solid #dce2e8;border-radius:10px;padding:16px;box-shadow:0 2px 8px #0000000a}}
.muted{{color:#637282}}.tag{{display:inline-block;background:#e9f3ff;padding:3px 7px;border-radius:12px;margin:2px}}
button,select{{padding:7px 9px}}form{{margin-top:8px}}pre{{white-space:pre-wrap}}a{{color:#0969da}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #eee}}
</style></head><body><header><strong>Telegram Work Assistant</strong><nav>
<a href="/?{token}">Overview</a><a href="/cases?{token}">Cases</a>
<a href="/inbox?{token}">Review inbox</a><a href="/tests?{token}">Testing</a>
<a href="/followups?{token}">Follow-ups</a><a href="/reports?{token}">Reports</a>
<a href="/settings?{token}">Settings</a></nav></header><main>{content}</main></body></html>'''

            def send_file(self, item):
                path = Path(item.get('path') or '').resolve()
                allowed = (config.BASE_DIR / 'storage' / 'evidence').resolve()
                try:
                    path.relative_to(allowed)
                except ValueError:
                    self.send_error(403); return
                if not path.is_file():
                    self.send_error(404); return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', item.get('mime_type') or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Disposition', f'inline; filename="{h(path.name)}"')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                parsed = urlparse(self.path); params = parse_qs(parsed.query)
                if not self.token_ok(params):
                    self.send_html(self.page('<h2>Access denied</h2>'), 403); return
                route = parsed.path
                if route == '/evidence':
                    item = service.database.evidence_item(int((params.get('id') or ['0'])[0]))
                    if not item:
                        self.send_error(404); return
                    self.send_file(item); return
                cases = service.database.list_cases(limit=100)
                inbox = service.database.inbox(limit=100)
                followups = service.database.list_followups(limit=100)
                tests = service.database.test_sessions(limit=100)
                if route == '/case':
                    case_id = int((params.get('id') or ['0'])[0])
                    item = service.database.case(case_id)
                    if not item:
                        self.send_html(self.page('<h2>Case not found</h2>'), 404); return
                    events = service.database.case_events(case_id)
                    case_tests = service.database.test_sessions(case_id=case_id)
                    evidence = service.database.evidence(case_id=case_id)
                    timeline = ''.join(f"<tr><td>{h(e['occurred_at'][:16].replace('T',' '))}</td><td>{h(e['event_type'])}</td><td>{h(e['detail'])}</td></tr>" for e in events)
                    evidence_rows = ''.join(f"<li><a href=\"/evidence?token={h(service.token)}&id={e['id']}\">Evidence #{e['id']} · {h(Path(e.get('path') or 'retained-metadata').name)}</a> — {h(e.get('caption'))}</li>" for e in evidence)
                    test_rows = ''.join(f"<li>TEST-{t['id']} [{h(t['result'])}] {h(t['scenario'])}</li>" for t in case_tests)
                    content = f'''<h1>CASE-{case_id}</h1>{self.case_card(item)}
<section class="card"><h2>Timeline</h2><table><tr><th>When</th><th>Type</th><th>Detail</th></tr>{timeline}</table></section>
<div class="grid"><section class="card"><h2>Testing</h2><ul>{test_rows or '<li>None</li>'}</ul></section>
<section class="card"><h2>Evidence</h2><ul>{evidence_rows or '<li>None</li>'}</ul></section></div>'''
                elif route == '/cases':
                    cards = ''.join(self.case_card(item) for item in cases) or '<p>No approved cases.</p>'
                    content = f'''<h1>Cases</h1><section class="card"><h2>Merge duplicates</h2>
<form method="post" action="/cases/merge{self.action()}">{self.hidden()}
Source case <input name="source" type="number" min="1" required> into target <input name="target" type="number" min="1" required> <button>Merge</button></form></section>
<div class="grid">{cards}</div>'''
                elif route == '/inbox':
                    cards = ''.join(self.inbox_card(item) for item in inbox) or '<p>Review inbox is clear.</p>'
                    content = '<h1>Review inbox</h1><p class="muted">Imported items never affect reports until accepted.</p><div class="grid">' + cards + '</div>'
                elif route == '/tests':
                    cards = ''.join(self.test_card(item) for item in tests) or '<p>No test sessions.</p>'
                    content = '<h1>Testing</h1><div class="grid">' + cards + '</div>'
                elif route == '/followups':
                    cards = ''.join(self.followup_card(item) for item in followups) or '<p>No pending follow-ups.</p>'
                    content = '<h1>Follow-ups</h1><div class="grid">' + cards + '</div>'
                elif route == '/reports':
                    report_cards = []
                    for summary in service.database.history():
                        report = service.database.report(summary['id'])
                        report_cards.append(f'''<section class="card"><h2>Report #{report['id']} · {h(report['kind'])}</h2>
<p class="muted">{h(report['created_at'])} · {h(report['style'])} · {'final' if report['finalized'] else 'draft'}</p>
<form method="post" action="/report/edit{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{report['id']}">
<textarea name="text" rows="14" style="width:100%">{h(report['text'])}</textarea><button>Save revised draft</button></form></section>''')
                    content = '<h1>Reports</h1><div class="grid">' + (''.join(report_cards) or '<p>No reports.</p>') + '</div>'
                elif route == '/settings':
                    masked = service.database.get_setting('mask_client_names') == 'true'
                    content = f'''<h1>Settings</h1><section class="card"><h2>Privacy</h2>
<form method="post" action="/settings{self.action()}">{self.hidden()}<label><input type="checkbox" name="mask_clients" value="on" {'checked' if masked else ''}> Mask client names in new reports</label><br><button>Save</button></form>
<p>AI configured: {h(bool(config.AI_KEY and config.AI_MODEL))}; model: {h(config.AI_MODEL or 'none')}</p>
<p>Dashboard binding: {h(service.host)}:{service.port}</p></section>'''
                else:
                    shift = service.database.active_shift()
                    content = f'''<h1>Today</h1><div class="grid">
<div class="card"><h2>Shift</h2><p>{h(shift['start'] if shift else 'No active shift')}</p><p>{h(shift['end'] if shift else '')}</p></div>
<div class="card"><h2>Cases</h2><p>{len(cases)} approved cases · {sum(c['status'] not in ('closed',) for c in cases)} open</p></div>
<div class="card"><h2>Review inbox</h2><p>{len(inbox)} owner-attributed items pending</p></div>
<div class="card"><h2>Follow-ups</h2><p>{len(followups)} pending</p></div></div>'''
                self.send_html(self.page(content))

            def do_POST(self):
                parsed = urlparse(self.path); query = parse_qs(parsed.query)
                length = min(int(self.headers.get('Content-Length', '0')), 65536)
                form = parse_qs(self.rfile.read(length).decode('utf-8'))
                if not self.token_ok(query) or not secrets.compare_digest((form.get('token') or [''])[0], service.token):
                    self.send_html(self.page('<h2>Access denied</h2>'), 403); return
                try:
                    if parsed.path == '/case/status':
                        service.database.update_case(int(form['id'][0]), 'status', form['status'][0])
                    elif parsed.path == '/cases/merge':
                        service.database.merge_cases(int(form['source'][0]), int(form['target'][0]))
                    elif parsed.path == '/inbox/accept':
                        target = int((form.get('case_id') or ['0'])[0]) or None
                        service.database.accept_source_message(int(form['id'][0]), case_id=target)
                    elif parsed.path == '/inbox/ignore':
                        service.database.ignore_source_message(int(form['id'][0]))
                    elif parsed.path == '/followup/complete':
                        service.database.complete_followup(int(form['id'][0]))
                    elif parsed.path == '/test/update':
                        session_id = int(form['id'][0])
                        service.database.update_test_session(session_id, 'result', form['result'][0])
                        service.database.update_test_session(session_id, 'actual', form.get('actual', [''])[0])
                        service.database.update_test_session(session_id, 'defects', form.get('defects', [''])[0])
                        service.database.update_test_session(session_id, 'retest_required',
                                                             'on' if 'retest_required' in form else 'off')
                    elif parsed.path == '/report/edit':
                        report = service.database.report(int(form['id'][0]))
                        if not report:
                            raise ValueError('Report not found.')
                        service.database.save_report(report['shift_id'], report['kind'], form['text'][0],
                                                     report['style'], source_report_id=report['id'])
                    elif parsed.path == '/settings':
                        service.database.set_setting('mask_client_names',
                                                     'true' if 'mask_clients' in form else 'false')
                    else:
                        raise ValueError('Unknown action.')
                except (ValueError, KeyError) as exc:
                    self.send_html(self.page(f'<h2>Could not update</h2><p>{h(exc)}</p>'), 400); return
                target = self.headers.get('Referer') or service.url
                self.send_response(303); self.send_header('Location', target); self.end_headers()

            def hidden(self):
                return f'<input type="hidden" name="token" value="{h(service.token)}">'

            def action(self):
                return '?token=' + h(service.token)

            def case_card(self, item):
                options = ''.join(f'<option value="{s}" {"selected" if s == item["status"] else ""}>{s}</option>' for s in STATUSES)
                return f'''<section class="card"><h2><a href="/case?token={h(service.token)}&id={item['id']}">CASE-{item['id']}: {h(item['title'])}</a></h2>
<p><span class="tag">{h(item['status'])}</span><span class="tag">{h(item['participation'])}</span></p>
<p>{h(item.get('client') or 'Client unspecified')} · {h(item.get('channel') or 'Channel unspecified')}</p>
<p class="muted">Next: {h(item.get('next_action') or 'not recorded')}<br>Waiting on: {h(item.get('waiting_on') or 'nobody recorded')}</p>
<form method="post" action="/case/status{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{item['id']}"><select name="status">{options}</select> <button>Update</button></form></section>'''

            def inbox_card(self, item):
                detail = item.get('redacted_text') or item.get('text') or '(media only)'
                return f'''<section class="card"><h2>Inbox #{item['id']}</h2><p><span class="tag">{h(item.get('classification'))}</span> confidence {round(float(item.get('confidence') or 0)*100)}%</p>
<p>{h(detail[:700])}</p><form method="post" action="/inbox/accept{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{item['id']}">Existing case ID <input type="number" min="1" name="case_id" style="width:90px"> <button>Accept</button></form>
<form method="post" action="/inbox/ignore{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{item['id']}"><button>Ignore</button></form></section>'''

            def followup_card(self, item):
                return f'''<section class="card"><h2>Follow-up #{item['id']}</h2><p>CASE-{item['case_id']}: {h(item['title'])}</p><p>Due {h(item['due_at'])}</p><p>{h(item.get('note'))}</p>
<form method="post" action="/followup/complete{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{item['id']}"><button>Complete</button></form></section>'''

            def test_card(self, item):
                options = ''.join(f'<option value="{value}" {"selected" if value == item["result"] else ""}>{value}</option>'
                                  for value in ('not_run','passed','failed','partial','blocked','inconclusive'))
                return f'''<section class="card"><h2>TEST-{item['id']}: {h(item['scenario'])}</h2>
<p>{h(item.get('environment'))} · build {h(item.get('build'))} · CASE-{h(item.get('case_id'))}</p>
<form method="post" action="/test/update{self.action()}">{self.hidden()}<input type="hidden" name="id" value="{item['id']}">
<label>Result <select name="result">{options}</select></label><br>
<label>Actual<br><textarea name="actual" rows="3" style="width:100%">{h(item.get('actual'))}</textarea></label><br>
<label>Defects<br><textarea name="defects" rows="2" style="width:100%">{h(item.get('defects'))}</textarea></label><br>
<label><input type="checkbox" name="retest_required" {'checked' if item.get('retest_required') else ''}> Retest required</label><br><button>Save test</button></form></section>'''

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name='local-dashboard', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
