import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { App } from '../src/renderer/src/App';

describe('Desktop Overlay App Component', () => {
  const mockCopilotAPI = {
    captureMessage: vi.fn(),
    requestSuggestion: vi.fn(),
    copySuggestion: vi.fn(),
    rejectSuggestion: vi.fn(),
    confirmSent: vi.fn(),
    getActivityToday: vi.fn(),
    createActivity: vi.fn(),
    previewReport: vi.fn(),
    finalizeReport: vi.fn(),
    createKnowledge: vi.fn(),
    createCase: vi.fn(),
    listCases: vi.fn(),
    getActiveMeeting: vi.fn(),
    getMeeting: vi.fn(),
    startMeeting: vi.fn(),
    addTranscriptSegment: vi.fn(),
    stopMeeting: vi.fn(),
    generateMeetingProposals: vi.fn(),
    reviewMeetingProposal: vi.fn(),
    listWindowSources: vi.fn(),
    selectWindowSource: vi.fn(),
    captureScreenOnce: vi.fn(),
    pauseScreenCapture: vi.fn(),
    onScreenPaused: vi.fn(),
    analyzeScreen: vi.fn(),
    proposeLearningCandidate: vi.fn(),
    reviewLearningCandidate: vi.fn(),
    listLearningCandidates: vi.fn(),
    getHealthDetailed: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    window.copilotAPI = mockCopilotAPI;
    mockCopilotAPI.getActivityToday.mockResolvedValue({ events: [] });
    mockCopilotAPI.listCases.mockResolvedValue({ cases: [] });
    mockCopilotAPI.getActiveMeeting.mockResolvedValue(null);
    mockCopilotAPI.onScreenPaused.mockReturnValue(vi.fn());
    mockCopilotAPI.listLearningCandidates.mockResolvedValue({ candidates: [] });
    mockCopilotAPI.getHealthDetailed.mockResolvedValue({
      status: 'healthy',
      api_status: 'healthy',
      database: { connected: true, schema_revision: '005_phase7', integrity_check: 'ok' },
      ai_provider: { mode: 'gemini', configured: true },
      backup: { last_backup_timestamp: '2026-09-21T01:00:00Z' },
      n8n: { configured: true },
    });
    // Mock clipboard
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  it('renders visible local-only banner stating application does not send externally', () => {
    render(<App />);
    expect(
      screen.getByText(/Local only — no external delivery/i)
    ).toBeInTheDocument();
  });

  it('displays loading state during suggestion generation', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 41 });
    mockCopilotAPI.requestSuggestion.mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 100))
    );

    render(<App />);
    const input = screen.getByLabelText(/Received Client Text/i);
    fireEvent.change(input, { target: { value: 'Why are attendance logs missing?' } });

    const btn = screen.getByRole('button', { name: /Generate Suggestion/i });
    fireEvent.click(btn);

    expect(screen.getByText(/Generating Grounded Suggestion.../i)).toBeInTheDocument();
  });

  it('renders grounded suggestion with draft, sources, and missing facts', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 42 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'suggested',
      suggestion: {
        id: 'sugg-1',
        captured_event_id: 1,
        resolved_case_id: 'case-1',
        lifecycle_status: 'suggested',
        draft: 'Please provide attendance logs and date range.',
        missing_facts: ['date_range_specified'],
        assumptions: [],
        confidence: 0.95,
        recommended_action: 'reply',
        sources: [
          {
            article_id: 'art-att',
            article_version_id: 'ver-1',
            version_number: 1,
            retrieval_rank: 1,
            retrieval_score: 1.0,
          },
        ],
        correlation_id: 'corr-1',
      },
    });

    render(<App />);
    const input = screen.getByLabelText(/Received Client Text/i);
    fireEvent.change(input, { target: { value: 'Missing attendance logs' } });

    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByText(/2. Grounded Suggestion/i)).toBeInTheDocument();
    });

    expect(mockCopilotAPI.requestSuggestion).toHaveBeenCalledWith({
      captured_event_id: 42,
      case_id: undefined,
      product_scope: 'core',
      issue_type: 'missing_logs',
    });

    // Check draft in editable textarea
    const textarea = screen.getByLabelText(/Editable Final Response/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe('Please provide attendance logs and date range.');

    // Check missing facts
    expect(screen.getByText(/date_range_specified/i)).toBeInTheDocument();

    // Check source citation
    expect(screen.getByText(/art-att/i)).toBeInTheDocument();
  });

  it('copy updates status to copied but does not send', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 43 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'suggested',
      suggestion: {
        id: 'sugg-copy',
        captured_event_id: 1,
        resolved_case_id: null,
        lifecycle_status: 'suggested',
        draft: 'Draft to copy',
        missing_facts: [],
        assumptions: [],
        confidence: 0.9,
        recommended_action: 'reply',
        sources: [],
        correlation_id: 'corr-copy',
      },
    });
    mockCopilotAPI.copySuggestion.mockResolvedValue({ lifecycle_status: 'copied' });

    render(<App />);
    fireEvent.change(screen.getByLabelText(/Received Client Text/i), {
      target: { value: 'Query to copy' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Copy Draft/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Copy Draft/i }));

    await waitFor(() => {
      expect(mockCopilotAPI.copySuggestion).toHaveBeenCalledWith('sugg-copy');
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('Draft to copy');
      expect(mockCopilotAPI.confirmSent).not.toHaveBeenCalled();
      expect(screen.getByText(/Draft copied to clipboard/i)).toBeInTheDocument();
    });
  });

  it('confirm-sent records explicit human confirmation locally', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 44 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'suggested',
      suggestion: {
        id: 'sugg-confirm',
        captured_event_id: 1,
        resolved_case_id: null,
        lifecycle_status: 'suggested',
        draft: 'Original draft',
        missing_facts: [],
        assumptions: [],
        confidence: 0.9,
        recommended_action: 'reply',
        sources: [],
        correlation_id: 'corr-confirm',
      },
    });
    mockCopilotAPI.confirmSent.mockResolvedValue({ id: 'sent-1' });

    render(<App />);
    fireEvent.change(screen.getByLabelText(/Received Client Text/i), {
      target: { value: 'Inquiry' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByLabelText(/Editable Final Response/i)).toBeInTheDocument();
    });

    // Human edits text
    const textarea = screen.getByLabelText(/Editable Final Response/i);
    fireEvent.change(textarea, { target: { value: 'Edited final text that human actually sent.' } });

    fireEvent.click(screen.getByRole('button', { name: /I Sent This Exact Response/i }));

    await waitFor(() => {
      expect(mockCopilotAPI.confirmSent).toHaveBeenCalledWith({
        suggestionId: 'sugg-confirm',
        exactSentText: 'Edited final text that human actually sent.',
        idempotencyKey: 'send-confirm-sugg-confirm',
      });
      expect(screen.getByText(/Response successfully confirmed as sent locally/i)).toBeInTheDocument();
    });
  });

  it('renders ambiguous case state when case reference is ambiguous', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 45 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'resolution_required',
      candidates: [
        { id: 'c1', case_number: 'CASE-01', title: 'First Issue', status: 'open', client_identifier: 'client-1' },
        { id: 'c2', case_number: 'CASE-02', title: 'Second Issue', status: 'open', client_identifier: 'client-1' },
      ],
    });

    render(<App />);
    fireEvent.change(screen.getByLabelText(/Received Client Text/i), {
      target: { value: 'Status of my case?' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByText(/Select Matching Case/i)).toBeInTheDocument();
      expect(screen.getByText(/CASE-01/i)).toBeInTheDocument();
      expect(screen.getByText(/CASE-02/i)).toBeInTheDocument();
    });
  });

  it('handles provider timeout safely', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 46 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'provider_timeout',
      message: 'AI provider request timed out.',
    });

    render(<App />);
    fireEvent.change(screen.getByLabelText(/Received Client Text/i), {
      target: { value: 'Query' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByText(/AI generation timed out/i)).toBeInTheDocument();
    });
  });

  it('handles knowledge unavailable safely', async () => {
    mockCopilotAPI.captureMessage.mockResolvedValue({ status: 'accepted', captured_event_id: 47 });
    mockCopilotAPI.requestSuggestion.mockResolvedValue({
      status: 'knowledge_unavailable',
    });

    render(<App />);
    fireEvent.change(screen.getByLabelText(/Received Client Text/i), {
      target: { value: 'Query' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Generate Suggestion/i }));

    await waitFor(() => {
      expect(screen.getByText(/No approved knowledge found/i)).toBeInTheDocument();
    });
  });

  it('verifies absence of secret tokens from renderer state and DOM', () => {
    const { container } = render(<App />);
    expect(container.innerHTML).not.toContain('token-');
    expect(container.innerHTML).not.toContain('sqlite:///');
    expect(container.innerHTML).not.toContain('COPILOT_API_TOKENS');
  });

  it('previews a truthful EOD report from verified work', async () => {
    mockCopilotAPI.previewReport.mockResolvedValue({
      id: 'report-1',
      report_type: 'eod',
      report_date: '2026-09-21',
      lifecycle_status: 'preview',
      facts_hash: 'a'.repeat(64),
      content: {
        summary: { verified_work_items: 1, non_completion_events_excluded: 2 },
        work_by_case: {
          unassigned: [{ event_type: 'support.completed', subject_id: 'support-1', details: { summary: 'Resolved login issue' } }],
        },
      },
    });
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /Preview EOD/i }));
    await waitFor(() => expect(screen.getByText(/EOD Report/i)).toBeInTheDocument());
    expect(screen.getByText(/Resolved login issue/i)).toBeInTheDocument();
    expect(screen.getByText(/Draft\/copied events excluded:/i)).toBeInTheDocument();
  });

  it('requires consent and shows visible active meeting state', async () => {
    mockCopilotAPI.startMeeting.mockResolvedValue({
      id: 'meeting-1',
      title: 'Client support meeting',
      lifecycle_status: 'active',
      retention_until: '2026-09-28T00:00:00Z',
      started_at: '2026-09-21T00:00:00Z',
    });
    render(<App />);
    const startButton = screen.getByRole('button', { name: /Start Meeting Transcript/i });
    expect(startButton).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Participants consented/i));
    fireEvent.click(startButton);
    await waitFor(() => expect(screen.getByText(/TRANSCRIPT CAPTURE ACTIVE/i)).toBeInTheDocument());
    expect(mockCopilotAPI.startMeeting).toHaveBeenCalledWith(expect.objectContaining({ consent_acknowledged: true }));
  });

  it('shows transcript uncertainty returned by the adapter', async () => {
    mockCopilotAPI.getActiveMeeting.mockResolvedValue({
      id: 'meeting-active',
      title: 'Incident call',
      lifecycle_status: 'active',
      retention_until: '2026-09-28T00:00:00Z',
      started_at: '2026-09-21T00:00:00Z',
    });
    mockCopilotAPI.getMeeting.mockResolvedValue({
      transcript_segments: [{
        id: 'segment-1', speaker_label: 'Client', transcript_text: 'Maybe it is fixed',
        confidence: 0.55, uncertainty_visible: true,
      }],
      proposals: [],
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText(/55% — uncertain/i)).toBeInTheDocument());
  });

  it('requires explicit window selection before on-demand screen capture', async () => {
    mockCopilotAPI.listWindowSources.mockResolvedValue([{ id: 'window-1', name: 'Support Portal' }]);
    mockCopilotAPI.selectWindowSource.mockResolvedValue({ id: 'window-1', name: 'Support Portal' });
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /List Windows/i }));
    await waitFor(() => expect(screen.getByText('Support Portal')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Capture One Screenshot/i })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Window to Approve/i), { target: { value: 'window-1' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve Selected Window/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Capture One Screenshot/i })).toBeInTheDocument());
    expect(mockCopilotAPI.captureScreenOnce).not.toHaveBeenCalled();
  });

  it('renders learning candidate and operational health sections', async () => {
    mockCopilotAPI.listLearningCandidates.mockResolvedValue({
      candidates: [
        {
          id: 'cand-1',
          candidate_title: 'Attendance Policy Note',
          candidate_content: 'Check date range and logs.',
          product_scope: 'core',
          issue_type: 'missing_logs',
          created_by: 'agent-1',
          lifecycle_status: 'proposed',
        },
      ],
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText('Response Learning Candidates')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('Attendance Policy Note')).toBeInTheDocument());
    expect(screen.getByText('Operational Health & Operations')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/HEALTHY/i)).toBeInTheDocument());
  });

  it('allows approving and rejecting learning candidates', async () => {
    mockCopilotAPI.listLearningCandidates.mockResolvedValue({
      candidates: [
        {
          id: 'cand-1',
          candidate_title: 'Attendance Policy Note',
          candidate_content: 'Check date range and logs.',
          product_scope: 'core',
          issue_type: 'missing_logs',
          created_by: 'agent-1',
          lifecycle_status: 'proposed',
        },
      ],
    });
    mockCopilotAPI.reviewLearningCandidate.mockResolvedValue({
      id: 'cand-1',
      lifecycle_status: 'approved',
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText('Attendance Policy Note')).toBeInTheDocument());
    const approveBtn = screen.getByRole('button', { name: /^Approve$/i });
    fireEvent.click(approveBtn);
    expect(mockCopilotAPI.reviewLearningCandidate).toHaveBeenCalledWith({
      candidateId: 'cand-1',
      decision: 'approve',
    });
  });
});
