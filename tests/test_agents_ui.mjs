// The Agents page model: strict parsing of the key list and the one-time create
// response, input bounds, masking, and each client's ready settings with the key
// or a placeholder. Offline: no browser, server or network.
import assert from 'node:assert/strict';
import * as agents from '../application/frontend/src/explorer/agentsModel.ts';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import { reactHookHost, flush } from './react_hook_host.mjs';

const id = '0a1b2c3d-1111-4222-8333-444455556666';
const secret = 'lic_ro_' + 'Ab-_'.repeat(10) + 'xyz';
const url = 'http://127.0.0.1:8002/mcp';
const key = { id, name: 'Claude Code', created_at: 1_790_000_000, expires_at: 1_790_000_000 + 30 * 86_400 };

assert.equal(secret.length, 50);
const list = agents.parseAgentKeyList({ keys: [key], mcp_url: url, max_keys: 32, scope: 'research.read' });
assert.deepEqual(list, { keys: [key], mcpUrl: url, maxKeys: 32 });
for (const bad of [{ keys: [key], mcp_url: 'http://127.0.0.1:8002/api', max_keys: 32 }, { keys: [{ ...key, id: 'x' }], mcp_url: url, max_keys: 32 },
  { keys: [{ ...key, expires_at: key.created_at }], mcp_url: url, max_keys: 32 }, { keys: {}, mcp_url: url, max_keys: 32 }, null]) {
  assert.throws(() => agents.parseAgentKeyList(bad));
}
assert.deepEqual(agents.parseCreatedAgentKey({ key, secret }), { key, secret });
assert.throws(() => agents.parseCreatedAgentKey({ key, secret: 'lic_ro_short' }));
assert.throws(() => agents.parseCreatedAgentKey({ key: { ...key, name: '' }, secret }));

assert.ok(agents.validKeyName(' Codex ') && !agents.validKeyName('  ') && !agents.validKeyName('a\nb') && !agents.validKeyName('x'.repeat(81)));
assert.ok(agents.validKeyDays(1) && agents.validKeyDays(365) && !agents.validKeyDays(0) && !agents.validKeyDays(366) && !agents.validKeyDays(1.5));
assert.equal(agents.maskedSecret(secret), 'lic_ro_' + '•'.repeat(43));
assert.equal(agents.daysLeft(key.expires_at, key.created_at * 1000), 30);
assert.equal(agents.daysLeft(key.created_at, key.expires_at * 1000), 0);
assert.equal(agents.installationOrigin(url), 'http://127.0.0.1:8002');

assert.deepEqual([...agents.AGENT_CLIENTS], ['Claude Code', 'Codex', 'Cursor', 'VS Code', 'Claude Desktop']);
for (const client of agents.AGENT_CLIENTS) {
  const placeholder = agents.clientSettings(client, url);
  const filled = agents.clientSettings(client, url, secret);
  assert.ok(placeholder.where.length > 20, client);
  if (client === 'Claude Desktop') {
    // Desktop gets the key from a private file, never inline.
    assert.equal(filled.text, placeholder.text);
    assert.ok(!filled.text.includes(secret));
    const config = JSON.parse(filled.text).mcpServers['leti-cook'];
    assert.deepEqual(config.args.slice(0, 2), ['-I', '-B']);
    assert.equal(config.args[config.args.indexOf('--base-url') + 1], 'http://127.0.0.1:8002');
    continue;
  }
  assert.ok(placeholder.text.includes('Bearer ' + agents.KEY_PLACEHOLDER), client);
  assert.ok(filled.text.includes('Bearer ' + secret) && filled.text.includes(url), client);
  assert.ok(!filled.text.includes(agents.KEY_PLACEHOLDER), client);
}
assert.equal(agents.clientSettings('Claude Code', url, secret).text,
  `claude mcp add --transport http --scope user leti-cook ${url} --header "Authorization: Bearer ${secret}"`);
assert.deepEqual(JSON.parse(agents.clientSettings('VS Code', url, secret).text),
  { servers: { 'leti-cook': { type: 'http', url, headers: { Authorization: 'Bearer ' + secret } } } });
assert.deepEqual(JSON.parse(agents.clientSettings('Cursor', url, secret).text),
  { mcpServers: { 'leti-cook': { url, headers: { Authorization: 'Bearer ' + secret } } } });
assert.match(agents.clientSettings('Codex', url, secret).text, /^\[mcp_servers\.leti-cook\]\nurl = "http:\/\/127\.0\.0\.1:8002\/mcp"\nhttp_headers = \{ "Authorization" = "Bearer lic_ro_/);

// Sign-in settings carry only the address; Claude Desktop's bridge needs a key file.
for (const client of agents.AGENT_CLIENTS) {
  const settings = agents.signInSettings(client, url);
  if (client === 'Claude Desktop') { assert.equal(settings, null); continue; }
  assert.ok(settings.text.includes(url) && !settings.text.includes('Bearer') && !settings.text.includes('Authorization'), client);
  assert.match(settings.where, /Allow/, client);
}
assert.equal(agents.signInSettings('Claude Code', url).text, `claude mcp add --transport http --scope user leti-cook ${url}`);
assert.deepEqual(JSON.parse(agents.signInSettings('Cursor', url).text), { mcpServers: { 'leti-cook': { url } } });
const connection = { id, name: 'Claude Code', redirect_host: 'http://localhost:60123', created_at: key.created_at,
  expires_at: key.expires_at, used_at: null };
assert.deepEqual(agents.parseAgentConnections({ connections: [connection], max_connections: 32 }), [connection]);
assert.deepEqual(agents.parseAgentConnections({ connections: [{ ...connection, used_at: key.created_at + 5 }] })[0].used_at, key.created_at + 5);
for (const bad of [{ connections: [{ ...connection, id: 'x' }] }, { connections: [{ ...connection, used_at: 'today' }] }, { connections: {} }]) {
  assert.throws(() => agents.parseAgentConnections(bad));
}
// The limit in the refusal is the one the server gave with the key list, never a number of the page's own.
assert.equal(agents.keyErrorMessage('agent_key_limit', 'x', 5), 'This installation already has 5 keys, the most it allows. Revoke one first.');
assert.equal(agents.keyErrorMessage('agent_key_limit', 'x'), 'This installation already has as many keys as it allows. Revoke one first.');
assert.doesNotMatch(readFileSync(new URL('../application/frontend/src/explorer/agentsModel.ts', import.meta.url), 'utf8'), /\b32\b/);
assert.equal(agents.keyErrorMessage('other', 'fallback'), 'fallback');

// The real Agents page, driven offline. Child sections are rendered from the props the page
// gives them; the hook host runs the page's own state and effects.
{
  class ExplorerApiError extends Error {
    constructor(message, status, code) { super(message); this.name = 'ExplorerApiError'; this.status = status; this.code = code; }
  }
  class ExplorerNetworkError extends Error {
    constructor() { super('No connection to the Leti Cook server.'); this.name = 'ExplorerNetworkError'; }
  }
  const store = () => new ExplorerApiError('Service unavailable', 503, 'agent_store_unavailable');
  const storeText = 'The agent key store is unavailable. See “Connect an agent through MCP” in the documentation.';
  const jsx = (type, props) => ({ type, props: props ?? {} });
  const { outputText } = ts.transpileModule(readFileSync(new URL('../application/frontend/src/explorer/AgentsPage.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  });
  const icon = function Icon() { return null; };
  function mountPage(answer) {
    const host = reactHookHost(), calls = [];
    const imports = {
      react: host.React, 'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'fragment' }, 'lucide-react': new Proxy({}, { get: () => icon }),
      './api': { ExplorerApiError, ExplorerNetworkError, isAbortError: (error) => error?.name === 'AbortError',
        requestJson: async (path, signal, init) => { calls.push(`${init?.method ?? 'GET'} ${path}`); return answer(path, init?.method ?? 'GET'); } },
      './clipboard': { copyText: async () => true }, './agentsModel': agents, './agents.css': {},
    };
    const exports = {};
    new Function('require', 'exports', outputText)((name) => { assert.ok(name in imports, name); return imports[name]; }, exports);
    host.mount(() => exports.AgentsPage(), {});
    return { host, calls };
  }
  // Sections drawn by hook-free components (Connections, NewKey) are expanded from the props the page gives them;
  // components with their own hooks (Connect, CopyButton) are left out.
  const DRAWN = new Set(['Connections', 'NewKey']);
  const expand = (node) => (node && typeof node === 'object' && !Array.isArray(node) && typeof node.type === 'function'
    ? (DRAWN.has(node.type.name) ? expand(node.type(node.props)) : null) : node);
  const text = (node) => {
    node = expand(node);
    if (node === null || node === undefined || typeof node === 'boolean') return '';
    if (typeof node === 'string' || typeof node === 'number') return String(node);
    if (Array.isArray(node)) return node.map(text).join(' ');
    return text(node.props.children);
  };
  const find = (node, test) => {
    node = expand(node);
    if (!node || typeof node !== 'object') return undefined;
    if (Array.isArray(node)) { for (const child of node) { const hit = find(child, test); if (hit) return hit; } return undefined; }
    if (test(node)) return node;
    return find(node.props?.children, test);
  };
  const alertText = (tree) => text(find(tree, (node) => node.props?.role === 'alert'));
  const section = (tree, heading) => text(find(tree, (node) => node.type === 'section' && node.props['aria-labelledby'] === heading));
  const listAnswer = { keys: [key], mcp_url: url, max_keys: 32 };

  // Neither list read (store unavailable): no "0 allowed · None yet", and the store's sentence once.
  {
    const { host } = mountPage(async () => { throw store(); });
    await flush();
    const tree = host.current;
    assert.equal(alertText(tree), storeText, 'one sentence, not the same one twice');
    const programs = section(tree, 'agents-connections-heading');
    assert.match(programs, /Connected programs\s+—/);
    assert.match(programs, /Could not be read\. Refresh to try again\./);
    assert.doesNotMatch(programs, /0 allowed|None yet/);
    const keys = section(tree, 'agents-keys-heading');
    assert.match(keys, /Keys\s+—/);
    assert.match(keys, /Could not be read\. Refresh to try again\./);
    assert.doesNotMatch(keys, /No keys yet/);
  }
  // One list read, the other not: only that section says so; the read one is shown as usual.
  {
    const { host } = mountPage(async (path) => { if (path === '/agent-keys') return listAnswer; throw new ExplorerNetworkError(); });
    await flush();
    const tree = host.current;
    assert.equal(alertText(tree), 'No connection to the Leti Cook server.');
    assert.match(section(tree, 'agents-connections-heading'), /—[\s\S]*Could not be read/);
    assert.match(section(tree, 'agents-keys-heading'), /1 of 32/);
    assert.doesNotMatch(section(tree, 'agents-keys-heading'), /Could not be read/);
  }
  // Both read: an empty list of programs is "None yet" again, with no alert.
  {
    const { host } = mountPage(async (path) => (path === '/agent-keys' ? listAnswer : { connections: [] }));
    await flush();
    assert.equal(find(host.current, (node) => node.props?.role === 'alert'), undefined);
    assert.match(section(host.current, 'agents-connections-heading'), /0 allowed[\s\S]*None yet/);
  }
  // Create key without an answer says the outcome is unknown and reads the list again (never posts twice).
  const submit = async (host) => {
    find(host.current, (node) => node.type === 'input' && node.props.placeholder === 'Claude Code on my laptop').props
      .onChange({ target: { value: 'Laptop' } });
    find(host.current, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} });
    await flush();
  };
  for (const [name, failure] of [['no connection', () => new ExplorerNetworkError()],
    ['client timeout', () => new ExplorerApiError('The Explorer request timed out. Try again.', 408, 'client_timeout')]]) {
    const { host, calls } = mountPage(async (path, method) => {
      if (method === 'POST') throw failure();
      return path === '/agent-keys' ? listAnswer : { connections: [] };
    });
    await flush();
    await submit(host);
    assert.equal(alertText(host.current), agents.KEY_CREATE_TEXT.unknown, name);
    assert.match(agents.KEY_CREATE_TEXT.unknown, /not known whether the key was created/);
    assert.deepEqual(calls, ['GET /agent-keys', 'GET /agent-connections', 'POST /agent-keys', 'GET /agent-keys', 'GET /agent-connections'], name);
  }
  // A refusal the server named is still "not created", without another read. At the limit the page names the limit
  // the server gave with the key list (it said 32 whatever the server allowed).
  for (const [limit, said] of [[32, 'This installation already has 32 keys, the most it allows. Revoke one first.'],
    [5, 'This installation already has 5 keys, the most it allows. Revoke one first.'],
    [undefined, 'This installation already has as many keys as it allows. Revoke one first.']]) {
    const { host, calls } = mountPage(async (path, method) => {
      if (method === 'POST') throw new ExplorerApiError('Too many keys', 409, 'agent_key_limit');
      if (path !== '/agent-keys') return { connections: [] };
      if (limit === undefined) throw store();
      return { ...listAnswer, max_keys: limit };
    });
    await flush();
    await submit(host);
    assert.ok(alertText(host.current).startsWith(said), String(limit));
    assert.deepEqual(calls.filter((call) => call.startsWith('GET')).length, 2, 'no read after a named refusal');
  }
  // An answer that cannot be read means the key exists but cannot be shown: say so and read the list again.
  {
    const { host, calls } = mountPage(async (path, method) => {
      if (method === 'POST') return undefined;
      return path === '/agent-keys' ? listAnswer : { connections: [] };
    });
    await flush();
    await submit(host);
    assert.equal(alertText(host.current), agents.KEY_CREATE_TEXT.unreadable);
    assert.equal(calls.at(-1), 'GET /agent-connections');
  }
  console.log('Agents page: an unread list says so, one sentence once, and an unknown key outcome reads the list again.');
}
console.log('agents ui: ok');
