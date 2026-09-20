import React, { useState, useEffect } from 'react';

// Declarations for window.copilotAPI injected via preload
declare global {
  interface Window {
    copilotAPI?: {
      captureMessage: (payload: any) => Promise<any>;
      requestSuggestion: (payload: {
        captured_event_id: number;
        case_id?: string;
        product_scope?: string;
        issue_type?: string;
      }) => Promise<any>;
      copySuggestion: (suggestionId: string) => Promise<any>;
      rejectSuggestion: (suggestionId: string) => Promise<any>;
      confirmSent: (params: { suggestionId: string; exactSentText: string; idempotencyKey: string }) => Promise<any>;
      getActivityToday: () => Promise<any>;
      createActivity: (payload: any) => Promise<any>;
      previewReport: (reportType: 'tod' | 'eod') => Promise<any>;
      finalizeReport: (params: { reportType: 'tod' | 'eod'; previewId: string; expectedFactsHash: string }) => Promise<any>;
      createKnowledge: (payload: any) => Promise<any>;
      createCase: (payload: any) => Promise<any>;
      listCases: () => Promise<any>;
    };
  }
}

export interface SourceItem {
  article_id: string;
  article_version_id: string;
  version_number: number;
  retrieval_rank: number;
  retrieval_score: number;
}

export interface Suggestion {
  id: string;
  captured_event_id: number;
  resolved_case_id: string | null;
  lifecycle_status: string;
  draft: string;
  missing_facts: string[];
  assumptions: string[];
  confidence: number;
  recommended_action: string;
  sources: SourceItem[];
  correlation_id: string;
}

export interface CaseCandidate {
  id: string;
  case_number: string;
  title: string;
  status: string;
  client_identifier: string;
}

export interface ActivityEvent {
  id: string;
  event_type: string;
  subject_type: string;
  subject_id: string;
  case_id: string | null;
  actor: string;
  details: Record<string, any>;
  occurred_at: string;
}

interface ReportSnapshot {
  id: string;
  report_type: 'tod' | 'eod';
  report_date: string;
  lifecycle_status: 'preview' | 'finalized';
  facts_hash: string;
  content: {
    summary: { verified_work_items: number; non_completion_events_excluded: number };
    work_by_case: Record<string, Array<{ event_type: string; subject_id: string; details: Record<string, any> }>>;
  };
}

export const App: React.FC = () => {
  const [clientMessage, setClientMessage] = useState('');
  const [clientId, setClientId] = useState('client-1');
  const [explicitCaseId, setExplicitCaseId] = useState('');
  const [productScope, setProductScope] = useState('core');
  const [issueType, setIssueType] = useState('missing_logs');

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [editableFinalText, setEditableFinalText] = useState('');
  const [candidates, setCandidates] = useState<CaseCandidate[]>([]);
  const [activities, setActivities] = useState<ActivityEvent[]>([]);
  const [activityType, setActivityType] = useState('support.completed');
  const [activitySummary, setActivitySummary] = useState('');
  const [report, setReport] = useState<ReportSnapshot | null>(null);
  const [knowledgeTitle, setKnowledgeTitle] = useState('');
  const [knowledgeContent, setKnowledgeContent] = useState('');
  const [caseNumber, setCaseNumber] = useState('');
  const [caseTitle, setCaseTitle] = useState('');
  const [openCases, setOpenCases] = useState<CaseCandidate[]>([]);

  const api = window.copilotAPI;

  const loadActivities = async () => {
    if (!api?.getActivityToday) return;
    try {
      const res = await api.getActivityToday();
      if (res?.events) {
        setActivities(res.events);
      }
    } catch (err) {
      console.error('Failed to load activity:', err);
    }
  };

  useEffect(() => {
    loadActivities();
    api?.listCases?.().then((res) => setOpenCases(res?.cases || [])).catch(() => undefined);
  }, []);

  const handleGenerateSuggestion = async (overrideCaseId?: string) => {
    if (!clientMessage.trim()) {
      setError('Please enter a client message.');
      return;
    }

    setLoading(true);
    setError(null);
    setStatusMessage(null);
    setCandidates([]);
    setSuggestion(null);

    try {
      if (!api) {
        throw new Error('Desktop IPC bridge unavailable.');
      }

      // 1. Capture message
      const eventId = `desktop-evt-${Date.now()}`;
      const capturePayload = {
        event: {
          provider: 'manual',
          event_id: eventId,
          event_type: 'message.received',
          occurred_at: new Date().toISOString(),
          actor: { external_id: clientId, role: 'client' },
          conversation: { external_id: `conv-${clientId}` },
          payload: { text: clientMessage },
          schema_version: 1,
        },
      };

      const captureResult = await api.captureMessage(capturePayload);
      if (!captureResult?.captured_event_id) {
        throw new Error('The captured message did not return a usable event ID.');
      }

      // 2. Request suggestion
      const caseToUse = overrideCaseId || (explicitCaseId.trim() ? explicitCaseId.trim() : undefined);
      const suggRes = await api.requestSuggestion({
        captured_event_id: captureResult.captured_event_id,
        case_id: caseToUse,
        product_scope: productScope.trim() || undefined,
        issue_type: issueType.trim() || undefined,
      });

      if (suggRes.status === 'suggested' && suggRes.suggestion) {
        setSuggestion(suggRes.suggestion);
        setEditableFinalText(suggRes.suggestion.draft);
      } else if (suggRes.status === 'resolution_required') {
        setCandidates(suggRes.candidates || []);
        setStatusMessage('Case reference is ambiguous. Please select a matching case.');
      } else if (suggRes.status === 'knowledge_unavailable') {
        setStatusMessage('No approved knowledge found for this inquiry. Manual reply required.');
      } else if (suggRes.status === 'provider_timeout') {
        setStatusMessage('AI generation timed out. Approved sources remain available.');
      } else if (suggRes.status === 'provider_unavailable') {
        setStatusMessage('AI provider is currently unavailable.');
      } else {
        setError(suggRes.message || 'Unable to generate suggestion.');
      }
    } catch (err: any) {
      setError(err.message || 'Failed to generate suggestion.');
    } finally {
      setLoading(false);
      loadActivities();
    }
  };

  const handleCopy = async () => {
    if (!suggestion || !api) return;
    try {
      await navigator.clipboard.writeText(editableFinalText);
      await api.copySuggestion(suggestion.id);
      setSuggestion((prev) => (prev ? { ...prev, lifecycle_status: 'copied' } : null));
      setStatusMessage('Draft copied to clipboard (Status: copied). Note: Copied text is NOT sent text.');
      loadActivities();
    } catch (err: any) {
      setError(err.message || 'Failed to copy.');
    }
  };

  const handleReject = async () => {
    if (!suggestion || !api) return;
    try {
      await api.rejectSuggestion(suggestion.id);
      setSuggestion((prev) => (prev ? { ...prev, lifecycle_status: 'rejected' } : null));
      setStatusMessage('Suggestion marked as rejected.');
      loadActivities();
    } catch (err: any) {
      setError(err.message || 'Failed to reject.');
    }
  };

  const handleConfirmSent = async () => {
    if (!suggestion || !api) return;
    if (!editableFinalText.trim()) {
      setError('Cannot confirm an empty response as sent.');
      return;
    }

    try {
      const idempKey = `send-confirm-${suggestion.id}`;
      await api.confirmSent({
        suggestionId: suggestion.id,
        exactSentText: editableFinalText,
        idempotencyKey: idempKey,
      });
      setSuggestion((prev) => (prev ? { ...prev, lifecycle_status: 'sent' } : null));
      setStatusMessage('Response successfully confirmed as sent locally.');
      loadActivities();
    } catch (err: any) {
      setError(err.message || 'Failed to confirm sent response.');
    }
  };

  const handleAddActivity = async () => {
    if (!api?.createActivity || !activitySummary.trim()) {
      setError('Enter a short verified activity summary.');
      return;
    }
    const subjectType = activityType.split('.')[0];
    try {
      await api.createActivity({
        event_type: activityType,
        subject_type: subjectType,
        subject_id: `manual-${Date.now()}`,
        case_id: explicitCaseId.trim() || undefined,
        details: { summary: activitySummary.trim() },
        occurred_at: new Date().toISOString(),
      });
      setActivitySummary('');
      setStatusMessage('Verified work activity recorded locally.');
      await loadActivities();
    } catch (err: any) {
      setError(err.message || 'Failed to record activity.');
    }
  };

  const handlePreviewReport = async (reportType: 'tod' | 'eod') => {
    if (!api?.previewReport) return;
    try {
      setReport(await api.previewReport(reportType));
      setStatusMessage(`${reportType.toUpperCase()} preview created from verified activity.`);
    } catch (err: any) {
      setError(err.message || 'Failed to create report preview.');
    }
  };

  const handleFinalizeReport = async () => {
    if (!api?.finalizeReport || !report) return;
    try {
      const finalized = await api.finalizeReport({
        reportType: report.report_type,
        previewId: report.id,
        expectedFactsHash: report.facts_hash,
      });
      setReport(finalized);
      setStatusMessage('Report finalized against its unchanged facts hash.');
    } catch (err: any) {
      setError(err.message || 'Report changed and must be previewed again.');
    }
  };

  const handleCreateKnowledge = async () => {
    if (!api?.createKnowledge || !knowledgeTitle.trim() || !knowledgeContent.trim()) {
      setError('Knowledge title and content are required.');
      return;
    }
    try {
      await api.createKnowledge({
        stable_key: `manual-${Date.now()}`,
        title: knowledgeTitle.trim(),
        product_scope: productScope.trim(),
        issue_type: issueType.trim(),
        initial_content: knowledgeContent.trim(),
        required_facts: [],
        prohibited_claims: ['resolved without verified resolution'],
      });
      setKnowledgeTitle('');
      setKnowledgeContent('');
      setStatusMessage('Knowledge article created and approved for retrieval.');
    } catch (err: any) {
      setError(err.message || 'Failed to create knowledge article.');
    }
  };

  const handleCreateCase = async () => {
    if (!api?.createCase || !caseNumber.trim() || !caseTitle.trim() || !clientId.trim()) {
      setError('Case number, title, and client identifier are required.');
      return;
    }
    try {
      const created = await api.createCase({
        case_number: caseNumber.trim(),
        title: caseTitle.trim(),
        client_identifier: clientId.trim(),
      });
      setOpenCases((current) => [created, ...current]);
      setExplicitCaseId(created.id);
      setCaseNumber('');
      setCaseTitle('');
      setStatusMessage('Case created and selected for the next suggestion.');
    } catch (err: any) {
      setError(err.message || 'Failed to create case.');
    }
  };

  return (
    <div className="container" role="main">
      <header>
        <div className="banner" role="status">
          🔒 Local only — no external delivery. The application never sends externally.
        </div>
        <h1>Support Copilot</h1>
      </header>

      {error && (
        <div className="alert alert-warning" role="alert">
          {error}
        </div>
      )}

      {statusMessage && (
        <div className="alert alert-info" role="status">
          {statusMessage}
        </div>
      )}

      {/* 1. Client Message Input */}
      <section className="card" aria-labelledby="client-msg-heading">
        <h2 id="client-msg-heading">1. Client Message</h2>
        <div className="form-group">
          <label htmlFor="client-message-input">Received Client Text</label>
          <textarea
            id="client-message-input"
            aria-label="Received Client Text"
            placeholder="Paste or enter client message..."
            value={clientMessage}
            onChange={(e) => setClientMessage(e.target.value)}
          />
        </div>

        <div className="action-row">
          <div className="form-group" style={{ flex: 1 }}>
            <label htmlFor="client-id-input">Client Identifier</label>
            <input
              id="client-id-input"
              aria-label="Client Identifier"
              type="text"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
            />
          </div>

          <div className="form-group" style={{ flex: 1 }}>
            <label htmlFor="explicit-case-input">Explicit Case ID (Optional)</label>
            <select
              id="explicit-case-input"
              aria-label="Explicit Case ID"
              value={explicitCaseId}
              onChange={(e) => setExplicitCaseId(e.target.value)}
            >
              <option value="">Resolve automatically</option>
              {openCases.map((item) => (
                <option key={item.id} value={item.id}>{item.case_number}: {item.title}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="action-row">
          <div className="form-group" style={{ flex: 1 }}>
            <label htmlFor="product-scope-input">Product Scope</label>
            <input
              id="product-scope-input"
              aria-label="Product Scope"
              type="text"
              value={productScope}
              onChange={(e) => setProductScope(e.target.value)}
            />
          </div>

          <div className="form-group" style={{ flex: 1 }}>
            <label htmlFor="issue-type-input">Issue Type</label>
            <input
              id="issue-type-input"
              aria-label="Issue Type"
              type="text"
              value={issueType}
              onChange={(e) => setIssueType(e.target.value)}
            />
          </div>
        </div>

        <button
          id="generate-suggestion-btn"
          className="btn"
          onClick={() => handleGenerateSuggestion()}
          disabled={loading || !clientMessage.trim()}
          aria-busy={loading}
        >
          {loading ? 'Generating Grounded Suggestion...' : 'Generate Suggestion'}
        </button>
      </section>

      {/* 2. Ambiguous Case Selection (if required) */}
      {candidates.length > 0 && (
        <section className="card" aria-labelledby="ambiguous-cases-heading">
          <h2 id="ambiguous-cases-heading">Select Matching Case</h2>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            Multiple open cases match this client. Select a case to resolve:
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {candidates.map((c) => (
              <button
                key={c.id}
                className="btn btn-secondary"
                style={{ justifyContent: 'space-between' }}
                onClick={() => handleGenerateSuggestion(c.id)}
              >
                <span><strong>{c.case_number}</strong>: {c.title}</span>
                <span className="tag tag-info">{c.status}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* 3. Grounded Suggestion Panel */}
      {suggestion && (
        <section className="card" aria-labelledby="suggestion-heading">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 id="suggestion-heading">2. Grounded Suggestion</h2>
            <span
              id="suggestion-status-tag"
              className={`tag ${
                suggestion.lifecycle_status === 'sent'
                  ? 'tag-info'
                  : suggestion.lifecycle_status === 'copied'
                  ? 'tag-warning'
                  : 'tag-info'
              }`}
            >
              Status: {suggestion.lifecycle_status}
            </span>
          </div>

          {/* Missing Facts Badges */}
          {suggestion.missing_facts?.length > 0 && (
            <div className="form-group">
              <label>Missing Required Facts (Verify with Client):</label>
              <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                {suggestion.missing_facts.map((fact, idx) => (
                  <span key={idx} className="tag tag-warning">
                    ⚠️ {fact}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Exact Knowledge Sources */}
          <div className="form-group">
            <label>Approved Sources & Versions:</label>
            <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {suggestion.sources?.map((s, idx) => (
                <li key={idx} style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                  📖 Article <code>{s.article_id}</code> (Version {s.version_number}) — Rank #{s.retrieval_rank}
                </li>
              ))}
            </ul>
          </div>

          {/* Editable Final Response */}
          <div className="form-group">
            <label htmlFor="final-response-input">Editable Final Response (Human Control)</label>
            <textarea
              id="final-response-input"
              aria-label="Editable Final Response"
              rows={4}
              value={editableFinalText}
              onChange={(e) => setEditableFinalText(e.target.value)}
              disabled={suggestion.lifecycle_status === 'sent' || suggestion.lifecycle_status === 'rejected'}
            />
          </div>

          {/* Actions */}
          <div className="action-row">
            <button
              id="copy-suggestion-btn"
              className="btn btn-secondary"
              onClick={handleCopy}
              disabled={suggestion.lifecycle_status === 'rejected'}
            >
              📋 Copy Draft
            </button>

            <button
              id="reject-suggestion-btn"
              className="btn btn-danger"
              onClick={handleReject}
              disabled={suggestion.lifecycle_status === 'sent' || suggestion.lifecycle_status === 'rejected'}
            >
              ✕ Reject
            </button>

            <button
              id="confirm-sent-btn"
              className="btn btn-success"
              onClick={handleConfirmSent}
              disabled={suggestion.lifecycle_status === 'sent' || suggestion.lifecycle_status === 'rejected'}
            >
              ✓ I Sent This Exact Response
            </button>
          </div>
        </section>
      )}

      <section className="card" aria-labelledby="setup-heading">
        <h2 id="setup-heading">Cases & Approved Knowledge</h2>
        <div className="form-group">
          <label htmlFor="case-number">New Case Number</label>
          <input id="case-number" value={caseNumber} onChange={(e) => setCaseNumber(e.target.value)} placeholder="CASE-001" />
        </div>
        <div className="form-group">
          <label htmlFor="case-title">New Case Title</label>
          <input id="case-title" value={caseTitle} onChange={(e) => setCaseTitle(e.target.value)} placeholder="Missing attendance logs" />
        </div>
        <button className="btn btn-secondary" onClick={handleCreateCase}>Create & Select Case</button>

        <div className="form-group" style={{ marginTop: '16px' }}>
          <label htmlFor="knowledge-title">Knowledge Article Title</label>
          <input id="knowledge-title" value={knowledgeTitle} onChange={(e) => setKnowledgeTitle(e.target.value)} />
        </div>
        <div className="form-group">
          <label htmlFor="knowledge-content">Approved Response Guidance</label>
          <textarea id="knowledge-content" value={knowledgeContent} onChange={(e) => setKnowledgeContent(e.target.value)} />
        </div>
        <button className="btn btn-secondary" onClick={handleCreateKnowledge}>Create & Approve Knowledge</button>
      </section>

      <section className="card" aria-labelledby="daily-memory-heading">
        <h2 id="daily-memory-heading">Daily Work Memory</h2>
        <div className="form-group">
          <label htmlFor="activity-type">Verified Activity Type</label>
          <select id="activity-type" value={activityType} onChange={(e) => setActivityType(e.target.value)}>
            <option value="support.completed">Support completed</option>
            <option value="test.completed">Testing completed</option>
            <option value="escalation.created">Escalation created</option>
            <option value="followup.created">Follow-up created</option>
            <option value="outcome.verified">Outcome verified</option>
          </select>
        </div>
        <div className="form-group">
          <label htmlFor="activity-summary">Verified Summary</label>
          <input
            id="activity-summary"
            value={activitySummary}
            onChange={(e) => setActivitySummary(e.target.value)}
            placeholder="What did you actually complete?"
          />
        </div>
        <div className="action-row">
          <button className="btn btn-secondary" onClick={handleAddActivity}>Record Activity</button>
          <button className="btn btn-secondary" onClick={() => handlePreviewReport('tod')}>Preview TOD</button>
          <button className="btn" onClick={() => handlePreviewReport('eod')}>Preview EOD</button>
        </div>
      </section>

      {report && (
        <section className="card" aria-labelledby="report-preview-heading">
          <h2 id="report-preview-heading">{report.report_type.toUpperCase()} Report — {report.report_date}</h2>
          <p><strong>Status:</strong> {report.lifecycle_status}</p>
          <p><strong>Verified work items:</strong> {report.content.summary.verified_work_items}</p>
          <p><strong>Draft/copied events excluded:</strong> {report.content.summary.non_completion_events_excluded}</p>
          {Object.entries(report.content.work_by_case).map(([caseId, items]) => (
            <div key={caseId} className="form-group">
              <label>Case: {caseId}</label>
              <ul>
                {items.map((item, index) => (
                  <li key={`${item.subject_id}-${index}`}>
                    {item.event_type}: {String(item.details.summary || item.subject_id)}
                  </li>
                ))}
              </ul>
            </div>
          ))}
          <button
            className="btn btn-success"
            onClick={handleFinalizeReport}
            disabled={report.lifecycle_status === 'finalized'}
          >
            Finalize Unchanged Report
          </button>
        </section>
      )}

      {/* 4. Recent Local Activity */}
      <section className="card" aria-labelledby="activity-heading">
        <h2 id="activity-heading">Today's Local Activity</h2>
        {activities.length === 0 ? (
          <p style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>No activity recorded today.</p>
        ) : (
          <ul className="activity-list">
            {activities.map((act) => (
              <li key={act.id} className="activity-item">
                <span>
                  <strong>{act.event_type}</strong> ({act.subject_type})
                </span>
                <span style={{ color: 'var(--text-secondary)' }}>
                  {new Date(act.occurred_at).toLocaleTimeString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
};
