"""
AI Test Condition Generator Modal & Draft Approval Component.
Renders the interactive modal for invoking server-side Gemini condition generation
and displaying the resulting draft test conditions for user inspection, editing, and approval.
"""
from __future__ import annotations
import html
from html import escape as h
import json


def render_ai_condition_modal(csrf_token: str, requirements: list[dict], selected_req_id: int | None = None) -> str:
    """
    Renders the AI Test Condition Generator modal with live draft review and approval controls.
    """
    req_options = "".join(
        f'<option value="{r["id"]}" {"selected" if selected_req_id == r["id"] else ""}>'
        f'REQ-{r["id"]}: {h(r.get("title") or "Untitled")}'
        f'</option>'
        for r in requirements
    )

    req_data_map = {}
    for r in requirements:
        req_data_map[str(r["id"])] = {
            "id": r["id"],
            "title": r.get("title") or "",
            "client": r.get("client") or r.get("product") or "",
            "ticket": r.get("ticket") or "",
            "user_story": r.get("user_story") or "",
            "acceptance_criteria": r.get("acceptance_criteria") or "",
            "description": r.get("description") or r.get("requirement_text") or "",
        }

    return f'''
<!-- AI Test Condition Generator & Draft Approval Modal -->
<div id="ai-condition-modal" class="modal-overlay" style="display:none;z-index:99999;align-items:flex-start;overflow-y:auto;padding:24px 16px;" onclick="if(event.target===this) closeAiConditionModal();">
  <div class="modal-card" style="max-width:800px;width:100%;margin:20px auto;box-shadow:var(--shadow-xl);border:1px solid var(--border-subtle);background:#ffffff;border-radius:var(--radius-lg);padding:24px;">
    
    <!-- Modal Header -->
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px;border-bottom:1px solid #f1f5f9;padding-bottom:14px;">
      <div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-size:20px;">✨</span>
          <h2 style="margin:0;font-size:18px;font-weight:700;color:var(--text-primary);">AI Test Condition Generator</h2>
          <span class="tag tag-info" style="font-size:11px;">Powered by Gemini</span>
        </div>
        <p class="muted" style="margin:4px 0 0 0;font-size:13px;">
          Derive comprehensive test condition drafts directly from Requirement specifications, user stories, and acceptance criteria.
        </p>
      </div>
      <button type="button" onclick="closeAiConditionModal();" style="background:none;border:none;font-size:22px;color:var(--text-secondary);cursor:pointer;line-height:1;padding:4px;">&times;</button>
    </div>

    <!-- Step 1: Configuration Form -->
    <div id="ai-cond-step-config" style="background:#f8fafc;border:1px solid var(--border-subtle);border-radius:var(--radius-md);padding:16px;margin-bottom:18px;">
      <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:12px;">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;color:var(--text-primary);">Target Requirement *</label>
          <select id="ai-cond-req-select" onchange="onAiReqChange();" style="width:100%;font-size:13px;padding:7px 10px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);">
            {req_options or '<option value="">No requirements available</option>'}
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;color:var(--text-primary);">Coverage Focus</label>
          <select id="ai-cond-focus-select" style="width:100%;font-size:13px;padding:7px 10px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);">
            <option value="comprehensive" selected>Full Coverage Suite</option>
            <option value="boundary">Boundary & Capacity Limits</option>
            <option value="negative">Negative & Error Handling</option>
            <option value="security">Security & Access Control</option>
            <option value="regression">Regression & Integration</option>
          </select>
        </div>
      </div>

      <!-- Live Requirement Context Summary -->
      <div id="ai-cond-req-preview" style="background:#ffffff;border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:10px 12px;margin-bottom:12px;font-size:12px;color:var(--text-secondary);">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
          <strong id="ai-preview-title" style="color:var(--text-primary);">Requirement Details</strong>
          <span id="ai-preview-meta" class="muted">Ticket: —</span>
        </div>
        <div id="ai-preview-story" style="font-style:italic;margin-bottom:4px;">No user story specified.</div>
        <div id="ai-preview-criteria" style="font-size:11px;color:var(--text-secondary);max-height:60px;overflow-y:auto;white-space:pre-line;"></div>
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div class="muted" style="font-size:11px;">
          <span>Models: <strong style="color:var(--text-primary);">gemini-3.5-flash</strong> / <strong style="color:var(--text-primary);">gemini-3.1-pro-preview</strong></span>
        </div>
        <button id="btn-run-ai-generate" type="button" class="btn-primary-action" onclick="fetchAiDraftConditions();" style="padding:7px 16px;font-size:13px;background:var(--color-primary);color:#fff;border:none;border-radius:var(--radius-sm);cursor:pointer;display:inline-flex;align-items:center;gap:6px;">
          <span>✨ Generate Draft Conditions</span>
        </button>
      </div>
    </div>

    <!-- Loading State -->
    <div id="ai-cond-loading" style="display:none;text-align:center;padding:32px 16px;background:#f8fafc;border-radius:var(--radius-md);margin-bottom:18px;">
      <div style="display:inline-block;width:32px;height:32px;border:3px solid #e2e8f0;border-top-color:var(--color-primary);border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:12px;"></div>
      <div style="font-size:14px;font-weight:600;color:var(--text-primary);">Analyzing Requirement & Generating Test Conditions...</div>
      <div class="muted" style="font-size:12px;margin-top:4px;">Invoking server-side Gemini model to evaluate functional, boundary, negative, and security angles.</div>
    </div>

    <!-- Error State -->
    <div id="ai-cond-error" style="display:none;background:#fef2f2;border:1px solid #fecaca;border-radius:var(--radius-md);padding:12px 16px;margin-bottom:18px;color:#991b1b;font-size:13px;"></div>

    <!-- Step 2: Draft Conditions Output & Approval Review -->
    <div id="ai-cond-draft-section" style="display:none;">
      
      <!-- Rationale & Strategy Banner -->
      <div id="ai-draft-banner" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:var(--radius-md);padding:12px 14px;margin-bottom:16px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <strong style="font-size:13px;color:#1e40af;">🧪 AI Test Strategy & Coverage Rationale</strong>
          <span id="ai-draft-model-badge" class="tag tag-info" style="font-size:10px;">gemini-3.5-flash</span>
        </div>
        <div id="ai-draft-rationale-text" style="font-size:12px;color:#1e3a8a;line-height:1.4;"></div>
      </div>

      <!-- Draft Conditions Action Header -->
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding:0 2px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <label style="font-size:12px;font-weight:600;display:flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" id="ai-draft-select-all" checked onchange="toggleSelectAllDrafts(this.checked);">
            <span>Select All Drafts</span>
          </label>
          <span id="ai-draft-count-label" class="muted" style="font-size:12px;">(0 conditions)</span>
        </div>
        <div style="font-size:11px;color:var(--text-secondary);">
          Review, customize text/categories, and approve into requirement baseline.
        </div>
      </div>

      <!-- Draft Conditions Card List Container -->
      <div id="ai-draft-cards-list" style="display:flex;flex-direction:column;gap:10px;max-height:380px;overflow-y:auto;padding-right:4px;margin-bottom:18px;">
        <!-- Injected via JavaScript -->
      </div>

      <!-- Approval & Actions Bar -->
      <div style="display:flex;justify-content:space-between;align-items:center;padding-top:14px;border-top:1px solid #f1f5f9;">
        <div>
          <button type="button" class="btn-subtle" onclick="discardDraftConditions();" style="font-size:12px;padding:6px 12px;">
            Discard Draft
          </button>
        </div>
        <div style="display:flex;gap:10px;align-items:center;">
          <span id="ai-draft-selected-summary" class="muted" style="font-size:12px;">0 of 0 selected</span>
          <button id="btn-save-as-draft" type="button" class="btn-subtle" onclick="submitDraftApproval('draft');" style="font-size:12px;padding:7px 14px;">
            Save as Drafts
          </button>
          <button id="btn-approve-conditions" type="button" class="primary" onclick="submitDraftApproval('approved');" style="font-size:13px;padding:7px 18px;background:var(--color-success);border-color:var(--color-success);color:#fff;font-weight:600;">
            ✓ Approve & Save Conditions
          </button>
        </div>
      </div>

    </div>

    <!-- Success Confirmation State -->
    <div id="ai-cond-success" style="display:none;text-align:center;padding:24px 16px;">
      <div style="font-size:32px;margin-bottom:8px;">✅</div>
      <h3 style="margin:0 0 6px 0;font-size:16px;color:var(--color-success);">Test Conditions Successfully Approved!</h3>
      <p id="ai-success-msg" class="muted" style="font-size:13px;margin-bottom:16px;">Test conditions have been added to the requirement baseline.</p>
      <button type="button" class="primary" onclick="closeAiConditionModal();window.location.reload();" style="padding:6px 16px;font-size:13px;">Done / Refresh View</button>
    </div>

  </div>
</div>

<script>
window.__REQ_DATA_MAP__ = {json.dumps(req_data_map)};
window.__CSRF_TOKEN__ = "{csrf_token}";
window.__AI_DRAFT_CONDITIONS__ = [];

function openAiConditionModal(reqId) {{
  const modal = document.getElementById('ai-condition-modal');
  if (!modal) return;
  modal.style.display = 'flex';
  
  // Reset views
  document.getElementById('ai-cond-step-config').style.display = 'block';
  document.getElementById('ai-cond-loading').style.display = 'none';
  document.getElementById('ai-cond-error').style.display = 'none';
  document.getElementById('ai-cond-draft-section').style.display = 'none';
  document.getElementById('ai-cond-success').style.display = 'none';

  if (reqId) {{
    const sel = document.getElementById('ai-cond-req-select');
    if (sel) {{
      sel.value = reqId.toString();
    }}
  }}
  onAiReqChange();
}}

function closeAiConditionModal() {{
  const modal = document.getElementById('ai-condition-modal');
  if (modal) modal.style.display = 'none';
}}

function onAiReqChange() {{
  const sel = document.getElementById('ai-cond-req-select');
  if (!sel) return;
  const reqId = sel.value;
  const data = window.__REQ_DATA_MAP__[reqId];
  if (!data) return;

  document.getElementById('ai-preview-title').textContent = 'REQ-' + data.id + ': ' + (data.title || 'Untitled');
  document.getElementById('ai-preview-meta').textContent = 'Client: ' + (data.client || '—') + ' · Ticket: ' + (data.ticket || '—');
  document.getElementById('ai-preview-story').textContent = data.user_story ? ('Story: ' + data.user_story) : 'No user story specified.';
  document.getElementById('ai-preview-criteria').textContent = data.acceptance_criteria ? ('Acceptance Criteria:\n' + data.acceptance_criteria) : (data.description || 'No criteria specified.');
}}

async function fetchAiDraftConditions() {{
  const sel = document.getElementById('ai-cond-req-select');
  if (!sel || !sel.value) {{
    alert('Please select a target requirement.');
    return;
  }}
  const reqId = parseInt(sel.value, 10);
  const focus = document.getElementById('ai-cond-focus-select').value || 'comprehensive';

  document.getElementById('ai-cond-error').style.display = 'none';
  document.getElementById('ai-cond-loading').style.display = 'block';
  document.getElementById('ai-cond-draft-section').style.display = 'none';
  document.getElementById('btn-run-ai-generate').disabled = true;

  try {{
    const resp = await fetch('/api/requirements/generate-test-conditions', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-CSRF-Token': window.__CSRF_TOKEN__
      }},
      body: JSON.stringify({{
        csrf_token: window.__CSRF_TOKEN__,
        requirement_id: reqId,
        focus_area: focus
      }})
    }});

    const data = await resp.json();
    if (!data.success) {{
      throw new Error(data.error || 'Failed to generate test conditions.');
    }}

    window.__AI_DRAFT_CONDITIONS__ = (data.draft_conditions || []).map((c, idx) => ({{
      ...c,
      _id: 'draft_' + idx,
      _selected: true
    }}));

    renderDraftConditionsUI(data);

  }} catch (err) {{
    const errBox = document.getElementById('ai-cond-error');
    errBox.textContent = 'Generation error: ' + err.message;
    errBox.style.display = 'block';
  }} finally {{
    document.getElementById('ai-cond-loading').style.display = 'none';
    document.getElementById('btn-run-ai-generate').disabled = false;
  }}
}}

function renderDraftConditionsUI(apiResponse) {{
  const list = document.getElementById('ai-draft-cards-list');
  list.innerHTML = '';

  document.getElementById('ai-draft-model-badge').textContent = apiResponse.model_used || 'gemini-3.5-flash';
  document.getElementById('ai-draft-rationale-text').textContent = apiResponse.rationale_summary || 'Comprehensive testing criteria covering functional, boundary, negative, and security paths.';
  document.getElementById('ai-draft-count-label').textContent = '(' + window.__AI_DRAFT_CONDITIONS__.length + ' conditions generated)';

  window.__AI_DRAFT_CONDITIONS__.forEach((cond, idx) => {{
    const card = document.createElement('div');
    card.id = 'draft-card-' + idx;
    card.className = 'draft-cond-card';
    card.style.cssText = 'border:1px solid #e2e8f0;border-radius:8px;padding:12px;background:#ffffff;transition:border-color 0.15s ease;';

    const catOptions = ['functional', 'boundary', 'negative', 'security', 'regression', 'validation']
      .map(cat => `<option value="${{cat}}" ${{cond.category === cat ? 'selected' : ''}}>${{cat.charAt(0).toUpperCase() + cat.slice(1)}}</option>`).join('');

    const riskOptions = ['high', 'medium', 'low']
      .map(r => `<option value="${{r}}" ${{cond.risk_level === r ? 'selected' : ''}}>${{r.charAt(0).toUpperCase() + r.slice(1)}} Risk</option>`).join('');

    card.innerHTML = `
      <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;">
        <input type="checkbox" id="draft-chk-${{idx}}" ${{cond._selected ? 'checked' : ''}} onchange="onDraftCheckChange(${{idx}}, this.checked);" style="margin-top:4px;cursor:pointer;">
        <div style="flex:1;">
          <input type="text" id="draft-title-${{idx}}" value="${{escapeHtml(cond.title || '')}}" placeholder="Condition Title..." style="width:100%;font-weight:600;font-size:13px;padding:4px 8px;border:1px solid #cbd5e1;border-radius:4px;margin-bottom:6px;">
          <textarea id="draft-desc-${{idx}}" rows="2" placeholder="Condition description & verification scope..." style="width:100%;font-size:12px;padding:4px 8px;border:1px solid #cbd5e1;border-radius:4px;resize:vertical;">${{escapeHtml(cond.description || '')}}</textarea>
        </div>
        <button type="button" onclick="removeDraftCondition(${{idx}});" style="background:none;border:none;color:#94a3b8;cursor:pointer;font-size:16px;line-height:1;padding:2px;" title="Discard this condition">&times;</button>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding-left:26px;font-size:11px;">
        <div style="display:flex;gap:8px;align-items:center;">
          <label class="muted">Category:</label>
          <select id="draft-cat-${{idx}}" style="font-size:11px;padding:2px 6px;border-radius:4px;border:1px solid #cbd5e1;">${{catOptions}}</select>
          <label class="muted">Risk:</label>
          <select id="draft-risk-${{idx}}" style="font-size:11px;padding:2px 6px;border-radius:4px;border:1px solid #cbd5e1;">${{riskOptions}}</select>
        </div>
        <div class="muted" style="font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${{escapeHtml(cond.rationale || '')}}">
          💡 ${{escapeHtml(cond.rationale || 'Covers requirement criteria')}}
        </div>
      </div>
    `;

    list.appendChild(card);
  }});

  document.getElementById('ai-cond-draft-section').style.display = 'block';
  updateDraftCount();
}}

function escapeHtml(str) {{
  return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function onDraftCheckChange(idx, isChecked) {{
  if (window.__AI_DRAFT_CONDITIONS__[idx]) {{
    window.__AI_DRAFT_CONDITIONS__[idx]._selected = isChecked;
  }}
  updateDraftCount();
}}

function toggleSelectAllDrafts(isChecked) {{
  window.__AI_DRAFT_CONDITIONS__.forEach((cond, idx) => {{
    cond._selected = isChecked;
    const chk = document.getElementById('draft-chk-' + idx);
    if (chk) chk.checked = isChecked;
  }});
  updateDraftCount();
}}

function removeDraftCondition(idx) {{
  const card = document.getElementById('draft-card-' + idx);
  if (card) card.remove();
  if (window.__AI_DRAFT_CONDITIONS__[idx]) {{
    window.__AI_DRAFT_CONDITIONS__[idx]._deleted = true;
  }}
  updateDraftCount();
}}

function updateDraftCount() {{
  const active = window.__AI_DRAFT_CONDITIONS__.filter(c => !c._deleted);
  const selected = active.filter(c => c._selected);
  document.getElementById('ai-draft-selected-summary').textContent = selected.length + ' of ' + active.length + ' selected';
  document.getElementById('btn-approve-conditions').textContent = '✓ Approve & Save Selected (' + selected.length + ')';
}}

function discardDraftConditions() {{
  window.__AI_DRAFT_CONDITIONS__ = [];
  document.getElementById('ai-cond-draft-section').style.display = 'none';
}}

async function submitDraftApproval(status) {{
  const sel = document.getElementById('ai-cond-req-select');
  if (!sel || !sel.value) return;
  const reqId = parseInt(sel.value, 10);

  // Harvest final edited values from inputs
  const conditionsToSave = [];
  window.__AI_DRAFT_CONDITIONS__.forEach((cond, idx) => {{
    if (cond._deleted || !cond._selected) return;
    const titleEl = document.getElementById('draft-title-' + idx);
    const descEl = document.getElementById('draft-desc-' + idx);
    const catEl = document.getElementById('draft-cat-' + idx);
    const riskEl = document.getElementById('draft-risk-' + idx);

    conditionsToSave.push({{
      title: titleEl ? titleEl.value.trim() : cond.title,
      description: descEl ? descEl.value.trim() : cond.description,
      category: catEl ? catEl.value : cond.category,
      risk_level: riskEl ? riskEl.value : cond.risk_level,
      rationale: cond.rationale || ''
    }});
  }});

  if (conditionsToSave.length === 0) {{
    alert('Please select at least one test condition to approve.');
    return;
  }}

  const btnApprove = document.getElementById('btn-approve-conditions');
  btnApprove.disabled = true;
  btnApprove.textContent = 'Saving...';

  try {{
    const resp = await fetch('/api/requirements/approve-draft-conditions', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-CSRF-Token': window.__CSRF_TOKEN__
      }},
      body: JSON.stringify({{
        csrf_token: window.__CSRF_TOKEN__,
        requirement_id: reqId,
        conditions: conditionsToSave,
        status: status || 'approved'
      }})
    }});

    const res = await resp.json();
    if (!res.success) {{
      throw new Error(res.error || 'Failed to save approved conditions.');
    }}

    document.getElementById('ai-cond-step-config').style.display = 'none';
    document.getElementById('ai-cond-draft-section').style.display = 'none';
    document.getElementById('ai-success-msg').textContent = res.message || ('Created ' + res.created_count + ' conditions for REQ-' + reqId);
    document.getElementById('ai-cond-success').style.display = 'block';

  }} catch (err) {{
    alert('Error saving approved conditions: ' + err.message);
  }} finally {{
    btnApprove.disabled = false;
    updateDraftCount();
  }}
}}
</script>
'''
