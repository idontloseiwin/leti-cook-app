// Home's System card and "Sync … ago" follow the honest collection rule.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';

function compiled(relative, imports) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }});
  const exports = {};
  new Function('require', 'exports', outputText)(name => {
    assert.ok(Object.hasOwn(imports, name), name);
    return imports[name];
  }, exports);
  return exports;
}

const format = compiled('../application/frontend/src/explorer/format.ts', {});
const home = compiled('../application/frontend/src/explorer/homeStatus.ts', {'./format': format});
const contracts = compiled('../application/frontend/src/explorer/contracts.ts', {});
const ready = payload => ({kind: 'ready', payload});

// System card: the server's status, with a short name for each collection signal.
assert.deepEqual(home.systemPresentation(ready({status: 'ok', status_reason: 'latest_run_carried_over', warnings: []})),
  {label: 'Healthy', tone: 'healthy', problem: false});
for (const [code, reason, message] of [
  ['collection_waiting_for_quota', 'Waiting for YouTube quota',
    'Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific.'],
  // One bucket ran out: the short reason names it; the
  // hover text says what goes on through the other bucket.
  ['collection_waiting_for_general_quota', 'Waiting for General quota',
    'General quota is exhausted; counters keep updating through Batch statistics. It resumes at midnight Pacific.'],
  ['collection_waiting_for_stats_quota', 'Waiting for Batch statistics quota',
    'Batch statistics quota is exhausted; video details, channel profiles and playlist checks continue through General. '
      + 'It resumes at midnight Pacific.'],
  ['collection_rate_limited', 'YouTube asked to slow down', 'YouTube asked to slow down; collection resumes on the next sync.'],
  ['no_full_pass', 'No full pass in 24 h',
    'No full pass in 24 h of running time; work is accumulating. Check quota use and the number of channels.'],
  ['collection_not_finishing', 'No sync finished recently',
    'No sync has finished for 2 h while the server was running. Check that automatic collection is running.'],
]) {
  assert.deepEqual(home.systemPresentation(ready({status: 'warning', status_reason: code, warnings: [{code, message}]})),
    {label: 'Warning', tone: 'warning', problem: true, reason, detail: message}, code);
}
assert.deepEqual(home.systemPresentation(ready({status: 'error', status_reason: 'latest_run_failed', warnings: []})),
  {label: 'Error', tone: 'error', problem: true, reason: 'Latest sync failed', detail: 'latest_run_failed'});
const long = 'Current video queue entries still have a recorded failure, and this message is long enough to be shortened.';
assert.equal(home.systemPresentation(ready({status: 'warning', warnings: [{code: 'video_queue_failures', message: long}]})).reason,
  long.slice(0, 89) + '…');
assert.equal(home.systemPresentation(ready({status: 'paused', warnings: [{code: 'collection_paused', message: 'Paused.'}]})).label, 'Paused');
assert.equal(home.systemPresentation({kind: 'loading'}).label, 'Checking…');
assert.equal(home.systemPresentation({kind: 'error', message: 'x'}).reason, 'System health unavailable');

// "Sync … ago" is the latest sync without errors; the last full pass is on hover.
const now = Date.parse('2026-11-08T12:00:00Z');
const summary = {last_successful_sync_at: '2026-11-07T10:00:00Z', last_sync_without_errors_at: '2026-11-08T11:48:00Z'};
const sync = home.homeSyncPresentation(ready(summary), now);
assert.equal(sync.value, 'Sync 12m ago');
assert.equal(sync.title, `${format.formatTimestamp('2026-11-08T11:48:00Z')} · Last full pass: ${format.formatTimestamp('2026-11-07T10:00:00Z')}`);
assert.match(home.homeSyncPresentation(ready({...summary, last_successful_sync_at: null}), now).title, /Last full pass: not recorded$/);
assert.deepEqual(home.homeSyncPresentation(ready({...summary, last_sync_without_errors_at: null}), now), {value: 'No successful sync'});
assert.deepEqual(home.homeSyncPresentation({kind: 'loading'}, now), {value: 'Loading…'});
assert.deepEqual(home.homeSyncPresentation({kind: 'error', message: 'x'}, now), {value: 'Sync unavailable'});
assert.equal(home.formatRelativeTime('2026-11-07T10:00:00Z', now), '26h ago');

// The home summary contract carries the new field and rejects a malformed one.
const wire = {
  contract_version: 1, last_successful_sync_at: null, last_sync_without_errors_at: '2026-11-08T11:48:00Z',
  last_data_change_at: null, saved_videos: null,
  source: {kind: 'stored_typed_video_inventory', format_source: 'video_inventory.video_type', scope: 'visible_channels',
    youtube_requests: false, database_writes: false},
};
assert.equal(contracts.parseExplorerHomeSummary(wire).last_sync_without_errors_at, '2026-11-08T11:48:00Z');
const missing = {...wire};
delete missing.last_sync_without_errors_at;
assert.throws(() => contracts.parseExplorerHomeSummary(missing), /last_sync_without_errors_at/);
assert.throws(() => contracts.parseExplorerHomeSummary({...wire, last_sync_without_errors_at: 5}), /last_sync_without_errors_at/);

// Home renders these helpers rather than its own copies.
const app = readFileSync(new URL('../application/frontend/src/explorer/ExplorerApp.tsx', import.meta.url), 'utf8');
assert.match(app, /import \{ formatRelativeTime, homeSyncPresentation, systemPresentation \} from '\.\/homeStatus';/);
assert.match(app, /<strong title=\{sync\.title\}>\{sync\.value\}<\/strong>/);
assert.doesNotMatch(app, /function (systemPresentation|shortReason|formatRelativeTime)\(/);
console.log('Home status and last sync follow the collection rule: passed');
