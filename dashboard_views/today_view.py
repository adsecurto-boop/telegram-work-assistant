"""Today Operations Cockpit view."""
from html import escape as h
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import config
from dashboard_views.components import (
    render_attention_strip,
    render_status_badge,
    render_priority_tag,
    render_type_badge,
    format_iso_time
)


def render_today_view(service, csrf_token: str, params: dict) -> str:
    shift = service.database.active_shift()
    work_items = service.work_item_service.list_work_items()
    active_blockers = service.work_item_service.get_blockers(status='active')
    test_cases = service.work_item_service.list_test_cases()
    
    # Testing metrics
    tc_passed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'pass')
    tc_failed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'fail')
    tc_blocked = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'blocked')
    failed_test_cases = [tc for tc in test_cases if tc.get('last_execution_result') == 'fail']
    total_tc = len(test_cases)
    pass_pct = int((tc_passed / total_tc) * 100) if total_tc > 0 else 0

    # Followups
    followups = service.database.list_followups(limit=100)
    now_utc = datetime.now(timezone.utc).isoformat()
    pending_followups = [f for f in followups if f.get('status') != 'completed']
    overdue_followups = []
    due_today_followups = []
    today_prefix = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m-%d')
    for f in pending_followups:
        d = f.get('due_at') or ''
        if d and d < now_utc:
            overdue_followups.append(f)
        elif d and d.startswith(today_prefix):
            due_today_followups.append(f)

    # Waiting items
    waiting_items = [
        w for w in work_items
        if w.get('operational_status') == 'waiting' or w.get('waiting_on') or w.get('is_blocked')
    ]

    # In-flight work items
    in_flight_items = [
        w for w in work_items
        if w.get('operational_status') in ('open', 'active', 'in_progress', 'blocked', 'waiting')
    ]
    
    # Cases & Inbox
    inbox = service.database.inbox()
    all_cases = service.database.list_cases(limit=100)
    active_cases = [c for c in all_cases if c.get('status') not in ('closed', 'resolved')]

    # Hero Shift Status
    if shift:
        shift_time = (shift.get('start') or '')[:16].replace('T', ' ')
        shift_badge = f'''<div class="shift-banner active">
          <span class="pulse-dot"></span>
          <div>
            <strong>Shift Active</strong> · Started at {h(shift_time)}
          </div>
        </div>'''
        shift_btn = f'''<form method="post" action="/shift/close" style="margin:0;display:inline;">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button class="danger" style="padding:6px 14px;font-size:12px;">Clock Out</button>
        </form>'''
    else:
        shift_badge = '''<div class="shift-banner inactive">
          <span class="static-dot"></span>
          <div>
            <strong>Standby / Off Duty</strong> · No active shift session
          </div>
        </div>'''
        shift_btn = f'''<form method="post" action="/shift/start" style="margin:0;display:inline;">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button class="primary" style="padding:6px 14px;font-size:12px;">Clock In (8h Shift)</button>
        </form>'''

    # 1. Hero Header
    hero_header = f'''<div class="today-hero">
  <div class="today-hero-left">
    <h1>Today Operations Overview · Work Cockpit</h1>
    <div style="margin-top:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      {shift_badge}
    </div>
  </div>
  <div class="today-hero-actions">
    {shift_btn}
    <a href="/work" class="btn-subtle">Work Items Hub &rarr;</a>
    <a href="/tests" class="btn-subtle">Testing Suite &rarr;</a>
  </div>
</div>'''

    # 2. Attention Strip
    attention_strip = render_attention_strip(
        active_blockers=active_blockers,
        failed_tests=failed_test_cases,
        overdue_followups=overdue_followups,
        waiting_items=waiting_items
    )

    # 3. 4-Card Metric Band
    metric_band = f'''<div class="metric-band">
  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Shift State</span>
      <span class="tag {'tag-ok' if shift else ''}">{'Active' if shift else 'Standby'}</span>
    </div>
    <div class="metric-card-value">{'On Duty' if shift else 'Off Duty'}</div>
    <div class="metric-card-desc">{(shift.get('start') or '')[:16].replace('T', ' ') if shift else 'Clock in to record active telemetry'}</div>
    <div class="metric-card-footer">
      <a href="/shifts" class="muted" style="font-size:12px;">Shift calendar &rarr;</a>
      {shift_btn}
    </div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Work Items Hub</span>
      <span class="tag tag-info">{len(in_flight_items)} In Flight</span>
    </div>
    <div class="metric-card-value">{len(in_flight_items)} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">of {len(work_items)} total</span></div>
    <div class="metric-card-desc">{sum(1 for w in work_items if w.get('operational_status') in ('active', 'in_progress'))} in progress · {len(active_blockers)} blocked</div>
    <div class="metric-card-footer">
      <a href="/work" class="muted" style="font-size:12px;">Work table &rarr;</a>
      <a href="/work?sub=kanban" class="tag" style="text-decoration:none;">Kanban Board</a>
    </div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <span class="metric-card-title">Testing Posture</span>
      <span class="tag {'tag-ok' if pass_pct >= 80 else 'tag-warn'}">{pass_pct}% Pass Rate</span>
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
      <span class="metric-card-title">Review Inbox & Follow-ups</span>
      <span class="tag {'tag-warn' if len(inbox) > 0 or len(overdue_followups) > 0 else 'tag-ok'}">{len(inbox)} Inbox</span>
    </div>
    <div class="metric-card-value">{len(active_cases)} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">open cases</span></div>
    <div class="metric-card-desc">{len(overdue_followups)} overdue follow-ups · {len(due_today_followups)} due today</div>
    <div class="metric-card-footer">
      <a href="/work?sub=inbox" class="muted" style="font-size:12px;">Review inbox &rarr;</a>
      <a href="/followups" class="tag" style="text-decoration:none;">Follow-ups</a>
    </div>
  </div>
</div>'''

    # 4. Priority Work Items Table (Sorted by Priority and Status)
    sorted_items = sorted(
        in_flight_items,
        key=lambda x: (
            0 if x.get('is_blocked') else 1,
            x.get('priority', 2) or 2,
            0 if x.get('operational_status') == 'in_progress' else 1
        )
    )[:8]

    priority_rows = []
    for item in sorted_items:
        d_id = item.get('display_id') or f"{item['entity_type'].upper()}-{item['entity_id']}"
        title = item.get('title') or 'Untitled'
        e_type = item.get('entity_type', 'item')
        e_id = item.get('entity_id')
        p_badge = render_priority_tag(item.get('priority'))
        s_badge = render_status_badge(item.get('operational_status'), is_waiting=bool(item.get('waiting_on')))
        t_badge = render_type_badge(e_type)
        stage_name = item.get('current_stage_name') or 'Backlog'
        client_info = item.get('client') or item.get('product') or '—'
        due = format_iso_time(item.get('due_date'))
        
        waiting_html = ''
        if item.get('waiting_on'):
            waiting_html = f"<div class='muted' style='font-size:11px;color:#d97706;'>⏳ Waiting on: {h(item['waiting_on'])}</div>"
        elif item.get('active_blockers_count', 0) > 0:
            waiting_html = f"<div class='muted' style='font-size:11px;color:#dc2626;'>🚫 {item['active_blockers_count']} Blocker(s) active</div>"

        priority_rows.append(f'''<tr>
  <td>{t_badge} <strong><a href="/work?item_type={e_type}&item_id={e_id}" style="text-decoration:none;color:var(--color-primary);">{h(d_id)}</a></strong></td>
  <td>
    <div style="font-weight:600;"><a href="/work?item_type={e_type}&item_id={e_id}" style="color:var(--text-primary);text-decoration:none;">{h(title)}</a></div>
    <div class="muted" style="font-size:12px;">{h(client_info)}</div>
    {waiting_html}
  </td>
  <td><span class="stage-pill">{h(stage_name)}</span></td>
  <td>{p_badge}</td>
  <td>{s_badge}</td>
  <td><span class="muted">{due}</span></td>
  <td style="text-align:right;">
    <a href="/work?item_type={e_type}&item_id={e_id}" class="btn-subtle" style="padding:4px 8px;font-size:12px;">Open &rarr;</a>
  </td>
</tr>''')

    work_queue_card = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Priority Work Queue ({len(in_flight_items)} In Flight)</h2>
      <p class="muted" style="margin:2px 0 0 0;">Items ranked by operational priority, active blockers, and in-progress state.</p>
    </div>
    <div style="display:flex;gap:8px;">
      <a href="/work" class="btn-subtle" style="font-size:12px;">Full Hub &rarr;</a>
      <a href="/work?action=new" class="btn-primary-action" style="font-size:12px;padding:5px 10px;">+ New Item</a>
    </div>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Title & Context</th>
          <th>Workflow Stage</th>
          <th>Priority</th>
          <th>Status</th>
          <th>Due Date</th>
          <th style="text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(priority_rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:24px;">No active work items in flight. Click "+ New Item" to create one.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 5. Follow-ups Due Today & Overdue Section
    all_urgent_followups = overdue_followups + due_today_followups
    followup_rows = []
    for f in all_urgent_followups[:6]:
        f_id = f['id']
        c_id = f.get('case_id')
        title = f.get('title') or 'Follow-up'
        due_str = format_iso_time(f.get('due_at'))
        is_overdue = f in overdue_followups
        due_badge = '<span class="tag tag-err">Overdue</span>' if is_overdue else '<span class="tag tag-warn">Due Today</span>'

        followup_rows.append(f'''<tr>
  <td><strong>#{f_id}</strong></td>
  <td>
    <strong>{h(title)}</strong>
    <div class="muted">Linked to CASE-{c_id} · {h(f.get('note') or '')}</div>
  </td>
  <td>{due_badge}</td>
  <td><span class="muted">{due_str}</span></td>
  <td style="text-align:right;">
    <div style="display:inline-flex;gap:4px;">
      <form method="post" action="/followup/complete" style="margin:0;display:inline;">
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <input type="hidden" name="id" value="{f_id}">
        <button class="primary" style="padding:3px 8px;font-size:11px;" title="Mark Completed">✓ Complete</button>
      </form>
      <form method="post" action="/followup/snooze" style="margin:0;display:inline;">
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <input type="hidden" name="id" value="{f_id}">
        <input type="hidden" name="days" value="1">
        <button class="btn-subtle" style="padding:3px 8px;font-size:11px;" title="Snooze 1 Day">+1d</button>
      </form>
    </div>
  </td>
</tr>''')

    followup_card = f'''<div class="card" style="margin-top:20px;">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Urgent Follow-ups ({len(all_urgent_followups)})</h2>
      <p class="muted" style="margin:2px 0 0 0;">Time-sensitive client follow-ups due today or overdue.</p>
    </div>
    <a href="/followups" class="btn-subtle" style="font-size:12px;">All Follow-ups &rarr;</a>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Subject & Case Link</th>
          <th>Urgency</th>
          <th>Due At</th>
          <th style="text-align:right;">Quick Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(followup_rows) or '<tr><td colspan="5" class="muted" style="text-align:center;padding:18px;">No overdue or pending follow-ups for today.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 6. Right Bento Column: Active Blockers Detail Card
    if active_blockers:
        blocker_cards = []
        for b in active_blockers[:4]:
            b_id = b['id']
            e_type = b['entity_type']
            e_id = b['entity_id']
            dep_type = b.get('dependency_type') or 'Dependency'
            waiting_person = b.get('waiting_on_member_name') or b.get('waiting_on_role') or 'External'
            blocker_cards.append(f'''<div class="blocker-item-box">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
    <strong>{h(e_type.upper())} #{e_id}</strong>
    <span class="tag tag-err">{h(dep_type)}</span>
  </div>
  <div style="font-size:13px;color:#7f1d1d;margin-bottom:6px;">{h(b['description'])}</div>
  <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-muted);">
    <span>Waiting on: <strong>{h(waiting_person)}</strong></span>
    <form method="post" action="/blockers/resolve" style="margin:0;display:inline;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="id" value="{b_id}">
      <input type="hidden" name="resolution_notes" value="Resolved from Today dashboard">
      <button class="btn-subtle" style="padding:2px 6px;font-size:10px;">Resolve</button>
    </form>
  </div>
</div>''')
        blocker_widget = f'''<div class="card" style="border-top:3px solid var(--color-danger);">
  <div class="card-header-row">
    <h3 class="card-title" style="color:var(--color-danger);">Active Blockers ({len(active_blockers)})</h3>
    <a href="/work?status=blocked" class="btn-subtle" style="font-size:11px;">View All &rarr;</a>
  </div>
  {''.join(blocker_cards)}
</div>'''
    else:
        blocker_widget = '''<div class="card" style="border-top:3px solid var(--color-success);padding:16px;">
  <div style="display:flex;align-items:center;gap:10px;">
    <span class="tag tag-ok">✓ Unblocked</span>
    <span style="font-size:13px;color:var(--text-secondary);">No blockers logged. All pipelines running smoothly.</span>
  </div>
</div>'''

    # 7. Right Bento Column: Quick Work Launch & Shortcuts
    shortcuts_card = f'''<div class="card">
  <h3 class="card-title" style="margin-bottom:12px;">Quick Operations</h3>
  <div style="display:flex;flex-direction:column;gap:8px;">
    <a href="/work?action=new" class="btn-primary-action" style="justify-content:center;padding:9px 12px;font-size:13px;">
      + Create Work Item
    </a>
    <a href="/tests?action=new_case" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <span>🧪</span> <span style="font-weight:500;">Add Test Case</span>
    </a>
    <a href="/work?sub=inbox" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <span>📥</span> <span style="font-weight:500;">Triage Review Inbox ({len(inbox)})</span>
    </a>
    <a href="/reports" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <span>📊</span> <span style="font-weight:500;">Shift Handover & EOD Reports</span>
    </a>
    <a href="/workspace" class="btn-subtle" style="justify-content:flex-start;padding:8px 12px;">
      <span>📁</span> <span style="font-weight:500;">Google Workspace Hub (Drive/Tasks)</span>
    </a>
  </div>
</div>'''

    # 8. Right Bento Column: Recent Audit Log
    recent_audits = service.database.get_audit_log(limit=5)
    audit_rows = []
    for a in recent_audits:
        act = a.get('action', '')
        summary = a.get('summary', '') or act
        time_str = format_iso_time(a.get('created_at'))
        audit_rows.append(f'''<div style="padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:12px;">
  <div style="font-weight:500;color:var(--text-primary);">{h(summary)}</div>
  <div class="muted" style="font-size:11px;">{time_str} · {h(a.get('actor') or 'system')}</div>
</div>''')

    audit_card = f'''<div class="card">
  <div class="card-header-row">
    <h3 class="card-title">Activity Stream</h3>
    <a href="/audit" class="muted" style="font-size:11px;">Full log &rarr;</a>
  </div>
  {''.join(audit_rows) or '<p class="muted" style="font-size:12px;">No recent audit activity.</p>'}
</div>'''

    # Return full Bento Layout
    return f'''{hero_header}
{attention_strip}
{metric_band}

<div class="bento-split">
  <div class="bento-main">
    {work_queue_card}
    {followup_card}
  </div>
  <div class="bento-side">
    {blocker_widget}
    {shortcuts_card}
    {audit_card}
  </div>
</div>'''
