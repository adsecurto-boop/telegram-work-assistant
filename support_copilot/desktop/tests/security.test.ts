import { describe, it, expect } from 'vitest';
import { SECURITY_PREFERENCES, ALLOWED_IPC_CHANNELS } from '../src/main/security';
import { isSensitiveWindowTitle, redactOcrText, shouldBlockOcrText } from '../src/main/screen_security';

describe('Desktop Electron Security Configuration', () => {
  it('enforces context isolation, disables node integration, and enables sandbox', () => {
    expect(SECURITY_PREFERENCES.contextIsolation).toBe(true);
    expect(SECURITY_PREFERENCES.nodeIntegration).toBe(false);
    expect(SECURITY_PREFERENCES.sandbox).toBe(true);
    expect(SECURITY_PREFERENCES.webSecurity).toBe(true);
  });

  it('exposes only narrow allowlisted IPC channels', () => {
    expect(ALLOWED_IPC_CHANNELS).toEqual([
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
    ]);
  });

  it('blocks sensitive windows and locally redacts personal identifiers', () => {
    expect(isSensitiveWindowTitle('Online Banking Login')).toBe(true);
    expect(shouldBlockOcrText('Enter OTP and CVV')).toBe(true);
    expect(redactOcrText('Email person@example.com or call +919876543210')).toContain('[REDACTED]');
    expect(redactOcrText('Email person@example.com or call +919876543210')).not.toContain('person@example.com');
  });

  it('prohibits arbitrary IPC channels', () => {
    const dangerousChannels = [
      'shell:execute',
      'fs:readFile',
      'fs:writeFile',
      'electron:remote',
      'copilot:raw-sql',
      'copilot:send-external',
    ];
    for (const ch of dangerousChannels) {
      expect((ALLOWED_IPC_CHANNELS as readonly string[]).includes(ch)).toBe(false);
    }
  });
});
