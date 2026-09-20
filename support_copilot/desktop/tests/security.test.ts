import { describe, it, expect } from 'vitest';
import { SECURITY_PREFERENCES, ALLOWED_IPC_CHANNELS } from '../src/main/main';

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
    ]);
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
