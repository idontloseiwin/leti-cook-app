import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';

function compiled(relative, imports) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
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

const healthApi = compiled('../application/frontend/src/api.ts', {'./sourceLifetime.ts': {}});
const websubPayload = JSON.parse(readFileSync(new URL('./fixtures/websub_health_wire.json', import.meta.url), 'utf8'));
assert.equal(healthApi.parseWebSubStatus(websubPayload), websubPayload);
for (const off of [{configured: false}, {configured: false, setting_error: 'websub_callback_url_invalid'}]) {
  assert.equal(healthApi.parseWebSubStatus(off), off);
}
for (const invalid of [null, {}, {configured: 'false'}, {configured: false, summary: {}}, {configured: true},
  {...websubPayload, summary: null}, ...[{deliveries_24h: true}, {events_24h: []},
    {subscriptions: {verified: -1}}, {events_pending: 0.1}, {last_delivery_at: 'yesterday'},
    {ramp_stage: '0'}, {breaker_open: 1}].map(change =>
      ({...websubPayload, summary: {...websubPayload.summary, ...change}}))]) {
  assert.throws(() => healthApi.parseWebSubStatus(invalid), /Invalid WebSub/);
}

const explanation = compiled('../application/frontend/src/collectionExplanation.ts', {});
const presentation = compiled('../application/frontend/src/pipelineHealthPresentation.ts', {
  './collectionExplanation': explanation,
});
const quota = compiled('../application/frontend/src/quotaPresentation.ts', {});
const serverText = compiled('../application/frontend/src/serverText.ts', {});
const bucket = (due, failed = 0) => ({due, failed, total: 10, leased: 0, paused: 0});
const base = {
  status: 'ok', generated_at: new Date().toISOString(), window_hours: 24,
  warnings: [], critical: [], collection_errors: [],
  runtime: {database_video_collection_enabled: true, database_channel_discovery_enabled: true, batch_stats_enabled: true},
  channels: {total: 865, visible: 863},
  queues: {
    stats: {mode: 'leased_video_collection_queue', scope: 'all_types', unit: 'videos',
      due: 5, all_due: 5, total: 30, leased: 0, failed: 0,
      by_type: {long: bucket(1), short: bucket(2), stream: bucket(2)}},
    channel_discovery: {mode: 'leased_channel_discovery_queue', scope: 'all_types', unit: 'playlists',
      due: 3, all_due: 3, total: 30, total_channels: 10, leased: 0, running: 0, failed: 0, playlist_not_found: 134,
      by_type: {long: bucket(0), short: bucket(2), stream: bucket(1)}},
    channel_metadata: {due: 0, running: 0, failed: 0},
  },
  latest_sync: {status: 'completed', errors: 0, stats_updated: 73, run_kind: 'complete',
    finished_at: '2026-10-22T10:00:00+00:00', duration_seconds: 125,
    explanation: {scope: 'recorded_run', current_recovery: 'not_evaluated', next_attempt: 'stored_queue', reasons: []}},
  last_successful_sync: {status: 'completed', errors: 0, run_kind: 'complete', finished_at: '2026-10-22T10:00:00+00:00', duration_seconds: 125},
  last_sync_without_errors: {status: 'completed', errors: 0, run_kind: 'complete', finished_at: '2026-10-22T10:00:00+00:00', duration_seconds: 125},
  server_started_at: '2026-10-22T08:00:00+00:00', collection_gap: null,
  recent_syncs: {count: 10, error_total: 2, status_counts: {completed: 7, partial: 2, error: 1}},
  history: {scope: 'recorded_windows', recovery: 'not_correlated', sync_errors: 2,
    transport_errors: 1, transport_read_status: 'ok', quota_day: '2026-10-22'},
  youtube_quota: {read_status: 'ok', quota_day: '2026-10-22', attempts: 10,
    bucket_units: {general: 6, video_batch_get_stats: 4},
    configured_bucket_limits: {general: 10000, video_batch_get_stats: 10000},
    estimated_bucket_remaining: {general: 9994, video_batch_get_stats: 9996},
    status_counts: {success: 9, transport_error: 1}, unfinished: {count: 0, stale_count: 0}},
};

assert.equal(presentation.parsePipelineHealth(base), base);
assert.equal(presentation.healthAssessment(base, false), 'ok');
assert.equal(presentation.healthAssessment(base, true), 'unknown');
// An unknown control state retains the existing API/browser contract.
// It does not invent boolean exhaustion flags or hide the reason in a parse error.
const unknownControl = {...base, status: 'warning',
  warnings: ['Quota state is unavailable; collector requests are paused until it can be read.'],
  quota_control: {status: 'unavailable', reason: 'quota_state_unavailable', scope: 'collector',
    exhausted_buckets: {general: null, video_batch_get_stats: null}},
};
assert.equal(presentation.parsePipelineHealth(unknownControl), unknownControl);
assert.equal(presentation.healthAssessment(unknownControl, false), 'warning');
assert.equal(Object.hasOwn(unknownControl.youtube_quota, 'exhausted_buckets'), false);
// Due work is normal: neutral, not yellow; failures and expired leases stay yellow.
assert.deepEqual(presentation.healthQueueState(base.queues.stats, 'stats'), {tone: 'neutral', label: '5 due'});
assert.deepEqual(presentation.healthQueueState({...base.queues.stats, expired_leases: 1}, 'stats'), {tone: 'warning', label: '1 expired leases'});
assert.deepEqual(presentation.healthQueueState({...base.queues.stats, all_due: 0, leased: 0}, 'stats'), {tone: 'ok', label: 'No work due'});
for (const [kind, tone, label] of [
  ['complete', 'ok', 'Complete'], ['carried_over', 'neutral', 'Partial · work carried over'],
  ['quota', 'warning', 'Stopped: waiting for quota'], ['rate_limited', 'warning', 'Stopped: rate limited'],
  ['failed', 'critical', 'Failed'], ['running', 'neutral', 'Running'],
]) assert.deepEqual(presentation.healthRunState({run_kind: kind}), {tone, label}, kind);
assert.deepEqual(presentation.healthRunState(undefined), {tone: 'unknown', label: 'Not recorded'});
assert.deepEqual(presentation.healthQueueState(undefined, 'stats'), {tone: 'unknown', label: 'Unavailable'});
assert.deepEqual(presentation.healthQueueState({all_due: null, leased: 0}, 'stats'), {tone: 'unknown', label: 'Unavailable'});
assert.deepEqual(presentation.healthQueueState(base.queues.stats, 'stats', 'read failed'), {tone: 'unknown', label: 'Unavailable'});
assert.deepEqual(presentation.healthQueueState({...base.queues.stats, all_due: 0, failed: 2}, 'stats'), {tone: 'warning', label: '2 failed'});
for (const mutate of [
  value => { delete value.queues.stats.by_type.short; },
  value => { value.queues.stats.by_type.untrusted = bucket(1); },
  value => { value.queues.stats.by_type.short.due = true; },
  value => { value.queues.stats.unit = 'channels'; },
  value => { value.history.transport_errors = -1; },
  value => { value.history.recovery = 'recovered'; },
  value => { value.history.transport_read_status = 'guessed'; },
  value => { value.latest_sync.explanation.reasons = [{code: 'secret', count: 1, stage: 'run'}]; },
  value => { value.latest_sync.explanation.reasons = [{code: 'metadata_followup', count: 1, stage: 'secret'}]; },
  value => { value.latest_sync.explanation.current_recovery = 'recovered'; },
  value => { value.youtube_quota.exhausted_buckets = {general: 'yes'}; },
  value => { value.latest_sync.run_kind = 'secret'; },
  value => { value.last_sync_without_errors.finished_at = 'yesterday'; },
  value => { value.server_started_at = 'at boot'; },
  value => { value.collection_gap = {server_started_at: '2026-10-22T08:00:00Z', collection_last_ran_at: 'never', hours: 2}; },
  value => { value.collection_gap = {server_started_at: '2026-10-22T08:00:00Z', collection_last_ran_at: '2026-10-21T08:00:00Z', hours: '24'}; },
]) {
  const invalid = structuredClone(base);
  mutate(invalid);
  assert.throws(() => presentation.parsePipelineHealth(invalid));
}

// Render real Health components through inert JSX/hooks, with no network/timers.
let fixture = base;
let readError;
const jsx = (type, props) => ({type, props});
const page = compiled('../application/frontend/src/explorer/HealthPage.tsx', {
  'react/jsx-runtime': {jsx, jsxs: jsx, Fragment: ({children}) => children},
  'lucide-react': {}, '../api': {loadPipelineHealth() {}},
  '../components/ProviderKeyBanner': {ProviderKeyBanner: () => null, providerKeyRefusal: () => undefined},
  '../components/VideoTypeReviewPanel': {VideoTypeReviewDisclosure: () => null},
  '../components/YouTubeAttribution': {YouTubeAttribution: () => null},
  '../format': {formatDateTime: value => value ?? '—', formatNumber: value => String(value), formatRelative: value => `rel(${value})`},
  '../pipelineHealthPresentation': presentation, '../quotaPresentation': quota,
  '../serverText': serverText, '../collectionExplanation': explanation,
  './useOperationalRead': {useOperationalRead: () => ({payload: fixture, error: readError, refresh() {}})},
  './health.css': {},
});
function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join(' ').replace(/\s+/g, ' ');
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'object') {
    if (typeof node.type === 'function') return textOf(node.type(node.props));
    return textOf(node.props?.children);
  }
  return String(node);
}
// Each Flag as [tone, text]: the tone is what the owner sees as its colour.
function flags(node, found = []) {
  if (Array.isArray(node)) { node.forEach(child => flags(child, found)); return found; }
  if (!node || typeof node !== 'object') return found;
  if (typeof node.type === 'function') {
    if (node.type.name === 'Flag') found.push([node.props.tone ?? 'unknown', textOf(node.props.children)]);
    return flags(node.type(node.props), found);
  }
  return flags(node.props?.children, found);
}
const fresh = textOf(page.HealthPage());
assert.ok(fresh.includes('Last sync rel(2026-10-22T10:00:00+00:00) Full pass · 2m 5s duration'));
assert.ok(!fresh.includes('Last successful sync'));
assert.ok(!fresh.includes('without collection'));
assert.deepEqual(flags(page.HealthPage()).filter(([, text]) => /due|Complete/.test(text)),
  [['neutral', '5 due'], ['neutral', '3 due'], ['ok', 'No work due'], ['ok', 'Complete']]);
for (const text of ['Healthy', 'Videos due 5', 'All formats', 'Shorts', 'Streams',
  'Total playlists 30', 'Distinct channels 10', 'Public playlist not found 134',
  '2 recorded errors', '1 recorded transport errors in this quota day',
  'A successful unrelated sync does not confirm that an affected item recovered.']) {
  assert.ok(fresh.includes(text), text);
}
assert.ok(!fresh.includes('Needs attention'));
assert.ok(!fresh.includes('recent sync window contains errors'));
assert.ok(!fresh.includes('current quota day contains YouTube transport errors'));
assert.ok(!fresh.includes('134 failed'));

// A partial sync without errors is normal work: Healthy, calm label, reasons kept.
const partialFixture = () => {
  const value = structuredClone(base);
  value.latest_sync = {...value.latest_sync, status: 'partial', run_kind: 'carried_over', finished_at: '2026-10-23T11:50:00+00:00'};
  value.latest_sync.explanation.reasons = [{code: 'metadata_followup', count: 7, stage: 'video_collection'}];
  value.last_sync_without_errors = {...value.latest_sync};
  return value;
};
fixture = partialFixture();
const partial = textOf(page.HealthPage());
assert.ok(partial.includes('Healthy'));
assert.ok(!partial.includes('Needs attention'));
assert.ok(partial.includes('Last sync rel(2026-10-23T11:50:00+00:00) Full pass rel(2026-10-22T10:00:00+00:00)'));
assert.ok(flags(page.HealthPage()).some(([tone, text]) => tone === 'neutral' && text === 'Partial · work carried over'));
assert.ok(partial.includes('7 videos required metadata and availability checks after a statistics response.'));
assert.ok(partial.includes('does not confirm recovery or the next attempt time'));
assert.ok(!partial.includes('metadata_followup'));
fixture.last_successful_sync = null;
assert.ok(textOf(page.HealthPage()).includes('No full pass recorded'));

// A quota stop waits in yellow; it is not a red failure.
fixture = structuredClone(base);
fixture.status = 'warning';
fixture.latest_sync = {...fixture.latest_sync, status: 'error', errors: 1, run_kind: 'quota'};
fixture.warnings = ['Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific.'];
const quotaStop = textOf(page.HealthPage());
assert.ok(quotaStop.includes('Needs attention'));
assert.ok(!quotaStop.includes('Critical'));
assert.ok(quotaStop.includes('resumes after the daily reset at midnight Pacific'));
assert.ok(flags(page.HealthPage()).some(([tone, text]) => tone === 'warning' && text === 'Stopped: waiting for quota'));
assert.ok(!flags(page.HealthPage()).some(([, text]) => text === 'Failed' || text === 'Error'));

// Downtime is stated once, neutrally; it is not a warning.
fixture = structuredClone(base);
fixture.collection_gap = {server_started_at: '2026-10-22T08:00:00+00:00', collection_last_ran_at: '2026-10-21T10:00:00+00:00', hours: 22};
const downtime = textOf(page.HealthPage());
for (const text of ['Server started rel(2026-10-22T08:00:00+00:00)', 'Before that, collection last ran 2026-10-21T10:00:00+00:00',
  '22h without collection. Warnings do not count that time.']) assert.ok(downtime.includes(text), text);
assert.ok(downtime.includes('Healthy'));

fixture = structuredClone(base);
fixture.status = 'critical';
fixture.collection_errors = [{component: 'stats_queue', error_type: 'DatabaseError'}];
fixture.history.transport_errors = null;
fixture.history.transport_read_status = 'unavailable';
const unavailable = textOf(page.HealthPage());
assert.ok(unavailable.includes('Critical'));
assert.ok(unavailable.includes('Videos due —'));
assert.ok(unavailable.includes('This read is unavailable'));
assert.ok(unavailable.includes('— recorded transport errors'));
assert.ok(!unavailable.includes('0 recorded transport errors'));

// An exhausted bucket says so and when it resumes; the other one does not.
assert.equal(quota.pipelineQuotaPresentation(base.youtube_quota, 'general').exhausted, false);
fixture = structuredClone(base);
fixture.youtube_quota.exhausted_buckets = {general: true, video_batch_get_stats: false};
fixture.youtube_quota.next_reset_at = '2026-10-23T07:00:00+00:00';
assert.equal(quota.pipelineQuotaPresentation(fixture.youtube_quota, 'general').exhausted, true);
assert.equal(quota.pipelineQuotaPresentation(fixture.youtube_quota, 'video_batch_get_stats').exhausted, false);
assert.equal(presentation.parsePipelineHealth(fixture), fixture);
const exhausted = textOf(page.HealthPage());
for (const text of ['Exhausted · YouTube refused this bucket today. It resumes at midnight Pacific (',
  '2026-10-23T07:00:00+00:00', 'your time); the other bucket keeps collecting.']) assert.ok(exhausted.includes(text), text);
assert.equal(exhausted.split('Exhausted · ').length, 2);

fixture = base;
readError = 'A later read failed';
const stale = textOf(page.HealthPage());
assert.ok(stale.includes('Stale reading'));
assert.ok(stale.includes('The last reading is not current health.'));
assert.ok(!stale.includes('No active critical or warning signals.'));
console.log('Health all-type queues, honest collection state, history, explanations, malformed and stale reads passed');

if (process.argv[2] === '--preview' && process.argv[3]) {
  fixture = partialFixture();
  readError = undefined;
  fixture.collection_gap = {server_started_at: '2026-10-22T08:00:00+00:00', collection_last_ran_at: '2026-10-21T10:00:00+00:00', hours: 22};
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  function html(node) {
    if (Array.isArray(node)) return node.map(html).join('');
    if (node === null || node === undefined || typeof node === 'boolean') return '';
    if (typeof node !== 'object') return escape(node);
    if (typeof node.type === 'function') return html(node.type(node.props));
    if (typeof node.type !== 'string') return '';
    const attributes = Object.entries(node.props ?? {}).filter(([key, value]) =>
      !['children', 'key', 'ref'].includes(key) && !key.startsWith('on') && value != null && value !== false
    ).map(([key, value]) => {
      if (key === 'style' && typeof value === 'object') value = Object.entries(value).map(([name, setting]) =>
        `${name.replace(/[A-Z]/g, char => '-' + char.toLowerCase())}:${setting}`).join(';');
      return value === true ? ` ${key}` : ` ${key === 'className' ? 'class' : key}="${escape(value)}"`;
    }).join('');
    return `<${node.type}${attributes}${node.type === 'details' ? ' open' : ''}>${html(node.props?.children)}</${node.type}>`;
  }
  const css = readFileSync(new URL('../application/frontend/src/explorer/health.css', import.meta.url), 'utf8');
  const variables = ':root{--explorer-bg:#080f13;--explorer-surface:#101820;--explorer-text:#e7f3fc;--explorer-muted:#87a9bd;--explorer-mint:#64e4c0;--explorer-blue:#53bafa;--explorer-amber:#e9b45b;--explorer-red:#f07b83;--explorer-line:#29414f;--explorer-line-soft:#1e303b}body{margin:0;background:var(--explorer-bg);font-family:Arial,sans-serif}*{box-sizing:border-box}.fixture-label{margin:0;padding:12px 32px;color:#e9b45b;background:#172124;font-size:12px}';
  writeFileSync(process.argv[3], `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Health · synthetic review fixture</title><style>${variables}\n${css}</style><body><p class="fixture-label">Synthetic Health preview · fixture values, not live server data</p><div class="explorer-app is-updated">${html(page.HealthPage())}</div></body></html>`, 'utf8');
  console.log(`Synthetic Health HTML saved: ${process.argv[3]}`);
}
