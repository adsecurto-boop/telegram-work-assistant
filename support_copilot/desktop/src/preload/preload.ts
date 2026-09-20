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
};

contextBridge.exposeInMainWorld('copilotAPI', copilotAPI);
