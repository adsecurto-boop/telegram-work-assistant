"""
Google Workspace Hub view for Personal Work Assistant.
Integrates Google Sheets, Tasks, Drive, Docs, and Keep bridge.
Adheres strictly to client-side token acquisition and mandatory confirmation dialogs.
"""
from __future__ import annotations
import html
import json
from datetime import datetime, timezone

def h(text: object) -> str:
    return html.escape(str(text) if text is not None else '')

def render_workspace_view(service, csrf_token: str) -> str:
    work_items = service.work_item_service.list_work_items()
    active_blockers = service.work_item_service.get_blockers(status='active')
    test_cases = service.work_item_service.list_test_cases()
    cases = service.database.list_cases()
    shift = service.database.active_shift()

    ops_payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'active_shift': shift,
        'work_items': [
            {
                'id': w.get('id'),
                'title': w.get('title', ''),
                'status': w.get('status', ''),
                'priority': w.get('priority', ''),
                'type': w.get('type', ''),
                'assignee': w.get('assignee', ''),
                'created_at': w.get('created_at', '')
            }
            for w in work_items[:100]
        ],
        'active_blockers': [
            {'id': b.get('id'), 'work_item_id': b.get('work_item_id'), 'reason': b.get('reason', '')}
            for b in active_blockers
        ],
        'test_summary': {
            'total': len(test_cases),
            'passed': sum(1 for tc in test_cases if tc.get('last_execution_result') == 'pass'),
            'failed': sum(1 for tc in test_cases if tc.get('last_execution_result') == 'fail')
        },
        'cases': [
            {
                'id': c.get('id'),
                'subject': c.get('subject', ''),
                'client': c.get('client', ''),
                'status': c.get('status', ''),
                'priority': c.get('priority', '')
            }
            for c in cases[:50]
        ]
    }
    ops_json = json.dumps(ops_payload)

    return f'''
<h1>Google Workspace Operations Hub</h1>
<p class="muted">Integrated Google Sheets, Tasks, Drive, Docs, and Keep operations. All Google Workspace calls execute securely in-browser with client-side OAuth tokens.</p>

<!-- Auth & Connection Card -->
<div class="card" id="ws-auth-card" style="margin-bottom:20px;">
  <div id="ws-signed-out" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
    <div>
      <h3 style="margin:0 0 6px 0;">Google Account Authorization</h3>
      <p class="muted" style="margin:0;">Connect your Google Account to synchronize Google Sheets, Tasks, Drive, and Docs. Access tokens remain cached strictly in-memory.</p>
    </div>
    <div>
      <button class="gsi-material-button" id="gsi-signin-btn" type="button">
        <div class="gsi-material-button-state"></div>
        <div class="gsi-material-button-content-wrapper">
          <div class="gsi-material-button-icon">
            <svg version="1.1" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" style="display: block;">
              <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"></path>
              <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"></path>
              <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"></path>
              <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"></path>
              <path fill="none" d="M0 0h48v48H0z"></path>
            </svg>
          </div>
          <span class="gsi-material-button-contents">Sign in with Google</span>
        </div>
      </button>
    </div>
  </div>

  <div id="ws-signed-in" style="display:none;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
    <div style="display:flex;align-items:center;gap:12px;">
      <img id="user-avatar" src="" alt="Avatar" style="width:42px;height:42px;border-radius:50%;border:1px solid #dce2e8;display:none;">
      <div>
        <div style="display:flex;align-items:center;gap:8px;">
          <strong id="user-name" style="font-size:15px;">Google User</strong>
          <span class="tag tag-ok" id="token-status">Token Active (In-Memory)</span>
        </div>
        <div class="muted" id="user-email" style="font-size:13px;">user@example.com</div>
        <div style="margin-top:4px;">
          <span class="tag">Google Sheets</span>
          <span class="tag">Google Tasks</span>
          <span class="tag">Google Drive</span>
          <span class="tag">Google Docs</span>
        </div>
      </div>
    </div>
    <div>
      <button type="button" id="ws-signout-btn" class="danger">Sign Out / Disconnect</button>
    </div>
  </div>
</div>

<!-- Tabs Navigation -->
<div class="ws-tabs">
  <button type="button" class="ws-tab-btn active" data-tab="tab-sheets">Google Sheets</button>
  <button type="button" class="ws-tab-btn" data-tab="tab-tasks">Google Tasks</button>
  <button type="button" class="ws-tab-btn" data-tab="tab-drive">Google Drive</button>
  <button type="button" class="ws-tab-btn" data-tab="tab-docs">Google Docs</button>
  <button type="button" class="ws-tab-btn" data-tab="tab-keep">Google Keep</button>
</div>

<!-- Tab 1: Google Sheets -->
<div id="tab-sheets" class="ws-panel active">
  <div class="grid" style="grid-template-columns: 1fr 1fr; margin-bottom: 20px;">
    <div class="card">
      <h2>Export Operations Data to Google Sheets</h2>
      <p class="muted">Exports live items from your SQLite operations database into a new Google Spreadsheet on your Google Drive.</p>
      <div style="margin-bottom:12px;">
        <label><strong>Export Dataset:</strong></label><br>
        <select id="sheets-export-type" style="width:100%;margin-top:4px;">
          <option value="work_items">All Work Items ({len(work_items)} records)</option>
          <option value="test_suite">Testing Posture & Execution Results ({len(test_cases)} tests)</option>
          <option value="cases">Support & Operation Cases ({len(cases)} cases)</option>
        </select>
      </div>
      <div style="margin-bottom:14px;">
        <label><strong>Spreadsheet Title:</strong></label><br>
        <input type="text" id="sheets-export-title" value="Personal Work Assistant - Operations Export" style="width:100%;box-sizing:border-box;margin-top:4px;">
      </div>
      <button type="button" id="btn-export-sheet" class="primary">Export to New Google Sheet</button>
      <div id="sheets-export-result" style="margin-top:12px;"></div>
    </div>

    <div class="card">
      <h2>Google Spreadsheets in Drive</h2>
      <p class="muted">Browse and preview spreadsheets in your Google Drive.</p>
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <input type="text" id="sheets-search" placeholder="Filter spreadsheet by name..." style="flex:1;">
        <button type="button" id="btn-refresh-sheets">Refresh</button>
      </div>
      <div id="sheets-list-container" style="max-height:300px;overflow-y:auto;">
        <p class="muted">Sign in with Google to list spreadsheets.</p>
      </div>
    </div>
  </div>

  <div class="card" id="sheet-preview-card" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h3 id="sheet-preview-title" style="margin:0;">Spreadsheet Preview</h3>
      <a id="sheet-open-link" href="#" target="_blank" class="tag tag-ok">Open in Google Sheets &rarr;</a>
    </div>
    <div id="sheet-preview-table-container" style="margin-top:12px;overflow-x:auto;max-height:400px;"></div>
  </div>
</div>

<!-- Tab 2: Google Tasks -->
<div id="tab-tasks" class="ws-panel">
  <div class="grid" style="grid-template-columns: 2fr 1fr; margin-bottom: 20px;">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <h2 style="margin:0;">Google Tasks</h2>
        <div style="display:flex;gap:8px;align-items:center;">
          <label class="muted">List:</label>
          <select id="task-list-selector"></select>
          <button type="button" id="btn-refresh-tasks">Refresh</button>
        </div>
      </div>
      <div id="tasks-container" style="max-height:450px;overflow-y:auto;">
        <p class="muted">Sign in with Google to load Google Tasks.</p>
      </div>
    </div>

    <div class="card">
      <h2>Add New Google Task</h2>
      <p class="muted">Create a task directly in your Google Tasks list.</p>
      <form id="form-create-task" onsubmit="return false;">
        <div style="margin-bottom:10px;">
          <label><strong>Title:</strong></label><br>
          <input type="text" id="new-task-title" required placeholder="e.g., Verify staging deployment" style="width:100%;box-sizing:border-box;margin-top:4px;">
        </div>
        <div style="margin-bottom:10px;">
          <label><strong>Notes / Context:</strong></label><br>
          <textarea id="new-task-notes" rows="3" placeholder="Additional instructions or references..." style="width:100%;box-sizing:border-box;margin-top:4px;"></textarea>
        </div>
        <div style="margin-bottom:14px;">
          <label><strong>Due Date (Optional):</strong></label><br>
          <input type="date" id="new-task-due" style="width:100%;box-sizing:border-box;margin-top:4px;">
        </div>
        <button type="button" id="btn-submit-task" class="primary">Create Google Task</button>
      </form>
      <div id="create-task-result" style="margin-top:10px;"></div>
    </div>
  </div>
</div>

<!-- Tab 3: Google Drive -->
<div id="tab-drive" class="ws-panel">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
      <div>
        <h2 style="margin:0 0 4px 0;">Google Drive Files</h2>
        <p class="muted" style="margin:0;">Browse, search, and manage your Google Drive assets.</p>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <input type="text" id="drive-search-input" placeholder="Search files in Drive..." style="min-width:220px;">
        <select id="drive-type-filter">
          <option value="all">All File Types</option>
          <option value="spreadsheets">Spreadsheets</option>
          <option value="documents">Documents</option>
          <option value="folders">Folders</option>
        </select>
        <button type="button" id="btn-refresh-drive">Search</button>
      </div>
    </div>

    <div style="display:flex;gap:10px;margin-bottom:14px;">
      <button type="button" id="btn-create-drive-folder">+ New Drive Folder</button>
    </div>

    <div id="drive-files-container" style="max-height:500px;overflow-y:auto;">
      <p class="muted">Sign in with Google to browse Drive files.</p>
    </div>
  </div>
</div>

<!-- Tab 4: Google Docs -->
<div id="tab-docs" class="ws-panel">
  <div class="grid" style="grid-template-columns: 1fr 1fr; margin-bottom: 20px;">
    <div class="card">
      <h2>Generate Daily Shift Handover Doc</h2>
      <p class="muted">Compiles the active shift posture, open blockers, test verification statistics, and active work items directly into a formatted Google Document.</p>
      <div style="margin-bottom:12px;">
        <label><strong>Document Title:</strong></label><br>
        <input type="text" id="docs-export-title" value="Daily Operations Handover - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}" style="width:100%;box-sizing:border-box;margin-top:4px;">
      </div>
      <div style="margin-bottom:12px;">
        <label><strong>Handover Notes / Summary:</strong></label><br>
        <textarea id="docs-handover-notes" rows="3" style="width:100%;box-sizing:border-box;margin-top:4px;">Shift operations normal. Automated test suites verified. All blocker statuses synced.</textarea>
      </div>
      <button type="button" id="btn-export-doc" class="primary">Generate & Export Google Doc</button>
      <div id="docs-export-result" style="margin-top:12px;"></div>
    </div>

    <div class="card">
      <h2>Google Docs in Drive</h2>
      <p class="muted">Browse and inspect Google Documents from your Drive.</p>
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <input type="text" id="docs-search" placeholder="Filter documents by name..." style="flex:1;">
        <button type="button" id="btn-refresh-docs">Refresh</button>
      </div>
      <div id="docs-list-container" style="max-height:300px;overflow-y:auto;">
        <p class="muted">Sign in with Google to list Google Docs.</p>
      </div>
    </div>
  </div>

  <div class="card" id="doc-preview-card" style="display:none;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h3 id="doc-preview-title" style="margin:0;">Document Content Preview</h3>
      <a id="doc-open-link" href="#" target="_blank" class="tag tag-ok">Open in Google Docs &rarr;</a>
    </div>
    <div id="doc-preview-body" style="margin-top:12px;background:#fcfcfc;border:1px solid #e1e4e8;border-radius:6px;padding:16px;max-height:400px;overflow-y:auto;white-space:pre-wrap;font-family:serif;font-size:15px;line-height:1.6;"></div>
  </div>
</div>

<!-- Tab 5: Google Keep -->
<div id="tab-keep" class="ws-panel">
  <div class="card" style="margin-bottom:20px;">
    <h2>Google Keep Status & Operations Notes Bridge</h2>
    <div style="background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:14px;margin:12px 0;">
      <strong style="color:#7a5200;">Google Keep API Authorization Notice</strong>
      <p style="margin:6px 0 0 0;color:#5a3c00;font-size:13px;line-height:1.5;">
        The official Google Keep REST API (<code>https://www.googleapis.com/auth/keep</code>) is restricted by Google Identity to Google Workspace Enterprise domains. Consumer accounts receive an <code>INVALID_ARGUMENT</code> scope restriction.
        To ensure seamless productivity, you can use the <strong>Operations Notes Bridge</strong> below to compose notes and bridge them directly into your <strong>Google Tasks</strong> or <strong>Google Docs</strong> with 1-click!
      </p>
    </div>

    <div class="grid" style="grid-template-columns: 1fr 1fr; margin-top:16px;">
      <div>
        <h3>Compose Work Note</h3>
        <input type="text" id="keep-note-title" placeholder="Note Title (e.g., Release Checklist)" style="width:100%;box-sizing:border-box;margin-bottom:8px;">
        <textarea id="keep-note-body" rows="6" placeholder="Write operations notes, investigation findings, or handover points..." style="width:100%;box-sizing:border-box;margin-bottom:12px;"></textarea>
        <div style="display:flex;gap:10px;">
          <button type="button" id="btn-bridge-to-tasks" class="primary">Push Note to Google Tasks</button>
          <button type="button" id="btn-bridge-to-docs">Export Note to Google Doc</button>
        </div>
        <div id="keep-bridge-result" style="margin-top:10px;"></div>
      </div>

      <div>
        <h3>Recent Operations Notes Scratchpad</h3>
        <p class="muted">Quick reference scratchpad stored for current session triage.</p>
        <div id="keep-local-notes-container" style="max-height:260px;overflow-y:auto;border:1px solid #e1e4e8;border-radius:6px;padding:10px;background:#fbfcfd;">
          <div class="ws-item-card">
            <div>
              <strong>Production Retest Verification</strong>
              <p class="muted" style="margin:4px 0 0 0;">Check all Webhook endpoints and verify SQLite WAL journal stability.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- MANDATORY CONFIRMATION MODAL -->
<div id="workspace-confirm-modal" class="modal-overlay">
  <div class="modal-card">
    <h3 id="modal-action-title" style="margin-top:0;color:#17212b;">Confirm Action</h3>
    <p id="modal-action-desc" class="muted" style="margin:14px 0;line-height:1.5;font-size:14px;color:#24292f;"></p>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:20px;">
      <button id="modal-cancel-btn" type="button">Cancel</button>
      <button id="modal-confirm-btn" type="button" class="primary">Confirm Action</button>
    </div>
  </div>
</div>

<!-- Hidden Form for Importing Google Tasks to Review Inbox -->
<form id="import-task-form" method="POST" action="/workspace/import-task" style="display:none;">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <input type="hidden" name="task_id" id="import-task-id">
  <input type="hidden" name="title" id="import-task-title">
  <input type="hidden" name="notes" id="import-task-notes">
  <input type="hidden" name="due" id="import-task-due">
</form>

<script id="ops-data" type="application/json">
{ops_json}
</script>

<script>
(function() {{
  'use strict';

  // In-Memory Token Cache (MANDATORY: Never stored in localStorage / sessionStorage)
  let cachedAccessToken = null;
  let cachedUser = null;
  let firebaseConfig = null;
  let tokenClient = null;

  const SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/tasks.readonly',
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/documents.readonly'
  ];

  // DOM Elements
  const btnSignIn = document.getElementById('gsi-signin-btn');
  const btnSignOut = document.getElementById('ws-signout-btn');
  const signedOutDiv = document.getElementById('ws-signed-out');
  const signedInDiv = document.getElementById('ws-signed-in');
  const userNameEl = document.getElementById('user-name');
  const userEmailEl = document.getElementById('user-email');
  const userAvatarEl = document.getElementById('user-avatar');

  // Modal elements
  const confirmModal = document.getElementById('workspace-confirm-modal');
  const modalTitle = document.getElementById('modal-action-title');
  const modalDesc = document.getElementById('modal-action-desc');
  const modalConfirmBtn = document.getElementById('modal-confirm-btn');
  const modalCancelBtn = document.getElementById('modal-cancel-btn');

  // Tab switching
  document.querySelectorAll('.ws-tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.ws-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.ws-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const target = document.getElementById(btn.getAttribute('data-tab'));
      if (target) target.classList.add('active');
    }});
  }});

  // Mandatory confirmation helper
  function requestConfirmation(title, description, isDanger, onConfirm) {{
    modalTitle.textContent = title;
    modalDesc.textContent = description;
    modalConfirmBtn.className = isDanger ? 'danger' : 'primary';
    modalConfirmBtn.textContent = isDanger ? 'Delete / Mutate' : 'Confirm Action';

    const handleConfirm = () => {{
      confirmModal.style.display = 'none';
      cleanup();
      onConfirm();
    }};
    const handleCancel = () => {{
      confirmModal.style.display = 'none';
      cleanup();
    }};
    function cleanup() {{
      modalConfirmBtn.removeEventListener('click', handleConfirm);
      modalCancelBtn.removeEventListener('click', handleCancel);
    }}
    modalConfirmBtn.addEventListener('click', handleConfirm);
    modalCancelBtn.addEventListener('click', handleCancel);
    confirmModal.style.display = 'flex';
  }}

  // Fetch Firebase applet config
  async function loadConfig() {{
    try {{
      const res = await fetch('/firebase-applet-config.json');
      if (res.ok) {{
        firebaseConfig = await res.json();
      }}
    }} catch (e) {{
      console.warn('Could not fetch firebase config:', e);
    }}
  }}

  // Initialize GSI Token Client
  function initGsiClient() {{
    if (window.google && window.google.accounts && window.google.accounts.oauth2 && firebaseConfig && firebaseConfig.oAuthClientId) {{
      tokenClient = window.google.accounts.oauth2.initTokenClient({{
        client_id: firebaseConfig.oAuthClientId,
        scope: SCOPES.join(' '),
        callback: async (tokenResponse) => {{
          if (tokenResponse.error) {{
            alert('Google Sign-in failed: ' + tokenResponse.error);
            return;
          }}
          cachedAccessToken = tokenResponse.access_token;
          await fetchUserProfile();
          onAuthenticated();
        }}
      }});
    }}
  }}

  async function fetchUserProfile() {{
    if (!cachedAccessToken) return;
    try {{
      const res = await fetch('https://www.googleapis.com/oauth2/v3/userinfo', {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      if (res.ok) {{
        cachedUser = await res.json();
      }}
    }} catch (e) {{
      console.warn('Could not fetch user profile:', e);
    }}
  }}

  function onAuthenticated() {{
    signedOutDiv.style.display = 'none';
    signedInDiv.style.display = 'flex';
    userNameEl.textContent = (cachedUser && cachedUser.name) || 'Authorized User';
    userEmailEl.textContent = (cachedUser && cachedUser.email) || 'adsecurto@gmail.com';
    if (cachedUser && cachedUser.picture) {{
      userAvatarEl.src = cachedUser.picture;
      userAvatarEl.style.display = 'inline-block';
    }}

    // Trigger initial data loads
    loadSpreadsheets();
    loadTaskLists();
    loadDriveFiles();
    loadDocs();
  }}

  function onSignOut() {{
    // Clear in-memory token
    cachedAccessToken = null;
    cachedUser = null;
    signedOutDiv.style.display = 'flex';
    signedInDiv.style.display = 'none';
    document.getElementById('sheets-list-container').innerHTML = '<p class="muted">Sign in with Google to list spreadsheets.</p>';
    document.getElementById('tasks-container').innerHTML = '<p class="muted">Sign in with Google to load Google Tasks.</p>';
    document.getElementById('drive-files-container').innerHTML = '<p class="muted">Sign in with Google to browse Drive files.</p>';
    document.getElementById('docs-list-container').innerHTML = '<p class="muted">Sign in with Google to list Google Docs.</p>';
  }}

  btnSignIn.addEventListener('click', () => {{
    if (!tokenClient) {{
      initGsiClient();
    }}
    if (tokenClient) {{
      tokenClient.requestAccessToken({{ prompt: 'consent' }});
    }} else {{
      alert('Google authentication service is initializing. Please retry in a moment.');
    }}
  }});

  btnSignOut.addEventListener('click', () => {{
    requestConfirmation(
      'Disconnect Google Account',
      'This will clear the active in-memory Google Workspace token from this session.',
      false,
      () => onSignOut()
    );
  }});

  // --- GOOGLE SHEETS INTEGRATION ---
  async function loadSpreadsheets() {{
    if (!cachedAccessToken) return;
    const container = document.getElementById('sheets-list-container');
    container.innerHTML = '<p class="muted">Loading spreadsheets from Google Drive...</p>';
    try {{
      const q = encodeURIComponent("mimeType='application/vnd.google-apps.spreadsheet' and trashed=false");
      const res = await fetch(`https://www.googleapis.com/drive/v3/files?q=${{q}}&pageSize=20&fields=files(id,name,modifiedTime,webViewLink)&orderBy=modifiedTime%20desc`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const data = await res.json();
      if (!data.files || data.files.length === 0) {{
        container.innerHTML = '<p class="muted">No spreadsheets found in Drive. Export one using the form on the left!</p>';
        return;
      }}
      container.innerHTML = data.files.map(f => `
        <div class="ws-item-card">
          <div style="flex:1;">
            <strong>${{escapeHtml(f.name)}}</strong><br>
            <span class="muted" style="font-size:11px;">Modified: ${{new Date(f.modifiedTime).toLocaleString()}}</span>
          </div>
          <div>
            <button type="button" class="ws-action-btn btn-view-sheet" data-id="${{f.id}}" data-name="${{escapeHtml(f.name)}}" data-link="${{f.webViewLink}}">Preview</button>
            <a href="${{f.webViewLink}}" target="_blank" class="tag tag-ok" style="font-size:11px;">Open</a>
          </div>
        </div>
      `).join('');

      container.querySelectorAll('.btn-view-sheet').forEach(b => {{
        b.addEventListener('click', () => {{
          previewSheet(b.getAttribute('data-id'), b.getAttribute('data-name'), b.getAttribute('data-link'));
        }});
      }});
    }} catch (e) {{
      container.innerHTML = `<p class="tag tag-err">Failed to load spreadsheets: ${{escapeHtml(e.message)}}</p>`;
    }}
  }}

  async function previewSheet(spreadsheetId, sheetName, link) {{
    if (!cachedAccessToken) return;
    const card = document.getElementById('sheet-preview-card');
    const title = document.getElementById('sheet-preview-title');
    const linkEl = document.getElementById('sheet-open-link');
    const tableContainer = document.getElementById('sheet-preview-table-container');

    card.style.display = 'block';
    title.textContent = 'Preview: ' + sheetName;
    linkEl.href = link;
    tableContainer.innerHTML = '<p class="muted">Fetching sheet data...</p>';

    try {{
      const metaRes = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${{spreadsheetId}}`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const meta = await metaRes.json();
      const firstSheet = (meta.sheets && meta.sheets[0]) ? meta.sheets[0].properties.title : 'Sheet1';

      const valRes = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${{spreadsheetId}}/values/${{encodeURIComponent(firstSheet)}}!A1:Z30`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const valData = await valRes.json();
      const rows = valData.values || [];

      if (rows.length === 0) {{
        tableContainer.innerHTML = '<p class="muted">Sheet is currently empty.</p>';
        return;
      }}

      let htmlTable = '<table>';
      htmlTable += '<thead><tr>' + (rows[0] || []).map(c => `<th>${{escapeHtml(c)}}</th>`).join('') + '</tr></thead><tbody>';
      for (let i = 1; i < rows.length; i++) {{
        htmlTable += '<tr>' + (rows[i] || []).map(c => `<td>${{escapeHtml(c)}}</td>`).join('') + '</tr>';
      }}
      htmlTable += '</tbody></table>';
      tableContainer.innerHTML = htmlTable;
    }} catch (e) {{
      tableContainer.innerHTML = `<p class="tag tag-err">Could not read sheet data: ${{escapeHtml(e.message)}}</p>`;
    }}
  }}

  document.getElementById('btn-refresh-sheets').addEventListener('click', loadSpreadsheets);

  // Export to Google Sheet (Requires Explicit User Confirmation)
  document.getElementById('btn-export-sheet').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const exportType = document.getElementById('sheets-export-type').value;
    const title = document.getElementById('sheets-export-title').value.trim() || 'Personal Work Assistant Export';
    const opsData = JSON.parse(document.getElementById('ops-data').textContent);

    let rows = [];
    if (exportType === 'work_items') {{
      rows.push(['Work Item ID', 'Title', 'Status', 'Priority', 'Type', 'Assignee', 'Created At']);
      (opsData.work_items || []).forEach(w => {{
        rows.push([w.id, w.title, w.status, w.priority, w.type, w.assignee, w.created_at]);
      }});
    }} else if (exportType === 'test_suite') {{
      rows.push(['Metric', 'Value']);
      rows.push(['Total Test Cases', opsData.test_summary.total]);
      rows.push(['Passed', opsData.test_summary.passed]);
      rows.push(['Failed', opsData.test_summary.failed]);
      rows.push(['Generated At', opsData.generated_at]);
    }} else {{
      rows.push(['Case ID', 'Subject', 'Client', 'Status', 'Priority']);
      (opsData.cases || []).forEach(c => {{
        rows.push([c.id, c.subject, c.client, c.status, c.priority]);
      }});
    }}

    requestConfirmation(
      'Confirm Google Sheets Export',
      `This operation will create a new Google Spreadsheet titled "${{title}}" with ${{rows.length}} rows of operational data in your Google Drive.`,
      false,
      async () => {{
        const resultDiv = document.getElementById('sheets-export-result');
        resultDiv.innerHTML = '<span class="tag">Creating spreadsheet in Google Sheets...</span>';
        try {{
          // 1. Create spreadsheet
          const createRes = await fetch('https://sheets.googleapis.com/v4/spreadsheets', {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{
              properties: {{ title: title }}
            }})
          }});
          const newSheet = await createRes.json();
          if (!newSheet.spreadsheetId) throw new Error(newSheet.error ? newSheet.error.message : 'Failed to create spreadsheet');

          // 2. Append values
          await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${{newSheet.spreadsheetId}}/values/A1:append?valueInputOption=USER_ENTERED`, {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ values: rows }})
          }});

          resultDiv.innerHTML = `
            <div style="margin-top:8px;">
              <span class="tag tag-ok">Export Successful!</span>
              <a href="https://docs.google.com/spreadsheets/d/${{newSheet.spreadsheetId}}/edit" target="_blank" style="margin-left:8px;font-weight:600;">Open in Google Sheets &rarr;</a>
            </div>
          `;
          loadSpreadsheets();
        }} catch (e) {{
          resultDiv.innerHTML = `<span class="tag tag-err">Export error: ${{escapeHtml(e.message)}}</span>`;
        }}
      }}
    );
  }});

  // --- GOOGLE TASKS INTEGRATION ---
  let currentTaskLists = [];

  async function loadTaskLists() {{
    if (!cachedAccessToken) return;
    const selector = document.getElementById('task-list-selector');
    selector.innerHTML = '<option>Loading...</option>';
    try {{
      const res = await fetch('https://tasks.googleapis.com/tasks/v1/users/@me/lists', {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const data = await res.json();
      currentTaskLists = data.items || [];
      if (currentTaskLists.length === 0) {{
        selector.innerHTML = '<option value="">No lists found</option>';
        return;
      }}
      selector.innerHTML = currentTaskLists.map(l => `<option value="${{l.id}}">${{escapeHtml(l.title)}}</option>`).join('');
      loadTasks(selector.value);
    }} catch (e) {{
      selector.innerHTML = '<option value="">Error loading lists</option>';
    }}
  }}

  document.getElementById('task-list-selector').addEventListener('change', (e) => {{
    loadTasks(e.target.value);
  }});

  async function loadTasks(listId) {{
    if (!cachedAccessToken || !listId) return;
    const container = document.getElementById('tasks-container');
    container.innerHTML = '<p class="muted">Loading tasks...</p>';
    try {{
      const res = await fetch(`https://tasks.googleapis.com/tasks/v1/lists/${{listId}}/tasks?showCompleted=true&showHidden=true`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const data = await res.json();
      const items = data.items || [];
      if (items.length === 0) {{
        container.innerHTML = '<p class="muted">No tasks in this list. Add one using the form on the right!</p>';
        return;
      }}

      container.innerHTML = items.map(t => {{
        const isCompleted = t.status === 'completed';
        return `
          <div class="ws-item-card" style="${{isCompleted ? 'opacity:0.65;background:#f9f9f9;' : ''}}">
            <div style="display:flex;align-items:flex-start;gap:10px;flex:1;">
              <input type="checkbox" class="task-toggle-cb" data-id="${{t.id}}" data-title="${{escapeHtml(t.title)}}" data-status="${{t.status}}" ${{isCompleted ? 'checked' : ''}} style="margin-top:3px;cursor:pointer;">
              <div>
                <strong style="${{isCompleted ? 'text-decoration:line-through;' : ''}}">${{escapeHtml(t.title || 'Untitled Task')}}</strong>
                ${{t.notes ? `<p class="muted" style="margin:2px 0 0 0;">${{escapeHtml(t.notes)}}</p>` : ''}}
                <div style="margin-top:4px;">
                  ${{t.due ? `<span class="tag">Due: ${{new Date(t.due).toLocaleDateString()}}</span>` : ''}}
                  <span class="tag ${{isCompleted ? 'tag-ok' : 'tag-warn'}}">${{t.status}}</span>
                </div>
              </div>
            </div>
            <div>
              <button type="button" class="ws-action-btn btn-import-task" data-id="${{t.id}}" data-title="${{escapeHtml(t.title)}}" data-notes="${{escapeHtml(t.notes || '')}}" data-due="${{t.due || ''}}">Import to Inbox</button>
            </div>
          </div>
        `;
      }}).join('');

      // Checkbox status toggle with confirmation
      container.querySelectorAll('.task-toggle-cb').forEach(cb => {{
        cb.addEventListener('change', (e) => {{
          const taskId = cb.getAttribute('data-id');
          const taskTitle = cb.getAttribute('data-title');
          const newStatus = cb.checked ? 'completed' : 'needsAction';
          // Revert checkbox state until confirmed
          cb.checked = !cb.checked;

          requestConfirmation(
            'Confirm Task Status Update',
            `Mark task "${{taskTitle}}" as ${{newStatus === 'completed' ? 'COMPLETED' : 'NEEDS ACTION'}} in Google Tasks?`,
            false,
            async () => {{
              try {{
                await fetch(`https://tasks.googleapis.com/tasks/v1/lists/${{listId}}/tasks/${{taskId}}`, {{
                  method: 'PATCH',
                  headers: {{
                    Authorization: 'Bearer ' + cachedAccessToken,
                    'Content-Type': 'application/json'
                  }},
                  body: JSON.stringify({{ status: newStatus }})
                }});
                loadTasks(listId);
              }} catch (err) {{
                alert('Failed to update task status: ' + err.message);
              }}
            }}
          );
        }});
      }});

      // Import to review inbox
      container.querySelectorAll('.btn-import-task').forEach(b => {{
        b.addEventListener('click', () => {{
          const tId = b.getAttribute('data-id');
          const tTitle = b.getAttribute('data-title');
          const tNotes = b.getAttribute('data-notes');
          const tDue = b.getAttribute('data-due');

          document.getElementById('import-task-id').value = tId;
          document.getElementById('import-task-title').value = tTitle;
          document.getElementById('import-task-notes').value = tNotes;
          document.getElementById('import-task-due').value = tDue;
          document.getElementById('import-task-form').submit();
        }});
      }});
    }} catch (e) {{
      container.innerHTML = `<p class="tag tag-err">Error loading tasks: ${{escapeHtml(e.message)}}</p>`;
    }}
  }}

  document.getElementById('btn-refresh-tasks').addEventListener('click', () => {{
    const listId = document.getElementById('task-list-selector').value;
    loadTasks(listId);
  }});

  // Create Task (Requires Confirmation Dialog)
  document.getElementById('btn-submit-task').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const listId = document.getElementById('task-list-selector').value;
    const title = document.getElementById('new-task-title').value.trim();
    const notes = document.getElementById('new-task-notes').value.trim();
    const due = document.getElementById('new-task-due').value;

    if (!title) {{
      alert('Task title is required.');
      return;
    }}

    requestConfirmation(
      'Create Google Task',
      `Add new task "${{title}}" to your Google Tasks list?`,
      false,
      async () => {{
        const resultDiv = document.getElementById('create-task-result');
        resultDiv.innerHTML = '<span class="tag">Creating task...</span>';
        try {{
          const body = {{ title: title }};
          if (notes) body.notes = notes;
          if (due) body.due = new Date(due).toISOString();

          const res = await fetch(`https://tasks.googleapis.com/tasks/v1/lists/${{listId}}/tasks`, {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify(body)
          }});
          if (!res.ok) throw new Error('Failed to create task');

          resultDiv.innerHTML = '<span class="tag tag-ok">Task created in Google Tasks!</span>';
          document.getElementById('new-task-title').value = '';
          document.getElementById('new-task-notes').value = '';
          document.getElementById('new-task-due').value = '';
          loadTasks(listId);
        }} catch (e) {{
          resultDiv.innerHTML = `<span class="tag tag-err">Error: ${{escapeHtml(e.message)}}</span>`;
        }}
      }}
    );
  }});

  // --- GOOGLE DRIVE INTEGRATION ---
  async function loadDriveFiles() {{
    if (!cachedAccessToken) return;
    const container = document.getElementById('drive-files-container');
    const searchVal = document.getElementById('drive-search-input').value.trim();
    const filterType = document.getElementById('drive-type-filter').value;
    container.innerHTML = '<p class="muted">Loading Drive files...</p>';

    let queryParts = ['trashed=false'];
    if (filterType === 'spreadsheets') {{
      queryParts.push("mimeType='application/vnd.google-apps.spreadsheet'");
    }} else if (filterType === 'documents') {{
      queryParts.push("mimeType='application/vnd.google-apps.document'");
    }} else if (filterType === 'folders') {{
      queryParts.push("mimeType='application/vnd.google-apps.folder'");
    }}
    if (searchVal) {{
      queryParts.push(`name contains '${{searchVal.replace(/'/g, "\\'")}}\\'`);
    }}

    try {{
      const q = encodeURIComponent(queryParts.join(' and '));
      const res = await fetch(`https://www.googleapis.com/drive/v3/files?q=${{q}}&pageSize=30&fields=files(id,name,mimeType,modifiedTime,webViewLink,size)&orderBy=modifiedTime%20desc`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const data = await res.json();
      const files = data.files || [];
      if (files.length === 0) {{
        container.innerHTML = '<p class="muted">No files matching query found in Google Drive.</p>';
        return;
      }}

      container.innerHTML = files.map(f => {{
        const isFolder = f.mimeType === 'application/vnd.google-apps.folder';
        const typeBadge = f.mimeType.replace('application/vnd.google-apps.', '');
        return `
          <div class="ws-item-card">
            <div style="flex:1;">
              <strong>${{escapeHtml(f.name)}}</strong> <span class="tag" style="font-size:11px;">${{typeBadge}}</span><br>
              <span class="muted" style="font-size:11px;">Modified: ${{new Date(f.modifiedTime).toLocaleString()}}</span>
            </div>
            <div>
              <a href="${{f.webViewLink}}" target="_blank" class="tag tag-ok" style="font-size:11px;">Open in Drive</a>
              <button type="button" class="ws-action-btn danger btn-delete-file" data-id="${{f.id}}" data-name="${{escapeHtml(f.name)}}">Delete</button>
            </div>
          </div>
        `;
      }}).join('');

      // Delete file with Mandatory Confirmation Dialog
      container.querySelectorAll('.btn-delete-file').forEach(b => {{
        b.addEventListener('click', () => {{
          const fileId = b.getAttribute('data-id');
          const fileName = b.getAttribute('data-name');
          requestConfirmation(
            'Delete File from Google Drive',
            `Are you sure you want to permanently delete "${{fileName}}" from your Google Drive? This action cannot be undone.`,
            true,
            async () => {{
              try {{
                const res = await fetch(`https://www.googleapis.com/drive/v3/files/${{fileId}}`, {{
                  method: 'DELETE',
                  headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
                }});
                if (res.ok || res.status === 204) {{
                  loadDriveFiles();
                }} else {{
                  throw new Error('Delete returned status ' + res.status);
                }}
              }} catch (err) {{
                alert('Failed to delete file: ' + err.message);
              }}
            }}
          );
        }});
      }});
    }} catch (e) {{
      container.innerHTML = `<p class="tag tag-err">Error loading Drive files: ${{escapeHtml(e.message)}}</p>`;
    }}
  }}

  document.getElementById('btn-refresh-drive').addEventListener('click', loadDriveFiles);

  // Create Folder in Drive
  document.getElementById('btn-create-drive-folder').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const folderName = prompt('Enter new folder name:');
    if (!folderName || !folderName.trim()) return;

    requestConfirmation(
      'Create Google Drive Folder',
      `Create a new folder titled "${{folderName.trim()}}" in your Google Drive root?`,
      false,
      async () => {{
        try {{
          await fetch('https://www.googleapis.com/drive/v3/files', {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{
              name: folderName.trim(),
              mimeType: 'application/vnd.google-apps.folder'
            }})
          }});
          loadDriveFiles();
        }} catch (err) {{
          alert('Failed to create folder: ' + err.message);
        }}
      }}
    );
  }});

  // --- GOOGLE DOCS INTEGRATION ---
  async function loadDocs() {{
    if (!cachedAccessToken) return;
    const container = document.getElementById('docs-list-container');
    container.innerHTML = '<p class="muted">Loading Google Docs from Drive...</p>';
    try {{
      const q = encodeURIComponent("mimeType='application/vnd.google-apps.document' and trashed=false");
      const res = await fetch(`https://www.googleapis.com/drive/v3/files?q=${{q}}&pageSize=20&fields=files(id,name,modifiedTime,webViewLink)&orderBy=modifiedTime%20desc`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const data = await res.json();
      const files = data.files || [];
      if (files.length === 0) {{
        container.innerHTML = '<p class="muted">No Google Docs found in Drive. Generate one using the form on the left!</p>';
        return;
      }}

      container.innerHTML = files.map(f => `
        <div class="ws-item-card">
          <div style="flex:1;">
            <strong>${{escapeHtml(f.name)}}</strong><br>
            <span class="muted" style="font-size:11px;">Modified: ${{new Date(f.modifiedTime).toLocaleString()}}</span>
          </div>
          <div>
            <button type="button" class="ws-action-btn btn-view-doc" data-id="${{f.id}}" data-name="${{escapeHtml(f.name)}}" data-link="${{f.webViewLink}}">Preview</button>
            <a href="${{f.webViewLink}}" target="_blank" class="tag tag-ok" style="font-size:11px;">Open</a>
          </div>
        </div>
      `).join('');

      container.querySelectorAll('.btn-view-doc').forEach(b => {{
        b.addEventListener('click', () => {{
          previewDoc(b.getAttribute('data-id'), b.getAttribute('data-name'), b.getAttribute('data-link'));
        }});
      }});
    }} catch (e) {{
      container.innerHTML = `<p class="tag tag-err">Error loading docs: ${{escapeHtml(e.message)}}</p>`;
    }}
  }}

  async function previewDoc(docId, docTitle, docLink) {{
    if (!cachedAccessToken) return;
    const card = document.getElementById('doc-preview-card');
    const title = document.getElementById('doc-preview-title');
    const linkEl = document.getElementById('doc-open-link');
    const bodyEl = document.getElementById('doc-preview-body');

    card.style.display = 'block';
    title.textContent = 'Document: ' + docTitle;
    linkEl.href = docLink;
    bodyEl.textContent = 'Loading document body...';

    try {{
      const res = await fetch(`https://docs.googleapis.com/v1/documents/${{docId}}`, {{
        headers: {{ Authorization: 'Bearer ' + cachedAccessToken }}
      }});
      const doc = await res.json();
      let text = '';
      if (doc.body && doc.body.content) {{
        doc.body.content.forEach(element => {{
          if (element.paragraph && element.paragraph.elements) {{
            element.paragraph.elements.forEach(pe => {{
              if (pe.textRun && pe.textRun.content) {{
                text += pe.textRun.content;
              }}
            }});
          }}
        }});
      }}
      bodyEl.textContent = text.trim() || '(Document content is blank)';
    }} catch (e) {{
      bodyEl.textContent = 'Error reading document: ' + e.message;
    }}
  }}

  document.getElementById('btn-refresh-docs').addEventListener('click', loadDocs);

  // Generate & Export Daily Shift Handover to Google Doc (Confirmation Required)
  document.getElementById('btn-export-doc').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const title = document.getElementById('docs-export-title').value.trim() || 'Daily Operations Handover';
    const notes = document.getElementById('docs-handover-notes').value.trim();
    const opsData = JSON.parse(document.getElementById('ops-data').textContent);

    let docBody = `Personal Work Assistant — Daily Operations Handover\n`;
    docBody += `Generated At: ${{opsData.generated_at}}\n\n`;
    docBody += `==============================\n`;
    docBody += `1. OPERATIONS OVERVIEW & NOTES\n`;
    docBody += `==============================\n`;
    docBody += `${{notes}}\n\n`;
    docBody += `==============================\n`;
    docBody += `2. VERIFICATION & TEST POSTURE\n`;
    docBody += `==============================\n`;
    docBody += `Total Defined Test Cases: ${{opsData.test_summary.total}}\n`;
    docBody += `Passed: ${{opsData.test_summary.passed}}\n`;
    docBody += `Failed: ${{opsData.test_summary.failed}}\n\n`;
    docBody += `==============================\n`;
    docBody += `3. ACTIVE BLOCKERS (${{opsData.active_blockers ? opsData.active_blockers.length : 0}})\n`;
    docBody += `==============================\n`;
    if (opsData.active_blockers && opsData.active_blockers.length > 0) {{
      opsData.active_blockers.forEach(b => {{
        docBody += `• Blocker #${{b.id}} (Work Item #${{b.work_item_id}}): ${{b.reason}}\n`;
      }});
    }} else {{
      docBody += `No active blockers logged.\n`;
    }}
    docBody += `\n==============================\n`;
    docBody += `4. RECENT WORK ITEMS (${{opsData.work_items ? opsData.work_items.length : 0}})\n`;
    docBody += `==============================\n`;
    (opsData.work_items || []).slice(0, 15).forEach(w => {{
      docBody += `• [${{w.status.toUpperCase()}}] #${{w.id}} ${{w.title}} (${{w.priority}}, Assigned: ${{w.assignee || 'Unassigned'}})\n`;
    }});

    requestConfirmation(
      'Generate Google Doc Handover Report',
      `Create new Google Document titled "${{title}}" with operations status and shift handover content?`,
      false,
      async () => {{
        const resultDiv = document.getElementById('docs-export-result');
        resultDiv.innerHTML = '<span class="tag">Creating Google Doc...</span>';
        try {{
          // 1. Create blank doc
          const createRes = await fetch('https://docs.googleapis.com/v1/documents', {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ title: title }})
          }});
          const newDoc = await createRes.json();
          if (!newDoc.documentId) throw new Error('Could not create Google Doc');

          // 2. Insert body text
          await fetch(`https://docs.googleapis.com/v1/documents/${{newDoc.documentId}}:batchUpdate`, {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{
              requests: [
                {{
                  insertText: {{
                    location: {{ index: 1 }},
                    text: docBody
                  }}
                }}
              ]
            }})
          }});

          resultDiv.innerHTML = `
            <div style="margin-top:8px;">
              <span class="tag tag-ok">Document Created!</span>
              <a href="https://docs.google.com/document/d/${{newDoc.documentId}}/edit" target="_blank" style="margin-left:8px;font-weight:600;">Open in Google Docs &rarr;</a>
            </div>
          `;
          loadDocs();
        }} catch (e) {{
          resultDiv.innerHTML = `<span class="tag tag-err">Export error: ${{escapeHtml(e.message)}}</span>`;
        }}
      }}
    );
  }});

  // --- GOOGLE KEEP BRIDGE ---
  document.getElementById('btn-bridge-to-tasks').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const title = document.getElementById('keep-note-title').value.trim();
    const body = document.getElementById('keep-note-body').value.trim();
    if (!title) {{
      alert('Please enter a note title.');
      return;
    }}
    const listId = document.getElementById('task-list-selector').value || '@default';

    requestConfirmation(
      'Push Note to Google Tasks',
      `Create a new Google Task from note "${{title}}"?`,
      false,
      async () => {{
        const resultDiv = document.getElementById('keep-bridge-result');
        resultDiv.innerHTML = '<span class="tag">Pushing to Google Tasks...</span>';
        try {{
          const res = await fetch(`https://tasks.googleapis.com/tasks/v1/lists/${{listId}}/tasks`, {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ title: title, notes: body }})
          }});
          if (!res.ok) throw new Error('Tasks API returned status ' + res.status);
          resultDiv.innerHTML = '<span class="tag tag-ok">Note converted to Google Task!</span>';
          loadTasks(listId);
        }} catch (e) {{
          resultDiv.innerHTML = `<span class="tag tag-err">Error: ${{escapeHtml(e.message)}}</span>`;
        }}
      }}
    );
  }});

  document.getElementById('btn-bridge-to-docs').addEventListener('click', () => {{
    if (!cachedAccessToken) {{
      alert('Please sign in with Google first.');
      return;
    }}
    const title = document.getElementById('keep-note-title').value.trim() || 'Work Note';
    const body = document.getElementById('keep-note-body').value.trim();

    requestConfirmation(
      'Export Note to Google Doc',
      `Create a new Google Document from note "${{title}}"?`,
      false,
      async () => {{
        const resultDiv = document.getElementById('keep-bridge-result');
        resultDiv.innerHTML = '<span class="tag">Exporting to Google Docs...</span>';
        try {{
          const createRes = await fetch('https://docs.googleapis.com/v1/documents', {{
            method: 'POST',
            headers: {{
              Authorization: 'Bearer ' + cachedAccessToken,
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ title: title }})
          }});
          const newDoc = await createRes.json();
          if (!newDoc.documentId) throw new Error('Could not create doc');

          if (body) {{
            await fetch(`https://docs.googleapis.com/v1/documents/${{newDoc.documentId}}:batchUpdate`, {{
              method: 'POST',
              headers: {{
                Authorization: 'Bearer ' + cachedAccessToken,
                'Content-Type': 'application/json'
              }},
              body: JSON.stringify({{
                requests: [{{ insertText: {{ location: {{ index: 1 }}, text: body }} }}]
              }})
            }});
          }}

          resultDiv.innerHTML = `
            <span class="tag tag-ok">Exported!</span>
            <a href="https://docs.google.com/document/d/${{newDoc.documentId}}/edit" target="_blank" style="margin-left:8px;font-weight:600;">Open in Google Docs &rarr;</a>
          `;
          loadDocs();
        }} catch (e) {{
          resultDiv.innerHTML = `<span class="tag tag-err">Error: ${{escapeHtml(e.message)}}</span>`;
        }}
      }}
    );
  }});

  function escapeHtml(str) {{
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }}

  // Startup initialization
  loadConfig().then(() => {{
    initGsiClient();
  }});

}})();
</script>
'''
