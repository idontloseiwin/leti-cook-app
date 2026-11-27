// Agent keys and ready-to-paste MCP client settings. Pure: no requests or storage.
// The key itself exists only in the create response and in memory until the page closes.

export type AgentKey = { id: string; name: string; created_at: number; expires_at: number };
export type AgentKeyList = { keys: AgentKey[]; mcpUrl: string; maxKeys: number };
export type CreatedAgentKey = { key: AgentKey; secret: string };

export const AGENT_CLIENTS = ['Claude Code', 'Codex', 'Cursor', 'VS Code', 'Claude Desktop'] as const;
export type AgentClient = (typeof AGENT_CLIENTS)[number];

export const KEY_PLACEHOLDER = 'YOUR_KEY';
const SECRET = /^lic_ro_[A-Za-z0-9_-]{43}$/;
const KEY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Unexpected agent key response.');
  return value as Record<string, unknown>;
}

function parseKey(value: unknown): AgentKey {
  const row = record(value);
  const { id, name, created_at: created, expires_at: expires } = row;
  if (typeof id !== 'string' || !KEY_ID.test(id) || typeof name !== 'string' || !name
      || !Number.isSafeInteger(created) || !Number.isSafeInteger(expires) || (expires as number) <= (created as number)) {
    throw new Error('Unexpected agent key response.');
  }
  return { id, name, created_at: created as number, expires_at: expires as number };
}

export function parseAgentKeyList(value: unknown): AgentKeyList {
  const body = record(value);
  if (!Array.isArray(body.keys) || typeof body.mcp_url !== 'string' || !/^https?:\/\/[^/]+\/mcp$/.test(body.mcp_url)
      || !Number.isSafeInteger(body.max_keys)) {
    throw new Error('Unexpected agent key response.');
  }
  const endpoint = new URL(body.mcp_url);
  if (endpoint.username || endpoint.password || endpoint.href !== endpoint.origin + '/mcp') {
    throw new Error('Unexpected agent key response.');
  }
  return { keys: body.keys.map(parseKey), mcpUrl: endpoint.href, maxKeys: body.max_keys as number };
}

// The parsed endpoint is an ASCII URL. Cursor handles authorization after installation.
export function cursorInstallUrl(mcpUrl: string): string {
  const link = new URL('cursor://anysphere.cursor-deeplink/mcp/install');
  link.searchParams.set('name', 'leti-cook');
  link.searchParams.set('config', btoa(JSON.stringify({ url: mcpUrl })));
  return link.href;
}

// A program the owner allowed on the Allow page; it holds its own tokens.
export type AgentConnection = {
  id: string; name: string; redirect_host: string; created_at: number; expires_at: number; used_at: number | null;
};

export function parseAgentConnections(value: unknown): AgentConnection[] {
  const body = record(value);
  if (!Array.isArray(body.connections)) throw new Error('Unexpected agent connection response.');
  return body.connections.map((item) => {
    const row = record(item);
    const { id, name, redirect_host: host, created_at: created, expires_at: expires, used_at: used } = row;
    if (typeof id !== 'string' || !KEY_ID.test(id) || typeof name !== 'string' || !name || typeof host !== 'string'
        || !Number.isSafeInteger(created) || !Number.isSafeInteger(expires) || (expires as number) <= (created as number)
        || (used !== null && !Number.isSafeInteger(used))) {
      throw new Error('Unexpected agent connection response.');
    }
    return { id, name, redirect_host: host, created_at: created as number, expires_at: expires as number,
      used_at: used as number | null };
  });
}

export function parseCreatedAgentKey(value: unknown): CreatedAgentKey {
  const body = record(value);
  if (typeof body.secret !== 'string' || !SECRET.test(body.secret)) throw new Error('Unexpected agent key response.');
  return { key: parseKey(body.key), secret: body.secret };
}

export function validKeyName(name: string): boolean {
  const trimmed = name.trim();
  return trimmed.length >= 1 && trimmed.length <= 80 && !/[\u0000-\u001f\u007f]/.test(name);
}

export function validKeyDays(days: number): boolean {
  return Number.isInteger(days) && days >= 1 && days <= 365;
}

export function maskedSecret(secret: string): string {
  return secret.slice(0, 7) + '•'.repeat(Math.max(0, secret.length - 7));
}

export function keyDate(seconds: number): string {
  return new Intl.DateTimeFormat('en', { year: 'numeric', month: 'short', day: 'numeric' }).format(new Date(seconds * 1000));
}

export function daysLeft(expiresAt: number, nowMs = Date.now()): number {
  return Math.max(0, Math.ceil((expiresAt * 1000 - nowMs) / 86_400_000));
}

// The installation origin is everything before /mcp; the bridge takes it as --base-url.
export function installationOrigin(mcpUrl: string): string {
  return mcpUrl.slice(0, -'/mcp'.length);
}

export type ClientSettings = { text: string; where: string };

// One client's settings with the key (or a placeholder). Formats follow each client's
// documented MCP configuration: an HTTP server with an Authorization header, or for
// Claude Desktop, which starts local programs only, the stdio bridge with a key file.
export function clientSettings(client: AgentClient, mcpUrl: string, key = KEY_PLACEHOLDER): ClientSettings {
  const bearer = 'Bearer ' + key;
  const json = (value: unknown) => JSON.stringify(value, null, 2);
  switch (client) {
    case 'Claude Code':
      return {
        text: `claude mcp add --transport http --scope user leti-cook ${mcpUrl} --header "Authorization: ${bearer}"`,
        where: 'Run once in a terminal. --scope user keeps it out of project files, for every project.',
      };
    case 'Codex':
      return {
        text: `[mcp_servers.leti-cook]\nurl = "${mcpUrl}"\nhttp_headers = { "Authorization" = "${bearer}" }`,
        where: 'Add to ~/.codex/config.toml (Windows: %USERPROFILE%\\.codex\\config.toml).',
      };
    case 'Cursor':
      return {
        text: json({ mcpServers: { 'leti-cook': { url: mcpUrl, headers: { Authorization: bearer } } } }),
        where: 'Add to ~/.cursor/mcp.json, not a project folder: a committed file would publish the key.',
      };
    case 'VS Code':
      return {
        text: json({ servers: { 'leti-cook': { type: 'http', url: mcpUrl, headers: { Authorization: bearer } } } }),
        where: 'Run “MCP: Open User Configuration” and add the server there, not to a project’s .vscode folder.',
      };
    case 'Claude Desktop':
      return {
        text: json({ mcpServers: { 'leti-cook': { command: 'python3', args: [
          '-I', '-B', '/path/to/leti-cook/integrations/mcp/server.py',
          '--base-url', installationOrigin(mcpUrl), '--token-file', '/private/path/leti-cook-agent.key',
        ] } } }),
        where: 'Claude Desktop starts local programs only. Save the key alone in a private file, replace both paths, '
          + 'and use Python 3.12 or newer (Windows: "py" with "-3.12" first). The bridge needs no packages.',
      };
  }
}

// Settings without a key: the program signs in and the owner clicks Allow on this
// installation's page. Claude Desktop's local bridge cannot sign in; it uses a key file.
export function signInSettings(client: AgentClient, mcpUrl: string): ClientSettings | null {
  const json = (value: unknown) => JSON.stringify(value, null, 2);
  switch (client) {
    case 'Claude Code':
      return {
        text: `claude mcp add --transport http --scope user leti-cook ${mcpUrl}`,
        where: 'Run once in a terminal, then in Claude Code type /mcp, choose leti-cook and Authenticate. '
          + 'Your browser opens this installation: click Allow.',
      };
    case 'Codex':
      return {
        text: `[mcp_servers.leti-cook]\nurl = "${mcpUrl}"`,
        where: 'Add to ~/.codex/config.toml, then run: codex mcp login leti-cook. Click Allow in the browser.',
      };
    case 'Cursor':
      return {
        text: json({ mcpServers: { 'leti-cook': { url: mcpUrl } } }),
        where: 'Add to ~/.cursor/mcp.json. Cursor shows a sign-in button for the server: click Allow in the browser.',
      };
    case 'VS Code':
      return {
        text: json({ servers: { 'leti-cook': { type: 'http', url: mcpUrl } } }),
        where: 'Run “MCP: Open User Configuration” and add the server. When VS Code asks to authenticate, click Allow in the browser.',
      };
    case 'Claude Desktop':
      return null;
  }
}

/** A section whose list could not be read shows this instead of an empty list. */
export const LIST_NOT_READ = 'Could not be read. Refresh to try again.';

/** Key creation without a trustworthy answer: the key may exist, so the page reads the list again. */
export const KEY_CREATE_TEXT = {
  unknown: 'No answer from the server, so it is not known whether the key was created. The list is refreshed; if a key you do not need appears, revoke it.',
  unreadable: 'The key was created, but the answer could not be read, so the key cannot be shown. The list is refreshed: revoke that key and create another.',
} as const;

/** No answer reached the page: the connection failed or the page stopped waiting (requestJson's errors). */
export function keyCreateOutcomeUnknown(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const { name, code } = error as { name?: unknown; code?: unknown };
  return name === 'ExplorerNetworkError' || code === 'client_timeout';
}

/** `maxKeys`: the limit the server gave with the key list; without a list read the text names no number. */
export function keyErrorMessage(code: string | undefined, fallback: string, maxKeys?: number): string {
  switch (code) {
    case 'agent_key_limit': return maxKeys === undefined ? 'This installation already has as many keys as it allows. Revoke one first.'
      : `This installation already has ${maxKeys} keys, the most it allows. Revoke one first.`;
    case 'invalid_agent_key_options': return 'Use a name of 1–80 characters and a lifetime of 1–365 days.';
    case 'owner_access_not_configured': return 'Finish owner access setup before creating agent keys.';
    case 'agent_store_unavailable': return 'The agent key store is unavailable. See “Connect an agent through MCP” in the documentation.';
    case 'owner_access_required': return 'Only the owner can manage agent keys.';
    default: return fallback;
  }
}
