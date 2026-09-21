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
      getActiveMeeting: () => Promise<any>;
      getMeeting: (sessionId: string) => Promise<any>;
      startMeeting: (payload: any) => Promise<any>;
      addTranscriptSegment: (payload: any) => Promise<any>;
      stopMeeting: (sessionId: string) => Promise<any>;
      generateMeetingProposals: (sessionId: string) => Promise<any>;
      reviewMeetingProposal: (payload: { proposalId: string; decision: 'approve' | 'reject' }) => Promise<any>;
      listWindowSources: () => Promise<Array<{ id: string; name: string }>>;
      selectWindowSource: (sourceId: string) => Promise<{ id: string; name: string }>;
      captureScreenOnce: () => Promise<any>;
      pauseScreenCapture: () => Promise<{ paused: boolean }>;
      onScreenPaused: (callback: () => void) => () => void;
      analyzeScreen: (payload: any) => Promise<any>;
      proposeLearningCandidate?: (payload: {
        suggestionId: string;
        candidate_title: string;
        candidate_content: string;
        content_reviewed_for_sensitive_data: true;
        target_stable_key?: string;
        target_article_id?: string;
        notes?: string;
      }) => Promise<any>;
      reviewLearningCandidate?: (payload: { candidateId: string; decision: 'approve' | 'reject' }) => Promise<any>;
      listLearningCandidates?: (status?: string) => Promise<any>;
      getHealthDetailed?: () => Promise<any>;
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

interface MeetingSession {
  id: string;
  title: string;
  lifecycle_status: 'active' | 'stopped';
  retention_until: string;
  started_at: string;
  stopped_at?: string;
}

interface TranscriptSegment {
  id: string;
  speaker_label?: string;
  transcript_text: string;
  confidence: number;
  uncertainty_visible: boolean;
}

interface MeetingProposal {
  id: string;
  proposal_type: 'summary' | 'action' | 'promise' | 'resolution';
  proposal_text: string;
  lifecycle_status: 'proposed' | 'approved' | 'rejected';
}

interface ScreenCaptureResult {
  blocked: boolean;
  reason?: string;
  source_name: string;
  image_data_url?: string;
  ocr_text?: string;
  ocr_confidence?: number;
  redaction_count?: number;
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
  const [meetingTitle, setMeetingTitle] = useState('Client support meeting');
  const [meetingConsent, setMeetingConsent] = useState(false);
  const [meeting, setMeeting] = useState<MeetingSession | null>(null);
  const [speakerLabel, setSpeakerLabel] = useState('');
  const [transcriptText, setTranscriptText] = useState('');
  const [transcriptConfidence, setTranscriptConfidence] = useState(0.9);
  const [transcriptSegments, setTranscriptSegments] = useState<TranscriptSegment[]>([]);
  const [meetingProposals, setMeetingProposals] = useState<MeetingProposal[]>([]);
  const [windowSources, setWindowSources] = useState<Array<{ id: string; name: string }>>([]);
  const [selectedWindowSource, setSelectedWindowSource] = useState('');
  const [approvedWindowName, setApprovedWindowName] = useState('');
  const [screenCapture, setScreenCapture] = useState<ScreenCaptureResult | null>(null);
  const [screenAnalysis, setScreenAnalysis] = useState<any>(null);
  const [screenBusy, setScreenBusy] = useState(false);

  const [learningCandidates, setLearningCandidates] = useState<any[]>([]);
  const [learningTitle, setLearningTitle] = useState('');
  const [learningContent, setLearningContent] = useState('');
  const [learningNotes, setLearningNotes] = useState('');
  const [learningContentReviewed, setLearningContentReviewed] = useState(false);
  const [healthData, setHealthData] = useState<any | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const api = window.copilotAPI;

  const handleLoadLearningCandidates = async () => {
    if (!api?.listLearningCandidates) return;
    try {
      const res = await api.listLearningCandidates();
      setLearningCandidates(res?.candidates || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load learning candidates');
    }
  };

  const handleProposeLearningCandidate = async () => {
    if (!suggestion || !api?.proposeLearningCandidate || !learningTitle.trim() || !learningContent.trim() || !learningContentReviewed) return;
    try {
      await api.proposeLearningCandidate({
        suggestionId: suggestion.id,
        candidate_title: learningTitle.trim(),
        candidate_content: learningContent.trim(),
        content_reviewed_for_sensitive_data: true,
        notes: learningNotes.trim(),
      });
      setStatusMessage('Learning candidate proposed successfully for human review.');
      setLearningTitle('');
      setLearningContent('');
      setLearningNotes('');
      setLearningContentReviewed(false);
      handleLoadLearningCandidates();
    } catch (err: any) {
      setError(err.message || 'Failed to propose learning candidate');
    }
  };

  const handleReviewLearningCandidate = async (candidateId: string, decision: 'approve' | 'reject') => {
    if (!api?.reviewLearningCandidate) return;
    try {
      await api.reviewLearningCandidate({ candidateId, decision });
      setStatusMessage(`Learning candidate ${decision === 'approve' ? 'approved' : 'rejected'}.`);
      handleLoadLearningCandidates();
    } catch (err: any) {
      setError(err.message || `Failed to ${decision} learning candidate`);
    }
  };

  const handleFetchHealth = async () => {
    if (!api?.getHealthDetailed) return;
    setHealthLoading(true);
    try {
      const res = await api.getHealthDetailed();
      setHealthData(res);
      setStatusMessage('Operational health status refreshed.');
    } catch (err: any) {
      setError(err.message || 'Failed to fetch operational health');
    } finally {
      setHealthLoading(false);
    }
  };

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
    handleLoadLearningCandidates();
    handleFetchHealth();
    api?.listCases?.().then((res) => setOpenCases(res?.cases || [])).catch(() => undefined);
    api?.getActiveMeeting?.().then(async (active) => {
      if (!active) return;
      setMeeting(active);
      const detail = await api.getMeeting(active.id);
      setTranscriptSegments(detail?.transcript_segments || []);
      setMeetingProposals(detail?.proposals || []);
    }).catch(() => undefined);
    const removePauseListener = api?.onScreenPaused?.(() => {
      setApprovedWindowName('');
      setSelectedWindowSource('');
      setScreenCapture(null);
      setScreenAnalysis(null);
      setStatusMessage('Screen assistance paused with Ctrl/Cmd+Shift+P.');
    });
    return () => removePauseListener?.();
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
    setLearningTitle('');
    setLearningContent('');
    setLearningNotes('');
    setLearningContentReviewed(false);

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
      setLearningContent(editableFinalText.trim());
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

  const handleStartMeeting = async () => {
    if (!api?.startMeeting || !meetingConsent) {
      setError('Confirm participant consent before starting transcript capture.');
      return;
    }
    try {
      const started = await api.startMeeting({
        title: meetingTitle.trim(),
        consent_acknowledged: true,
        consent_note: 'Operator confirmed that participants consented to transcription.',
        transcript_retention_days: 7,
      });
      setMeeting(started);
      setTranscriptSegments([]);
      setMeetingProposals([]);
      setStatusMessage('Meeting transcript capture is visibly active.');
    } catch (err: any) {
      setError(err.message || 'Failed to start meeting session.');
    }
  };

  const handleAddTranscript = async () => {
    if (!api?.addTranscriptSegment || !meeting || !transcriptText.trim()) return;
    try {
      const segment = await api.addTranscriptSegment({
        sessionId: meeting.id,
        speaker_label: speakerLabel.trim() || undefined,
        transcript_text: transcriptText.trim(),
        confidence: transcriptConfidence,
        occurred_at: new Date().toISOString(),
      });
      setTranscriptSegments((items) => [...items, segment]);
      setTranscriptText('');
    } catch (err: any) {
      setError(err.message || 'Failed to add transcript segment.');
    }
  };

  const handleStopMeeting = async () => {
    if (!api?.stopMeeting || !meeting) return;
    try {
      setMeeting(await api.stopMeeting(meeting.id));
      setStatusMessage('Meeting stopped. Transcript capture is off.');
    } catch (err: any) {
      setError(err.message || 'Failed to stop meeting.');
    }
  };

  const handleGenerateMeetingProposals = async () => {
    if (!api?.generateMeetingProposals || !meeting) return;
    try {
      const result = await api.generateMeetingProposals(meeting.id);
      setMeetingProposals(result?.proposals || []);
      setStatusMessage('Meeting summary and actions are proposals until you approve them.');
    } catch (err: any) {
      setError(err.message || 'Failed to generate meeting proposals.');
    }
  };

  const handleReviewMeetingProposal = async (proposalId: string, decision: 'approve' | 'reject') => {
    if (!api?.reviewMeetingProposal) return;
    try {
      const reviewed = await api.reviewMeetingProposal({ proposalId, decision });
      setMeetingProposals((items) => items.map((item) => item.id === proposalId ? reviewed : item));
      await loadActivities();
    } catch (err: any) {
      setError(err.message || 'Failed to review meeting proposal.');
    }
  };

  const handleListWindows = async () => {
    if (!api?.listWindowSources) return;
    try {
      setWindowSources(await api.listWindowSources());
      setStatusMessage('Select exactly one window. No screenshot has been captured.');
    } catch (err: any) {
      setError(err.message || 'Failed to enumerate windows.');
    }
  };

  const handleApproveWindow = async () => {
    if (!api?.selectWindowSource || !selectedWindowSource) return;
    try {
      const selected = await api.selectWindowSource(selectedWindowSource);
      setApprovedWindowName(selected.name);
      setScreenCapture(null);
      setScreenAnalysis(null);
      setStatusMessage('Window approved. Capture remains on-demand only.');
    } catch (err: any) {
      setError(err.message || 'Window approval failed.');
    }
  };

  const handleCaptureScreen = async () => {
    if (!api?.captureScreenOnce) return;
    setScreenBusy(true);
    try {
      const captured = await api.captureScreenOnce();
      setScreenCapture(captured);
      setScreenAnalysis(null);
      if (captured.blocked) {
        setStatusMessage(captured.reason);
      } else {
        setStatusMessage(`Local OCR complete. ${captured.redaction_count} sensitive text region(s) redacted.`);
      }
    } catch (err: any) {
      setError(err.message || 'Screen capture failed.');
    } finally {
      setScreenBusy(false);
    }
  };

  const handlePauseScreen = async () => {
    await api?.pauseScreenCapture?.();
    setApprovedWindowName('');
    setSelectedWindowSource('');
    setScreenCapture(null);
    setScreenAnalysis(null);
    setStatusMessage('Screen assistance paused and preview cleared.');
  };

  const handleAnalyzeScreen = async () => {
    if (!api?.analyzeScreen || !screenCapture?.image_data_url) return;
    setScreenBusy(true);
    try {
      const analysis = await api.analyzeScreen({
        image_data_url: screenCapture.image_data_url,
        ocr_text: screenCapture.ocr_text || '',
        ocr_confidence: screenCapture.ocr_confidence || 0,
        product_scope: productScope.trim() || undefined,
        issue_type: issueType.trim() || undefined,
      });
      setScreenAnalysis(analysis);
    } catch (err: any) {
      setError(err.message || 'Screen analysis failed.');
    } finally {
      setScreenBusy(false);
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

          {suggestion.lifecycle_status === 'sent' && (
            <div className="form-group" style={{ marginTop: '16px', padding: '12px', background: 'var(--bg-secondary)', borderRadius: '6px' }}>
              <label htmlFor="learning-title-input"><strong>Propose for Knowledge Learning</strong></label>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                This confirmed sent response can be proposed as a learning candidate. It requires authorized human approval before entering retrieval.
              </p>
              <input
                id="learning-title-input"
                type="text"
                placeholder="Candidate Title (e.g. Attendance Policy Resolution)"
                value={learningTitle}
                onChange={(e) => setLearningTitle(e.target.value)}
                style={{ marginBottom: '8px' }}
              />
              <label htmlFor="learning-content-input"><strong>Generalized Knowledge Content</strong></label>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                Review and generalize the sent response. Remove client names, email addresses, account numbers, credentials, and case-specific facts.
              </p>
              <textarea
                id="learning-content-input"
                value={learningContent}
                onChange={(e) => setLearningContent(e.target.value)}
                rows={4}
                style={{ marginBottom: '8px' }}
              />
              <textarea
                placeholder="Notes or rationale (optional)"
                value={learningNotes}
                onChange={(e) => setLearningNotes(e.target.value)}
                rows={2}
                style={{ marginBottom: '8px' }}
              />
              <label style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', marginBottom: '8px' }}>
                <input
                  type="checkbox"
                  checked={learningContentReviewed}
                  onChange={(e) => setLearningContentReviewed(e.target.checked)}
                />
                I reviewed this knowledge content and removed client-specific or sensitive information.
              </label>
              <button
                id="propose-learning-btn"
                className="btn btn-secondary"
                onClick={handleProposeLearningCandidate}
                disabled={!learningTitle.trim() || !learningContent.trim() || !learningContentReviewed}
              >
                Propose Learning Candidate
              </button>
            </div>
          )}
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

      <section className="card" aria-labelledby="meeting-heading">
        <h2 id="meeting-heading">Meeting Copilot</h2>
        <div className="alert alert-info" role="status" style={{ marginBottom: '12px' }}>
          Transcript entry is manual or adapter-supplied. No raw audio or microphone recording is performed.
        </div>
        {!meeting ? (
          <>
            <div className="form-group">
              <label htmlFor="meeting-title">Meeting Title</label>
              <input id="meeting-title" value={meetingTitle} onChange={(e) => setMeetingTitle(e.target.value)} />
            </div>
            <label style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', marginBottom: '12px' }}>
              <input
                aria-label="Participants consented"
                type="checkbox"
                checked={meetingConsent}
                onChange={(e) => setMeetingConsent(e.target.checked)}
              />
              I confirm all participants consented to transcription. Transcript retention: 7 days.
            </label>
            <button className="btn" onClick={handleStartMeeting} disabled={!meetingConsent || !meetingTitle.trim()}>
              Start Meeting Transcript
            </button>
          </>
        ) : (
          <>
            <div className={`alert ${meeting.lifecycle_status === 'active' ? 'alert-warning' : 'alert-info'}`} role="status">
              {meeting.lifecycle_status === 'active' ? '🔴 TRANSCRIPT CAPTURE ACTIVE' : '⏹ TRANSCRIPT CAPTURE STOPPED'} — {meeting.title}
            </div>
            {meeting.lifecycle_status === 'active' && (
              <>
                <div className="action-row">
                  <div className="form-group" style={{ flex: 1 }}>
                    <label htmlFor="speaker-label">Speaker</label>
                    <input id="speaker-label" value={speakerLabel} onChange={(e) => setSpeakerLabel(e.target.value)} />
                  </div>
                  <div className="form-group" style={{ flex: 1 }}>
                    <label htmlFor="transcript-confidence">Confidence</label>
                    <input
                      id="transcript-confidence"
                      type="number"
                      min="0"
                      max="1"
                      step="0.05"
                      value={transcriptConfidence}
                      onChange={(e) => setTranscriptConfidence(Number(e.target.value))}
                    />
                  </div>
                </div>
                <div className="form-group">
                  <label htmlFor="transcript-text">Transcript Segment</label>
                  <textarea id="transcript-text" value={transcriptText} onChange={(e) => setTranscriptText(e.target.value)} />
                </div>
                <div className="action-row">
                  <button className="btn btn-secondary" onClick={handleAddTranscript}>Add Transcript Segment</button>
                  <button className="btn btn-danger" onClick={handleStopMeeting}>Stop Meeting</button>
                </div>
              </>
            )}
            <ul className="activity-list">
              {transcriptSegments.map((segment) => (
                <li key={segment.id} className="activity-item">
                  <span><strong>{segment.speaker_label || 'Unknown speaker'}:</strong> {segment.transcript_text}</span>
                  <span className={segment.uncertainty_visible ? 'tag tag-warning' : 'tag tag-info'}>
                    {Math.round(segment.confidence * 100)}%{segment.uncertainty_visible ? ' — uncertain' : ''}
                  </span>
                </li>
              ))}
            </ul>
            {meeting.lifecycle_status === 'stopped' && meetingProposals.length === 0 && (
              <button className="btn" onClick={handleGenerateMeetingProposals}>Generate Meeting Proposals</button>
            )}
            {meetingProposals.map((proposal) => (
              <div key={proposal.id} className="form-group">
                <label>{proposal.proposal_type.toUpperCase()} — {proposal.lifecycle_status}</label>
                <div style={{ whiteSpace: 'pre-wrap', fontSize: '13px' }}>{proposal.proposal_text}</div>
                {proposal.lifecycle_status === 'proposed' && (
                  <div className="action-row" style={{ marginTop: '8px' }}>
                    <button className="btn btn-success" onClick={() => handleReviewMeetingProposal(proposal.id, 'approve')}>Approve</button>
                    <button className="btn btn-danger" onClick={() => handleReviewMeetingProposal(proposal.id, 'reject')}>Reject</button>
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </section>

      <section className="card" aria-labelledby="screen-heading">
        <h2 id="screen-heading">On-Demand Screen Assistance</h2>
        <div className="alert alert-info" role="status">
          Capture is off by default. Select one window; pause anytime with Ctrl/Cmd+Shift+P. No mouse or keyboard control is available.
        </div>
        <div className="action-row">
          <button className="btn btn-secondary" onClick={handleListWindows}>List Windows</button>
          <button className="btn btn-danger" onClick={handlePauseScreen}>Pause & Clear</button>
        </div>
        {windowSources.length > 0 && (
          <div className="form-group">
            <label htmlFor="window-source">Window to Approve</label>
            <select id="window-source" value={selectedWindowSource} onChange={(e) => setSelectedWindowSource(e.target.value)}>
              <option value="">Choose a window</option>
              {windowSources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}
            </select>
            <button className="btn btn-secondary" onClick={handleApproveWindow} disabled={!selectedWindowSource}>Approve Selected Window</button>
          </div>
        )}
        {approvedWindowName && (
          <>
            <p><strong>Approved window:</strong> {approvedWindowName}</p>
            <button className="btn" onClick={handleCaptureScreen} disabled={screenBusy}>
              {screenBusy ? 'Processing Locally...' : 'Capture One Screenshot & Run Local OCR'}
            </button>
          </>
        )}
        {screenCapture?.blocked && <div className="alert alert-warning">{screenCapture.reason}</div>}
        {screenCapture?.image_data_url && (
          <>
            <img src={screenCapture.image_data_url} alt={`Redacted capture of ${screenCapture.source_name}`} style={{ width: '100%', marginTop: '12px', borderRadius: '6px' }} />
            <p><strong>OCR confidence:</strong> {Math.round((screenCapture.ocr_confidence || 0) * 100)}%</p>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: '11px' }}>{screenCapture.ocr_text}</pre>
            <button className="btn" onClick={handleAnalyzeScreen} disabled={screenBusy}>Analyze Redacted Screenshot</button>
          </>
        )}
        {screenAnalysis && (
          <div className="form-group">
            <label>Proposed Troubleshooting — {screenAnalysis.status}</label>
            <ul>{(screenAnalysis.recommended_steps || []).map((step: string, index: number) => <li key={index}>{step}</li>)}</ul>
            <p><strong>Uncertainty:</strong> {screenAnalysis.uncertainty}</p>
            <p><strong>Evidence:</strong> {(screenAnalysis.sources || []).map((source: any) => `${source.title} v${source.version_number}`).join(', ') || 'None'}</p>
          </div>
        )}
      </section>

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

      {/* 5. Response Learning Candidates */}
      <section className="card" aria-labelledby="learning-heading">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 id="learning-heading">Response Learning Candidates</h2>
          <button className="btn btn-secondary" onClick={handleLoadLearningCandidates}>Refresh Candidates</button>
        </div>
        {learningCandidates.length === 0 ? (
          <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '8px' }}>No learning candidates found.</p>
        ) : (
          <div style={{ marginTop: '12px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {learningCandidates.map((cand) => (
              <div key={cand.id} className="form-group" style={{ borderBottom: '1px solid var(--border-color)', paddingBottom: '10px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <strong>{cand.candidate_title}</strong>
                  <span className={`tag ${cand.lifecycle_status === 'approved' ? 'tag-success' : cand.lifecycle_status === 'rejected' ? 'tag-danger' : 'tag-warning'}`}>
                    {cand.lifecycle_status}
                  </span>
                </div>
                <div style={{ whiteSpace: 'pre-wrap', fontSize: '12px', margin: '6px 0' }}>{cand.candidate_content}</div>
                <small style={{ color: 'var(--text-secondary)' }}>
                  Scope: {cand.product_scope} / {cand.issue_type} | Proposed by: {cand.created_by}
                </small>
                {cand.lifecycle_status === 'proposed' && (
                  <div className="action-row" style={{ marginTop: '8px' }}>
                    <button className="btn btn-success" onClick={() => handleReviewLearningCandidate(cand.id, 'approve')}>Approve</button>
                    <button className="btn btn-danger" onClick={() => handleReviewLearningCandidate(cand.id, 'reject')}>Reject</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 6. Operational Health & Operations */}
      <section className="card" aria-labelledby="health-heading">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 id="health-heading">Operational Health & Operations</h2>
          <button className="btn btn-secondary" onClick={handleFetchHealth} disabled={healthLoading}>
            {healthLoading ? 'Checking...' : 'Check Health'}
          </button>
        </div>
        {healthData && (
          <div style={{ marginTop: '12px', fontSize: '13px' }}>
            <p>
              <strong>Overall Status:</strong>{' '}
              <span className={`tag ${healthData.status === 'healthy' ? 'tag-success' : healthData.status === 'degraded' ? 'tag-warning' : 'tag-danger'}`}>
                {healthData.status.toUpperCase()}
              </span>
            </p>
            <p><strong>Database:</strong> {healthData.database?.connected ? 'Connected' : 'Disconnected'} (Revision: {healthData.database?.schema_revision || 'unknown'}, Integrity: {healthData.database?.integrity_check})</p>
            <p><strong>AI Provider:</strong> Mode: {healthData.ai_provider?.mode} ({healthData.ai_provider?.configured ? 'Configured' : 'Not configured'})</p>
            <p><strong>Last Backup:</strong> {healthData.backup?.last_backup_timestamp || 'No recorded backups'}</p>
            <p><strong>n8n Integration:</strong> {healthData.n8n?.configured ? 'Configured' : 'Not configured'}</p>
            <p style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '8px' }}>
              Operations: Run <code>uv run python -m support_copilot.operations backup</code> to create an online backup. See <code>docs/support-copilot/DISASTER_RECOVERY.md</code> for full recovery procedures.
            </p>
          </div>
        )}
      </section>
    </div>
  );
};
