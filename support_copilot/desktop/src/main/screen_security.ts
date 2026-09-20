export const SENSITIVE_WINDOW_PATTERN = /\b(password|passcode|payment|bank(?:ing)?|checkout|credential|wallet|sign[ -]?in|log[ -]?in)\b/i;
export const SENSITIVE_OCR_PATTERN = /\b(password|passcode|one[ -]?time password|otp|cvv|credit card|debit card|bank account|payment details|private key|seed phrase)\b/i;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/i;
const PHONE_OR_ACCOUNT_PATTERN = /^\+?[\d() -]{8,}$/;
const TOKEN_PATTERN = /^(?:AIza|sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}$/;

export function isSensitiveWindowTitle(title: string): boolean {
  return SENSITIVE_WINDOW_PATTERN.test(title);
}

export function shouldBlockOcrText(text: string): boolean {
  return SENSITIVE_OCR_PATTERN.test(text);
}

export function shouldRedactWord(word: string): boolean {
  const normalized = word.replace(/^[,.;:'"\[]+|[,.;:'"\]]+$/g, '');
  return EMAIL_PATTERN.test(normalized)
    || PHONE_OR_ACCOUNT_PATTERN.test(normalized)
    || TOKEN_PATTERN.test(normalized);
}

export function redactOcrText(text: string): string {
  return text
    .split(/(\s+)/)
    .map((part) => shouldRedactWord(part) ? '[REDACTED]' : part)
    .join('');
}
