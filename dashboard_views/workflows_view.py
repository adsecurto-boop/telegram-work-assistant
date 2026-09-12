"""Workflows and Lifecycle Templates Management view."""
from html import escape as h
from dashboard_views.components import render_stage_pipeline


def render_workflows_view(service, csrf_token: str, params: dict) -> str:
    templates = service.workflow_service.list_templates()
    roles = service.member_service.get_roles()

    tmpl_cards = []
    for tmpl in templates:
        t_id = tmpl['id']
        name = tmpl.get('name') or 'Untitled Template'
        desc = tmpl.get('description') or ''
        w_type = tmpl.get('work_type') or 'requirement'
        is_def = tmpl.get('is_default')
        stages = tmpl.get('stages', [])
        
        def_tag = '<span class="tag tag-ok">Default Template</span>' if is_def else ''
        type_tag = f'<span class="tag tag-info">{h(w_type.upper())}</span>'

        pipeline_html = render_stage_pipeline(stages, current_stage_id=stages[0]['id'] if stages else None)

        stage_table_rows = []
        for s in stages:
            is_wait = s.get('is_waiting')
            wait_tag = '<span class="tag tag-warn">Waiting / External</span>' if is_wait else '<span class="tag">Active Work</span>'
            stage_table_rows.append(f'''<tr>
  <td><strong>#{s.get("stage_order")}</strong></td>
  <td><strong>{h(s["name"])}</strong></td>
  <td><span class="muted">{h(s.get("expected_role") or "Any Team Member")}</span></td>
  <td><span class="muted">{s.get("expected_duration_hours") or 0}h</span></td>
  <td>{wait_tag}</td>
  <td><div class="muted" style="font-size:12px;">{h(s.get("description") or "—")}</div></td>
</tr>''')

        tmpl_cards.append(f'''<div class="card" style="margin-bottom:20px;">
  <div class="card-header-row">
    <div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
        <h2 class="card-title">{h(name)}</h2>
        {type_tag}
        {def_tag}
      </div>
      <p class="muted" style="margin:0;">{h(desc)}</p>
    </div>
    <button class="btn-subtle" onclick="openAddStageModal({t_id}, '{h(name)}')">+ Add Stage</button>
  </div>
  
  <div style="background:#f8fafc;padding:12px;border-radius:var(--radius-md);margin-bottom:12px;border:1px solid var(--border-subtle);">
    {pipeline_html}
  </div>

  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>Order</th>
          <th>Stage Name</th>
          <th>Expected Role</th>
          <th>Target Duration</th>
          <th>Stage Nature</th>
          <th>Description & Deliverables</th>
        </tr>
      </thead>
      <tbody>
        {''.join(stage_table_rows) or '<tr><td colspan="6" class="muted" style="text-align:center;padding:16px;">No stages defined in this template yet.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>''')

    role_options = ''.join(f'<option value="{h(r["name"])}">{h(r["name"])}</option>' for r in roles)

    # Modals
    add_tmpl_modal = f'''<div id="new-template-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:550px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Create Workflow Template</h3>
      <button type="button" onclick="document.getElementById('new-template-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/workflows/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Template Name *</label>
        <input name="name" required placeholder="e.g. Critical Defect Lifecycle" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Work Type</label>
          <select name="work_type" style="width:100%;">
            <option value="requirement">Requirement</option>
            <option value="task">Task</option>
            <option value="case">Support Case</option>
            <option value="defect">Defect / Bug</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Default Template?</label>
          <select name="is_default" style="width:100%;">
            <option value="0">No</option>
            <option value="1">Yes (Set as Default)</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Description</label>
        <textarea name="description" rows="2" placeholder="Describe when to use this lifecycle..." style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-template-modal').style.display='none'">Cancel</button>
        <button class="primary">Create Template</button>
      </div>
    </form>
  </div>
</div>'''

    add_stage_modal = f'''<div id="add-stage-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:550px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;" id="add-stage-modal-title">+ Add Stage to Template</h3>
      <button type="button" onclick="document.getElementById('add-stage-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/workflows/stages/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="template_id" id="add-stage-template-id" value="">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Stage Name *</label>
        <input name="name" required placeholder="e.g. Stakeholder Sign-off" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Stage Order Number *</label>
          <input type="number" name="stage_order" value="1" min="1" required style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Expected Role</label>
          <select name="expected_role" style="width:100%;">{role_options}</select>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Target Duration (Hours)</label>
          <input type="number" step="0.5" name="expected_duration_hours" value="4.0" style="width:100%;">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Is Waiting on External?</label>
          <select name="is_waiting" style="width:100%;">
            <option value="0">No (Active Team Execution)</option>
            <option value="1">Yes (Waiting on Client / 3rd Party)</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Description & Checklist</label>
        <textarea name="description" rows="2" placeholder="Required checks or exit criteria..." style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('add-stage-modal').style.display='none'">Cancel</button>
        <button class="primary">Add Stage</button>
      </div>
    </form>
  </div>
</div>

<script>
function openAddStageModal(templateId, templateName) {{
  document.getElementById('add-stage-template-id').value = templateId;
  document.getElementById('add-stage-modal-title').innerText = '+ Add Stage to ' + templateName;
  document.getElementById('add-stage-modal').style.display = 'flex';
}}
</script>'''

    return f'''<div class="work-hub-header">
  <div>
    <h1>Workflow Templates & Lifecycles</h1>
    <p class="muted">Standard operating procedures, governance stages, and expected role deliverables.</p>
  </div>
  <button class="btn-primary-action" onclick="document.getElementById('new-template-modal').style.display='flex'">+ New Template</button>
</div>
{''.join(tmpl_cards)}
{add_tmpl_modal}
{add_stage_modal}'''
