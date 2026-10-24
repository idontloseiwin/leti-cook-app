// Text from the server reaches the page only as a plain sentence. Anything
// that looks like it carries a URL, a credential or a stack trace is replaced
// by fixed wording, and known error codes always use fixed wording.
const UNSAFE_TEXT = /:\/\/|[?&](?:key|token|access_token|signature)=|AIza[0-9A-Za-z_-]{20}|\bBearer\s|Traceback|[A-Za-z0-9+/_-]{40}/i;

const CODE_MESSAGES: Readonly<Record<string, string>> = {
  authentication_required: 'Your session has ended. Sign in again to continue.',
  hot_runtime_not_ready: 'The application is starting or running maintenance. Try again in a moment.',
  provider_access_denied: 'YouTube refused access with the API key, so nothing was sent. See Health for what to do.',
  provider_key_rejected: 'YouTube rejected the API key, so nothing was sent. See Health for what to do.',
  request_too_large: 'The request is too large.',
  setup_required: 'Setup is not finished. Open setup to continue.',
  trash_unavailable: 'Channel removal is unavailable. The change is not confirmed; reload before trying again.',
  unsupported_starter_channel: 'This starter channel is not available.',
  user_work_unavailable: 'Saved work is unavailable right now. Try again.',
  workspace_not_started: 'Workspace preparation has not started.',
};

export function plainServerText(value: unknown, maxLength = 360): string | undefined {
  if (typeof value !== 'string') return undefined;
  const text = value.trim().replace(/\s+/g, ' ');
  if (!text || text.length > maxLength || UNSAFE_TEXT.test(text)) return undefined;
  return text;
}

function statusMessage(status: number): string {
  if (status === 401) return CODE_MESSAGES.authentication_required;
  if (status === 403) return 'This action is not allowed.';
  if (status === 404) return 'This item was not found.';
  if (status === 408) return 'The request timed out. Try again.';
  if (status === 409) return 'This changed on the server. Reload and try again.';
  if (status === 413) return CODE_MESSAGES.request_too_large;
  if (status === 429) return 'Too many requests. Wait a moment and try again.';
  if (status === 503) return 'The service is unavailable right now. Try again in a moment.';
  if (status >= 500) return `The server could not complete the request (HTTP ${status}).`;
  return `The request was not accepted (HTTP ${status}).`;
}

// A coded response or a 4xx answer is written by the backend itself; an
// uncoded 5xx may carry exception text, so it gets only the status wording.
export function serverErrorMessage(status: number, code?: string, text?: unknown): string {
  if (code && Object.hasOwn(CODE_MESSAGES, code)) return CODE_MESSAGES[code];
  const plain = code || status < 500 ? plainServerText(text) : undefined;
  return plain ?? statusMessage(status);
}
