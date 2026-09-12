"""Follow-ups Management View."""
from html import escape as h
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import config
from dashboard_views.components import format_iso_time


def render_followups_view(service, csrf_token: str, params: dict) -> str:
    tab = (params.get('tab') or ['all'])[0].lower()
    followups = service.database.list_followups(limit=200)

    now_utc = datetime.now(timezone.utc).isoformat()
    today_prefix = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m-%d')

    pending = [f for f in followups if f.get('status') != 'completed']
    overdue = [f for f in pending if f.get('due_at') and f['due_at'] < now_utc]
    due_today = [f for f in pending if f.get('due_at') and f['due_at'].startswith(today_prefix)]
    upcoming = [f for f in pending if f.get('due_at') and f['due_at'] > now_utc and not f['due_at'].startswith(today_prefix)]
    completed = [f for f in followups if f.get('status') == 'completed']

    # Select list based on tab
    if tab == 'overdue':
        display_items = overdue
    elif tab == 'today':
        display_items = due_today
    elif tab == 'upcoming':
        display_items = upcoming
    elif tab == 'completed':
        display_items = completed
    else:
        display_items = pending

    def f_tab(name, key, count_val):
        active = (tab == key)
        cls = 'sub-nav-pill active' if active else 'sub-nav-pill'
        return f'<a href="/followups?tab={key}" class="{cls}">{name} <span class="nav-counter">{count_val}</span></a>'

    sub_nav = f'''<div class="sub-nav-wrapper">
  <div class="sub-nav-bar">
    {f_tab('All Pending', 'all', len(pending))}
    {f_tab('⚠️ Overdue', 'overdue', len(overdue))}
    {f_tab('📅 Due Today', 'today', len(due_today))}
    {f_tab('⏳ Upcoming', 'upcoming', len(upcoming))}
    {f_tab('✓ Completed', 'completed', len(completed))}
  </div>
</div>'''

    rows = []
    for f in display_items:
        f_id = f['id']
        c_id = f.get('case_id')
        title = f.get('title') or 'Follow-up'
        due_str = format_iso_time(f.get('due_at'))
        is_done = f.get('status') == 'completed'
        is_ovd = (f.get('due_at') or '') < now_utc and not is_done
        is_td = (f.get('due_at') or '').startswith(today_prefix) and not is_done

        if is_done:
            status_badge = '<span class="tag tag-ok">Completed</span>'
        elif is_ovd:
            status_badge = '<span class="tag tag-err">Overdue</span>'
        elif is_td:
            status_badge = '<span class="tag tag-warn">Due Today</span>'
        else:
            status_badge = '<span class="tag tag-info">Scheduled</span>'

        actions_html = ''
        if not is_done:
            actions_html = f'''<div style="display:inline-flex;gap:4px;">
  <form method="post" action="/followup/complete" style="margin:0;display:inline;">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="id" value="{f_id}">
    <button class="primary" style="padding:3px 8px;font-size:11px;">✓ Complete</button>
  </form>
  <form method="post" action="/followup/snooze" style="margin:0;display:inline;">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="id" value="{f_id}">
    <input type="hidden" name="days" value="1">
    <button class="btn-subtle" style="padding:3px 6px;font-size:11px;" title="Snooze 1 Day">+1d</button>
  </form>
  <form method="post" action="/followup/snooze" style="margin:0;display:inline;">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="id" value="{f_id}">
    <input type="hidden" name="days" value="3">
    <button class="btn-subtle" style="padding:3px 6px;font-size:11px;" title="Snooze 3 Days">+3d</button>
  </form>
</div>'''

        rows.append(f'''<tr>
  <td><strong>#{f_id}</strong></td>
  <td>
    <div style="font-weight:600;">{h(title)}</div>
    <div class="muted" style="font-size:12px;">{h(f.get("note") or "")}</div>
  </td>
  <td><a href="/work?sub=cases&item_type=case&item_id={c_id}" class="item-link">CASE-{c_id}</a></td>
  <td>{status_badge}</td>
  <td><span class="muted">{due_str}</span></td>
  <td style="text-align:right;">{actions_html}</td>
</tr>''')

    return f'''{sub_nav}
<div class="work-hub-header">
  <div>
    <h1>Follow-ups & Client Touchpoints</h1>
    <p class="muted">Track time-sensitive operational deliverables, stakeholder check-ins, and customer SLA responses.</p>
  </div>
</div>

<div class="card">
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Subject & Notes</th>
          <th>Linked Case</th>
          <th>Urgency</th>
          <th>Due At</th>
          <th style="text-align:right;">Actions</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) or '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px;">No follow-ups in this view.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''
