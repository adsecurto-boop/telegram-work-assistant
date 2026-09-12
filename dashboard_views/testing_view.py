"""Testing Operations Workspace: Test Conditions, Test Cases, Executions, Defects, Traceability Matrix, and Sign-off Reports."""
from html import escape as h
from dashboard_views.components import (
    render_status_badge,
    render_priority_tag,
    format_iso_time
)


def render_testing_view(service, csrf_token: str, params: dict, test_sub: str = 'cases') -> str:
    test_cases = service.work_item_service.list_test_cases()
    test_conditions = service.work_item_service.list_test_conditions()
    defects = service.work_item_service.list_defects()
    requirements = service.work_item_service.list_requirements()
    executions = service.work_item_service.list_test_executions(limit=60)
    members = service.member_service.get_members()

    # Metrics
    tc_passed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'pass')
    tc_failed = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'fail')
    tc_blocked = sum(1 for tc in test_cases if tc.get('last_execution_result') == 'blocked')
    tc_unrun = sum(1 for tc in test_cases if not tc.get('last_execution_result') or tc.get('last_execution_result') == 'not_run')
    total_tc = len(test_cases)
    pass_pct = int((tc_passed / total_tc) * 100) if total_tc > 0 else 0

    open_defects = [d for d in defects if d.get('status') not in ('verified', 'closed')]
    crit_defects = [d for d in open_defects if d.get('severity') in ('blocker', 'critical')]

    # Sub-nav pills
    def test_pill(name, key, count_val=None):
        active = (test_sub == key)
        cls = 'sub-nav-pill active' if active else 'sub-nav-pill'
        cnt = f' <span class="nav-counter">{count_val}</span>' if count_val is not None else ''
        return f'<a href="/tests?test_sub={key}" class="{cls}">{name}{cnt}</a>'

    sub_nav = f'''<div class="sub-nav-wrapper">
  <div class="sub-nav-bar">
    {test_pill('🧪 Test Cases & Execution', 'cases', total_tc)}
    {test_pill('📋 Test Conditions', 'conditions', len(test_conditions))}
    {test_pill('🐛 Defects & Retest', 'defects', len(open_defects))}
    {test_pill('🗺 Traceability Matrix', 'traceability', len(requirements))}
    {test_pill('⏱ Execution History', 'executions', len(executions))}
    {test_pill('📊 Readiness & Reports', 'reports', len(requirements))}
  </div>
</div>'''

    # Metric Strip
    metrics_html = f'''<div class="metric-band" style="margin-bottom:16px;">
  <div class="metric-card">
    <div class="metric-card-top"><span class="metric-card-title">Overall Pass Rate</span></div>
    <div class="metric-card-value" style="color:var(--color-success);">{pass_pct}%</div>
    <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:{pass_pct}%;"></div></div>
  </div>
  <div class="metric-card">
    <div class="metric-card-top"><span class="metric-card-title">Test Results</span><span class="tag tag-ok">{tc_passed} Pass</span></div>
    <div class="metric-card-value">{tc_failed} <span class="muted" style="font-size:13px;color:var(--color-danger);">failed</span></div>
    <div class="metric-card-desc">{tc_blocked} blocked · {tc_unrun} unexecuted</div>
  </div>
  <div class="metric-card">
    <div class="metric-card-top"><span class="metric-card-title">Active Defects</span><span class="tag {'tag-err' if crit_defects else 'tag-ok'}">{len(crit_defects)} Critical</span></div>
    <div class="metric-card-value" style="color:{'var(--color-danger)' if open_defects else 'var(--color-success)'};">{len(open_defects)} <span class="muted" style="font-size:13px;">open</span></div>
    <div class="metric-card-desc">Across all requirements & builds</div>
  </div>
  <div class="metric-card">
    <div class="metric-card-top"><span class="metric-card-title">Test Conditions</span></div>
    <div class="metric-card-value">{len(test_conditions)} <span class="muted" style="font-size:13px;">conditions</span></div>
    <div class="metric-card-desc">{sum(1 for c in test_conditions if c.get('status') == 'approved')} approved coverage criteria</div>
  </div>
</div>'''

    # 1. TAB: TEST CASES
    if test_sub == 'cases':
        rows = []
        for tc in test_cases:
            tc_id = tc['id']
            title = tc.get('title') or 'Untitled Test Case'
            req_id = tc.get('requirement_id')
            cond_id = tc.get('test_condition_id')
            req_link = f'<a href="/work?item_type=requirement&item_id={req_id}" class="item-link">REQ-{req_id}</a>' if req_id else '<span class="muted">—</span>'
            p_badge = render_priority_tag(tc.get('priority'))
            last_res = tc.get('last_execution_result') or 'not_run'
            res_badge = render_status_badge(last_res)
            auto_badge = '<span class="tag tag-info">Automated</span>' if tc.get('automation_status') == 'automated' else '<span class="tag">Manual</span>'
            cond_str = f'<div class="muted" style="font-size:11px;">Condition: #{cond_id}</div>' if cond_id else ''

            rows.append(f'''<tr>
  <td><strong>TC-{tc_id}</strong></td>
  <td>
    <div style="font-weight:600;">{h(title)}</div>
    <div class="muted" style="font-size:12px;">{h(tc.get("objective") or "")[:90]}</div>
    {cond_str}
  </td>
  <td>{req_link}</td>
  <td>{p_badge}</td>
  <td>{auto_badge}</td>
  <td>{res_badge}</td>
  <td style="text-align:right;">
    <form method="post" action="/tests/executions/record" style="margin:0;display:inline-flex;gap:4px;align-items:center;">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="test_case_id" value="{tc_id}">
      <select name="result" style="padding:3px 6px;font-size:12px;">
        <option value="pass">Pass</option>
        <option value="fail">Fail</option>
        <option value="blocked">Blocked</option>
      </select>
      <input name="actual_result" placeholder="Actual result..." style="padding:3px 6px;font-size:12px;width:120px;">
      <button class="primary" style="padding:3px 8px;font-size:12px;">Run</button>
    </form>
  </td>
</tr>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Test Cases Repository ({total_tc})</h2>
      <p class="muted">Manage and execute test cases with one-click result capture.</p>
    </div>
    <button class="btn-primary-action" onclick="document.getElementById('new-tc-modal').style.display='flex'">+ Add Test Case</button>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Test Case Title & Objective</th>
          <th>Requirement</th>
          <th>Priority</th>
          <th>Type</th>
          <th>Last Result</th>
          <th style="text-align:right;">Execute Test</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:28px;">No test cases found.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 2. TAB: TEST CONDITIONS
    elif test_sub == 'conditions':
        cond_rows = []
        for cond in test_conditions:
            c_id = cond['id']
            c_title = cond.get('title') or 'Untitled Condition'
            req_id = cond.get('requirement_id')
            category = cond.get('category') or 'functional'
            risk = cond.get('risk_level') or 'medium'
            status = cond.get('status') or 'draft'
            req_link = f'<a href="/work?item_type=requirement&item_id={req_id}" class="item-link">REQ-{req_id}</a>' if req_id else '<span class="muted">—</span>'
            
            risk_badge = f'<span class="tag tag-err">High Risk</span>' if risk == 'high' else (f'<span class="tag tag-warn">Med Risk</span>' if risk == 'medium' else '<span class="tag">Low Risk</span>')
            cat_badge = f'<span class="tag tag-info">{h(category.title())}</span>'
            st_badge = render_status_badge(status)

            approve_btn = ''
            if status != 'approved':
                approve_btn = f'''<form method="post" action="/tests/conditions/approve" style="margin:0;display:inline;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="id" value="{c_id}">
  <button class="btn-subtle" style="padding:2px 6px;font-size:11px;">Approve</button>
</form>'''

            cond_rows.append(f'''<tr>
  <td><strong>COND-{c_id}</strong></td>
  <td>
    <div style="font-weight:600;">{h(c_title)}</div>
    <div class="muted" style="font-size:12px;">{h(cond.get("description") or "")[:120]}</div>
  </td>
  <td>{req_link}</td>
  <td>{cat_badge}</td>
  <td>{risk_badge}</td>
  <td>{st_badge}</td>
  <td style="text-align:right;">
    {approve_btn}
  </td>
</tr>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Test Conditions & Coverage Baseline ({len(test_conditions)})</h2>
      <p class="muted">Approved testing criteria ensuring all functional, boundary, negative, and regression angles are covered.</p>
    </div>
    <button class="btn-primary-action" onclick="document.getElementById('new-cond-modal').style.display='flex'">+ Add Condition</button>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Condition & Scope</th>
          <th>Requirement</th>
          <th>Category</th>
          <th>Risk</th>
          <th>Status</th>
          <th style="text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(cond_rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:28px;">No test conditions recorded.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 3. TAB: DEFECTS & RETEST
    elif test_sub == 'defects':
        def_rows = []
        for d in defects:
            d_id = d['id']
            d_title = d.get('title') or 'Untitled Defect'
            sev = d.get('severity') or 'major'
            status = d.get('status') or 'new'
            req_id = d.get('requirement_id')
            tc_id = d.get('test_case_id')
            req_link = f'<a href="/work?item_type=requirement&item_id={req_id}" class="item-link">REQ-{req_id}</a>' if req_id else '<span class="muted">—</span>'
            tc_link = f'<span class="tag">TC-{tc_id}</span>' if tc_id else '<span class="muted">—</span>'
            
            sev_badge = f'<span class="tag tag-err">{h(sev.upper())}</span>' if sev in ('blocker', 'critical') else f'<span class="tag tag-warn">{h(sev.title())}</span>'
            st_badge = render_status_badge(status)

            retest_form = f'''<form method="post" action="/tests/defects/retest" style="margin:0;display:inline-flex;gap:4px;align-items:center;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="defect_id" value="{d_id}">
  <select name="result" style="padding:2px 4px;font-size:11px;">
    <option value="pass">Retest Pass (Close)</option>
    <option value="fail">Retest Fail (Reopen)</option>
  </select>
  <input name="retest_notes" placeholder="Retest notes..." style="padding:2px 4px;font-size:11px;width:95px;">
  <button class="primary" style="padding:2px 6px;font-size:11px;">Retest</button>
</form>''' if status in ('fix_ready', 'retest_required', 'retesting', 'in_development') else (
                f'''<form method="post" action="/tests/defects/reopen" style="margin:0;display:inline;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="defect_id" value="{d_id}">
  <button class="btn-subtle" style="padding:2px 6px;font-size:11px;">Reopen</button>
</form>''' if status in ('verified', 'closed') else ''
            )

            def_rows.append(f'''<tr>
  <td><strong>DEF-{d_id}</strong></td>
  <td>
    <div style="font-weight:600;">{h(d_title)}</div>
    <div class="muted" style="font-size:12px;">Build: {h(d.get("build_found") or "—")} · Assignee: {h(d.get("assignee_name") or "Unassigned")}</div>
  </td>
  <td>{req_link}</td>
  <td>{tc_link}</td>
  <td>{sev_badge}</td>
  <td>{st_badge}</td>
  <td style="text-align:right;">
    {retest_form}
  </td>
</tr>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Defect Tracking & Retesting ({len(defects)})</h2>
      <p class="muted">Bug triage, assignment, build verification, and retest lifecycle tracking.</p>
    </div>
    <button class="btn-primary-action" onclick="document.getElementById('new-defect-modal').style.display='flex'">+ Log Defect</button>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Defect Details</th>
          <th>Requirement</th>
          <th>Test Case</th>
          <th>Severity</th>
          <th>Status</th>
          <th style="text-align:right;">Retest / Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(def_rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:28px;">No defects recorded.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 4. TAB: TRACEABILITY MATRIX
    elif test_sub == 'traceability':
        req_rows = []
        for req in requirements:
            r_id = req['id']
            r_title = req.get('title') or 'Untitled'
            linked_tcs = [tc for tc in test_cases if tc.get('requirement_id') == r_id]
            linked_conds = [c for c in test_conditions if c.get('requirement_id') == r_id]
            linked_defs = [d for d in defects if d.get('requirement_id') == r_id]
            
            pass_cnt = sum(1 for tc in linked_tcs if tc.get('last_execution_result') == 'pass')
            fail_cnt = sum(1 for tc in linked_tcs if tc.get('last_execution_result') == 'fail')
            
            cov_badge = f'<span class="tag tag-ok">{len(linked_conds)} Conds / {len(linked_tcs)} Tests</span>' if linked_tcs else '<span class="tag tag-err">No Coverage</span>'
            def_badge = f'<span class="tag tag-err">{len(linked_defs)} Defects</span>' if linked_defs else '<span class="tag tag-ok">0 Defects</span>'

            req_rows.append(f'''<tr>
  <td><strong><a href="/work?item_type=requirement&item_id={r_id}" class="item-link">REQ-{r_id}</a></strong></td>
  <td>
    <div style="font-weight:600;"><a href="/work?item_type=requirement&item_id={r_id}" style="color:var(--text-primary);text-decoration:none;">{h(r_title)}</a></div>
    <div class="muted" style="font-size:12px;">Client: {h(req.get("client") or "—")} · Ticket: {h(req.get("ticket") or "—")}</div>
  </td>
  <td>{cov_badge}</td>
  <td>
    <span style="color:var(--color-success);font-weight:600;">{pass_cnt} Pass</span> · 
    <span style="color:var(--color-danger);font-weight:600;">{fail_cnt} Fail</span>
  </td>
  <td>{def_badge}</td>
  <td style="text-align:right;">
    <a href="/work?item_type=requirement&item_id={r_id}" class="btn-subtle" style="padding:4px 8px;font-size:12px;">Manage Tests &rarr;</a>
  </td>
</tr>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Requirements Traceability Matrix ({len(requirements)})</h2>
      <p class="muted">Verification and coverage mapping between Business Requirements, Test Conditions, Test Cases, and Defects.</p>
    </div>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>Requirement</th>
          <th>Title & Context</th>
          <th>Coverage Status</th>
          <th>Execution Results</th>
          <th>Defects</th>
          <th style="text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        {''.join(req_rows) or '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px;">No requirements recorded.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 5. TAB: EXECUTIONS
    elif test_sub == 'executions':
        exec_rows = []
        for ex in executions:
            e_id = ex['id']
            tc_id = ex.get('test_case_id')
            res = ex.get('result') or 'pass'
            res_badge = render_status_badge(res)
            time_str = format_iso_time(ex.get('executed_at'))
            actor = ex.get('executor_name') or 'QA Tester'

            exec_rows.append(f'''<tr>
  <td><strong>EX-{e_id}</strong></td>
  <td><strong>TC-{tc_id}</strong></td>
  <td>{res_badge}</td>
  <td><span class="muted">{time_str}</span></td>
  <td>{h(actor)}</td>
  <td><div style="max-width:300px;font-size:12px;">{h(ex.get("actual_result") or "—")}</div></td>
  <td>{f'<span class="tag tag-err">{h(ex["defect_reference"])}</span>' if ex.get("defect_reference") else '<span class="muted">—</span>'}</td>
</tr>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Recent Execution Logs ({len(executions)})</h2>
      <p class="muted">Audit trail of manual and automated test execution results.</p>
    </div>
  </div>
  <div class="table-scroll-wrap">
    <table>
      <thead>
        <tr>
          <th>Exec ID</th>
          <th>Test Case</th>
          <th>Result</th>
          <th>Executed At</th>
          <th>Tester</th>
          <th>Actual Result / Notes</th>
          <th>Defect Link</th>
        </tr>
      </thead>
      <tbody>
        {''.join(exec_rows) or '<tr><td colspan="7" class="muted" style="text-align:center;padding:28px;">No test executions recorded yet.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>'''

    # 6. TAB: REPORTS & SIGN-OFF
    elif test_sub == 'reports':
        rep_cards = []
        for req in requirements:
            r_id = req['id']
            r_title = req.get('title') or 'Untitled'
            posture = service.work_item_service.get_testing_posture(r_id)
            readiness = posture.get('readiness', 'NOT_READY')
            
            ready_badge = '<span class="tag tag-ok">Ready for Sign-Off</span>' if readiness == 'READY' else '<span class="tag tag-err">Not Ready</span>'

            rep_cards.append(f'''<div style="border:1px solid var(--border-subtle);border-radius:var(--radius-md);padding:16px;margin-bottom:12px;background:#fff;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <div>
      <strong>REQ-{r_id}: {h(r_title)}</strong>
      <div class="muted" style="font-size:12px;">Client: {h(req.get("client") or "—")} · Ticket: {h(req.get("ticket") or "—")}</div>
    </div>
    {ready_badge}
  </div>
  <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:10px;font-size:12px;margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9;">
    <div>Conditions: <strong>{posture.get('test_conditions_count', 0)}</strong></div>
    <div>Passed Tests: <strong style="color:var(--color-success);">{posture.get('passed_tests', 0)} / {posture.get('test_cases_count', 0)}</strong></div>
    <div>Active Defects: <strong style="color:{'var(--color-danger)' if posture.get('active_defects', 0) > 0 else 'var(--text-primary)'};">{posture.get('active_defects', 0)}</strong></div>
    <div>Active Blockers: <strong>{posture.get('active_blockers', 0)}</strong></div>
  </div>
</div>''')

        main_section = f'''<div class="card">
  <div class="card-header-row">
    <div>
      <h2 class="card-title">Release Readiness & Testing Posture ({len(requirements)})</h2>
      <p class="muted">Quality sign-off posture, coverage verification, and blocker metrics per requirement.</p>
    </div>
  </div>
  {''.join(rep_cards) or '<p class="muted" style="text-align:center;padding:28px;">No requirements to report on.</p>'}
</div>'''

    # Modals
    req_options = '<option value="">Unlinked (Standalone Test)</option>' + ''.join(
        f'<option value="{r["id"]}">REQ-{r["id"]}: {h(r["title"])}</option>' for r in requirements
    )
    req_options_required = ''.join(
        f'<option value="{r["id"]}">REQ-{r["id"]}: {h(r["title"])}</option>' for r in requirements
    )

    modal_tc = f'''<div id="new-tc-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:550px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Create New Test Case</h3>
      <button type="button" onclick="document.getElementById('new-tc-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/tests/cases/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Test Case Title *</label>
        <input name="title" required placeholder="e.g. Verify multi-currency conversion with invalid ISO code" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Link to Requirement</label>
          <select name="requirement_id" style="width:100%;">{req_options}</select>
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
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Objective</label>
        <textarea name="objective" rows="2" placeholder="State test objective..." style="width:100%;"></textarea>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Expected Result</label>
        <textarea name="expected_result" rows="2" placeholder="State expected result..." style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-tc-modal').style.display='none'">Cancel</button>
        <button class="primary">Save Test Case</button>
      </div>
    </form>
  </div>
</div>'''

    modal_cond = f'''<div id="new-cond-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:550px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Create Test Condition</h3>
      <button type="button" onclick="document.getElementById('new-cond-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/tests/conditions/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Requirement *</label>
        <select name="requirement_id" required style="width:100%;">{req_options_required}</select>
      </div>
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Condition Title *</label>
        <input name="title" required placeholder="e.g. Boundary validation on high volume cart checkout" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Category</label>
          <select name="category" style="width:100%;">
            <option value="functional">Functional</option>
            <option value="boundary">Boundary & Limits</option>
            <option value="negative">Negative / Error Flow</option>
            <option value="security">Security & Access</option>
            <option value="regression">Regression</option>
            <option value="validation">Validation</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Risk Level</label>
          <select name="risk_level" style="width:100%;">
            <option value="high">High Risk</option>
            <option value="medium" selected>Medium Risk</option>
            <option value="low">Low Risk</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Condition Description & Scope</label>
        <textarea name="description" rows="2" placeholder="Details of test condition..." style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-cond-modal').style.display='none'">Cancel</button>
        <button class="primary">Save Condition</button>
      </div>
    </form>
  </div>
</div>'''

    modal_defect = f'''<div id="new-defect-modal" class="modal-overlay" style="display:none;z-index:99999;" onclick="if(event.target===this) this.style.display='none'">
  <div class="modal-card" style="max-width:550px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
      <h3 style="margin:0;">+ Log New Defect</h3>
      <button type="button" onclick="document.getElementById('new-defect-modal').style.display='none'" style="background:none;border:none;font-size:20px;cursor:pointer;">&times;</button>
    </div>
    <form method="post" action="/tests/defects/create">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Defect Summary *</label>
        <input name="title" required placeholder="e.g. 500 error on checkout when item stock is 0" style="width:100%;">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Severity</label>
          <select name="severity" style="width:100%;">
            <option value="blocker">Blocker</option>
            <option value="critical">Critical</option>
            <option value="major" selected>Major</option>
            <option value="minor">Minor</option>
            <option value="trivial">Trivial</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Priority</label>
          <select name="priority" style="width:100%;">
            <option value="1">P1 · Urgent</option>
            <option value="2" selected>P2 · High</option>
            <option value="3">P3 · Medium</option>
          </select>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Requirement</label>
          <select name="requirement_id" style="width:100%;">{req_options}</select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Build Found</label>
          <input name="build_found" placeholder="e.g. v2.4.0-rc2" style="width:100%;">
        </div>
      </div>
      <div style="margin-bottom:10px;">
        <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px;">Steps to Reproduce</label>
        <textarea name="steps_to_reproduce" rows="2" placeholder="1. Add item\n2. Proceed to checkout" style="width:100%;"></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-subtle" onclick="document.getElementById('new-defect-modal').style.display='none'">Cancel</button>
        <button class="danger">Log Defect</button>
      </div>
    </form>
  </div>
</div>'''

    return f'''{sub_nav}
<div class="work-hub-header">
  <div>
    <h1>Testing Operations Workspace</h1>
    <p class="muted">Test repository, automated verification, defect tracking, and release readiness.</p>
  </div>
</div>
{metrics_html}
{main_section}
{modal_tc}
{modal_cond}
{modal_defect}'''
