import http from 'node:http';
import fs from 'node:fs';
import { spawn, execSync, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const WORKSPACE_SCOPES = [
  'https://www.googleapis.com/auth/drive',
  'https://www.googleapis.com/auth/drive.file',
  'https://www.googleapis.com/auth/drive.readonly',
  'https://www.googleapis.com/auth/spreadsheets',
  'https://www.googleapis.com/auth/spreadsheets.readonly',
  'https://www.googleapis.com/auth/tasks',
  'https://www.googleapis.com/auth/tasks.readonly',
  'https://www.googleapis.com/auth/documents',
  'https://www.googleapis.com/auth/documents.readonly'
];

const WORKSPACE_API_ENDPOINTS = {
  drive: 'https://www.googleapis.com/drive/v3',
  sheets: 'https://sheets.googleapis.com/v4',
  tasks: 'https://tasks.googleapis.com/tasks/v1',
  docs: 'https://docs.googleapis.com/v1',
  keepNotice: 'Google Keep API requires Google Workspace enterprise domain authorization.'
};

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = 3000;
const PYTHON_PORT = 8765;
const PYTHON_HOST = '127.0.0.1';
const AI_STUDIO_PREVIEW = process.env.AI_STUDIO_PREVIEW === 'true';

let dashboardToken = '';
let pythonProcess: ChildProcess | null = null;
let isPythonReady = false;

function getDbToken(): string {
  try {
    const out = execSync(
      `python3 -c "from database import Database; import config; db = Database(config.DB_PATH); print(db.get_setting('dashboard_token') or '')"`,
      { encoding: 'utf-8', timeout: 3000 }
    ).trim();
    return out;
  } catch (e) {
    return '';
  }
}

function checkPortOpen(port: number, host: string): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: host,
        port: port,
        path: '/',
        method: 'GET',
        timeout: 1000
      },
      (res) => {
        resolve(true);
      }
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.end();
  });
}

async function ensurePythonDashboard() {
  const open = await checkPortOpen(PYTHON_PORT, PYTHON_HOST);
  if (open) {
    console.log(`[server] Port ${PYTHON_PORT} is already open, reusing existing service`);
    isPythonReady = true;
    if (AI_STUDIO_PREVIEW && !dashboardToken) {
      dashboardToken = getDbToken();
    }
    return;
  }

  console.log('[server] Starting Python dashboard on port', PYTHON_PORT);
  pythonProcess = spawn('python3', ['dashboard.py', '--host', PYTHON_HOST, '--port', String(PYTHON_PORT)], {
    env: { ...process.env, ALLOW_IFRAME: AI_STUDIO_PREVIEW ? 'true' : 'false' },
    stdio: ['ignore', 'pipe', 'pipe']
  });

  pythonProcess.stdout?.on('data', (data: Buffer) => {
    const text = data.toString();
    process.stdout.write(`[python] ${text}`);
    const match = text.match(/DASHBOARD_TOKEN=([^\s]+)/);
    if (match) {
      if (AI_STUDIO_PREVIEW) dashboardToken = match[1];
      isPythonReady = true;
    }
    if (text.includes('Dashboard running on')) {
      isPythonReady = true;
    }
  });

  pythonProcess.stderr?.on('data', (data: Buffer) => {
    process.stderr.write(`[python err] ${data.toString()}`);
  });

  pythonProcess.on('exit', (code, signal) => {
    console.log(`[server] Python dashboard exited with code ${code}, signal ${signal}`);
    isPythonReady = false;
    setTimeout(ensurePythonDashboard, 2000);
  });

  // Give it a moment to boot
  for (let i = 0; i < 15; i++) {
    await new Promise((r) => setTimeout(r, 200));
    if (await checkPortOpen(PYTHON_PORT, PYTHON_HOST)) {
      isPythonReady = true;
      if (AI_STUDIO_PREVIEW && !dashboardToken) {
        dashboardToken = getDbToken();
      }
      break;
    }
  }
}

// Initial launch
ensurePythonDashboard();

// Periodic health monitor
setInterval(async () => {
  const open = await checkPortOpen(PYTHON_PORT, PYTHON_HOST);
  if (!open && !pythonProcess) {
    console.log('[server] Python dashboard unreachable, restarting...');
    ensurePythonDashboard();
  }
}, 5000);

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost:3000'}`);

  if (url.pathname === '/firebase-applet-config.json') {
    try {
      const configPath = path.join(__dirname, 'firebase-applet-config.json');
      if (fs.existsSync(configPath)) {
        const configData = fs.readFileSync(configPath, 'utf-8');
        res.writeHead(200, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store'
        });
        res.end(configData);
        return;
      }
    } catch (err: any) {
      console.error('[server] Error serving firebase config:', err.message);
    }
  }

  if (url.pathname === '/api/workspace-scopes') {
    res.writeHead(200, {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store'
    });
    res.end(JSON.stringify({ scopes: WORKSPACE_SCOPES, endpoints: WORKSPACE_API_ENDPOINTS }));
    return;
  }

  if (AI_STUDIO_PREVIEW && !dashboardToken) {
    dashboardToken = getDbToken();
  }

  // If backend not ready yet, wait briefly for Python process to be responsive
  if (!isPythonReady) {
    for (let i = 0; i < 15; i++) {
      const open = await checkPortOpen(PYTHON_PORT, PYTHON_HOST);
      if (open) {
        isPythonReady = true;
        if (AI_STUDIO_PREVIEW && !dashboardToken) {
          dashboardToken = getDbToken();
        }
        break;
      }
      await new Promise((r) => setTimeout(r, 200));
    }

    if (!isPythonReady) {
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store'
      });
      res.end(`<!doctype html>
<html>
<head><meta charset="utf-8"><title>Personal Work Assistant</title>
<meta http-equiv="refresh" content="1">
<style>body{font:15px system-ui,-apple-system,sans-serif;padding:40px;background:#f4f6f8;color:#17212b;text-align:center;}</style>
</head>
<body>
  <h2>Loading Personal Work Assistant...</h2>
  <p>Connecting to operations hub services. This will refresh automatically.</p>
</body>
</html>`);
      return;
    }
  }

  // Forward request to python dashboard
  const proxyHeaders = { ...req.headers };
  proxyHeaders.host = `${PYTHON_HOST}:${PYTHON_PORT}`;

  const cookies = req.headers.cookie || '';
  if (AI_STUDIO_PREVIEW && dashboardToken) {
    proxyHeaders['x-dashboard-token'] = dashboardToken;
  }

  const proxyReq = http.request(
    {
      hostname: PYTHON_HOST,
      port: PYTHON_PORT,
      path: req.url,
      method: req.method,
      headers: proxyHeaders
    },
    (proxyRes) => {
      const responseHeaders = { ...proxyRes.headers };

      if (AI_STUDIO_PREVIEW) {
      // Explicit preview-only iframe transforms.
      delete responseHeaders['x-frame-options'];

      if (responseHeaders['content-security-policy']) {
        const csp = String(responseHeaders['content-security-policy']);
        responseHeaders['content-security-policy'] = csp.replace(/frame-ancestors\s+[^;]+(;|$)/gi, '');
      }

      // Ensure cookies work in cross-site preview iframe
      if (responseHeaders['set-cookie']) {
        const cookiesList = Array.isArray(responseHeaders['set-cookie'])
          ? responseHeaders['set-cookie']
          : [responseHeaders['set-cookie']];
        responseHeaders['set-cookie'] = cookiesList.map((c) =>
          c.replace(/SameSite=Strict/i, 'SameSite=None; Secure')
        );
      }
      }

      res.writeHead(proxyRes.statusCode || 200, responseHeaders);
      proxyRes.pipe(res);
    }
  );

  proxyReq.on('error', (err) => {
    console.error('[server proxy err]', err.message);
    if (!res.headersSent) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end('<h3>Connecting to Operations Hub service</h3><p>Please wait a moment while the dashboard initializes...</p><meta http-equiv="refresh" content="2">');
    }
  });

  req.pipe(proxyReq);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[server] Operations Hub server listening on http://127.0.0.1:${PORT}`);
});

function handleShutdown() {
  console.log('[server] Shutting down gracefully...');
  server.close();
  if (pythonProcess) {
    pythonProcess.kill('SIGTERM');
  }
  process.exit(0);
}

process.on('SIGINT', handleShutdown);
process.on('SIGTERM', handleShutdown);
