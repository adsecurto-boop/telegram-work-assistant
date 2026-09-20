import { app, BrowserWindow, desktopCapturer, globalShortcut, ipcMain } from 'electron';
import path from 'path';
import sharp from 'sharp';
import { createWorker } from 'tesseract.js';
import { SECURITY_PREFERENCES } from './security';
import { isSensitiveWindowTitle, redactOcrText, shouldBlockOcrText, shouldRedactWord } from './screen_security';
export { ALLOWED_IPC_CHANNELS, SECURITY_PREFERENCES } from './security';

let mainWindow: BrowserWindow | null = null;
let approvedScreenSource: { id: string; name: string } | null = null;
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
          "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' http://127.0.0.1:8000;",
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

  const meetingRequest = async (pathName: string, method = 'GET', payload?: any) => {
    const res = await fetch(`${CORE_API_URL}${pathName}`, {
      method,
      headers: {
        ...(payload ? { 'Content-Type': 'application/json' } : {}),
        Authorization: `Bearer ${CORE_API_TOKEN}`,
      },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Meeting operation failed');
    }
    return res.json();
  };

  ipcMain.handle('copilot:get-active-meeting', () => meetingRequest('/v1/meetings/active/current'));
  ipcMain.handle('copilot:get-meeting', (_event, sessionId) => meetingRequest(`/v1/meetings/${sessionId}`));
  ipcMain.handle('copilot:start-meeting', (_event, payload) => meetingRequest('/v1/meetings/start', 'POST', payload));
  ipcMain.handle('copilot:add-transcript-segment', (_event, { sessionId, ...payload }) =>
    meetingRequest(`/v1/meetings/${sessionId}/transcript-segments`, 'POST', payload));
  ipcMain.handle('copilot:stop-meeting', (_event, sessionId) =>
    meetingRequest(`/v1/meetings/${sessionId}/stop`, 'POST'));
  ipcMain.handle('copilot:generate-meeting-proposals', (_event, sessionId) =>
    meetingRequest(`/v1/meetings/${sessionId}/proposals`, 'POST'));
  ipcMain.handle('copilot:review-meeting-proposal', (_event, { proposalId, decision }) =>
    meetingRequest(`/v1/meetings/proposals/${proposalId}/review`, 'POST', { decision }));

  ipcMain.handle('copilot:list-window-sources', async () => {
    const sources = await desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 0, height: 0 } });
    return sources
      .filter((source) => source.name !== 'Support Copilot')
      .map((source) => ({ id: source.id, name: source.name }));
  });

  ipcMain.handle('copilot:select-window-source', async (_event, sourceId: string) => {
    const sources = await desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 0, height: 0 } });
    const source = sources.find((item) => item.id === sourceId);
    if (!source) throw new Error('Selected window is no longer available.');
    if (isSensitiveWindowTitle(source.name)) {
      approvedScreenSource = null;
      throw new Error('Password, payment, banking, credential, and login windows cannot be captured.');
    }
    approvedScreenSource = { id: source.id, name: source.name };
    return approvedScreenSource;
  });

  ipcMain.handle('copilot:pause-screen-capture', async () => {
    approvedScreenSource = null;
    return { paused: true };
  });

  ipcMain.handle('copilot:capture-screen-once', async () => {
    if (!approvedScreenSource) throw new Error('Select and approve a window before capture.');
    const sources = await desktopCapturer.getSources({
      types: ['window'],
      thumbnailSize: { width: 1440, height: 900 },
      fetchWindowIcons: false,
    });
    const source = sources.find((item) => item.id === approvedScreenSource?.id);
    if (!source || source.name !== approvedScreenSource.name) {
      approvedScreenSource = null;
      throw new Error('Approved window changed or closed. Select it again.');
    }
    if (isSensitiveWindowTitle(source.name)) {
      approvedScreenSource = null;
      throw new Error('Sensitive window capture was blocked.');
    }
    const imageBuffer = source.thumbnail.toPNG();
    const languageData = require('@tesseract.js-data/eng');
    const worker = await createWorker(languageData.code, 1, {
      langPath: languageData.langPath,
      gzip: languageData.gzip,
      cachePath: path.join(app.getPath('userData'), 'ocr-cache'),
    });
    try {
      const result = await worker.recognize(imageBuffer, {}, { text: true, blocks: true });
      const ocrText = result.data.text || '';
      if (shouldBlockOcrText(ocrText)) {
        return {
          blocked: true,
          reason: 'Sensitive password, payment, banking, or credential content was detected locally.',
          source_name: source.name,
        };
      }
      const words: Array<{ text: string; bbox: { x0: number; y0: number; x1: number; y1: number } }> = [];
      for (const block of result.data.blocks || []) {
        for (const paragraph of block.paragraphs || []) {
          for (const line of paragraph.lines || []) {
            words.push(...(line.words || []));
          }
        }
      }
      const redactionBoxes = words.filter((word) => shouldRedactWord(word.text)).map((word) => word.bbox);
      let safeBuffer = imageBuffer;
      if (redactionBoxes.length > 0) {
        const metadata = await sharp(imageBuffer).metadata();
        const overlay = Buffer.from(
          `<svg width="${metadata.width}" height="${metadata.height}" xmlns="http://www.w3.org/2000/svg">`
          + redactionBoxes.map((box) => `<rect x="${box.x0}" y="${box.y0}" width="${Math.max(1, box.x1 - box.x0)}" height="${Math.max(1, box.y1 - box.y0)}" fill="black"/>`).join('')
          + '</svg>',
        );
        safeBuffer = await sharp(imageBuffer).composite([{ input: overlay }]).png().toBuffer();
      }
      return {
        blocked: false,
        source_name: source.name,
        image_data_url: `data:image/png;base64,${safeBuffer.toString('base64')}`,
        ocr_text: redactOcrText(ocrText),
        ocr_confidence: result.data.confidence / 100,
        redaction_count: redactionBoxes.length,
      };
    } finally {
      await worker.terminate();
    }
  });

  ipcMain.handle('copilot:analyze-screen', async (_event, payload) => {
    const res = await fetch(`${CORE_API_URL}/v1/screen/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CORE_API_TOKEN}` },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Screen analysis failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:propose-learning-candidate', async (_event, { suggestionId, ...payload }) => {
    const res = await fetch(`${CORE_API_URL}/v1/suggestions/${suggestionId}/learning-candidate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CORE_API_TOKEN}` },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Propose learning candidate failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:review-learning-candidate', async (_event, { candidateId, decision }) => {
    const res = await fetch(`${CORE_API_URL}/v1/learning/candidates/${candidateId}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CORE_API_TOKEN}` },
      body: JSON.stringify({ decision }),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Review learning candidate failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:list-learning-candidates', async (_event, status) => {
    const url = status ? `${CORE_API_URL}/v1/learning/candidates?status_filter=${status}` : `${CORE_API_URL}/v1/learning/candidates`;
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${CORE_API_TOKEN}` },
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'List learning candidates failed');
    }
    return res.json();
  });

  ipcMain.handle('copilot:get-health-detailed', async () => {
    const res = await fetch(`${CORE_API_URL}/v1/health/detailed`, {
      headers: { Authorization: `Bearer ${CORE_API_TOKEN}` },
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Get health detailed failed');
    }
    return res.json();
  });
}

if (app) {
  app.whenReady().then(() => {
    registerIpcHandlers();
    createMainWindow();
    globalShortcut.register('CommandOrControl+Shift+P', () => {
      approvedScreenSource = null;
      mainWindow?.webContents.send('copilot:screen-paused');
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('will-quit', () => globalShortcut.unregisterAll());
}
