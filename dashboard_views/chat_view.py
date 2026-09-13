"""
Interactive AI Chatbot Dashboard View.
Provides a multi-turn scrollable chat interface powered by Gemini
with dynamic role selection, model routing, and workspace context integration.
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List

from gemini_chat_service import ROLE_DEFINITIONS, ROUTING_MODES, MODEL_COMPLEX, MODEL_GENERAL, MODEL_FAST


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def format_message_text(text: str) -> str:
    """Format markdown-like text to safe HTML with code blocks and paragraphs."""
    if not text:
        return ""
    
    # Escape HTML first
    safe = h(text)
    
    # Format code blocks ```code```
    import re
    def code_block_sub(match):
        code_content = match.group(1).strip()
        return f'<pre class="chat-code-block"><code>{code_content}</code></pre>'
    
    safe = re.sub(r'```(?:[a-zA-Z0-9_-]+)?\n?(.*?)```', code_block_sub, safe, flags=re.DOTALL)
    
    # Format inline code `code`
    safe = re.sub(r'`([^`]+)`', r'<code class="chat-inline-code">\1</code>', safe)
    
    # Format bold **text**
    safe = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', safe)
    
    # Format italic *text*
    safe = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', safe)
    
    # Convert line breaks to paragraphs/br
    lines = safe.split("\n")
    formatted_lines = []
    in_list = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                formatted_lines.append('<ul class="chat-bullet-list">')
                in_list = True
            item_text = stripped[2:]
            formatted_lines.append(f'<li>{item_text}</li>')
        elif re.match(r'^\d+\.\s+', stripped):
            if not in_list:
                formatted_lines.append('<ol class="chat-ordered-list">')
                in_list = True
            item_text = re.sub(r'^\d+\.\s+', '', stripped)
            formatted_lines.append(f'<li>{item_text}</li>')
        else:
            if in_list:
                formatted_lines.append('</ul>' if formatted_lines[-1].startswith('<li>') else '</ol>')
                in_list = False
            if stripped:
                formatted_lines.append(f'<p class="chat-p">{line}</p>')
            else:
                formatted_lines.append('<div class="chat-spacer"></div>')
                
    if in_list:
        formatted_lines.append('</ul>')
        
    return "".join(formatted_lines)


def render_chat_view(
    conversation_turns: List[Dict[str, Any]],
    current_role: str = "general_assistant",
    current_mode: str = "auto",
    custom_system_instruction: str = "",
    csrf_token: str = "",
) -> str:
    """Render the full Chat View HTML for dashboard."""
    role_options_html = []
    for r_key, r_data in ROLE_DEFINITIONS.items():
        selected = 'selected' if r_key == current_role else ''
        role_options_html.append(
            f'<option value="{r_key}" {selected}>{h(r_data["title"])}</option>'
        )

    mode_options_html = []
    for m_key, m_label in ROUTING_MODES.items():
        selected = 'selected' if m_key == current_mode else ''
        mode_options_html.append(
            f'<option value="{m_key}" {selected}>{h(m_label)}</option>'
        )

    # Render past turns
    turns_html = []
    if not conversation_turns:
        turns_html.append(f'''
        <div class="chat-empty-state" id="chat-empty-placeholder">
          <div class="empty-icon-wrap">
            <svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="#3b82f6" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          </div>
          <h3 style="margin: 12px 0 6px 0; color: #1e293b;">Welcome to the Gemini Chat Assistant</h3>
          <p style="color: #64748b; max-width: 540px; margin: 0 auto 20px auto; font-size: 13px;">
            Choose a specialized role and model complexity. The assistant has full read-only awareness of your active cases, test sessions, and workspace context.
          </p>
          <div class="quick-prompts-grid">
            <button type="button" class="quick-prompt-btn" onclick="sendQuickPrompt('Analyze our active test conditions and suggest regression coverage risks.')">
              🧪 <strong>Test Coverage Analysis</strong><br><span style="font-size:11px;color:#64748b;">Review test cases and find missing edge cases</span>
            </button>
            <button type="button" class="quick-prompt-btn" onclick="sendQuickPrompt('Draft a clear client update for our highest priority investigating case.')">
              💬 <strong>Draft Client Update</strong><br><span style="font-size:11px;color:#64748b;">Professional support update with next actions</span>
            </button>
            <button type="button" class="quick-prompt-btn" onclick="sendQuickPrompt('Help me break down a technical blocker into actionable sub-tasks and root causes.')">
              🔍 <strong>Root Cause Breakdown</strong><br><span style="font-size:11px;color:#64748b;">Deep dive into complex technical problems</span>
            </button>
            <button type="button" class="quick-prompt-btn" onclick="sendQuickPrompt('Summarize today\\'s active shift progress and remaining follow-ups.')">
              📋 <strong>Shift & Standup Summary</strong><br><span style="font-size:11px;color:#64748b;">Consolidate work logs and pending items</span>
            </button>
          </div>
        </div>
        ''')
    else:
        for t in conversation_turns:
            role = t.get("role", "user")
            text = t.get("text", "")
            created_at = t.get("created_at", "")[:19].replace("T", " ")
            meta = t.get("metadata", {})
            model_tag = meta.get("model") or meta.get("model_used") or ""
            role_tag = meta.get("role_key") or ""
            channel_tag = t.get("source_channel", "system").title()
            
            is_user = role == "user"
            bubble_class = "user-bubble" if is_user else "assistant-bubble"
            avatar_html = (
                '<div class="chat-avatar user-avatar">U</div>'
                if is_user
                else '<div class="chat-avatar bot-avatar"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2 2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM4 14a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6zm2 2v4h12v-4H6zm3-4a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm6 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg></div>'
            )

            meta_chips = []
            if model_tag:
                meta_chips.append(f'<span class="model-badge">{h(model_tag)}</span>')
            if role_tag:
                r_title = ROLE_DEFINITIONS.get(role_tag, {}).get("title", role_tag)
                meta_chips.append(f'<span class="role-badge">{h(r_title)}</span>')
            if channel_tag != 'System':
                meta_chips.append(f'<span class="role-badge">{h(channel_tag)}</span>')

            formatted_body = format_message_text(text)

            turns_html.append(f'''
            <div class="chat-message-row {'user-row' if is_user else 'assistant-row'}">
              {avatar_html if not is_user else ''}
              <div class="message-bubble-wrapper">
                <div class="message-meta-header">
                  <span class="message-sender">{ 'You' if is_user else 'Gemini Assistant' }</span>
                  <div class="meta-chips-group">
                    {' '.join(meta_chips)}
                    <span class="message-time">{h(created_at)}</span>
                  </div>
                </div>
                <div class="chat-message-bubble {bubble_class}">
                  {formatted_body}
                </div>
              </div>
              {avatar_html if is_user else ''}
            </div>
            ''')

    return f'''
<div class="chat-view-container">
  <!-- Top Chat Control Bar -->
  <div class="chat-control-bar card">
    <div class="chat-control-left">
      <div class="control-group">
        <label for="chat-role-select" class="control-label">Assistant Role / Persona:</label>
        <select id="chat-role-select" class="chat-select" onchange="handleRoleChange()">
          {''.join(role_options_html)}
        </select>
      </div>

      <div class="control-group">
        <label for="chat-mode-select" class="control-label">Model Routing & Complexity:</label>
        <select id="chat-mode-select" class="chat-select" onchange="handleModeChange()">
          {''.join(mode_options_html)}
        </select>
      </div>
    </div>

    <div class="chat-control-right">
      <button type="button" class="btn-subtle" onclick="toggleCustomPrompt()" id="btn-custom-prompt" title="Configure System Instruction">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        System Prompt
      </button>
      <button type="button" class="btn-subtle" onclick="clearChatHistory()" title="Clear Conversation Thread">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        Clear Thread
      </button>
    </div>
  </div>

  <!-- Expandable Custom System Prompt Box -->
  <div id="custom-prompt-panel" class="custom-prompt-panel card" style="display: none;">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
      <h4 style="margin: 0; font-size: 13px; font-weight: 600; color: #1e293b;">Custom System Instruction</h4>
      <span style="font-size: 11px; color: #64748b;">Directs Gemini's behavior for this conversation</span>
    </div>
    <textarea id="custom-system-prompt" rows="3" class="custom-prompt-input" placeholder="Enter custom persona instructions, domain constraints, or specialized output requirements...">{h(custom_system_instruction)}</textarea>
  </div>

  <!-- Role Description Banner -->
  <div class="role-banner" id="role-banner">
    <span class="role-banner-icon" id="role-banner-icon">🎯</span>
    <div class="role-banner-text">
      <strong id="role-banner-title">{h(ROLE_DEFINITIONS.get(current_role, {}).get("title", "Assistant"))}</strong>: 
      <span id="role-banner-desc">{h(ROLE_DEFINITIONS.get(current_role, {}).get("description", ""))}</span>
    </div>
    <div class="model-spec-tag" id="model-spec-tag">
      Target: <code>{MODEL_GENERAL}</code>
    </div>
  </div>

  <!-- Scrollable Messages Container -->
  <div class="chat-thread-card card">
    <div class="chat-thread-container" id="chat-thread-container">
      {''.join(turns_html)}
    </div>

    <!-- Typing Indicator (Hidden by default) -->
    <div class="typing-indicator-row" id="typing-indicator" style="display: none;">
      <div class="chat-avatar bot-avatar">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2 2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM4 14a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6zm2 2v4h12v-4H6zm3-4a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm6 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg>
      </div>
      <div class="typing-bubble">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </div>
      <span class="typing-text" id="typing-status-text">Gemini is reasoning...</span>
    </div>

    <!-- Message Input Bar -->
    <div class="chat-input-wrapper">
      <div class="chat-input-box">
        <textarea
          id="chat-user-input"
          class="chat-textarea"
          placeholder="Ask a question, request test analysis, or discuss a support case... (Press Enter to send, Shift+Enter for newline)"
          rows="2"
          onkeydown="handleInputKeydown(event)"
        ></textarea>
        <button type="button" class="chat-send-btn" id="chat-send-btn" onclick="submitChatMessage()">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          Send
        </button>
      </div>
      <div class="chat-input-footer">
        <span class="keyboard-hint">💡 ProTip: Shift + Enter for new lines • Multi-turn history is automatically saved</span>
        <div class="active-context-pill">
          <span class="pulse-dot"></span> Workspace Context Active
        </div>
      </div>
    </div>
  </div>
</div>

<style>
.chat-view-container {{
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 1200px;
  margin: 0 auto;
}}

.chat-control-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  gap: 16px;
  flex-wrap: wrap;
}}
.chat-control-left {{
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}}
.chat-control-right {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
.control-group {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
.control-label {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  white-space: nowrap;
}}
.chat-select {{
  padding: 6px 10px;
  font-size: 13px;
  border-radius: var(--radius-sm);
  background: #f8fafc;
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  cursor: pointer;
}}
.chat-select:focus {{
  border-color: var(--color-primary);
  background: #ffffff;
}}

.custom-prompt-panel {{
  padding: 14px 18px;
  border-color: #cbd5e1;
  background: #f8fafc;
}}
.custom-prompt-input {{
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: #ffffff;
  box-sizing: border-box;
  font-family: inherit;
}}

.role-banner {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: var(--radius-md);
  font-size: 13px;
  color: #1e3a8a;
}}
.role-banner-icon {{
  font-size: 18px;
}}
.role-banner-text {{
  flex-grow: 1;
}}
.model-spec-tag {{
  font-size: 11px;
  background: #dbeafe;
  color: #1e40af;
  padding: 3px 8px;
  border-radius: 6px;
  font-weight: 600;
}}

.chat-thread-card {{
  padding: 0;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 280px);
  min-height: 520px;
  overflow: hidden;
  border-radius: var(--radius-md);
}}

.chat-thread-container {{
  flex-grow: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  scroll-behavior: smooth;
  background: #fafafa;
}}

/* Message Rows */
.chat-message-row {{
  display: flex;
  gap: 12px;
  width: 100%;
}}
.user-row {{
  justify-content: flex-end;
}}
.assistant-row {{
  justify-content: flex-start;
}}

.chat-avatar {{
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 13px;
  flex-shrink: 0;
}}
.user-avatar {{
  background: #2563eb;
  color: #ffffff;
}}
.bot-avatar {{
  background: #4338ca;
  color: #ffffff;
}}

.message-bubble-wrapper {{
  display: flex;
  flex-direction: column;
  max-width: 78%;
}}
.user-row .message-bubble-wrapper {{
  align-items: flex-end;
}}
.assistant-row .message-bubble-wrapper {{
  align-items: flex-start;
}}

.message-meta-header {{
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--text-muted);
}}
.message-sender {{
  font-weight: 600;
  color: var(--text-secondary);
}}
.meta-chips-group {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.model-badge {{
  background: #e0e7ff;
  color: #3730a3;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-family: monospace;
}}
.role-badge {{
  background: #f1f5f9;
  color: #475569;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
}}

.chat-message-bubble {{
  padding: 14px 18px;
  border-radius: var(--radius-md);
  font-size: 14px;
  line-height: 1.6;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
  word-break: break-word;
}}
.user-bubble {{
  background: #2563eb;
  color: #ffffff;
  border-bottom-right-radius: 2px;
}}
.user-bubble p {{
  color: #ffffff !important;
  margin: 0;
}}
.assistant-bubble {{
  background: #ffffff;
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-bottom-left-radius: 2px;
}}

/* Message Text Typography */
.chat-p {{
  margin: 0 0 8px 0;
}}
.chat-p:last-child {{
  margin-bottom: 0;
}}
.chat-spacer {{
  height: 8px;
}}
.chat-bullet-list, .chat-ordered-list {{
  margin: 6px 0 8px 20px;
  padding: 0;
}}
.chat-bullet-list li, .chat-ordered-list li {{
  margin-bottom: 4px;
}}
.chat-code-block {{
  background: #0f172a;
  color: #f8fafc;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  overflow-x: auto;
  margin: 8px 0;
}}
.chat-inline-code {{
  background: #f1f5f9;
  color: #0f172a;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
  font-family: monospace;
}}
.user-bubble .chat-inline-code {{
  background: rgba(255, 255, 255, 0.2);
  color: #ffffff;
}}

/* Empty State */
.chat-empty-state {{
  text-align: center;
  padding: 30px 20px;
  margin: auto;
}}
.empty-icon-wrap {{
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: #eff6ff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto;
}}
.quick-prompts-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  max-width: 640px;
  margin: 0 auto;
  text-align: left;
}}
.quick-prompt-btn {{
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  cursor: pointer;
  text-align: left;
  transition: all 0.15s;
  display: block;
  width: 100%;
}}
.quick-prompt-btn:hover {{
  border-color: #3b82f6;
  background: #f8fafc;
  transform: translateY(-1px);
}}

/* Typing Indicator */
.typing-indicator-row {{
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 24px;
  background: #f1f5f9;
  border-top: 1px solid var(--border-subtle);
}}
.typing-bubble {{
  display: inline-flex;
  gap: 4px;
  padding: 6px 10px;
  background: #ffffff;
  border-radius: 14px;
  border: 1px solid #cbd5e1;
}}
.typing-bubble .dot {{
  width: 6px;
  height: 6px;
  background: #3b82f6;
  border-radius: 50%;
  animation: typingBounce 1.2s infinite ease-in-out;
}}
.typing-bubble .dot:nth-child(2) {{ animation-delay: 0.2s; }}
.typing-bubble .dot:nth-child(3) {{ animation-delay: 0.4s; }}
@keyframes typingBounce {{
  0%, 60%, 100% {{ transform: translateY(0); }}
  30% {{ transform: translateY(-4px); }}
}}
.typing-text {{
  font-size: 12px;
  color: var(--text-secondary);
  font-style: italic;
}}

/* Input Box */
.chat-input-wrapper {{
  padding: 16px 20px;
  background: #ffffff;
  border-top: 1px solid var(--border-subtle);
}}
.chat-input-box {{
  display: flex;
  gap: 10px;
  align-items: flex-end;
}}
.chat-textarea {{
  flex-grow: 1;
  resize: none;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.4;
  border: 1px solid #cbd5e1;
  border-radius: var(--radius-sm);
  background: #ffffff;
  box-sizing: border-box;
}}
.chat-textarea:focus {{
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}}
.chat-send-btn {{
  background: var(--color-primary);
  color: #ffffff;
  border: 1px solid var(--color-primary);
  padding: 10px 18px;
  border-radius: var(--radius-sm);
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 44px;
  transition: background 0.15s;
}}
.chat-send-btn:hover {{
  background: var(--color-primary-hover);
}}
.chat-send-btn:disabled {{
  background: #94a3b8;
  border-color: #94a3b8;
  cursor: not-allowed;
}}
.chat-input-footer {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-muted);
}}
.active-context-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #16a34a;
  font-weight: 500;
}}

@media (max-width: 768px) {{
  .chat-thread-card {{ height: calc(100vh - 220px); }}
  .quick-prompts-grid {{ grid-template-columns: 1fr; }}
  .message-bubble-wrapper {{ max-width: 90%; }}
  .chat-control-bar {{ flex-direction: column; align-items: stretch; }}
  .chat-control-left {{ flex-direction: column; align-items: stretch; }}
}}
</style>

<script>
const CSRF_TOKEN = '{csrf_token}';
const ROLES_INFO = {json.dumps(ROLE_DEFINITIONS)};
const MODELS_INFO = {{
  'complex': '{MODEL_COMPLEX}',
  'general': '{MODEL_GENERAL}',
  'fast': '{MODEL_FAST}',
  'auto': 'Auto-routed ({MODEL_GENERAL} / {MODEL_COMPLEX} / {MODEL_FAST})'
}};

function handleRoleChange() {{
  const roleKey = document.getElementById('chat-role-select').value;
  const roleData = ROLES_INFO[roleKey] || {{ title: 'Assistant', description: '' }};
  document.getElementById('role-banner-title').innerText = roleData.title;
  document.getElementById('role-banner-desc').innerText = roleData.description;
  
  if (roleKey === 'custom') {{
    document.getElementById('custom-prompt-panel').style.display = 'block';
  }}
}}

function handleModeChange() {{
  const modeKey = document.getElementById('chat-mode-select').value;
  const targetModel = MODELS_INFO[modeKey] || '{MODEL_GENERAL}';
  document.getElementById('model-spec-tag').innerHTML = 'Target: <code>' + targetModel + '</code>';
}}

function toggleCustomPrompt() {{
  const panel = document.getElementById('custom-prompt-panel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}}

function sendQuickPrompt(promptText) {{
  document.getElementById('chat-user-input').value = promptText;
  submitChatMessage();
}}

function handleInputKeydown(e) {{
  if (e.key === 'Enter' && !e.shiftKey) {{
    e.preventDefault();
    submitChatMessage();
  }}
}}

function scrollToBottom() {{
  const container = document.getElementById('chat-thread-container');
  if (container) {{
    container.scrollTop = container.scrollHeight;
  }}
}}

window.addEventListener('DOMContentLoaded', () => {{
  scrollToBottom();
  handleRoleChange();
  handleModeChange();
}});

async function submitChatMessage() {{
  const inputEl = document.getElementById('chat-user-input');
  const messageText = inputEl.value.trim();
  if (!messageText) return;

  const roleKey = document.getElementById('chat-role-select').value;
  const taskMode = document.getElementById('chat-mode-select').value;
  const customPrompt = document.getElementById('custom-system-prompt').value;
  const clientMessageId = 'web:' + crypto.randomUUID();
  const sendBtn = document.getElementById('chat-send-btn');
  const container = document.getElementById('chat-thread-container');
  const emptyPlaceholder = document.getElementById('chat-empty-placeholder');
  const typingIndicator = document.getElementById('typing-indicator');

  if (emptyPlaceholder) {{
    emptyPlaceholder.style.display = 'none';
  }}

  // Append user message to UI immediately
  const nowStr = new Date().toISOString().substring(0, 19).replace('T', ' ');
  const userRowHtml = `
    <div class="chat-message-row user-row">
      <div class="message-bubble-wrapper">
        <div class="message-meta-header">
          <span class="message-sender">You</span>
          <span class="message-time">${{nowStr}}</span>
        </div>
        <div class="chat-message-bubble user-bubble">
          <p class="chat-p">${{escapeHtml(messageText)}}</p>
        </div>
      </div>
      <div class="chat-avatar user-avatar">U</div>
    </div>
  `;
  container.insertAdjacentHTML('beforeend', userRowHtml);
  inputEl.value = '';
  scrollToBottom();

  // Show typing indicator
  sendBtn.disabled = true;
  typingIndicator.style.display = 'flex';
  document.getElementById('typing-status-text').innerText = 'Assistant is processing your request...';

  try {{
    const response = await fetch('/api/chat', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-CSRF-Token': CSRF_TOKEN
      }},
      body: JSON.stringify({{
        message: messageText,
        role_key: roleKey,
        task_mode: taskMode,
        custom_system_instruction: customPrompt,
        client_message_id: clientMessageId,
        csrf_token: CSRF_TOKEN
      }})
    }});

    const data = await response.json();
    typingIndicator.style.display = 'none';
    sendBtn.disabled = false;

    if (data.success) {{
      const modelBadge = data.model_used ? `<span class="model-badge">${{escapeHtml(data.model_used)}}</span>` : '';
      const roleBadge = data.role_title ? `<span class="role-badge">${{escapeHtml(data.role_title)}}</span>` : '';
      const formattedReply = formatMarkdownClient(data.reply);

      const botRowHtml = `
        <div class="chat-message-row assistant-row">
          <div class="chat-avatar bot-avatar">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2 2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM4 14a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6zm2 2v4h12v-4H6zm3-4a1 1 0 1 1 0-2 1 1 0 0 1 0 2zm6 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg>
          </div>
          <div class="message-bubble-wrapper">
            <div class="message-meta-header">
              <span class="message-sender">Gemini Assistant</span>
              <div class="meta-chips-group">
                ${{modelBadge}}
                ${{roleBadge}}
                <span class="message-time">${{data.created_at ? data.created_at.substring(0, 19).replace('T', ' ') : nowStr}}</span>
              </div>
            </div>
            <div class="chat-message-bubble assistant-bubble">
              ${{formattedReply}}
            </div>
          </div>
        </div>
      `;
      container.insertAdjacentHTML('beforeend', botRowHtml);
      if (data.proposal_id) {{
        const proposalHtml = `<div class="chat-message-row assistant-row"><div class="message-bubble-wrapper"><div class="chat-message-bubble assistant-bubble"><strong>Confirmation required</strong><br><button onclick="resolveChatProposal('${{escapeHtml(data.proposal_id)}}','confirm')">Confirm</button> <button onclick="resolveChatProposal('${{escapeHtml(data.proposal_id)}}','cancel')">Cancel</button></div></div></div>`;
        container.insertAdjacentHTML('beforeend', proposalHtml);
      }}
      scrollToBottom();
    }} else {{
      alert('Chat error: ' + (data.error || 'Failed to process message'));
    }}
  }} catch (err) {{
    typingIndicator.style.display = 'none';
    sendBtn.disabled = false;
    alert('Network or server error: ' + err.message);
  }}
}}

async function resolveChatProposal(proposalId, action) {{
  const response = await fetch('/api/chat/proposal', {{method:'POST', headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF_TOKEN}}, body:JSON.stringify({{proposal_id:proposalId, action:action, csrf_token:CSRF_TOKEN}})}});
  const data = await response.json();
  if (!data.success) alert(data.error || 'Proposal could not be completed.');
  else window.location.reload();
}}

async function clearChatHistory() {{
  if (!confirm('Are you sure you want to clear the conversation thread?')) return;
  try {{
    const res = await fetch('/api/chat/clear', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-CSRF-Token': CSRF_TOKEN
      }},
      body: JSON.stringify({{ csrf_token: CSRF_TOKEN }})
    }});
    const data = await res.json();
    if (data.success) {{
      window.location.reload();
    }} else {{
      alert('Could not clear history: ' + (data.error || 'unknown error'));
    }}
  }} catch (err) {{
    alert('Error: ' + err.message);
  }}
}}

function escapeHtml(str) {{
  if (!str) return '';
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}}

function formatMarkdownClient(raw) {{
  if (!raw) return '';
  let safe = escapeHtml(raw);
  // Code blocks
  safe = safe.replace(/```(?:[a-zA-Z0-9_-]+)?\\n?([\\s\\S]*?)```/g, '<pre class="chat-code-block"><code>$1</code></pre>');
  // Inline code
  safe = safe.replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>');
  // Bold
  safe = safe.replace(/\\*\\*([^\\*]+)\\*\\*/g, '<strong>$1</strong>');
  // Italic
  safe = safe.replace(/\\*([^\\*]+)\\*/g, '<em>$1</em>');
  
  const lines = safe.split('\\n');
  let result = '';
  let inList = false;
  for (let l of lines) {{
    let s = l.trim();
    if (s.startsWith('- ') || s.startsWith('* ')) {{
      if (!inList) {{ result += '<ul class="chat-bullet-list">'; inList = true; }}
      result += '<li>' + s.substring(2) + '</li>';
    }} else {{
      if (inList) {{ result += '</ul>'; inList = false; }}
      if (s) {{
        result += '<p class="chat-p">' + l + '</p>';
      }} else {{
        result += '<div class="chat-spacer"></div>';
      }}
    }}
  }}
  if (inList) result += '</ul>';
  return result;
}}
</script>
'''
