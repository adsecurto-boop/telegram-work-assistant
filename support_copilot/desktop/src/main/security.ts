export const ALLOWED_IPC_CHANNELS = [
  'copilot:capture-message',
  'copilot:request-suggestion',
  'copilot:copy-suggestion',
  'copilot:reject-suggestion',
  'copilot:confirm-sent',
  'copilot:get-activity',
  'copilot:create-activity',
  'copilot:preview-report',
  'copilot:finalize-report',
  'copilot:create-knowledge',
  'copilot:create-case',
  'copilot:list-cases',
  'copilot:get-active-meeting',
  'copilot:get-meeting',
  'copilot:start-meeting',
  'copilot:add-transcript-segment',
  'copilot:stop-meeting',
  'copilot:generate-meeting-proposals',
  'copilot:review-meeting-proposal',
  'copilot:list-window-sources',
  'copilot:select-window-source',
  'copilot:capture-screen-once',
  'copilot:pause-screen-capture',
  'copilot:analyze-screen',
  'copilot:propose-learning-candidate',
  'copilot:review-learning-candidate',
  'copilot:list-learning-candidates',
  'copilot:get-health-detailed',
] as const;

export interface SecurityConfig {
  contextIsolation: boolean;
  nodeIntegration: boolean;
  sandbox: boolean;
  webSecurity: boolean;
}

export const SECURITY_PREFERENCES: SecurityConfig = {
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
  webSecurity: true,
};
