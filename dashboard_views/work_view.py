"""Work Items Operations Hub view, including List/Table, Kanban, Detail Drawer, and Progressive Creation."""
from html import escape as h
from datetime import datetime, timezone
import urllib.parse
from dashboard_views.components import (
    render_status_badge,
    render_priority_tag,
    render_type_badge,
    render_stage_pipeline,
    format_iso_time
)


def render_work_view(service, csrf_token: str, params: dict, sub: str = 'items') -> str:
    # 1. Fetch data
    work_items = service.work_item_service.list_work_items()
    templates = service.workflow_service.list_templates()
    members = service.member_service.get_members()
    roles = service.member_service.get_roles()
    inbox = service.database.inbox()
    all_cases = service.database.list_cases(limit=100)

    # 2. Extract filter parameters
    filter_type = (params.get('type') or ['all'])[0].lower()
    filter_status = (params.get('status') or ['all'])[0].lower()
    filter_priority = (params.get('priority') or ['all'])[0]
    filter_client = (params.get('client') or ['all'])[0]
    filter_search = (params.get('q') or [''])[0].strip().lower()
    selected_item_type = (params.get('item_type') or [''])[0]
    selected_item_id_raw = (params.get('item_id') or [''])[0]
    is_new_action = (params.get('action') or [''])[0] == 'new'

    # Filter clients list for dropdown
    clients_set = set()
    for w in work_items:
        cl = w.get('client') or w.get('product')
        if cl:
            clients_set.add(cl)
    clients_list = sorted(list(clients_set))

    # Apply filters
    filtered_items = []
    for item in work_items:
        # Type filter
        if filter_type != 'all' and item.get('entity_type') != filter_type:
            continue
        
        # Status filter
        op_st = (item.get('operational_status') or 'active').lower()
        if filter_status == 'active' and op_st not in ('active', 'in_progress', 'open'):
            continue
        elif filter_status == 'blocked' and not item.get('is_blocked') and op_st != 'blocked':
            continue
        elif filter_status == 'waiting' and op_st != 'waiting' and not item.get('waiting_on'):
            continue
        elif filter_status == 'completed' and op_st not in ('completed', 'resolved', 'closed'):
            continue
        elif filter_status not in ('all', 'active', 'blocked', 'waiting', 'completed') and op_st != filter_status:
            continue

        # Priority filter
        if filter_priority != 'all':
            try:
                if int(item.get('priority') or 2) != int(filter_priority):
                    continue
            except Exception:
                pass

        # Client filter
        if filter_client != 'all':
            if (item.get('client') or item.get('product')) != filter_client:
                continue

        # Search query
        if filter_search:
            title = (item.get('title') or '').lower()
            d_id = (item.get('display_id') or '').lower()
            ticket = (item.get('ticket') or '').lower()
            if filter_search not in title and filter_search not in d_id and filter_search not in ticket:
                continue

        filtered_items.append(item)

    # Sub Navigation Pills
    def sub_pill(name, key, count_val=None):
        active = (sub == key)
        cls = 'sub-nav-pill active' if active else 'sub-nav-pill'
        cnt = f' <span class="nav-counter">{count_val}</span>' if count_val is not None else ''
        return f'<a href="/work?sub={key}" class="{cls}">{name}{cnt}</a>'

    sub_nav = f'''<div class="sub-nav-wrapper">
  <div class="sub-nav-bar">
    {sub_pill('📋 Work Items Table', 'items', len(work_items))}
    {sub_pill('📊 Kanban Board', 'kanban')}
    {sub_pill('📥 Review Inbox', 'inbox', len(inbox))}
    {sub_pill('📁 Cases Hub', 'cases', len(all_cases))}
  </div>
</div>'''

    # Filter Bar with Active Chips
    active_chips = []
    if filter_type != 'all':
        active_chips.append(f'<span class="filter-chip">Type: {h(filter_type.upper())} <a href="/work?sub={sub}&status={filter_status}&priority={filter_priority}&client={filter_client}&q={filter_search}">✕</a></span>')
    if filter_status != 'all':
        active_chips.append(f'<span class="filter-chip">Status: {h(filter_status.title())} <a href="/work?sub={sub}&type={filter_type}&priority={filter_priority}&client={filter_client}&q={filter_search}">✕</a></span>')
    if filter_priority != 'all':
        active_chips.append(f'<span class="filter-chip">Priority: P{filter_priority} <a href="/work?sub={sub}&type={filter_type}&status={filter_status}&client={filter_client}&q={filter_search}">✕</a></span>')
    if filter_client != 'all':
        active_chips.append(f'<span class="filter-chip">Client: {h(filter_client)} <a href="/work?sub={sub}&type={filter_type}&status={filter_status}&priority={filter_priority}&q={filter_search}">✕</a></span>')
    if filter_search:
        active_chips.append(f'<span class="filter-chip">Search: "{h(filter_search)}" <a href="/work?sub={sub}&type={filter_type}&status={filter_status}&priority={filter_priority}&client={filter_client}">✕</a></span>')

    chips_html = ''
    if active_chips:
        chips_html = f'''<div class="active-filter-chips">
  <span class="muted" style="font-size:12px;font-weight:600;">Active Filters:</span>
  {''.join(active_chips)}
  <a href="/work?sub={sub}" class="clear-all-link">Clear All</a>
</div>'''

    type_options = ''.join(f'<option value="{t}" {"selected" if filter_type==t else ""}>{label}</option>' for t, label in [
        ('all', 'All Work Types'),
        ('requirement', 'Requirements'),
        ('task', 'Tasks'),
        ('case', 'Support Cases')
    ])

    status_options = ''.join(f'<option value="{st}" {"selected" if filter_status==st else ""}>{label}</option>' for st, label in [
        ('all', 'All Statuses'),
        ('active', 'Active / In Progress'),
        ('blocked', 'Blocked (Active Blocker)'),
        ('waiting', 'Waiting on External'),
        ('completed', 'Completed / Resolved')
    ])

    priority_options = ''.join(f'<option value="{p}" {"selected" if filter_priority==str(p) else ""}>{label}</option>' for p, label in [
        ('all', 'All Priorities'),
        ('1', 'P1 · Critical'),
        ('2', 'P2 · High'),
        ('3', 'P3 · Medium'),
        ('4', 'P4 · Low')
    ])

    client_options = '<option value="all">All Clients / Products</option>' + ''.join(
        f'<option value="{h(c)}" {"selected" if filter_client==c else ""}>{h(c)}</option>' for c in clients_list
    )

    filter_bar = f'''<div class="filter-bar">
  <form method="get" action="/work" style="display:contents;">
    <input type="hidden" name="sub" value="{sub}">
    <div style="flex:1;min-width:180px;">
      <input type="text" name="q" placeholder="Search work items by title, ticket, or ID..." value="{h(filter_search)}" style="width:100%;">
    </div>
    <select name="type" onchange="this.form.submit()">{type_options}</select>
    <select name="status" onchange="this.form.submit()">{status_options}</select>
    <select name="priority" onchange="this.form.submit()">{priority_options}</select>
    <select name="client" onchange="this.form.submit()">{client_options}</select>
    <button type="submit" class="btn-subtle">Filter</button>
  </form>
  <div style="margin-left:auto;display:flex;gap:8px;">
    <button type="button" class="btn-primary-action" onclick="document.getElementById('new-item-modal').style.display='flex'">+ New Work Item</button>
  </div>
</div>
{chips_html}'''

    # Table View Content
    if sub == 'items':
        rows = []
        for item in filtered_items:
            e_type = item.get('entity_type', 'item')
            e_id = item.get('entity_id')
            d_id = item.get('display_id') or f"{e_type.upper()}-{e_id}"
            title = item.get('title') or 'Untitled'
            t_badge = render_type_badge(e_type)
            p_badge = render_priority_tag(item.get('priority'))
            s_badge = render_status_badge(item.get('operational_status'), is_waiting=bool(item.get('waiting_on')))
            stage_name = item.get('current_stage_name') or 'Backlog'
            client_name = item.get('client') or item.get('product') or '—'
            owner = item.get('owner_name') or 'Unassigned'
            due = format_iso_time(item.get('due_date'))
            
            blockers_count = item.get('active_blockers_count', 0)
            blocker_tag = f'<span class="tag tag-err" style="font-size:11px;">{blockers_count} Blocker{"s" if blockers_count>1 else ""}</span>' if blockers_count > 0 else ''

            rows.append(f'''<tr class="{'row-blocked' if blockers_count > 0 else ''}">
  <td>
    {t_badge}
    <strong><a href="/work?sub={sub}&item_type={e_type}&item_id={e_id}" class="item-link">{h(d_id)}</a></strong>
  </td>
  <td>
    <div style="font-weight:600;"><a href="/work?sub={sub}&item_type={e_type}&item_id={e_id}" style="color:var(--text-primary);text-decoration:none;">{h(title)}</a></div>
    <div class="muted" style="font-size:12px;">{h(client_name)} {f'· Ticket: {h(item["ticket"])}' if item.get('ticket') else ''}</div>
  </td>
  <td><span class="stage-pill">{h(stage_name)}</span></td>
  <td>{p_badge}</td>
  <td>{s_badge}</td>
  <td><span class="muted" style="font-size:12px;">{h(owner)}</span></td>
  <td><span class="muted">{due}</span></td>
  <td>{blocker_tag}</td>
  <td style="text-align:right;">
    <a href="/work?sub={sub}&item_type={e_type}&item_id={e_id}" class="btn-subtle" style="padding:4px 8px;font-size:12px;">Detail &rarr;</a>
  </td>
</tr>''')

        main_content = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">All Work Items ({len(filtered_items)})</h2>
      <p class="muted" style="margin:2px 0 0 0;">Unified operational registry spanning Requirements, QA Cases, Tasks, and Defects.</p>
    </div>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Title & Details</th>
          <th>Workflow Stage</th>
          <th>Priority</th>
          <th>Status</th>
          <th>Assignee</th>
          <th>Due Date</th>
          <th>Blockers</th>
          <th style="text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) or '<tr><td colspan="9" class="muted" style="text-align:center;padding:32px;">No work items matching the current filter criteria.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # Kanban View Content
    elif sub == 'kanban':
        cols = {
            'backlog': {'title': 'Backlog / Draft', 'items': []},
            'active': {'title': 'In Progress', 'items': []},
            'blocked': {'title': 'Blocked / Waiting', 'items': []},
            'review': {'title': 'QA & Review', 'items': []},
            'completed': {'title': 'Completed / Closed', 'items': []},
        }

        for item in filtered_items:
            st = (item.get('operational_status') or 'active').lower()
            if item.get('is_blocked') or st == 'blocked' or item.get('waiting_on'):
                cols['blocked']['items'].append(item)
            elif st in ('completed', 'resolved', 'closed'):
                cols['completed']['items'].append(item)
            elif 'review' in (item.get('current_stage_name') or '').lower() or 'qa' in (item.get('current_stage_name') or '').lower() or 'test' in (item.get('current_stage_name') or '').lower():
                cols['review']['items'].append(item)
            elif st in ('in_progress', 'active', 'open'):
                cols['active']['items'].append(item)
            else:
                cols['backlog']['items'].append(item)

        board_cols_html = []
        for col_key, col_data in cols.items():
            card_items_html = []
            for item in col_data['items']:
                e_type = item.get('entity_type', 'item')
                e_id = item.get('entity_id')
                d_id = item.get('display_id') or f"{e_type.upper()}-{e_id}"
                t_badge = render_type_badge(e_type)
                p_badge = render_priority_tag(item.get('priority'))
                stage_name = item.get('current_stage_name') or 'Backlog'
                client_name = item.get('client') or item.get('product') or ''
                blockers_count = item.get('active_blockers_count', 0)

                card_items_html.append(f'''<div class="kanban-item {'card-blocked' if blockers_count>0 else ''}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
    <div>{t_badge} <strong><a href="/work?sub=kanban&item_type={e_type}&item_id={e_id}" style="color:var(--color-primary);text-decoration:none;">{h(d_id)}</a></strong></div>
    {p_badge}
  </div>
  <div style="font-weight:600;font-size:13px;margin-bottom:6px;"><a href="/work?sub=kanban&item_type={e_type}&item_id={e_id}" style="color:var(--text-primary);text-decoration:none;">{h(item.get('title') or 'Untitled')}</a></div>
  <div class="muted" style="font-size:11px;margin-bottom:6px;">{h(client_name)}</div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:6px;border-top:1px solid #f1f5f9;">
    <span class="stage-pill" style="font-size:11px;">{h(stage_name)}</span>
    <a href="/work?sub=kanban&item_type={e_type}&item_id={e_id}" class="btn-subtle" style="padding:2px 6px;font-size:11px;">Open &rarr;</a>
  </div>
</div>''')

            board_cols_html.append(f'''<div class="kanban-col">
  <h3>
    <span>{col_data['title']}</span>
    <span class="tag" style="background:#e2e8f0;">{len(col_data['items'])}</span>
  </h3>
  {''.join(card_items_html) or '<div class="muted" style="text-align:center;padding:24px 0;font-size:12px;">No items in this stage</div>'}
</div>''')

        main_content = f'''<div class="card" style="padding:14px;">
  <div class="kanban-board">
    {''.join(board_cols_html)}
  </div>
</div>'''

    # Review Inbox Content
    elif sub == 'inbox':
        rows = []
        for msg in inbox:
            m_id = msg['id']
            author = msg.get('author_name') or 'Telegram User'
            text = msg.get('text') or ''
            time_str = format_iso_time(msg.get('occurred_at'))
            rows.append(f'''<tr>
  <td><input type="checkbox" name="message_ids" value="{m_id}"></td>
  <td><strong>#{m_id}</strong></td>
  <td><span class="muted">{time_str}</span></td>
  <td><strong>{h(author)}</strong></td>
  <td><div style="max-width:500px;font-size:13px;">{h(text)}</div></td>
  <td style="text-align:right;">
    <form method="post" action="/inbox/bulk/confirm" style="margin:0;display:inline;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="bulk_token" value="">
    </form>
  </td>
</tr>''')

        main_content = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Review Inbox ({len(inbox)})</h2>
      <p class="muted">Triage incoming messages and connectors into tasks or cases.</p>
    </div>
    <a href="/inbox" class="btn-subtle">Advanced Triage Tools &rarr;</a>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th width="30"><input type="checkbox" onclick="document.querySelectorAll('input[name=message_ids]').forEach(c=>c.checked=this.checked)"></th>
          <th>ID</th>
          <th>Received At</th>
          <th>Author</th>
          <th>Message Text</th>
          <th style="text-align:right;">Actions</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) or '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px;">Inbox is empty. All messages triaged!</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # Cases Hub Content
    elif sub == 'cases':
        rows = []
        for c in all_cases:
            c_id = c['id']
            title = c.get('title') or 'Untitled Case'
            status = c.get('status') or 'open'
            client_name = c.get('client') or '—'
            p_badge = render_priority_tag(c.get('priority'))
            s_badge = render_status_badge(status)
            rows.append(f'''<tr>
  <td><strong><a href="/work?sub=cases&item_type=case&item_id={c_id}" class="item-link">CASE-{c_id}</a></strong></td>
  <td>
    <div style="font-weight:600;"><a href="/work?sub=cases&item_type=case&item_id={c_id}" style="color:var(--text-primary);text-decoration:none;">{h(title)}</a></div>
    <div class="muted" style="font-size:12px;">{h(client_name)} {f'· Ticket: {h(c["ticket"])}' if c.get('ticket') else ''}</div>
  </td>
  <td>{p_badge}</td>
  <td>{s_badge}</td>
  <td><span class="muted">{h(c.get('waiting_on') or '—')}</span></td>
  <td style="text-align:right;">
    <a href="/work?sub=cases&item_type=case&item_id={c_id}" class="btn-subtle" style="padding:4px 8px;font-size:12px;">Detail &rarr;</a>
  </td>
</tr>''')

        main_content = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Support Cases Hub ({len(all_cases)})</h2>
      <p class="muted">Customer issues, operational tickets, and stakeholder communications.</p>
    </div>
    <a href="/work?action=new&default_type=case" class="btn-primary-action">+ New Case</a>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>Case ID</th>
          <th>Title & Client</th>
          <th>Priority</th>
          <th>Status</th>
          <th>Waiting On</th>
          <th style="text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) or '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px;">No support cases recorded.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # Work Item Detail Drawer / Panel (if selected)
    drawer_html = ''
    if selected_item_type and selected_item_id_raw.isdigit():
        detail_id = int(selected_item_id_raw)
        detail = service.work_item_service.get_work_item_detail(selected_item_type, detail_id)
        if detail:
            drawer_html = render_work_item_drawer(detail, csrf_token, members, templates, roles, sub)

    # Progressive "+ New Work Item" Modal (Always in DOM, toggled via CSS/JS)
    new_modal_html = render_new_work_item_modal(
        csrf_token, members, templates, sub,
        params.get('default_type', ['requirement'])[0],
        is_open=is_new_action
    )

    return f'''{sub_nav}
<div class="work-hub-header">
  <div>
    <h1>Work Items Hub</h1>
    <p class="muted">Unified workspace for Requirements, Tasks, QA Test Cases, and Support Cases.</p>
  </div>
</div>
{filter_bar}
{main_content}
{drawer_html}
{new_modal_html}'''


def render_work_item_drawer(detail: dict, csrf_token: str, members: list[dict],
                            templates: list[dict], roles: list[dict], sub: str) -> str:
    """Renders the comprehensive, responsive Right-Side Detail Drawer for a single work item."""
    e_type = detail.get('entity_type', 'item')
    e_id = detail.get('entity_id')
    d_id = detail.get('display_id') or f"{e_type.upper()}-{e_id}"
    title = detail.get('title') or 'Untitled'
    op_status = detail.get('operational_status') or 'active'
    priority = detail.get('priority', 2)
    stages = detail.get('stages', [])
    current_stage_id = detail.get('current_stage_id')
    is_blocked = bool(detail.get('active_blockers'))
    
    # Visual Workflow Pipeline
    pipeline_html = render_stage_pipeline(
        stages=stages,
        current_stage_id=current_stage_id,
        is_waiting=bool(detail.get('waiting_on')),
        clickable=True,
        entity_type=e_type,
        entity_id=e_id,
        csrf_token=csrf_token
    )

    # Stage options for stage advancement
    stage_options = ''.join(
        f'<option value="{s["id"]}" {"selected" if s["id"]==current_stage_id else ""}>{s["stage_order"]}. {h(s["name"])} ({h(s.get("expected_role") or "Any")})</option>'
        for s in stages
    )

    # Member options for assignment
    member_options = '<option value="">Unassigned</option>' + ''.join(
        f'<option value="{m["id"]}" {"selected" if m["id"]==detail.get("owner_member_id") else ""}>{h(m["name"])} ({h(", ".join(m.get("roles", [])))})</option>'
        for m in members
    )

    # Priority options
    p_options = ''.join(
        f'<option value="{p}" {"selected" if str(priority)==str(p) else ""}>{label}</option>'
        for p, label in [(1, 'P1 · Critical'), (2, 'P2 · High'), (3, 'P3 · Medium'), (4, 'P4 · Low')]
    )

    # Status options
    status_options = ''.join(
        f'<option value="{st}" {"selected" if op_status==st else ""}>{label}</option>'
        for st, label in [
            ('active', 'Active / In Progress'),
            ('blocked', 'Blocked'),
            ('waiting', 'Waiting on External'),
            ('completed', 'Completed / Resolved'),
            ('cancelled', 'Cancelled')
        ]
    )

    # 1. Blockers List & Forms
    blockers_list = detail.get('blockers', [])
    blocker_items_html = []
    for b in blockers_list:
        b_id = b['id']
        is_act = b.get('status') == 'active'
        dep_type = b.get('dependency_type') or 'Dependency'
        badge = '<span class="tag tag-err">Active Blocker</span>' if is_act else '<span class="tag tag-ok">Resolved</span>'
        
        resolve_btn = ''
        if is_act:
            resolve_btn = f'''<form method="post" action="/blockers/resolve" style="margin:4px 0 0 0;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="id" value="{b_id}">
  <input name="resolution_notes" placeholder="Resolution notes..." style="font-size:11px;padding:3px 6px;width:160px;">
  <button class="primary" style="padding:3px 8px;font-size:11px;">Resolve Blocker</button>
</form>'''

        blocker_items_html.append(f'''<div class="blocker-card-drawer {'is-active' if is_act else 'is-resolved'}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
    <div><strong>{h(dep_type)}</strong> · <span class="muted">#{b_id}</span></div>
    {badge}
  </div>
  <div style="font-size:13px;margin-bottom:4px;">{h(b.get('description') or '')}</div>
  <div class="muted" style="font-size:11px;">Logged: {format_iso_time(b.get('created_at'))} · Waiting on: <strong>{h(b.get('waiting_on_role') or 'External')}</strong></div>
  {resolve_btn}
</div>''')

    add_blocker_form = f'''<form method="post" action="/blockers/create" class="drawer-subform">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="entity_type" value="{e_type}">
  <input type="hidden" name="entity_id" value="{e_id}">
  <h4 style="margin:0 0 8px 0;font-size:13px;">+ Log New Blocker / Dependency</h4>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;">
    <select name="dependency_type" required>
      <option value="Development">Development Dependency</option>
      <option value="Vendor">Vendor / 3rd Party</option>
      <option value="Client">Client Feedback</option>
      <option value="Environment">Environment / Infra</option>
      <option value="Review">Internal QA / Review</option>
    </select>
    <input name="waiting_on_role" placeholder="Waiting on role/person...">
  </div>
  <textarea name="description" rows="2" placeholder="Describe the blocker reason and unblock condition..." required style="width:100%;margin-bottom:8px;"></textarea>
  <button class="danger" style="font-size:12px;padding:5px 12px;">Log Blocker</button>
</form>'''

    # 2. Related Testing & QA Section (for Requirements)
    related_testing_html = ''
    if e_type == 'requirement':
        conds = detail.get('test_conditions', [])
        tcs = detail.get('related_test_cases', [])
        defs = detail.get('defects', [])
        arts = detail.get('artifacts', [])
        posture = detail.get('posture', {})
        timeline_events = detail.get('timeline', [])

        # Posture Banner
        ready_cls = 'tag-ok' if posture.get('readiness') == 'READY' else 'tag-err'
        ready_label = 'READY FOR SIGN-OFF' if posture.get('readiness') == 'READY' else 'TESTING IN PROGRESS'
        posture_banner = f'''<div style="background:#f8fafc;border:1px solid var(--border-subtle);border-radius:var(--radius-md);padding:12px;margin-bottom:14px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <strong style="font-size:12px;">Testing Posture & Sign-Off Readiness</strong>
    <span class="tag {ready_cls}">{ready_label}</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:8px;font-size:12px;margin-top:8px;">
    <div>Passed: <strong style="color:var(--color-success);">{posture.get('passed_tests', 0)}/{posture.get('test_cases_count', 0)}</strong></div>
    <div>Conditions: <strong>{posture.get('test_conditions_count', 0)}</strong></div>
    <div>Defects: <strong style="color:{'var(--color-danger)' if posture.get('active_defects', 0) > 0 else 'var(--text-primary)'};">{posture.get('active_defects', 0)} active</strong></div>
    <div>Blockers: <strong>{posture.get('active_blockers', 0)}</strong></div>
  </div>
</div>'''

        # Test Conditions Rows
        cond_rows = []
        for c in conds:
            c_id = c['id']
            c_st = c.get('status') or 'draft'
            c_risk = c.get('risk_level') or 'medium'
            risk_badge = f'<span class="tag tag-err">High Risk</span>' if c_risk == 'high' else f'<span class="tag">Med Risk</span>'
            st_badge = render_status_badge(c_st)
            approve_btn = f'''<form method="post" action="/tests/conditions/approve" style="margin:0;display:inline;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="id" value="{c_id}">
  <button class="btn-subtle" style="padding:1px 5px;font-size:10px;">Approve</button>
</form>''' if c_st != 'approved' else ''

            cond_rows.append(f'''<tr>
  <td><strong>COND-{c_id}</strong></td>
  <td>
    <div style="font-weight:600;font-size:12px;">{h(c['title'])}</div>
    <div class="muted" style="font-size:11px;">{h(c.get('category', 'functional').title())}</div>
  </td>
  <td>{risk_badge}</td>
  <td>{st_badge}</td>
  <td style="text-align:right;">{approve_btn}</td>
</tr>''')

        # Test Cases Rows
        tc_rows = []
        for tc in tcs:
            tc_id = tc['id']
            last_res = tc.get('last_execution_result') or 'not_run'
            res_badge = render_status_badge(last_res)
            tc_rows.append(f'''<tr>
  <td><strong>TC-{tc_id}</strong></td>
  <td>
    <div style="font-weight:600;font-size:12px;">{h(tc['title'])}</div>
    <div class="muted" style="font-size:11px;">{h(tc.get('objective') or '')[:70]}</div>
  </td>
  <td>{res_badge}</td>
  <td style="text-align:right;">
    <form method="post" action="/tests/executions/record" style="margin:0;display:inline-flex;gap:3px;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="test_case_id" value="{tc_id}">
      <select name="result" style="padding:2px 4px;font-size:11px;">
        <option value="pass">Pass</option>
        <option value="fail">Fail</option>
        <option value="blocked">Blocked</option>
      </select>
      <input name="actual_result" placeholder="Notes" style="padding:2px 4px;font-size:11px;width:75px;">
      <button class="primary" style="padding:2px 5px;font-size:11px;">Run</button>
    </form>
  </td>
</tr>''')

        # Defects Rows
        def_rows = []
        for d in defs:
            d_id = d['id']
            d_sev = d.get('severity') or 'major'
            d_st = d.get('status') or 'new'
            sev_badge = f'<span class="tag tag-err">{h(d_sev.upper())}</span>' if d_sev in ('blocker', 'critical') else f'<span class="tag tag-warn">{h(d_sev.title())}</span>'
            st_badge = render_status_badge(d_st)
            retest_btn = f'''<form method="post" action="/tests/defects/retest" style="margin:0;display:inline-flex;gap:2px;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="defect_id" value="{d_id}">
  <select name="result" style="padding:1px 3px;font-size:10px;">
    <option value="pass">Pass (Close)</option>
    <option value="fail">Fail (Reopen)</option>
  </select>
  <button class="primary" style="padding:1px 4px;font-size:10px;">Retest</button>
</form>''' if d_st in ('fix_ready', 'retest_required', 'retesting', 'in_development') else ''

            def_rows.append(f'''<tr>
  <td><strong>DEF-{d_id}</strong></td>
  <td>
    <div style="font-weight:600;font-size:12px;">{h(d['title'])}</div>
    <div class="muted" style="font-size:11px;">Build: {h(d.get('build_found') or '—')}</div>
  </td>
  <td>{sev_badge}</td>
  <td>{st_badge}</td>
  <td style="text-align:right;">{retest_btn}</td>
</tr>''')

        # Artifacts Rows
        art_rows = []
        for a in arts:
            a_id = a['id']
            a_name = a.get('name') or 'Artifact'
            a_url = a.get('url')
            a_link = f'<a href="{h(a_url)}" target="_blank" class="item-link">{h(a_name)} ↗</a>' if a_url else h(a_name)
            art_rows.append(f'''<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #f1f5f9;font-size:12px;">
  <div><span class="tag tag-info">{h(a.get('artifact_type', 'doc').upper())}</span> {a_link}</div>
  <form method="post" action="/artifacts/delete" style="margin:0;">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <input type="hidden" name="id" value="{a_id}">
    <button class="btn-subtle" style="padding:1px 4px;font-size:10px;color:var(--color-danger);">&times;</button>
  </form>
</div>''')

        add_cond_form = f'''<form method="post" action="/tests/conditions/create" class="drawer-subform" style="margin-top:8px;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="requirement_id" value="{e_id}">
  <h4 style="margin:0 0 4px 0;font-size:12px;">+ Add Test Condition</h4>
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:4px;margin-bottom:4px;">
    <input name="title" placeholder="Condition title..." required style="font-size:11px;">
    <select name="category" style="font-size:11px;">
      <option value="functional">Functional</option>
      <option value="boundary">Boundary</option>
      <option value="negative">Negative</option>
      <option value="regression">Regression</option>
    </select>
    <select name="risk_level" style="font-size:11px;">
      <option value="high">High Risk</option>
      <option value="medium" selected>Med Risk</option>
      <option value="low">Low Risk</option>
    </select>
  </div>
  <button class="btn-subtle" style="font-size:11px;padding:3px 8px;">+ Save Condition</button>
</form>'''

        add_tc_form = f'''<form method="post" action="/tests/cases/create" class="drawer-subform" style="margin-top:8px;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="requirement_id" value="{e_id}">
  <h4 style="margin:0 0 4px 0;font-size:12px;">+ Add Test Case</h4>
  <input name="title" placeholder="Test case title..." required style="width:100%;margin-bottom:4px;font-size:11px;">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px;">
    <textarea name="objective" rows="2" placeholder="Objective..." style="font-size:11px;"></textarea>
    <textarea name="expected_result" rows="2" placeholder="Expected result..." style="font-size:11px;"></textarea>
  </div>
  <button class="btn-subtle" style="font-size:11px;padding:3px 8px;">+ Save Test Case</button>
</form>'''

        add_def_form = f'''<form method="post" action="/tests/defects/create" class="drawer-subform" style="margin-top:8px;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="requirement_id" value="{e_id}">
  <h4 style="margin:0 0 4px 0;font-size:12px;">+ Log Defect on Requirement</h4>
  <input name="title" placeholder="Defect summary..." required style="width:100%;margin-bottom:4px;font-size:11px;">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px;">
    <select name="severity" style="font-size:11px;">
      <option value="blocker">Blocker</option>
      <option value="critical">Critical</option>
      <option value="major" selected>Major</option>
      <option value="minor">Minor</option>
    </select>
    <input name="build_found" placeholder="Build (e.g. rc1)" style="font-size:11px;">
  </div>
  <textarea name="steps_to_reproduce" rows="2" placeholder="Steps to reproduce..." style="width:100%;margin-bottom:4px;font-size:11px;"></textarea>
  <button class="danger" style="font-size:11px;padding:3px 8px;">Log Defect</button>
</form>'''

        add_art_form = f'''<form method="post" action="/artifacts/create" class="drawer-subform" style="margin-top:8px;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="entity_type" value="{e_type}">
  <input type="hidden" name="entity_id" value="{e_id}">
  <h4 style="margin:0 0 4px 0;font-size:12px;">+ Link Google Workspace Document / Artifact</h4>
  <div style="display:grid;grid-template-columns:2fr 1fr;gap:4px;margin-bottom:4px;">
    <input name="name" placeholder="Title (e.g. Test Plan Doc)" required style="font-size:11px;">
    <select name="artifact_type" style="font-size:11px;">
      <option value="doc">Google Doc</option>
      <option value="sheet">Google Sheet</option>
      <option value="drive">Drive Folder</option>
      <option value="log">Test Log</option>
    </select>
  </div>
  <input name="url" placeholder="https://docs.google.com/..." style="width:100%;margin-bottom:4px;font-size:11px;">
  <button class="btn-subtle" style="font-size:11px;padding:3px 8px;">+ Link Artifact</button>
</form>'''

        related_testing_html = f'''<div class="drawer-section">
  <h3 class="drawer-section-title">🧪 Testing Operations & Quality Hub</h3>
  {posture_banner}
  
  <!-- Test Conditions -->
  <div style="margin-bottom:12px;">
    <div style="font-weight:600;font-size:12px;margin-bottom:4px;">Test Conditions ({len(conds)})</div>
    <div class="table-scroll-wrap">
      <table>
        <thead><tr><th>ID</th><th>Condition</th><th>Risk</th><th>Status</th><th>Action</th></tr></thead>
        <tbody>{''.join(cond_rows) or '<tr><td colspan="5" class="muted" style="text-align:center;padding:8px;">No test conditions.</td></tr>'}</tbody>
      </table>
    </div>
    {add_cond_form}
  </div>

  <!-- Test Cases -->
  <div style="margin-bottom:12px;">
    <div style="font-weight:600;font-size:12px;margin-bottom:4px;">Test Cases & Quick Execution ({len(tcs)})</div>
    <div class="table-scroll-wrap">
      <table>
        <thead><tr><th>ID</th><th>Title</th><th>Last Result</th><th style="text-align:right;">Execute</th></tr></thead>
        <tbody>{''.join(tc_rows) or '<tr><td colspan="4" class="muted" style="text-align:center;padding:8px;">No test cases.</td></tr>'}</tbody>
      </table>
    </div>
    {add_tc_form}
  </div>

  <!-- Defects -->
  <div style="margin-bottom:12px;">
    <div style="font-weight:600;font-size:12px;margin-bottom:4px;">Defects & Retesting ({len(defs)})</div>
    <div class="table-scroll-wrap">
      <table>
        <thead><tr><th>ID</th><th>Defect</th><th>Severity</th><th>Status</th><th style="text-align:right;">Retest</th></tr></thead>
        <tbody>{''.join(def_rows) or '<tr><td colspan="5" class="muted" style="text-align:center;padding:8px;">No defects logged.</td></tr>'}</tbody>
      </table>
    </div>
    {add_def_form}
  </div>

  <!-- Artifacts -->
  <div>
    <div style="font-weight:600;font-size:12px;margin-bottom:4px;">Linked Workspace Artifacts ({len(arts)})</div>
    {''.join(art_rows) or '<p class="muted" style="font-size:11px;margin:0 0 6px 0;">No linked artifacts.</p>'}
    {add_art_form}
  </div>
</div>'''

    # 3. Stage Transition History
    history_list = detail.get('stage_history', [])
    history_items_html = []
    for hist in history_list:
        st_name = hist.get('stage_name') or f"Stage #{hist.get('stage_id')}"
        actor = hist.get('actor_name') or 'Team Member'
        ent_time = format_iso_time(hist.get('entered_at'))
        note = hist.get('note') or ''
        note_div = f'<div style="font-size:12px;margin-top:2px;color:var(--text-secondary);">{h(note)}</div>' if note else ''
        history_items_html.append(f'''<div class="history-item">
  <div class="history-dot"></div>
  <div class="history-content">
    <div style="font-weight:600;font-size:12px;">{h(st_name)}</div>
    <div class="muted" style="font-size:11px;">{ent_time} · by {h(actor)}</div>
    {note_div}
  </div>
</div>''')

    req_fields_html = ''
    if e_type == 'requirement':
        u_story = h(detail.get("user_story") or "")
        a_crit = h(detail.get("acceptance_criteria") or "")
        req_fields_html = f'''<div style="margin-bottom:12px;">
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">User Story</label>
          <textarea name="user_story" rows="2" style="width:100%;">{u_story}</textarea>
        </div>
        <div style="margin-bottom:12px;">
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Acceptance Criteria</label>
          <textarea name="acceptance_criteria" rows="3" style="width:100%;">{a_crit}</textarea>
        </div>'''

    return f'''<div class="drawer-overlay" id="work-item-drawer" onclick="if(event.target===this) window.location.href='/work?sub={sub}'">
  <div class="drawer-panel" role="dialog" aria-label="Work Item Details">
    <!-- Drawer Header -->
    <div class="drawer-header">
      <div style="display:flex;align-items:center;gap:8px;">
        {render_type_badge(e_type)}
        <span class="drawer-display-id">{h(d_id)}</span>
        {render_status_badge(op_status, is_waiting=bool(detail.get('waiting_on')))}
        {render_priority_tag(priority)}
      </div>
      <a href="/work?sub={sub}" class="drawer-close-btn" title="Close Drawer (ESC)">&times;</a>
    </div>

    <!-- Drawer Body -->
    <div class="drawer-body">
      <!-- Workflow Stage Banner & Advance Form -->
      <div class="drawer-section" style="background:#f8fafc;padding:14px;border-radius:var(--radius-md);border:1px solid var(--border-subtle);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <strong style="font-size:13px;color:var(--text-primary);">Workflow Lifecycle Progression</strong>
          <span class="tag tag-info">Template: {h(detail.get("workflow_template", {}).get("name") if detail.get("workflow_template") else "Default")}</span>
        </div>
        {pipeline_html}
        <form method="post" action="/workflow/stage/transition" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <input type="hidden" name="entity_type" value="{e_type}">
          <input type="hidden" name="entity_id" value="{e_id}">
          <label style="font-size:12px;font-weight:600;">Advance Stage:</label>
          <select name="new_stage_id" style="flex:1;min-width:180px;">{stage_options}</select>
          <input name="transition_note" placeholder="Optional transition note..." style="flex:1;min-width:160px;font-size:12px;">
          <button class="primary" style="font-size:12px;padding:6px 12px;">Transition Stage</button>
        </form>
      </div>

      <!-- Core Fields Edit Form -->
      <form method="post" action="/work/item/update" class="drawer-section">
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <input type="hidden" name="entity_type" value="{e_type}">
        <input type="hidden" name="entity_id" value="{e_id}">
        
        <h3 class="drawer-section-title">📝 Core Item Details</h3>
        
        <div style="margin-bottom:12px;">
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Title</label>
          <input name="title" value="{h(title)}" required style="width:100%;">
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Priority</label>
            <select name="priority" style="width:100%;">{p_options}</select>
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Operational Status</label>
            <select name="operational_status" style="width:100%;">{status_options}</select>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Assignee / Owner</label>
            <select name="owner_member_id" style="width:100%;">{member_options}</select>
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Due Date</label>
            <input type="date" name="due_date" value="{h(detail.get('due_date') or '')}" style="width:100%;">
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Client / Project</label>
            <input name="client" value="{h(detail.get('client') or detail.get('product') or '')}" style="width:100%;">
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Ticket Reference</label>
            <input name="ticket" value="{h(detail.get('ticket') or '')}" placeholder="e.g. JIRA-104" style="width:100%;">
          </div>
        </div>

        {req_fields_html}

        <div style="margin-bottom:14px;">
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Next Action / Waiting Context</label>
          <input name="next_action" value="{h(detail.get('next_action') or detail.get('waiting_on') or '')}" placeholder="What is the next operational step..." style="width:100%;">
        </div>

        <div style="display:flex;justify-content:space-between;align-items:center;">
          <button class="primary" style="padding:8px 16px;">Save Changes</button>
          <button type="button" class="danger" onclick="document.getElementById('delete-confirm-modal').style.display='flex'" style="padding:6px 12px;font-size:12px;">Delete Work Item</button>
        </div>
      </form>

      <!-- Blockers & Dependencies Section -->
      <div class="drawer-section">
        <h3 class="drawer-section-title">🚫 Blockers & Waiting Dependencies ({len(detail.get('active_blockers', []))} Active)</h3>
        {''.join(blocker_items_html) or '<p class="muted" style="font-size:12px;margin-bottom:12px;">No blockers recorded for this item.</p>'}
        {add_blocker_form}
      </div>

      <!-- Related Testing Section -->
      {related_testing_html}

      <!-- Stage Transition Timeline Section -->
      <div class="drawer-section">
        <h3 class="drawer-section-title">⏱ Stage History & Audit Timeline</h3>
        <div class="history-timeline">
          {''.join(history_items_html) or '<p class="muted" style="font-size:12px;">No historical stage transitions recorded.</p>'}
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Native Confirmation Modal for Delete -->
<div id="delete-confirm-modal" class="modal-overlay" style="display:none;z-index:99999;">
  <div class="modal-card">
    <h3 style="color:var(--color-danger);margin-top:0;">Confirm Permanent Deletion</h3>
    <p>Are you sure you want to delete <strong>{h(d_id)}: {h(title)}</strong>?</p>
    <p class="muted" style="font-size:12px;">This operation will remove the item, its associated metadata, blockers, and unlink test executions.</p>
    <form method="post" action="/work/item/delete" style="display:flex;justify-content:flex-end;gap:10px;margin-top:20px;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="entity_type" value="{e_type}">
      <input type="hidden" name="entity_id" value="{e_id}">
      <button type="button" class="btn-subtle" onclick="document.getElementById('delete-confirm-modal').style.display='none'">Cancel</button>
      <button class="danger">Delete Permanently</button>
    </form>
  </div>
</div>'''


def render_new_work_item_modal(csrf_token: str, members: list[dict], templates: list[dict], sub: str, default_type: str = 'requirement', is_open: bool = False) -> str:
    """Renders the Progressive "+ New Work Item" Modal with contextual fields based on selected type."""
    member_opts = '<option value="">Unassigned</option>' + ''.join(
        f'<option value="{m["id"]}">{h(m["name"])}</option>' for m in members
    )
    tmpl_opts = ''.join(
        f'<option value="{t["id"]}">{h(t["name"])} ({h(t["work_type"])})</option>' for t in templates
    )

    return f'''<div class="modal-overlay" id="new-item-modal" style="display:{'flex' if is_open else 'none'};z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:620px;width:95%;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h2 style="margin:0;font-size:18px;">Create New Requirement & Work Item</h2>
      <button type="button" onclick="document.getElementById('new-item-modal').style.display='none'" style="background:none;border:none;font-size:22px;color:var(--text-muted);cursor:pointer;">&times;</button>
    </div>

    <!-- Step 1: Work Item Type Tabs -->
    <div class="type-selector-tabs" style="display:flex;gap:8px;margin-bottom:16px;">
      <button type="button" class="type-tab-btn active" id="tab-req" onclick="selectWorkType('requirement')">📋 Requirement</button>
      <button type="button" class="type-tab-btn" id="tab-task" onclick="selectWorkType('task')">✅ Task</button>
      <button type="button" class="type-tab-btn" id="tab-case" onclick="selectWorkType('case')">📁 Support Case</button>
    </div>

    <!-- Step 2: Progressive Forms -->
    <!-- Form 1: Requirement -->
    <form method="post" action="/requirements/create" id="form-requirement" class="progressive-form">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Requirement Title *</label>
        <input name="title" required placeholder="e.g. Automated Multi-Currency Checkout" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Client / Product</label>
          <input name="client" placeholder="e.g. Shopify Global" style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Ticket Reference</label>
          <input name="ticket" placeholder="e.g. REQ-402" style="width:100%;">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Workflow Lifecycle</label>
          <select name="workflow_template_id" style="width:100%;">{tmpl_opts}</select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Priority</label>
          <select name="priority" style="width:100%;">
            <option value="1">P1 · Critical</option>
            <option value="2" selected>P2 · High</option>
            <option value="3">P3 · Medium</option>
            <option value="4">P4 · Low</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">User Story</label>
        <textarea name="user_story" rows="2" placeholder="As a customer, I want to pay in EUR so that..." style="width:100%;"></textarea>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Acceptance Criteria</label>
        <textarea name="acceptance_criteria" rows="2" placeholder="1. Converts at real-time rate\n2. Receipt displays EUR" style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-item-modal').style.display='none'">Cancel</button>
        <button class="primary">Create New Requirement</button>
      </div>
    </form>

    <!-- Form 2: Task -->
    <form method="post" action="/tasks/create" id="form-task" class="progressive-form" style="display:none;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Task Title *</label>
        <input name="title" required placeholder="e.g. Implement webhook retry logic" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Project / Component</label>
          <input name="project" placeholder="e.g. Payment Gateway" style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Ticket Reference</label>
          <input name="ticket" placeholder="e.g. TASK-88" style="width:100%;">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Priority</label>
          <select name="priority" style="width:100%;">
            <option value="1">P1 · Critical</option>
            <option value="2" selected>P2 · High</option>
            <option value="3">P3 · Medium</option>
            <option value="4">P4 · Low</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Due Date</label>
          <input type="date" name="due_date" style="width:100%;">
        </div>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Next Action</label>
        <input name="next_action" placeholder="Immediate next step..." style="width:100%;">
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <a href="/work?sub={sub}" class="btn-subtle">Cancel</a>
        <button class="primary">Create Task</button>
      </div>
    </form>

    <!-- Form 3: Case -->
    <form method="post" action="/cases/create" id="form-case" class="progressive-form" style="display:none;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Case Subject / Title *</label>
        <input name="title" required placeholder="e.g. Settlement latency spike in production" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Client Name</label>
          <input name="client" placeholder="e.g. Enterprise Client A" style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Severity / Priority</label>
          <select name="priority" style="width:100%;">
            <option value="1">P1 · Critical Outage</option>
            <option value="2" selected>P2 · High Degradation</option>
            <option value="3">P3 · Medium Issue</option>
            <option value="4">P4 · Minor Inquiry</option>
          </select>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Next Action</label>
          <input name="next_action" placeholder="Next operational response..." style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Waiting On Stakeholder</label>
          <input name="waiting_on" placeholder="e.g. DevOps / DBA team" style="width:100%;">
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <a href="/work?sub={sub}" class="btn-subtle">Cancel</a>
        <button class="primary">Create Support Case</button>
      </div>
    </form>
  </div>
</div>

<script>
function selectWorkType(type) {{
  document.querySelectorAll('.type-tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.progressive-form').forEach(f => f.style.display = 'none');
  if (type === 'requirement') {{
    document.getElementById('tab-req').classList.add('active');
    document.getElementById('form-requirement').style.display = 'block';
  }} else if (type === 'task') {{
    document.getElementById('tab-task').classList.add('active');
    document.getElementById('form-task').style.display = 'block';
  }} else if (type === 'case') {{
    document.getElementById('tab-case').classList.add('active');
    document.getElementById('form-case').style.display = 'block';
  }}
}}
</script>'''
