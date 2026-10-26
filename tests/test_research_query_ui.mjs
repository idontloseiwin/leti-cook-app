import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
const text = readFileSync(new URL('../application/frontend/src/explorer/researchQuery.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
const exports = {};
new Function('require', 'exports', outputText)(name => name === '../sourceLifetime' ? lifetime : { ExplorerApiError: class extends Error {} }, exports);
const filters = { folder: '11111111-1111-1111-1111-111111111111', type: 'short', title: 'Straße', minimumViews: '9007199254740993', sort: 'video.views', direction: 'desc' };
const request = exports.researchQuery(filters);
assert.deepEqual(request.video_types, ['short']);
assert.equal(request.where.all[1].value, '9007199254740993');
assert.equal(request.scope.include_descendants, false);
assert.deepEqual(exports.researchQuery(filters, 'opaque').page, { size: 25, after: 'opaque' });
for (const n of ['-1', '1.5', '01', '18446744073709551616', 42]) assert.throws(() => exports.uint(n));
assert.equal(exports.uint('18446744073709551615'), '18446744073709551615');

const now = Date.now(), evaluated = new Date(now).toISOString(), deadline = new Date(now+120000).toISOString();
const values = ['abcdefghijk', 'UC'+'a'.repeat(22), 'short', 'Do not execute this title <script>', evaluated, '0', '9007199254740993'];
const cells = Object.fromEntries(exports.researchFields.map((field, i) => [field, { state: 'known', value: values[i], provenance_ref: 'youtube', observed_at: evaluated, expires_at: deadline }]));
const valid = { query_version: 1, query_fingerprint: 'a'.repeat(64), columns: exports.researchFields,
  context: { source_id: 'public.videos.current.v1', consistency: 'current', evaluated_at: evaluated, valid_before: deadline },
  source_read_at: evaluated, source_expires_at: deadline, rows: [{ id: values[0], cells }],
  provenance: { youtube: { origin: 'youtube_reported', source_paths: ['videos.statistics.viewCount'], method: 'reported_current_v1' } },
  coverage: { status: 'complete', channels_complete: 1, channels_in_scope: 1, reasons: {} },
  page: { has_more: false, next_cursor: null } };
function tagged(change = () => {}) { const data = structuredClone(valid); change(data); return lifetime.captureSourceLifetime(data, undefined, performance.now()); }
const parsed = exports.parseResearchPage(tagged(), 'short');
assert.equal(parsed.rows[0].cells['video.views'].value, values[6]);
assert.ok(lifetime.sourceLifetime(parsed));
for (const change of [
  data => { data.rows[0].cells['video.views'].value = 9007199254740993; },
  data => { data.rows[0].cells['video.views'].state = 'unknown'; },
  data => { data.rows[0].cells['video.type'].value = 'long'; },
  data => { data.rows.push(data.rows[0]); },
  data => { data.page.has_more = true; },
  data => { data.rows[0].id = 'unsafe/id'; },
  data => { data.rows[0].cells['video.views'].expires_at = evaluated; },
  data => { data.context.valid_before = evaluated; },
  data => { data.coverage.channels_complete = 2; },
  data => { data.source_read_at = 'invalid'; },
]) assert.throws(() => exports.parseResearchPage(tagged(change), 'short'));
assert.throws(() => exports.parseResearchPage(structuredClone(valid), 'short'));
const unknown = tagged(data => { data.rows[0].cells['video.views'].state = 'unknown'; data.rows[0].cells['video.views'].value = null; });
assert.equal(exports.parseResearchPage(unknown, 'short').rows[0].cells['video.views'].value, null);
console.log('Research query UI: request, precision, source lifetime, identity, coverage and malformed-response checks passed.');

class ApiError extends Error { constructor(code) { super(code); this.code = code; } }
function retryFixture(sequence, { hold = false } = {}) {
  const api = {}, delays = [], timers = new Set(); let calls = 0;
  new Function('require', 'exports', 'setTimeout', 'clearTimeout', outputText)(
    name => name === '../sourceLifetime' ? lifetime : { ExplorerApiError: ApiError,
      requestJson: async () => { calls++; const next = sequence.shift(); if (next instanceof Error) throw next; return next; } },
    api, (fn, ms) => { const id = {}; timers.add(id); delays.push(ms); if (!hold) queueMicrotask(() => { timers.delete(id); fn(); }); return id; },
    id => timers.delete(id));
  return { api, delays, timers, calls: () => calls };
}
let fixture = retryFixture([new ApiError('catalogue_preparing'), tagged()]);
let notices = 0;
assert.equal((await fixture.api.loadResearchPage(filters, new AbortController().signal, undefined, () => notices++)).rows.length, 1);
assert.equal(fixture.calls(), 2); assert.equal(notices, 1); assert.deepEqual(fixture.delays, [2000]);
fixture = retryFixture(Array.from({length: 4}, () => new ApiError('catalogue_preparing')));
await assert.rejects(fixture.api.loadResearchPage(filters, new AbortController().signal), /catalogue_preparing/);
assert.equal(fixture.calls(), 3); assert.deepEqual(fixture.delays, [2000, 2000]);
for (const code of ['policy_denied', 'backend_unavailable', 'query_busy', 'query_budget_exceeded', 'cursor_expired']) {
  fixture = retryFixture([new ApiError(code)]);
  await assert.rejects(fixture.api.loadResearchPage(filters, new AbortController().signal), new RegExp(code));
  assert.equal(fixture.calls(), 1); assert.equal(fixture.delays.length, 0);
}
fixture = retryFixture([new ApiError('catalogue_preparing')], { hold: true });
const controller = new AbortController(), pending = fixture.api.loadResearchPage(filters, controller.signal);
await Promise.resolve(); await Promise.resolve();
assert.equal(fixture.timers.size, 1);
controller.abort(); await assert.rejects(pending, /abort/i);
assert.equal(fixture.timers.size, 0); assert.equal(fixture.calls(), 1);
console.log('Preparation retry: bounded attempts, cancellation, timer cleanup, and no retries for other failures passed.');
