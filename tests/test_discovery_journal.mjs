import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
const source = readFileSync(new URL('../application/frontend/src/discoveryJournal.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
const api = {};
new Function('exports', outputText)(api);
const sent = [];
globalThis.fetch = async (path, options) => { sent.push({ path, body: JSON.parse(options.body) }); return new Response(null, {status: 204}); };
const settle = () => new Promise(resolve => setImmediate(resolve));
function response(id = 'a'.repeat(32)) {
  const payload = {context: {valid_before: new Date(Date.now() + 60_000).toISOString()}, rows: [{id: 'fxboot00001', cells: {}}]};
  api.observeDiscoveryResponse(payload, new Headers({'X-Leti-Discovery-Read': id}));
  return payload;
}
const first = response();
await settle();
assert.deepEqual(sent[0].body, {read_id: 'a'.repeat(32), status: 'received', indices: [0]});
const parsed = {};
api.transferDiscoveryReceipt(first.rows[0], parsed);
response('b'.repeat(32)); // Same video, newer read: the old row keeps its own receipt.
await settle();
api.reportDiscoveryRendered([parsed]);
api.reportDiscoveryRendered([parsed]);
await settle();
assert.equal(sent.length, 3);
assert.deepEqual(sent[2].body, {read_id: 'a'.repeat(32), status: 'rendered', indices: [0]});
assert.equal(JSON.stringify(sent).includes('fxboot00001'), false);
api.reportDiscoveryRendered([{video_id: 'fxboot00001'}]);
response('invalid');
await settle();
assert.equal(sent.length, 3);
globalThis.fetch = async () => { throw new Error('offline'); };
response('c'.repeat(32));
await settle(); // A lost report is not retried or surfaced as a read error.
assert.ok(readFileSync(new URL('../application/frontend/src/explorer/VirtualVideoGrid.tsx', import.meta.url), 'utf8').includes('reportDiscoveryRendered(rows.flatMap'));
const panelSource = readFileSync(new URL('../application/frontend/src/explorer/DiscoveryJournalPanel.tsx', import.meta.url), 'utf8');
const panelOutput = ts.transpileModule(panelSource, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const panel = {};
new Function('require', 'exports', panelOutput)(name => ({react:{}, './api':{}, 'react/jsx-runtime':{}})[name], panel);
const item = {id:'event',ts:new Date().toISOString(),operation:'browser',status:'rendered',subject_ref:'a'.repeat(32),read_id:'b'.repeat(32),dropped_total:2};
assert.equal(panel.parseDiscoveryJournal({status:'ok',events:[item],metrics:{dropped:1}}).events[0].status, 'rendered');
for (const invalid of [{...item,status:'visible'}, {...item,subject_ref:'fxboot00001'}, {...item,dropped_total:-1}]) {
  assert.throws(() => panel.parseDiscoveryJournal({status:'ok',events:[invalid],metrics:{}}));
}
assert.throws(() => panel.parseDiscoveryJournal({status:'ok',events:Array(201).fill(item),metrics:{}}));
assert.ok(panelSource.includes('client reports') && panelSource.includes('remain unknown'));
// The panel stays one closed line until it is opened; only an open panel reads and draws the journal.
const drawn = {};
const effects = [];
let stateCalls = 0, isOpen = false, journalReads = 0;
const node = (type, props) => ({type, props});
new Function('require', 'exports', panelOutput)(name => ({
  react: {useState: initial => (stateCalls++ === 0 ? [isOpen, () => {}] : [initial, () => {}]), useEffect: effect => effects.push(effect)},
  './api': {requestJson: () => { journalReads += 1; return new Promise(() => {}); }},
  'react/jsx-runtime': {jsx: node, jsxs: node, Fragment: 'fragment'},
})[name], drawn);
const draw = open => { isOpen = open; stateCalls = 0; effects.length = 0; const tree = drawn.DiscoveryJournalPanel(); effects.forEach(effect => effect()); return tree; };
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.props?.children)] : [];
const words = value => Array.isArray(value) ? value.map(words).join(' ') : value && typeof value === 'object' ? words(value.props?.children) : typeof value === 'string' ? value : '';
const closed = draw(false);
assert.equal(journalReads, 0);
assert.ok(words(closed).includes('Discovery support journal') && !words(closed).includes('client reports'));
assert.ok(nodes(closed).some(item => item.type === 'details') && !nodes(closed).some(item => item.type === 'table'));
const opened = draw(true);
assert.equal(journalReads, 1);
assert.ok(words(opened).includes('client reports') && nodes(opened).some(item => item.type === 'table'));
console.log('discovery journal browser receipt tests passed (no browser paint claim)');
