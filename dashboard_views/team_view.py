"""Team Roster and Role Management view."""
from html import escape as h


def render_team_view(service, csrf_token: str, params: dict) -> str:
    members = service.member_service.get_members()
    roles = service.member_service.get_roles()
    work_items = service.work_item_service.list_work_items()

    # Calculate active work count per member
    member_work_counts = {}
    for w in work_items:
        o_name = w.get('owner_name')
        if o_name:
            member_work_counts[o_name] = member_work_counts.get(o_name, 0) + 1

    member_cards = []
    for m in members:
        m_id = m['id']
        name = m.get('name') or 'Unnamed Member'
        email = m.get('email') or '—'
        tg = m.get('telegram_handle') or '—'
        m_roles = m.get('roles', [])
        work_cnt = member_work_counts.get(name, 0)

        # Avatar initials
        initials = ''.join([part[0].upper() for part in name.split()[:2]]) or 'TM'

        roles_pills = ''.join(f'<span class="tag tag-info" style="font-size:11px;">{h(r)}</span>' for r in m_roles)

        member_cards.append(f'''<div class="team-card">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
    <div class="team-avatar">{h(initials)}</div>
    <div>
      <div style="font-weight:600;font-size:15px;color:var(--text-primary);">{h(name)}</div>
      <div class="muted" style="font-size:12px;">{h(email)}</div>
    </div>
  </div>
  <div style="margin-bottom:10px;display:flex;gap:4px;flex-wrap:wrap;">
    {roles_pills or '<span class="muted" style="font-size:11px;">No roles assigned</span>'}
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;padding-top:10px;border-top:1px solid #f1f5f9;font-size:12px;">
    <span>Telegram: <strong>{h(tg)}</strong></span>
    <span class="tag">{work_cnt} Active Item{'s' if work_cnt!=1 else ''}</span>
  </div>
</div>''')

    role_rows = []
    for r in roles:
        r_id = r['id']
        r_name = r.get('name') or ''
        r_desc = r.get('description') or '—'
        assigned_cnt = sum(1 for m in members if r_name in m.get('roles', []))
        role_rows.append(f'''<tr>
  <td><strong>{h(r_name)}</strong></td>
  <td><div class="muted">{h(r_desc)}</div></td>
  <td><span class="tag tag-info">{assigned_cnt} Member{'s' if assigned_cnt!=1 else ''}</span></td>
</tr>''')

    # Modals
    role_options_html = ''.join(
        f'<label style="display:flex;align-items:center;gap:6px;font-size:13px;"><input type="checkbox" name="roles" value="{h(r["name"])}"> {h(r["name"])}</label>'
        for r in roles
    )

    add_member_modal = f'''<div id="new-member-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:520px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Add Team Member</h3>
      <button type="button" onclick="document.getElementById('new-member-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/team/members/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Full Name *</label>
        <input name="name" required placeholder="e.g. Alex Morgan" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Email</label>
          <input type="email" name="email" placeholder="alex@company.com" style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Telegram Handle</label>
          <input name="telegram_handle" placeholder="@alex_qa" style="width:100%;">
        </div>
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:6px;">Assign Roles</label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
          {role_options_html}
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-member-modal').style.display='none'">Cancel</button>
        <button class="primary">Add Member</button>
      </div>
    </form>
  </div>
</div>'''

    add_role_modal = f'''<div id="new-role-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:480px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Create Custom Role</h3>
      <button type="button" onclick="document.getElementById('new-role-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/team/roles/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Role Name *</label>
        <input name="name" required placeholder="e.g. Lead SDET" style="width:100%;">
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Description</label>
        <textarea name="description" rows="2" placeholder="Responsibilities of this role..." style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-role-modal').style.display='none'">Cancel</button>
        <button class="primary">Save Role</button>
      </div>
    </form>
  </div>
</div>'''

    return f'''<div class="work-hub-header">
  <div>
    <h1>Team & Role Directory</h1>
    <p class="muted">Manage cross-functional members, skill disciplines, and ownership assignments.</p>
  </div>
  <div style="display:flex;gap:8px;">
    <button class="btn-subtle" onclick="document.getElementById('new-role-modal').style.display='flex'">+ Add Role</button>
    <button class="btn-primary-action" onclick="document.getElementById('new-member-modal').style.display='flex'">+ Add Member</button>
  </div>
</div>

<div class="team-grid">
  {''.join(member_cards) or '<div class="muted" style="grid-column:1/-1;padding:24px;text-align:center;">No team members registered. Click "+ Add Member" to get started.</div>'}
</div>

<div class="card" style="margin-top:24px;">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Role Definitions & Disciplines ({len(roles)})</h2>
      <p class="muted">Workflow stage requirements match against these organizational roles.</p>
    </div>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>Role Title</th>
          <th>Description & Scope</th>
          <th>Assigned Members</th>
        </tr>
      </thead>
      <tbody>
        {''.join(role_rows)}
      </tbody>
    </table>
  </div>
</div>

{add_member_modal}
{add_role_modal}'''
