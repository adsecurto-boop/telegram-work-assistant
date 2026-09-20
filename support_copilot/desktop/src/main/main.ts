import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'path';

// Allowlisted IPC channels
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

let mainWindow: BrowserWindow | null = null;
const CORE_API_URL = process.env.COPILOT_CORE_API_URL || 'http://127.0.0.1:8000';
const CORE_API_TOKEN = process.env.COPILOT_DESKTOP_API_TOKEN || '';

export function createMainWindow(): BrowserWindow {
  mainWindow = new BrowserWindow({
    width: 480,
    height: 720,
    alwaysOnTop: true,
    resizable: true,
    frame: true,
    webPreferences: {
      ...SECURITY_PREFERENCES,
      preload: path.join(__dirname, '../preload/preload.js'),
    },
  });

  // Strict CSP
  mainWindow.webContents.session.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8000;",
        ],
      },
    });
  });

  // Block any non-loopback navigation
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('http://127.0.0.1') && !url.startsWith('http://localhost') && !url.startsWith('file://')) {
      event.preventDefault();
    }
  });

  // Block opening new windows
  mainWindow.webContents.setWindowOpenHandler(() => {
    return { action: 'deny' };
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));
  }

  return mainWindow;
}

// IPC Handlers - Bridge between Renderer and Local Loopback Core API
// The renderer NEVER receives the API token directly.
export function registerIpcHandlers(): void {
  ipcMain.handle('copilot:capture-message', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/captures/manual-message`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify(payload),
    });
    return res.json();
  });

  ipcMain.handle('copilot:request-suggestion', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/suggestions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify(payload),
    });
    return res.json();
  });

  ipcMain.handle('copilot:copy-suggestion', async (_event, suggestionId: string) => {
    const res = await fetch(`${CORE_API_URL}/v1/suggestions/${suggestionId}/copy`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
    });
    return res.json();
  });

  ipcMain.handle('copilot:reject-suggestion', async (_event, suggestionId: string) => {
    const res = await fetch(`${CORE_API_URL}/v1/suggestions/${suggestionId}/reject`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
    });
    return res.json();
  });

  ipcMain.handle('copilot:confirm-sent', async (_event, { suggestionId, exactSentText, idempotencyKey }) => {
    const res = await fetch(`${CORE_API_URL}/v1/suggestions/${suggestionId}/confirm-sent`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify({
        exact_sent_text: exactSentText,
        idempotency_key: idempotencyKey,
      }),
    });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.detail || 'Confirmation failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:get-activity', async () => {
    const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata';
    const res = await fetch(`${CORE_API_URL}/v1/activity/today?timezone_name=${encodeURIComponent(timezoneName)}`, {
      headers: {
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
    });
    return res.json();
  });

  ipcMain.handle('copilot:create-activity', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/activity/events`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.detail || 'Activity recording failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:preview-report', async (_event, reportType: 'tod' | 'eod') => {
    const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata';
    const res = await fetch(`${CORE_API_URL}/v1/reports/${reportType}/preview`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify({ timezone_name: timezoneName }),
    });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.detail || 'Report preview failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:finalize-report', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/reports/${payload.reportType}/finalize`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      body: JSON.stringify({
        preview_id: payload.previewId,
        expected_facts_hash: payload.expectedFactsHash,
      }),
    });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.detail || 'Report finalization failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:create-knowledge', async (_event, payload) => {
    const createResponse = await fetch(`${CORE_API_URL}/v1/knowledge/articles`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CORE_API_TOKEN}` },
      body: JSON.stringify(payload),
    });
    if (!createResponse.ok) {
      const error = await createResponse.json();
      throw new Error(error.detail || 'Knowledge article creation failed');
    }
    const article = await createResponse.json();
    const approveResponse = await fetch(
      `${CORE_API_URL}/v1/knowledge/articles/${article.id}/versions/1/approve`,
      { method: 'POST', headers: { Authorization: `Bearer ${CORE_API_TOKEN}` } },
    );
    if (!approveResponse.ok) {
      const error = await approveResponse.json();
      throw new Error(error.detail || 'Knowledge article approval failed');
    }
    return { article, version: await approveResponse.json() };
  });

  ipcMain.handle('copilot:create-case', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/cases`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CORE_API_TOKEN}` },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Case creation failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:list-cases', async () => {
    const res = await fetch(`${CORE_API_URL}/v1/cases?status_filter=open`, {
      headers: { Authorization: `Bearer ${CORE_API_TOKEN}` },
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Case loading failed');
    }
    return res.json();
  });
}

if (app) {
  app.whenReady().then(() => {
    registerIpcHandlers();
    createMainWindow();
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });
}
