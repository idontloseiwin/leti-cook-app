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
const api = compiled('../application/frontend/src/explorer/channelTrashApi.ts', {'../sourceLifetime': lifetime});
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
