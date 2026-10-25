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
