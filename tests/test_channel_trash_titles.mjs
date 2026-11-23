import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';

function compiled(path, imports) {
  const {outputText} = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
  });
  const exports = {};
  new Function('require', 'exports', outputText)(name => {assert.ok(name in imports, name); return imports[name];}, exports);
  return exports;
}
const serverText = compiled('../application/frontend/src/serverText.ts', {});
const api = compiled('../application/frontend/src/explorer/channelTrashApi.ts', {'../sourceLifetime': lifetime, '../serverText.ts': serverText});
const id = '44444444-4444-4444-8444-444444444444';
const now = Date.now();
const stamp = offset => new Date(now + offset).toISOString();
const response = lifetime.captureSourceLifetime({source_read_at: stamp(0), source_expires_at: stamp(10000),
  items: [{id, title: '<Example channel>', source_expires_at: stamp(10000)}]}, undefined, performance.now());
const parsed = api.parseTrashTitles(response, [id]);
assert.equal(api.currentChannelName(parsed.items[0]), '<Example channel>');
assert.equal(api.currentChannelName({title: 'Name without expiry evidence'}), undefined);
for (const raw of [{items: [response.items[0], response.items[0]]}, {items: [{...response.items[0], id: 'unexpected'}]},
  {items: [{...response.items[0], source_expires_at: 'invalid'}]}]) assert.throws(() => api.parseTrashTitles(raw, [id]));
const old = lifetime.captureSourceLifetime({source_read_at: stamp(0), source_expires_at: stamp(-1),
  items: [{id, title: 'Expired name', source_expires_at: stamp(-1)}]}, undefined, performance.now());
assert.equal(api.currentChannelName(api.parseTrashTitles(old, [id]).items[0]), undefined);

// Execute the actual dialog render. Optional titles never hide the link or Restore.
const render = (apiAvailable, titles = parsed, channel) => {
  const states = [{items: [{id, user_input: '@example', restore_until: stamp(10000), can_restore: true}], next_cursor: null},
    false, '', now, undefined, titles, 0];
  const jsx = (type, props) => ({type, ...props});
  const component = compiled('../application/frontend/src/explorer/ChannelTrashControls.tsx', {
    react: {useState: () => [states.shift(), () => {}], useRef: value => ({current: value}), useEffect() {}},
    'react/jsx-runtime': {jsx, jsxs: jsx, Fragment: 'fragment'},
    './channelTrashApi': api, '../sourceLifetimeReact': {useSourceExpiry() {}}, './saved.css': {}, './channel-trash.css': {},
  });
  return component.ChannelTrashDialog({channelId: channel?.channel_id, channel, apiAvailable, onClose() {}, onChanged() {}});
};
const text = value => typeof value === 'string' ? value : Array.isArray(value) ? value.map(text).join(' ')
  : value && typeof value === 'object' ? text(value.children) : '';
assert.match(text(render(true)), /<Example channel>/);
assert.match(text(render(false)), /@example/);
assert.match(text(render(false)), /Restore channel/);
assert.doesNotMatch(text(render(false)), /<Example channel>/);
assert.doesNotMatch(text(render(true, api.parseTrashTitles(old, [id]))), /Expired name/);
const channel = lifetime.transferSourceLifetime(parsed.items[0], {channel_id: 'UC'+'a'.repeat(22), title: '<Example channel>'});
assert.match(text(render(true, undefined, channel)), /<Example channel>/);
assert.match(text(render(false, undefined, channel)), /UCaaaaaaaaaaaaaaaaaaaaaa/);
assert.doesNotMatch(text(render(false, undefined, channel)), /<Example channel>/);
console.log('Trash titles: fresh/expired/blocked names, preserved identities and restore controls passed.');

// A lost or unreadable answer reads in the page's words, never "Failed to fetch" or the JSON parser's text.
// A removal or restore says its result is not confirmed. The dialog shows the error's message as is.
{
  const realFetch = globalThis.fetch;
  const lost = 'No connection to the Leti Cook server.';
  const unreadable = 'The server answer could not be read.';
  const unconfirmed = ' The result is not confirmed; refresh status before retrying.';
  const item = {id, user_input: '@example', removed_at: stamp(-1000), restore_until: stamp(10000), can_restore: true};
  const calls = {
    list: () => api.loadDeletedChannels(new AbortController().signal),
    remove: () => api.removeTrackedChannel('UC' + 'a'.repeat(22), new AbortController().signal),
    restore: () => api.restoreDeletedChannel(item, new AbortController().signal),
  };
  const broken = () => new Response(new ReadableStream({ pull(controller) { controller.error(new TypeError('network error')); } }));
  const cases = [
    ['no answer', async () => { throw new TypeError('Failed to fetch'); }, lost],
    ['a proxy page', async () => new Response('<html><body>502 Bad Gateway</body></html>', {status: 502}), unreadable],
    ['an HTML 200', async () => new Response('<!doctype html><title>Sign in</title>', {status: 200}), unreadable],
    ['a body cut mid-read', async () => broken(), lost],
    ['invalid UTF-8', async () => new Response(new Uint8Array([0x7b, 0xff, 0xfe, 0x7d]), {status: 200}), unreadable],
  ];
  try {
    for (const [name, fetch, words] of cases) {
      globalThis.fetch = fetch;
      for (const [call, run] of Object.entries(calls)) {
        const failure = await run().then(() => null, error => error);
        assert.ok(failure instanceof Error, `${name} / ${call}`);
        assert.equal(failure.message, call === 'list' ? words : words + unconfirmed, `${name} / ${call}`);
        assert.doesNotMatch(failure.message, /Failed to fetch|JSON|Unexpected token|encoding/, `${name} / ${call}`);
      }
    }
    // Known refusals keep their words; a cancel stays a cancel (the dialog has its own sentence for it).
    globalThis.fetch = async () => new Response(JSON.stringify({code: 'conflict'}), {status: 409});
    assert.equal((await calls.restore().then(() => null, error => error)).message, 'This channel changed. Refresh the list before trying again.');
    globalThis.fetch = async () => new Response(JSON.stringify({code: 'constructor'}), {status: 409});
    assert.equal((await calls.list().then(() => null, error => error)).message, 'The operation was not confirmed. Refresh status before retrying.');
    globalThis.fetch = async (path, init) => new Promise((resolve, reject) => init.signal.addEventListener('abort',
      () => reject(new DOMException('The operation was aborted.', 'AbortError')), {once: true}));
    const controller = new AbortController();
    const cancelled = api.loadDeletedChannels(controller.signal).then(() => null, error => error);
    controller.abort();
    assert.equal((await cancelled).name, 'AbortError');
  } finally { globalThis.fetch = realFetch; }
  console.log('deleted channels name a lost or unreadable answer in plain words; changes say they are not confirmed.');
}

// A removed channel whose stored ID is not version 4 (written directly in the database)
// is listed, named and restored like the others.
{
  const realFetch = globalThis.fetch;
  const other = '6ba7b810-9dad-11d1-80b4-00c04fd430c8';
  const item = {id: other, user_input: '@one', removed_at: stamp(-1000), restore_until: stamp(10000), can_restore: true};
  const page = api.parseTrashPage({items: [item, {...item, id}], next_cursor: other});
  assert.deepEqual(page.items.map(row => row.id), [other, id]);
  assert.equal(page.next_cursor, other);
  for (const bad of ['not-a-uuid', other.toUpperCase(), other.replaceAll('-', ''), '{' + other + '}'])
    assert.throws(() => api.parseTrashPage({items: [{...item, id: bad}], next_cursor: null}), /Invalid deleted-channel list/);
  const seen = [];
  try {
    globalThis.fetch = async (path, init) => {
      seen.push([path, init.method]);
      if (path.includes('/titles?')) return new Response(JSON.stringify({source_read_at: stamp(0), source_expires_at: stamp(10000),
        items: [{id: other, title: 'Hand-made', source_expires_at: stamp(10000)}]}));
      if (path.endsWith('/restore')) return new Response(JSON.stringify({status: 'restored'}));
      return new Response(JSON.stringify({items: [item], next_cursor: null}));
    };
    const signal = new AbortController().signal;
    assert.deepEqual((await api.loadDeletedChannels(signal, other)).items.map(row => row.id), [other]);
    assert.deepEqual((await api.loadDeletedChannelTitles([item], signal)).items.map(row => row.title), ['Hand-made']);
    await api.restoreDeletedChannel(item, signal);
  } finally { globalThis.fetch = realFetch; }
  assert.deepEqual(seen, [['/user-work/deleted-channels?after=' + other, 'GET'], ['/user-work/deleted-channels/titles?ids=' + other, 'GET'],
    ['/user-work/deleted-channels/' + other + '/restore', 'POST']]);
  console.log('a removed channel with another UUID version is listed, named and restored like the others.');
}
