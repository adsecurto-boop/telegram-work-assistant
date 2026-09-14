"""Focused project list/detail views; all metrics are derived from durable rows."""
from html import escape as h


def render_projects_view(service, csrf_token, params):
    status = (params.get('status') or [''])[0] or None
    search = (params.get('q') or [''])[0] or None
    projects = service.project_service.list_projects(status=status, search=search)
    rows = []
    for project in projects:
        total, complete = project['task_count'] or 0, project['completed_count'] or 0
        progress = int(complete * 100 / total) if total else 0
        rows.append(f'''<tr><td><a href="/projects/{project['id']}"><strong>{h(project['code'])}</strong></a></td>
<td>{h(project['name'])}</td><td>{h(project['status'])}</td><td>{h(project['health'])}</td>
<td>{complete}/{total} · {progress}%</td><td>{project['blocked_count'] or 0}</td><td>{h(project['due_date'] or '—')}</td></tr>''')
    return f'''<h1>Projects</h1><p class="muted">Private work planning; stored members are directory records, not authenticated users.</p>
<section class="card"><form method="get" action="/projects"><label>Search <input name="q" value="{h(search or '')}"></label>
<label>Status <select name="status"><option value="">All</option>{''.join(f'<option value="{s}" {"selected" if status==s else ""}>{s.replace("_", " ").title()}</option>' for s in ('planned','active','on_hold','completed','cancelled'))}</select></label><button>Filter</button></form></section>
<section class="card"><h2>Create project</h2><form method="post" action="/projects/create"><input type="hidden" name="csrf_token" value="{csrf_token}">
<label>Name <input required name="name"></label><label>Code <input name="code" placeholder="ALPHA"></label><label>Due date <input type="date" name="due_date"></label>
<label>Status <select name="status"><option>planned</option><option>active</option><option>on_hold</option></select></label><button class="primary">Create project</button></form></section>
<section class="card"><table><tr><th>Code</th><th>Project</th><th>Status</th><th>Health</th><th>Progress</th><th>Blocked</th><th>Due</th></tr>{''.join(rows) or '<tr><td colspan="7" class="muted">No projects yet. Create one to link tasks and track progress.</td></tr>'}</table></section>'''


def render_project_detail_view(service, csrf_token, project_id):
    project = service.project_service.project_detail(project_id)
    if not project: return '<h1>Project not found</h1><p><a href="/projects">Back to projects</a></p>'
    total, done = len(project['tasks']), sum(t['status'] == 'completed' for t in project['tasks'])
    progress = int(done * 100 / total) if total else 0
    tasks = ''.join(f'<li>#{t["id"]} · {h(t["title"])} · <strong>{h(t["status"])}</strong> · due {h(t["due_date"] or "—")}</li>' for t in project['tasks']) or '<li class="muted">No linked tasks.</li>'
    members = ', '.join(f'{h(m["name"])} ({h(m["project_role"])})' for m in project['members']) or 'No members assigned'
    return f'''<p><a href="/projects">← Projects</a></p><h1>{h(project['code'])} · {h(project['name'])}</h1>
<section class="card"><p>{h(project['description'] or 'No description')}</p><p><strong>{h(project['status'])}</strong> · {h(project['health'])} · Due {h(project['due_date'] or '—')}</p>
<p>Progress: {done}/{total} ({progress}%) · Members: {members}</p></section>
<section class="card"><h2>Tasks</h2><ul>{tasks}</ul></section>'''
