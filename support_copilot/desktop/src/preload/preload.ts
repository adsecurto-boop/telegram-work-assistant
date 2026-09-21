import { contextBridge, ipcRenderer } from 'electron';

export interface CopilotAPI {
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
  proposeLearningCandidate: (payload: { suggestionId: string; candidate_title: string; candidate_content: string; content_reviewed_for_sensitive_data: true; target_stable_key?: string; target_article_id?: string; notes?: string }) => Promise<any>;
  reviewLearningCandidate: (payload: { candidateId: string; decision: 'approve' | 'reject' }) => Promise<any>;
  listLearningCandidates: (status?: string) => Promise<any>;
  getHealthDetailed: () => Promise<any>;
}

export const copilotAPI: CopilotAPI = {
  captureMessage: (payload: any) => ipcRenderer.invoke('copilot:capture-message', payload),
  requestSuggestion: (payload) => ipcRenderer.invoke('copilot:request-suggestion', payload),
  copySuggestion: (suggestionId: string) => ipcRenderer.invoke('copilot:copy-suggestion', suggestionId),
  rejectSuggestion: (suggestionId: string) => ipcRenderer.invoke('copilot:reject-suggestion', suggestionId),
  confirmSent: (params) => ipcRenderer.invoke('copilot:confirm-sent', params),
  getActivityToday: () => ipcRenderer.invoke('copilot:get-activity'),
  createActivity: (payload) => ipcRenderer.invoke('copilot:create-activity', payload),
  previewReport: (reportType) => ipcRenderer.invoke('copilot:preview-report', reportType),
  finalizeReport: (params) => ipcRenderer.invoke('copilot:finalize-report', params),
  createKnowledge: (payload) => ipcRenderer.invoke('copilot:create-knowledge', payload),
  createCase: (payload) => ipcRenderer.invoke('copilot:create-case', payload),
  listCases: () => ipcRenderer.invoke('copilot:list-cases'),
  getActiveMeeting: () => ipcRenderer.invoke('copilot:get-active-meeting'),
  getMeeting: (sessionId) => ipcRenderer.invoke('copilot:get-meeting', sessionId),
  startMeeting: (payload) => ipcRenderer.invoke('copilot:start-meeting', payload),
  addTranscriptSegment: (payload) => ipcRenderer.invoke('copilot:add-transcript-segment', payload),
  stopMeeting: (sessionId) => ipcRenderer.invoke('copilot:stop-meeting', sessionId),
  generateMeetingProposals: (sessionId) => ipcRenderer.invoke('copilot:generate-meeting-proposals', sessionId),
  reviewMeetingProposal: (payload) => ipcRenderer.invoke('copilot:review-meeting-proposal', payload),
  listWindowSources: () => ipcRenderer.invoke('copilot:list-window-sources'),
  selectWindowSource: (sourceId) => ipcRenderer.invoke('copilot:select-window-source', sourceId),
  captureScreenOnce: () => ipcRenderer.invoke('copilot:capture-screen-once'),
  pauseScreenCapture: () => ipcRenderer.invoke('copilot:pause-screen-capture'),
  onScreenPaused: (callback) => {
    const listener = () => callback();
    ipcRenderer.on('copilot:screen-paused', listener);
    return () => ipcRenderer.removeListener('copilot:screen-paused', listener);
  },
  analyzeScreen: (payload) => ipcRenderer.invoke('copilot:analyze-screen', payload),
  proposeLearningCandidate: (payload) => ipcRenderer.invoke('copilot:propose-learning-candidate', payload),
  reviewLearningCandidate: (payload) => ipcRenderer.invoke('copilot:review-learning-candidate', payload),
  listLearningCandidates: (status) => ipcRenderer.invoke('copilot:list-learning-candidates', status),
  getHealthDetailed: () => ipcRenderer.invoke('copilot:get-health-detailed'),
};

contextBridge.exposeInMainWorld('copilotAPI', copilotAPI);
