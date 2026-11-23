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

// The page's own words when a request got no usable answer: never the
// browser's "Failed to fetch" or a JSON parser's message. A change adds that its result
// is unknown, because the server may have done it before the answer was lost.
export const TRANSPORT_TEXT = {
  noConnection: 'No connection to the Leti Cook server.',
  unreadable: 'The server answer could not be read.',
  notConfirmed: 'The result is not confirmed; refresh status before retrying.',
} as const;

function transportError(text: string, change: boolean, name: string, cause: unknown): Error {
  const error = new Error(change ? `${text} ${TRANSPORT_TEXT.notConfirmed}` : text, { cause });
  error.name = name;
  return error;
}

/**
 * For a separate fetch wrapper: fetch() or a read of the body rejected. A TypeError means no
 * answer arrived; it becomes "No connection to the Leti Cook server." (named like requestJson's
 * ExplorerNetworkError). A cancel and any other error pass unchanged.
 */
export function noAnswerError(error: unknown, change = false): unknown {
  if (!(error instanceof TypeError) || error.name === 'AbortError') return error;
  return transportError(TRANSPORT_TEXT.noConnection, change, 'ExplorerNetworkError', error);
}

/** An answer that is not readable UTF-8 JSON, such as a proxy's HTML page. */
export function unreadableAnswerError(cause: unknown, change = false): Error {
  return transportError(TRANSPORT_TEXT.unreadable, change, 'UnreadableAnswer', cause);
}

/** response.json() rejected: a cut connection (TypeError) or an unreadable body (SyntaxError); a cancel passes. */
export function answerReadError(error: unknown, change = false): unknown {
  return error instanceof SyntaxError ? unreadableAnswerError(error, change) : noAnswerError(error, change);
}

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
