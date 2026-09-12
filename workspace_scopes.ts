/**
 * Configured OAuth Scopes for Google Workspace APIs
 * Google Sheets, Google Tasks, Google Drive, and Google Docs
 */
export const WORKSPACE_SCOPES = [
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

export const WORKSPACE_API_ENDPOINTS = {
  drive: 'https://www.googleapis.com/drive/v3',
  sheets: 'https://sheets.googleapis.com/v4',
  tasks: 'https://tasks.googleapis.com/tasks/v1',
  docs: 'https://docs.googleapis.com/v1',
  keepNotice: 'Google Keep API requires Google Workspace enterprise domain authorization.'
};
