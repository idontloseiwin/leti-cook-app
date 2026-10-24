import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as sourceLifetime from '../application/frontend/src/sourceLifetime.ts';
// Import only the parser and its actual constants; unrelated UI imports are not exercised here.
const path = new URL('../application/frontend/src/explorer/telemetryWorkspace.ts', import.meta.url);
const text = readFileSync(path, 'utf8');
const parserSource = text.slice(text.indexOf('export const TELEMETRY_VIEW_LIMIT'), text.indexOf('export type TelemetryRecentSnapshot'));
const { parseTelemetryEvent } = await import('data:text/javascript;base64,' + Buffer.from(ts.transpile(parserSource, {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext})).toString('base64'));
const event = {schema_version: 1, id: 'event', ts: '2026-10-15T10:00:00Z', kind: 'pipeline', source: 'collector', target: 'collector', operation: 'http_started', status: 'started'};
assert.ok(parseTelemetryEvent(event));
const traced = {...event, schema_version: 2, trace_id: '1'.repeat(32), span_id: '2'.repeat(32), writer_id: '3'.repeat(32), sequence: 1, monotonic_ns: 12345678900000000, video_ids: ['v0000000001'], job_id: 'be81a8a3-e534-4ef5-9707-91196b321109', error_type: 'Timeout', quota_bucket: 'video_batch_get_stats'};
assert.deepEqual(parseTelemetryEvent(traced), traced);
for (const retry_after_seconds of [0, 30, Number.MAX_SAFE_INTEGER]) {
  const encoded = JSON.parse(JSON.stringify({...traced, retry_after_seconds}));
  assert.deepEqual(parseTelemetryEvent(encoded), encoded);
  assert.equal(parseTelemetryEvent(encoded).retry_after_seconds, retry_after_seconds);
  assert.equal(Object.hasOwn(encoded, 'due_at'), false); // Delay does not invent a scheduled deadline.
}
for (const retry_after_seconds of [-1, true, false, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '30', null, {}, []]) {
  assert.equal(parseTelemetryEvent({...traced, retry_after_seconds}), null);
}
assert.equal(Object.hasOwn(parseTelemetryEvent(traced), 'retry_after_seconds'), false);
for (const bad of [{raw_payload: 'secret'}, {trace_id: 'bad'}, {video_ids: new Array(51).fill('v0000000001')}, {video_id: 'bad'}, {sequence: -1}, {job_id: {}}, {due_at: '2026-10-15'}, {schema_version: 3}]) assert.equal(parseTelemetryEvent({...traced, ...bad}), null);
function compiled(relative, imports, suffix = '') {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8') + suffix;
  const { outputText } = ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }});
  const exports = {};
  new Function('require', 'exports', outputText)(name => {
    assert.ok(Object.hasOwn(imports, name), name);
    return imports[name];
  }, exports);
  return exports;
}
const mapping = compiled('../application/frontend/src/telemetryMapping.ts', {});
const collectionExplanation = compiled('../application/frontend/src/collectionExplanation.ts', {});
const workspace = compiled('../application/frontend/src/explorer/telemetryWorkspace.ts', {
  '../telemetryMapping': mapping, './localTime': { formatExactLocal: value => value || 'Not recorded' },
  '../sourceLifetime': sourceLifetime,
  '../collectionExplanation': collectionExplanation,
});
const refusal = parseTelemetryEvent({...traced, operation: 'video_validation', status: 'failed',
  phase: 'contentDetails.duration', reason: 'duration_missing', video_id: 'jGyDPxNWHeA',
  channel_id: 'UC' + 'a'.repeat(22), batch_id: '22192efa-5a67-48fe-83ea-2364f5528571', sync_run_id: 201 });
assert.ok(refusal);
const receipt = {...refusal, id: 'receipt', operation: 'video_write', status: 'confirmed',
  reason: 'provider_identity_or_lifecycle_error'};
assert.equal(workspace.telemetryFailed(refusal), true);
assert.equal(workspace.telemetryFailed(receipt), false);
assert.deepEqual(workspace.filterTelemetryEvents([refusal, receipt], {
  query: '', kind: '', failedOnly: true,
}), [refusal]);

let runFixture;
let streamFixture = {events: [], arrivals: [], connection: 'connected', journalStatus: 'ok', rejected: 0};
// Execute the real inspector with inert hooks/JSX: no browser, HTTP or timers.
const jsx = (type, props) => ({type, props});
const page = compiled('../application/frontend/src/explorer/TelemetryPage.tsx', {
  react: {useRef: () => ({current: null}), useEffect: () => {}, useState: value => [value, () => {}], useMemo: fn => fn()},
  'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {},
  './useOperationalRead': {useOperationalRead: load => load === workspace.loadTelemetrySystem
    ? {payload: runFixture, updatedAt: Date.now(), refresh() {}} : {payload: {events: [], rejected: 0}, refresh() {}}},
  './useTelemetryStream': {useTelemetryStream: () => streamFixture}, './TelemetryFlowMapChart': {},
  '../serverText': {}, './telemetryWorkspace': workspace, './telemetry.css': {},
}, '\nexport { EventDetails };\n');
function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join(' ');
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'object') return textOf(node.props?.children);
  return String(node);
}
const inspector = textOf(page.EventDetails({event: refusal, onClose() {}}));
for (const expected of ['Rejected field', 'contentDetails.duration', 'Reason', 'duration_missing',
  'Video ID', refusal.video_id, 'Channel ID', refusal.channel_id, 'Batch ID', refusal.batch_id, 'Sync run', '201']) {
  assert.ok(inspector.includes(expected), expected);
}
const legacy = textOf(page.EventDetails({event, onClose() {}}));
const providerManifest = {...traced, operation: 'video_provider_manifest', endpoint: 'videos',
  requested_count: 2, received_count: 1, video_ids: ['v0000000001']};
const manifestText = textOf(page.EventDetails({event: parseTelemetryEvent(providerManifest), onClose() {}}));
for (const expected of ['API endpoint videos', 'Requested IDs 2', 'Returned IDs 1',
  'Returned requested IDs v0000000001', 'Envelope v 2']) assert.ok(manifestText.includes(expected), expected);
const retryText = textOf(page.EventDetails({event: {...receipt, retry_after_seconds: 30}, onClose() {}}));
assert.ok(retryText.includes('Requested retry delay (seconds) 30'));
assert.ok(!retryText.includes('Next due'));
assert.ok(legacy.includes('Phase Not recorded'));
assert.ok(legacy.includes('Reason Not recorded'));
assert.ok(!legacy.includes('Rejected field'));
assert.ok(!legacy.includes('Video ID'));
const malformed = textOf(page.EventDetails({event: {...event, reason: {raw: 'DO_NOT_RENDER'},
  video_id: {raw: 'DO_NOT_RENDER'}, batch_id: {raw: 'DO_NOT_RENDER'}}, onClose() {}}));
assert.ok(!malformed.includes('DO_NOT_RENDER'));
// Known refusals are errors; normal admission decisions and uncertain ACKs stay distinct.
const writeRejected = {...receipt, id: 'write-rejected', status: 'rejected'};
const providerInvalid = {...providerManifest, id: 'invalid-manifest', status: 'invalid'};
const providerInvalidItem = {...event, id: 'invalid-item', operation: 'video_provider_item', status: 'invalid'};
const admissionRejected = {...event, id: 'admission', operation: 'dispatch_offer', status: 'rejected'};
const unknownWrite = {...receipt, id: 'unknown-write', status: 'unknown'};
const missingCounter = {...event, id: 'missing-counter', operation: 'video_provider_item', status: 'observed', reason: 'view_count_missing'};
const errorEvents = [refusal, writeRejected, providerInvalid, providerInvalidItem];
const allEvents = [...errorEvents, admissionRejected, unknownWrite, missingCounter, receipt];
assert.deepEqual(workspace.filterTelemetryEvents(allEvents, {query: '', kind: '', failedOnly: true}), errorEvents);
assert.equal(workspace.telemetryFlows(allEvents)[0].errors, errorEvents.length);
for (const operation of ['video_batch', 'common_pass']) {
  const output = textOf(page.EventDetails({event: {...event, operation, requested_count: 50, received_count: 47}, onClose() {}}));
  assert.ok(output.includes('Confirmed updates 47'), operation);
  assert.ok(!output.includes('Returned IDs'), operation);
}
for (const [operation, label] of [['video_claim', 'Claimed videos'], ['common_claim', 'Claimed videos'],
  ['inventory_write', 'Acknowledged page items'], ['future_operation', 'Received count']]) {
  const output = textOf(page.EventDetails({event: {...event, operation, received_count: 4}, onClose() {}}));
  assert.ok(output.includes(`${label} 4`), operation);
  assert.ok(!output.includes('Returned IDs'), operation);
}
const summaryInput = {now: '2026-10-19T19:00:00Z', latest_run: null, recent_runs: [{
  id: 359, status: 'partial', stats_updated: 1351, errors: 0,
  summary_json: {video_collection: {claimed: 1353, updated: 1351, unavailable_marked: 1,
    metadata_deferred: 1, unresolved: 1, errors: 0, completion_outcome: 'partial', incomplete: true,
    publication_waiting: 3, stream_lifecycle_probed: 2, provider_payload: 'DO_NOT_RENDER'},
    arbitrary: 'DO_NOT_RENDER'},
}]};
runFixture = workspace.parseTelemetrySystemSummary(summaryInput);
// Run context/history use their bounded endpoint and retain its acquisition clock.
{
  const savedFetch = globalThis.fetch;
  const readAt = Date.now(), expiry = readAt + 1000, acquiredAt = performance.now();
  const input = {...structuredClone(summaryInput), recent_runs_read_status: 'ok',
    source_read_at: new Date(readAt).toISOString(), source_expires_at: new Date(expiry).toISOString()};
  const headers = {get: name => name === 'X-Source-Expires-At' ? input.source_expires_at
    : name === 'X-Source-Read-At' ? input.source_read_at : null};
  const signal = new AbortController().signal;
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/system/run-history');
    assert.equal(options.signal, signal);
    assert.equal(options.cache, 'no-store');
    return {ok: true, headers, json: async () => input};
  };
  try {
    const result = await workspace.loadTelemetrySystem(signal);
    assert.equal(sourceLifetime.sourceExpired(result, readAt, acquiredAt), false);
    assert.equal(sourceLifetime.sourceExpired(result, expiry, acquiredAt), true);
    assert.equal(sourceLifetime.sourceExpired(result.recent_runs[0], readAt - 10000, acquiredAt + 2000), true);
    assert.throws(() => workspace.parseTelemetrySystemSummary({...input, recent_runs_read_status: 'unavailable'}));
  } finally { globalThis.fetch = savedFetch; }
}
assert.equal(runFixture.recent_runs[0].collection.metadata_deferred, 1);
assert.equal(Object.hasOwn(runFixture.recent_runs[0].collection, 'provider_payload'), false);
assert.equal(workspace.telemetryWaitingPublications(runFixture.recent_runs[0].collection), 3);
assert.equal(workspace.telemetryWaitingPublications({stream_lifecycle_probed: 2}), 2);
assert.equal(workspace.telemetryWaitingPublications({}), undefined);
const explanation = workspace.telemetryRunExplanation(runFixture.recent_runs[0]);
assert.ok(explanation.includes('1 video recorded as needing additional metadata checks.'));
assert.ok(explanation.includes('Unresolved items in video collection: 1.'));
assert.ok(!/deleted|premiere|YouTube did not return/.test(explanation));
assert.ok(workspace.telemetryRunExplanation({status: 'partial'}).includes('not recorded'));
assert.ok(workspace.telemetryRunExplanation({status: 'partial', yield_requested: true}).includes('later pass'));
assert.equal(workspace.telemetryRunExplanation({status: 'completed', collection: {publication_waiting: 3}}), undefined);
for (const bad of [-1, 0.5, '1', true, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, {}]) {
  const copy = structuredClone(summaryInput);
  copy.recent_runs[0].summary_json.video_collection.metadata_deferred = bad;
  assert.throws(() => workspace.parseTelemetrySystemSummary(copy));
}
for (const bad of [{incomplete: 'true'}, {yield_requested: 1}, {completion_outcome: 'untrusted text'}]) {
  const copy = structuredClone(summaryInput);
  Object.assign(copy.recent_runs[0].summary_json.video_collection, bad);
  assert.throws(() => workspace.parseTelemetrySystemSummary(copy));
}
const pageText = textOf(page.TelemetryPage());
assert.ok(pageText.includes('1 video recorded as needing additional metadata checks.'));
assert.ok(pageText.includes('Recorded errors 0'));
assert.ok(pageText.includes('1 unfinished in this run'));
assert.ok(pageText.includes('A missing counter alone does not confirm that a video is unavailable.'));
assert.ok(workspace.telemetryRunExplanation({status: 'error'}).includes('saved'));
assert.ok(pageText.includes('Waiting publications handled 3'));
assert.ok(!pageText.includes('DO_NOT_RENDER'));
// New explanations retain exact recorded causes and remain explicitly historical.
const explainedRun = structuredClone(summaryInput);
explainedRun.recent_runs[0].explanation = {
  scope: 'recorded_run', current_recovery: 'not_evaluated', next_attempt: 'stored_queue',
  reasons: [{code: 'metadata_followup', count: 1, stage: 'video_collection'}],
};
runFixture = workspace.parseTelemetrySystemSummary(explainedRun);
const explainedText = textOf(page.TelemetryPage());
assert.ok(explainedText.includes('1 video required metadata and availability checks after a statistics response.'));
assert.ok(explainedText.includes('later recovery is not assessed here'));
assert.ok(!explainedText.includes('metadata_followup'));
for (const bad of [
  {...explainedRun.recent_runs[0].explanation, current_recovery: 'recovered'},
  {...explainedRun.recent_runs[0].explanation, reasons: [{code: 'DO_NOT_RENDER', count: 1, stage: 'video_collection'}]},
]) {
  const input = structuredClone(explainedRun);
  input.recent_runs[0].explanation = bad;
  assert.throws(() => workspace.parseTelemetrySystemSummary(input));
}
streamFixture = {...streamFixture, events: [refusal], journalStatus: 'unavailable'};
const unavailableText = textOf(page.TelemetryPage());
assert.ok(unavailableText.includes('Journal unavailable'));
assert.ok(unavailableText.includes('Previously received events are kept'));
assert.ok(unavailableText.includes('video_validation'));
assert.ok(!unavailableText.includes('Connected · quiet'));
function nodesOf(node) {
  if (Array.isArray(node)) return node.flatMap(nodesOf);
  return node && typeof node === 'object' ? [node, ...nodesOf(node.props?.children)] : [];
}
const chartProps = nodesOf(page.TelemetryPage()).find(node => Object.hasOwn(node.props ?? {}, 'journalUnavailable'))?.props;
assert.equal(chartProps.connected, true); // Transport and source state are not conflated.
assert.equal(chartProps.journalUnavailable, true);
streamFixture = {...streamFixture, journalStatus: 'ok'};
assert.ok(!textOf(page.TelemetryPage()).includes('Journal unavailable'));

// Exercise the real SSE adapter: control messages cannot become events/cursors.
const stream = compiled('../application/frontend/src/explorer/telemetryStream.ts', {'./telemetryWorkspace': workspace});
for (const data of ['{}', 'null', '[]', '{"status":"other"}', '{"status":"ok","payload":"no"}', 'x'.repeat(65), 42]) {
  assert.equal(stream.parseTelemetryJournalStatus(data), null);
}
const listeners = new Map();
let closed = false;
const fakeSource = {readyState: 1, onopen: null, onmessage: null, onerror: null,
  addEventListener(type, listener) { listeners.set(type, listener); },
  removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
  close() { closed = true; }};
const connections = [], journalStates = [], batches = [], cursors = [];
let rejectedMessages = 0;
const disconnect = stream.connectTelemetryStream({createSource: () => fakeSource,
  onConnection: state => connections.push(state), onJournalStatus: state => journalStates.push(state),
  onEvents: batch => batches.push(batch), onCursor: id => cursors.push(id), onRejected: () => rejectedMessages++});
fakeSource.onopen();
const statusListener = listeners.get('telemetry-status');
statusListener({data: '{"status":"ok"}'});
statusListener({data: '{"status":"unavailable"}'});
assert.equal(connections.at(-1), 'connected');
assert.equal(journalStates.at(-1), 'unavailable');
assert.deepEqual(batches, []);
assert.deepEqual(cursors, []);
statusListener({data: '{"status":"ok","payload":"no"}'});
assert.equal(rejectedMessages, 1);
assert.equal(journalStates.at(-1), 'unavailable');
statusListener({data: '{"status":"ok"}'});
assert.equal(journalStates.at(-1), 'ok');
fakeSource.onmessage({data: JSON.stringify(event), lastEventId: event.id});
await new Promise(resolve => setTimeout(resolve, 140));
assert.deepEqual(batches, [[event]]);
assert.deepEqual(cursors, [event.id]);
fakeSource.onerror();
assert.equal(connections.at(-1), 'reconnecting');
assert.equal(journalStates.at(-1), 'unknown');
fakeSource.onopen();
statusListener({data: '{"status":"ok"}'});
disconnect();
assert.equal(closed, true);
assert.equal(listeners.size, 0);
statusListener({data: '{"status":"unavailable"}'});
assert.equal(journalStates.at(-1), 'ok');
assert.deepEqual(batches, [[event]]);
console.log('Pipeline telemetry: envelope parsing, known failures, boundary labels, partial outcomes and journal failure/recovery passed.');
