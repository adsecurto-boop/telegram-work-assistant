"""Shared UI components, helpers, and widgets for the Operations Hub Dashboard."""
from html import escape as h
import urllib.parse


def format_iso_time(iso_str: str | None) -> str:
    if not iso_str:
        return '—'
    s = iso_str.replace('T', ' ')
    if len(s) >= 16:
        return s[:16]
    return s


def render_status_badge(status: str | None, is_waiting: bool = False) -> str:
    st = (status or 'active').lower()
    if is_waiting or st in ('waiting', 'waiting_client', 'waiting_internal'):
        return '<span class="tag tag-warn" style="font-weight:600;"><span class="status-indicator waiting"></span>Waiting on External</span>'
    elif st in ('in_progress', 'active', 'open'):
        return '<span class="tag tag-info" style="font-weight:600;"><span class="status-indicator active"></span>In Progress</span>'
    elif st in ('blocked', 'defect'):
        return '<span class="tag tag-err" style="font-weight:600;"><span class="status-indicator blocked"></span>Blocked</span>'
    elif st in ('completed', 'resolved', 'closed', 'pass', 'passed'):
        return '<span class="tag tag-ok" style="font-weight:600;"><span class="status-indicator done"></span>Completed</span>'
    elif st in ('pending', 'draft', 'backlog'):
        return '<span class="tag" style="font-weight:500;"><span class="status-indicator pending"></span>Backlog</span>'
    return f'<span class="tag">{h(st.title())}</span>'


def render_priority_tag(priority: int | str | None) -> str:
    try:
        p = int(priority or 2)
    except Exception:
        p = 2
    if p == 1:
        return '<span class="p-badge p1" title="Priority 1 - Critical">P1 · Critical</span>'
    elif p == 2:
        return '<span class="p-badge p2" title="Priority 2 - High">P2 · High</span>'
    elif p == 3:
        return '<span class="p-badge p3" title="Priority 3 - Medium">P3 · Medium</span>'
    return '<span class="p-badge p4" title="Priority 4 - Low">P4 · Low</span>'


def render_type_badge(entity_type: str | None) -> str:
    t = (entity_type or 'item').lower()
    if t == 'requirement':
        return '<span class="type-tag req">REQ</span>'
    elif t == 'case':
        return '<span class="type-tag case">CASE</span>'
    elif t == 'task':
        return '<span class="type-tag task">TASK</span>'
    elif t == 'defect':
        return '<span class="type-tag defect">BUG</span>'
    elif t == 'test_case':
        return '<span class="type-tag test">TEST</span>'
    return f'<span class="type-tag">{h(t.upper()[:4])}</span>'


def render_attention_strip(active_blockers: list[dict], failed_tests: list[dict],
                           overdue_followups: list[dict], waiting_items: list[dict]) -> str:
    """Renders the top exception-first Attention Strip."""
    has_exceptions = bool(active_blockers or failed_tests or overdue_followups or waiting_items)
    
    if not has_exceptions:
        return '''<div class="attention-strip clear" role="region" aria-label="Operational Health">
  <div class="attention-strip-inner">
    <div class="attention-status-left">
      <span class="attention-icon-ok">✓</span>
      <div>
        <strong>All Operations Clear</strong>
        <span class="muted" style="margin-left:8px;">No active blockers, failed tests, or overdue items requiring urgent intervention.</span>
      </div>
    </div>
    <div class="attention-actions">
      <a href="/work" class="btn-subtle" style="font-size:12px;">Browse Work Items &rarr;</a>
    </div>
  </div>
</div>'''

    exception_chips = []
    if active_blockers:
        exception_chips.append(f'''<a href="/work?status=blocked" class="attn-badge attn-err" title="{len(active_blockers)} active blockers">
          <span class="attn-badge-dot"></span>
          <strong>{len(active_blockers)} Active Blocker{'s' if len(active_blockers) > 1 else ''}</strong>
        </a>''')

    if failed_tests:
        exception_chips.append(f'''<a href="/tests" class="attn-badge attn-err" title="{len(failed_tests)} test cases failed">
          <span class="attn-badge-dot"></span>
          <strong>{len(failed_tests)} Failed Test{'s' if len(failed_tests) > 1 else ''}</strong>
        </a>''')

    if overdue_followups:
        exception_chips.append(f'''<a href="/followups?tab=overdue" class="attn-badge attn-warn" title="{len(overdue_followups)} overdue followups">
          <span class="attn-badge-dot"></span>
          <strong>{len(overdue_followups)} Overdue Follow-up{'s' if len(overdue_followups) > 1 else ''}</strong>
        </a>''')

    if waiting_items:
        exception_chips.append(f'''<a href="/work?status=waiting" class="attn-badge attn-info" title="{len(waiting_items)} items waiting on external teammates or clients">
          <span class="attn-badge-dot"></span>
          <strong>{len(waiting_items)} Waiting on Others</strong>
        </a>''')

    # Top blocker preview summary text
    first_desc = ''
    if active_blockers:
        b = active_blockers[0]
        first_desc = f"Primary Blocker: {h(b['entity_type'].upper())} #{b['entity_id']} — {h(b.get('description', ''))[:80]}"
    elif failed_tests:
        ft = failed_tests[0]
        first_desc = f"Testing Blocker: TC-{ft['id']} ({h(ft.get('title', ''))[:60]}) failed"
    elif overdue_followups:
        of = overdue_followups[0]
        first_desc = f"Overdue: #{of['id']} (CASE-{of.get('case_id')}) due {format_iso_time(of.get('due_at'))}"
    elif waiting_items:
        wi = waiting_items[0]
        first_desc = f"Waiting on: {h(wi.get('waiting_on') or 'external feedback')} for {h(wi.get('display_id'))}"

    return f'''<div class="attention-strip alert" role="region" aria-label="Attention Required">
  <div class="attention-strip-inner">
    <div class="attention-status-left">
      <div class="attention-alert-icon">!</div>
      <div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:3px;">
          <strong style="color:#991b1b;font-size:14px;">Attention Required</strong>
          {''.join(exception_chips)}
        </div>
        <div class="attention-desc">{first_desc}</div>
      </div>
    </div>
    <div class="attention-actions">
      <a href="/work?status=blocked" class="btn-primary-action" style="font-size:12px;padding:6px 12px;background:#dc2626;border-color:#dc2626;">Triage Exceptions &rarr;</a>
    </div>
  </div>
</div>'''


def render_stage_pipeline(stages: list[dict], current_stage_id: int | None,
                          is_waiting: bool = False, clickable: bool = False,
                          entity_type: str = '', entity_id: int = 0, csrf_token: str = '') -> str:
    """Renders a visual horizontal stage pipeline with completed, current, and upcoming stages."""
    if not stages:
        return '<p class="muted" style="font-size:12px;">No workflow stages configured.</p>'

    current_idx = -1
    for idx, s in enumerate(stages):
        if s['id'] == current_stage_id:
            current_idx = idx
            break
    if current_idx == -1 and stages:
        current_idx = 0

    stage_nodes = []
    for idx, s in enumerate(stages):
        is_current = (idx == current_idx)
        is_done = (idx < current_idx)
        is_upcoming = (idx > current_idx)

        node_class = 'pipeline-step'
        if is_done:
            node_class += ' step-done'
            icon = '✓'
        elif is_current:
            node_class += ' step-current'
            if is_waiting or s.get('is_waiting'):
                node_class += ' step-waiting'
                icon = '⏳'
            else:
                icon = '●'
        else:
            node_class += ' step-upcoming'
            icon = str(idx + 1)

        role_info = f"<span class='step-role'>{h(s.get('expected_role') or '')}</span>" if s.get('expected_role') else ''
        dur_info = f"<span class='step-dur'>{s.get('expected_duration_hours')}h</span>" if s.get('expected_duration_hours') else ''

        stage_nodes.append(f'''<div class="{node_class}" title="{h(s.get('description') or s['name'])}">
  <div class="step-indicator">
    <span class="step-icon">{icon}</span>
  </div>
  <div class="step-details">
    <div class="step-title">{h(s['name'])}</div>
    <div class="step-meta">{role_info}{dur_info}</div>
  </div>
</div>''')

    return f'<div class="pipeline-bar">{"".join(stage_nodes)}</div>'
