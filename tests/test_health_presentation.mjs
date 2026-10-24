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
  latest_sync: {status: 'completed', errors: 0, stats_updated: 73,
    explanation: {scope: 'recorded_run', current_recovery: 'not_evaluated', next_attempt: 'stored_queue', reasons: []}},
  last_successful_sync: {status: 'completed', errors: 0},
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
assert.deepEqual(presentation.healthQueueState(base.queues.stats, 'stats'), {tone: 'warning', label: '5 due'});
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
  '../format': {formatDateTime: value => value ?? '—', formatNumber: value => String(value), formatRelative: () => 'recent'},
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
const fresh = textOf(page.HealthPage());
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

fixture = structuredClone(base);
fixture.status = 'warning';
fixture.latest_sync.status = 'partial';
fixture.latest_sync.explanation.reasons = [{code: 'metadata_followup', count: 7, stage: 'video_collection'}];
fixture.warnings = ['latest recorded sync left admitted work unresolved; see its recorded reasons'];
const partial = textOf(page.HealthPage());
assert.ok(partial.includes('Needs attention'));
assert.ok(partial.includes('7 videos required metadata and availability checks after a statistics response.'));
assert.ok(partial.includes('does not confirm recovery or the next attempt time'));
assert.ok(!partial.includes('metadata_followup'));

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

fixture = base;
readError = 'A later read failed';
const stale = textOf(page.HealthPage());
assert.ok(stale.includes('Stale reading'));
assert.ok(stale.includes('The last reading is not current health.'));
assert.ok(!stale.includes('No active critical or warning signals.'));
console.log('Health all-type queues, history, explanations, malformed and stale reads passed');

if (process.argv[2] === '--preview' && process.argv[3]) {
  fixture = structuredClone(base);
  readError = undefined;
  fixture.status = 'warning';
  fixture.latest_sync.status = 'partial';
  fixture.latest_sync.explanation.reasons = [{code: 'metadata_followup', count: 7, stage: 'video_collection'}];
  fixture.warnings = ['latest recorded sync left admitted work unresolved; see its recorded reasons'];
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
