// Library Inspector adapter: 's per-video operations
// and 's growth evidence → the existing Inspector panels, for all three
// types; frozen opening (Library page) and query opening (saved collection);
// strict refusal of unknown shapes; exact (BigInt) counters; per-type states;
// busy retries. Offline: real wire examples, no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as contracts from '../application/frontend/src/explorer/contracts.ts';
import * as source from '../application/frontend/src/sourceLifetime.ts';
import * as inspector from '../application/frontend/src/explorer/libraryData/inspector.ts';
import { libraryGates } from '../application/frontend/src/explorer/libraryData/capabilities.ts';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const contract = (fn, pattern) => assert.throws(fn, (error) => error instanceof ExplorerContractError
  && (!pattern || pattern.test(error.message)), String(pattern ?? 'contract error'));
const notMember = (fn) => assert.throws(fn, inspector.InspectorNotInPopulation);
const clone = (value) => structuredClone(value);
const fixture = JSON.parse(read('./fixtures/library_inspector_wire.json'));
const op = (name) => clone(fixture.operations[name].response);
const CHANNEL = 'UCWPDMAINAAAAAAAAAAAAAAA';
// Instants follow the clock of the run that generated the fixture (tests/library_wire_fixtures_pg.py): they are read
// from it; counts, values and their relations stay literal.
const CLOCKS = fixture.operations['video.static_details.v1:long:frozen'].response.result.clocks;
const SHORT_CLOCKS = fixture.operations['video.static_details.v1:short:query'].response.result.clocks;
const frozen = (videoType = 'long', extra = {}) => ({
  channelId: CHANNEL, videoType, frozenRef: fixture.frozen_ref, sessionKey: 'session-1', checkedAt: CLOCKS.counters_checked_at,
  active: true, ...extra,
});
const query = (videoType, scope = { kind: 'channel', id: CHANNEL }, extra = {}) => ({
  kind: 'query', channelId: CHANNEL, videoType, scope, checkedAt: null, active: true, ...extra,
});
// An instant `seconds` after (or before) another, in the server's microsecond form.
const later = (iso, seconds) => {
  const [whole, fraction = ''] = iso.replace('Z', '').split('.');
  return new Date(Date.parse(whole + 'Z') + seconds * 1000).toISOString().slice(0, 19) + (fraction ? '.' + fraction : '') + 'Z';
};
const expectFrozen = (videoId = 'L0000000020', videoType = 'long', more = {}) => ({ videoId, videoType, mode: 'frozen', ...more });
const expectQuery = (videoId, videoType, more = {}) => ({ videoId, videoType, mode: 'query', ...more });

// Presentation modules use extensionless imports: compile them with their dependencies injected.
const compile = (path) => ts.transpileModule(read(path), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const moduleWith = (path, imports) => {
  const exports = {};
  new Function('require', 'exports', compile(path))((name) => {
    assert.ok(name in imports, name);
    return imports[name];
  }, exports);
  return exports;
};
const sharedFormat = moduleWith('../application/frontend/src/format.ts', {});
const format = moduleWith('../application/frontend/src/explorer/format.ts', { '../format': sharedFormat, '../format.ts': sharedFormat });
const localTime = { formatExactLocal: (value, missing = 'Not recorded') => value ?? missing, formatTimestampLocal: (value, missing = 'Not available') => value ?? missing };
const videoGrowth = moduleWith('../application/frontend/src/explorer/videoGrowth.ts', {});
const history = moduleWith('../application/frontend/src/explorer/videoHistory.ts', {
  './contracts': contracts, './format': format, './localTime': localTime, '../sourceLifetime': source, './videoGrowth': videoGrowth,
});
const launch = moduleWith('../application/frontend/src/explorer/videoLaunch.ts', {
  './contracts': contracts, './format': format, '../sourceLifetime': source,
});
const baseline = moduleWith('../application/frontend/src/explorer/videoLifetimeBaseline.ts', {
  './contracts': contracts, '../sourceLifetime': source,
});
// "Observed views" presentation lives in videoHistory.ts (one file fewer in the package).
const growthText = history;

// ---------------------------------------------------------------------------
// Requests: the exact Inspector shapes; launch and baseline always the plain channel query.
{
  const { INSPECTOR_OPERATIONS: ops } = inspector;
  for (const [name, entry] of Object.entries(fixture.operations)) {
    const [operation, type, mode] = name.split(':');
    const video = entry.request.parameters.video_id;
    const opening = mode === 'frozen' ? frozen(type) : query(type);
    assert.deepEqual(inspector.inspectorRequest(operation, opening, video), entry.request, name);
  }
  const collection = query('short', { kind: 'collection', id: '0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3' });
  assert.deepEqual(inspector.inspectorRequest(ops.staticDetails, collection, 'H0000000001').query.scope, collection.scope);
  assert.deepEqual(inspector.inspectorRequest(ops.history, collection, 'H0000000001').query.scope, collection.scope);
  for (const operation of [ops.launch, ops.baseline]) {
    const request = inspector.inspectorRequest(operation, collection, 'H0000000001');
    assert.deepEqual(request.query.scope, { kind: 'channel', id: CHANNEL }, 'the channel query, never the opening scope');
    assert.ok(!('where' in request.query) && !('time' in request.query), 'no Library conditions');
  }
  assert.deepEqual(inspector.inspectorRequest(ops.staticDetails, frozen(), 'L0000000020', { description: true }).parameters,
    { video_id: 'L0000000020', population: { kind: 'frozen', frozen_ref: fixture.frozen_ref }, description: true });
  assert.ok(!('description' in inspector.inspectorRequest(ops.staticDetails, frozen(), 'L0000000020', { description: false }).parameters));
  assert.throws(() => inspector.inspectorRequest(ops.history, frozen(), 'L0000000020', { description: true }));
  assert.throws(() => inspector.inspectorRequest(ops.history, frozen('long', { channelId: 'UCshort' }), 'L0000000020'));
  assert.throws(() => inspector.inspectorRequest(ops.history, frozen('long', { frozenRef: 'not a token' }), 'L0000000020'));
  assert.throws(() => inspector.inspectorRequest(ops.history, frozen('clip'), 'L0000000020'));
  assert.throws(() => inspector.inspectorRequest(ops.history, query('long', { kind: 'channel', id: 'UC' + 'B'.repeat(22) }), 'L0000000020'));
  assert.throws(() => inspector.inspectorRequest(ops.history, frozen(), 'short'));
  assert.deepEqual(inspector.INSPECTOR_FROZEN_FIELDS, ['video.id', 'video.title', 'video.thumbnail_url', 'video.duration_seconds',
    'video.views', 'video.likes', 'video.comments', 'video.published_at', 'video.category_id', 'video.tags']);
}
console.log('inspector requests: Inspector shapes, frozen_ref, channel query, description flag');

// ---------------------------------------------------------------------------
// Real wire → panel presentation.
const staticLong = 'video.static_details.v1:long:frozen', staticShort = 'video.static_details.v1:short:query';
const historyLong = 'video.observed_history.v1:long:frozen';
const launchLong = 'video.launch_comparison.v1:long:frozen', launchStream = 'video.launch_comparison.v1:stream:query';
const baselineLong = 'video.lifetime_baseline.v1:long:frozen';
{
  assert.deepEqual(inspector.parseStaticDetails(op(staticLong), expectFrozen()), {
    video_type: 'long', like_rate: { state: 'known', value: '4.299504' }, comment_rate: { state: 'unknown' },
    title_length: { state: 'known', value: 15 }, tag_count: { state: 'known', value: 1 }, tags: { state: 'known', value: ['a'] },
    metadata_checked_at: CLOCKS.metadata_checked_at, description: null,
  });
  assert.deepEqual(inspector.parseStaticDetails(op(staticShort), expectQuery('H0000000001', 'short')), {
    video_type: 'short', like_rate: { state: 'known', value: '3.699969' }, comment_rate: { state: 'known', value: '0.198863' },
    title_length: { state: 'known', value: 12 }, tag_count: { state: 'known', value: 3 }, tags: { state: 'known', value: ['Straße', 'b', 'c'] },
    metadata_checked_at: SHORT_CLOCKS.metadata_checked_at, description: null,
  });

  const view = inspector.parseObservedHistory(op(historyLong), expectFrozen());
  const wire = fixture.operations[historyLong].response.result;
  assert.equal(view.status, 'ready');
  assert.deepEqual(view.window, { from_at: wire.window.from_at, to_at: CLOCKS.counters_checked_at });
  assert.equal(view.points.length, 69);
  assert.deepEqual(view.points[0], { recorded_at: wire.points[0].recorded_at, views: '82' });
  assert.deepEqual(view.intervals[0], { from_at: wire.points[0].recorded_at, to_at: wire.points[1].recorded_at,
    actual_interval_seconds: '3513', delta_views: '2178', views_per_day: '53566.52', definition: 'known' });
  assert.deepEqual(view.latest_interval, { from_at: wire.points.at(-2).recorded_at, to_at: CLOCKS.counters_checked_at,
    actual_interval_seconds: '43106', delta_views: '312', views_per_day: '625.36' });
  assert.deepEqual(view.coverage, { point_count: 69, valid_view_count: 69, missing_view_count: 0, duplicate_timestamp_count: 0 });
  assert.equal(view.interaction_metrics.likes.status, 'ready');
  assert.equal(view.interaction_metrics.likes.latest_interval.per_day, '28.06');
  assert.equal(view.interaction_metrics.comments.coverage.valid_count, 0);
  assert.equal(view.interaction_metrics.comments.latest_interval, null);
  // The panel helpers draw the adapter's view exactly as they drew the old payload.
  const series = history.videoHistorySeries(view, 'views');
  assert.equal(series.points.length, 69);
  assert.equal(series.points.at(-1).value, '72520');
  assert.equal(history.historyIntervalRate(series.latest_interval, 'views'), '~+625.36 views/day');
  assert.equal(history.videoHistorySeries(view, 'likes').points.at(-1).value, '3118');
  assert.ok(history.videoHistoryGeometry(view, 'interval', 'views'));
  assert.equal(history.videoHistoryGeometry(view, 'cumulative', 'comments'), null, 'no known comments: no chart');

  const launchView = inspector.parseLaunchComparison(op(launchLong), expectFrozen());
  assert.equal(launchView.video_type, 'long');
  const launchWire = fixture.operations[launchLong].response.result;
  assert.deepEqual(launchView.window, { from_at: launchWire.window.from_at, to_at: launchWire.window.to_at });
  assert.equal(launchView.window.to_at, fixture.operations[staticLong].response.result.inspection.evaluated_at, 'the 28 days end at the frozen clock');
  assert.deepEqual(launchView.coverage, { eligible_video_count: 138, excluded_video_count: 2 });
  assert.deepEqual(launchView.horizons.map((item) => [item.key, item.eligible_count, item.quality, item.target.status, item.target.comparison.status]),
    [['24h', 56, 'ready', 'ready', 'ready'], ['7d', 61, 'ready', 'ready', 'ready'], ['28d', 47, 'ready', 'not_reached', 'unavailable']]);
  assert.deepEqual(launchView.horizons[0].target, {
    status: 'ready', target_at: launchWire.horizons[0].target.target_at, observed_at: launchWire.horizons[0].target.observed_at, actual_age_seconds: '82934',
    views: '36673', comparison: { status: 'ready', reference_count: 55, median: '25556', p25: '12342.5', p75: '35514',
      classification: 'above_typical', multiplier: '1.44', multiplier_reason: null } });
  assert.equal(launch.launchMultiplierLabel(launchView.horizons[0].target.comparison, '36673'), '1.44× typical');
  const streamView = inspector.parseLaunchComparison(op(launchStream), expectQuery('S0000000005', 'stream'));
  assert.equal(streamView.video_type, 'stream');
  assert.deepEqual(streamView.horizons.map((item) => item.target.comparison.classification), ['below_typical', 'below_typical', null]);

  assert.deepEqual(inspector.parseLifetimeBaseline(op(baselineLong), expectFrozen()), {
    video_type: 'long', status: 'ready', sample_size: 137, missing_views: 1, median: '58989', p25: '37801', p75: '83293',
    excluded_count: 2, definition: 'known', unknown_definition_count: 0 });
  // The server reply names the count of observations with an unknown definition beside the state; it is read, never refused.
  assert.equal(fixture.operations[baselineLong].response.result.definition.unknown_definition_epoch_count, 0);
}
console.log('inspector parsers: real static/history/launch/baseline for Long, Shorts and Streams');

// ---------------------------------------------------------------------------
// Strict refusal of unknown shapes and broken invariants.
{
  const cases = [
    [staticLong, (r, expect = expectFrozen()) => inspector.parseStaticDetails(r, expect)],
    [historyLong, (r, expect = expectFrozen()) => inspector.parseObservedHistory(r, expect)],
    [launchLong, (r, expect = expectFrozen()) => inspector.parseLaunchComparison(r, expect)],
    [baselineLong, (r, expect = expectFrozen()) => inspector.parseLifetimeBaseline(r, expect)],
  ];
  for (const [name, parse] of cases) {
    parse(op(name));
    // A frozen answer is not a query answer, nor one of another video or type.
    contract(() => parse(op(name), expectQuery('L0000000020', 'long')), /expected query/);
    contract(() => parse(op(name), expectFrozen('L0000000021')), /another video/);
    contract(() => parse(op(name), expectFrozen('L0000000020', 'short')), /another video type/);
    const mutate = (change) => { const value = op(name); change(value); return value; };
    contract(() => parse(mutate((r) => { r.extra = 1; })), /unknown key/);
    contract(() => parse(mutate((r) => { r.context.cursor = 'x'; })), /unknown key/);
    contract(() => parse(mutate((r) => { r.result.score = 1; })), /unknown key/);
    contract(() => parse(mutate((r) => { delete r.result.inspection; })), /missing key/);
    contract(() => parse(mutate((r) => { r.operation = 'video.summary.v1'; })));
    contract(() => parse(mutate((r) => { r.provenance.method = 'video.summary.v1'; })));
    contract(() => parse(mutate((r) => { r.version = 2; })));
    contract(() => parse(mutate((r) => { r.context.consistency = 'frozen'; })));
    contract(() => parse(mutate((r) => { r.result.video_id = 'L0000000021'; })), /another video/);
    contract(() => parse(mutate((r) => { r.result.video_type = 'short'; })), /another video type/);
    contract(() => parse(mutate((r) => { r.result.membership = 'maybe'; })));
    contract(() => parse(mutate((r) => { r.result.inspection.position = 138; })), /outside the frozen result/);
    contract(() => parse(mutate((r) => { r.context.valid_before = later(r.context.valid_before, 3600); })), /outlives the frozen result/);
    contract(() => parse(mutate((r) => { r.coverage.reasons = { source_rows_unavailable: 'two' }; })));
    contract(() => parse(mutate((r) => { r.coverage.status = 'complete'; })), /complete scope/);
    contract(() => parse(mutate((r) => { r.source_expires_at = later(r.context.valid_before, 7200); })), /outlives/);
    parse(mutate((r) => { r.source_read_at = r.context.evaluated_at; r.source_expires_at = r.context.valid_before; }));
  }
  contract(() => inspector.parseStaticDetails(op(staticLong), expectQuery('L0000000020', 'long')));
  contract(() => inspector.parseStaticDetails(op(staticShort), expectFrozen('H0000000001', 'short')));
  contract(() => inspector.parseStaticDetails(op(staticLong), expectFrozen('L0000000020', 'stream')));

  const staticMutation = (change) => { const r = op(staticLong); change(r.result); return () => inspector.parseStaticDetails(r, expectFrozen()); };
  contract(staticMutation((r) => { r.derived['video.like_rate'].value = '4.2995'; }), /not the stored rate/);
  contract(staticMutation((r) => { r.derived['video.like_rate'].value = '4.2995041'; }), /expected a rate/);
  contract(staticMutation((r) => { r.derived['video.title_length'].value = '14'; }), /title length/);
  contract(staticMutation((r) => { r.derived['video.tag_count'].value = '2'; }), /tag count/);
  contract(staticMutation((r) => { r.cells['video.views'].value = 72520; }), /unsigned decimal/);
  contract(staticMutation((r) => { r.cells['video.views'].state = 'stale'; }));
  contract(staticMutation((r) => { r.cells['video.comments'].value = '5'; }), /carries no value/);
  contract(staticMutation((r) => { r.cells['video.description'] = r.cells['video.title']; }), /unknown key/);
  contract(staticMutation((r) => { r.clocks.metadata_checked_at = r.clocks.counters_checked_at; }), /metadata clock/);
  contract(staticMutation((r) => { r.category.id = '11'; }), /another category/);
  contract(staticMutation((r) => { r.methods.rates = 'float'; }));

  const historyMutation = (change) => { const r = op(historyLong); change(r.result); return () => inspector.parseObservedHistory(r, expectFrozen()); };
  contract(historyMutation((r) => { r.series.views.intervals[3].delta = '1'; }), /exact change/);
  contract(historyMutation((r) => { const i = r.series.views.intervals[3]; i.per_day = (Number(i.per_day) + 0.01).toFixed(2); }), /exact change/);
  contract(historyMutation((r) => { r.series.views.intervals[3].actual_interval_seconds = '3600'; }), /exact interval/);
  contract(historyMutation((r) => { r.series.views.intervals[3].definition = 'incompatible_definition'; }), /match the points/);
  contract(historyMutation((r) => { r.series.views.latest_interval.delta = '311'; }), /last interval/);
  contract(historyMutation((r) => { r.series.views.coverage.valid_count = 68; }), /match the points/);
  contract(historyMutation((r) => { r.points.reverse(); }), /out of order/);
  contract(historyMutation((r) => { r.points.pop(); }), /end at the counter observation|match the points/);
  contract(historyMutation((r) => { r.window.from_at = later(r.window.from_at, -1); }), /28-day window/);
  contract(historyMutation((r) => { r.window.rule = 'strict_28d_not_card_comparison_interval'; }), /unexpected window/);
  contract(historyMutation((r) => { r.window.endpoint = 'catalogue'; }));
  contract(historyMutation((r) => { r.status = 'unavailable'; }), /views series state/);
  contract(historyMutation((r) => { r.series.likes.reason = 'gone'; }));

  const launchMutation = (change) => { const r = op(launchLong); change(r.result); return () => inspector.parseLaunchComparison(r, expectFrozen()); };
  contract(launchMutation((r) => { r.horizons[0].target.comparison.multiplier = '1.45'; }), /exact ratio/);
  contract(launchMutation((r) => { r.horizons[0].target.comparison.classification = 'typical'; }), /quartiles/);
  contract(launchMutation((r) => { r.horizons[0].target.comparison.p25 = '12342.3'; }), /0\.25/);
  contract(launchMutation((r) => { r.horizons[0].target.comparison.reference_count = 56; }), /comparison of this horizon/);
  contract(launchMutation((r) => { r.horizons[0].counts.typical = 26; }), /add up/);
  contract(launchMutation((r) => { r.horizons[0].quality = 'preliminary'; }), /unexpected cohort/);
  contract(launchMutation((r) => { r.horizons[0].target.actual_age_seconds = '82935'; }), /real snapshot/);
  contract(launchMutation((r) => { r.horizons[2].target.target_at = '2026-10-28T00:00:00Z'; }), /reference point|window/);
  contract(launchMutation((r) => { r.horizons.pop(); }), /three horizons/);
  contract(launchMutation((r) => { r.reference.query_conditions_applied = true; }), /unexpected reference/);
  contract(launchMutation((r) => { r.reference.reference_point = 'actual_end_at'; }), /unexpected reference/);
  contract(launchMutation((r) => { r.window.to_at = r.window.from_at; }), /window/);
  contract(launchMutation((r) => { r.horizons[0].target.status = 'measured'; }));

  const baselineMutation = (change) => { const r = op(baselineLong); change(r.result); return () => inspector.parseLifetimeBaseline(r, expectFrozen()); };
  contract(baselineMutation((r) => { r.status = 'preliminary'; }), /status of this sample size/);
  contract(baselineMutation((r) => { r.missing_views = 0; }), /add up/);
  contract(baselineMutation((r) => { r.median = '58989.1'; }), /0\.25/);
  contract(baselineMutation((r) => { r.p25 = '90000'; }), /out of order/);
  contract(baselineMutation((r) => { r.definition = { state: 'known', epoch: null, unknown_definition_epoch_count: 0 }; }));
  // The count of stored views without a recorded definition is part of the missing views and agrees with the state.
  contract(baselineMutation((r) => { delete r.definition.unknown_definition_epoch_count; }), /missing key/);
  contract(baselineMutation((r) => { r.definition.unknown_definition_epoch_count = -1; }));
  contract(baselineMutation((r) => { r.definition.unknown_definition_epoch_count = r.missing_views + 1; }), /more than the missing views/);
  contract(baselineMutation((r) => { r.definition.extra = 1; }), /unknown key/);
  const oneUndefined = op(baselineLong);
  oneUndefined.result.definition.unknown_definition_epoch_count = 1;
  assert.equal(inspector.parseLifetimeBaseline(oneUndefined, expectFrozen()).unknown_definition_count, 1, 'one value left out, the baseline stays');
  const allUndefined = op(baselineLong);
  Object.assign(allUndefined.result, { status: 'insufficient_data', sample_size: 0, missing_views: 138, median: null, p25: null, p75: null,
    definition: { state: 'unknown_definition_epoch', epoch: null, unknown_definition_epoch_count: 0 } });
  contract(() => inspector.parseLifetimeBaseline(allUndefined, expectFrozen()), /definition state/);
  allUndefined.result.definition.unknown_definition_epoch_count = 137;
  assert.equal(inspector.parseLifetimeBaseline(allUndefined, expectFrozen()).definition, 'unknown_definition_epoch');
  const none = op(baselineLong);
  Object.assign(none.result, { status: 'insufficient_data', sample_size: 0, missing_views: 138, median: null, p25: null, p75: null,
    definition: { state: 'no_known_values', epoch: null, unknown_definition_epoch_count: 1 } });
  contract(() => inspector.parseLifetimeBaseline(none, expectFrozen()), /definition state/);
  contract(baselineMutation((r) => { r.target = null; }), /video views/);
  contract(baselineMutation((r) => { r.reference.query_conditions_applied = true; }), /unexpected reference/);
}
console.log('inspector parsers: unknown keys, enums, identities and every checked invariant are refused');

// ---------------------------------------------------------------------------
// Counters beyond 2^53 stay exact strings (BigInt checks only); 2^64 is refused.
{
  const MAX = '18446744073709551615';
  const r = op(staticShort);
  Object.assign(r.result.cells['video.views'], { value: MAX });
  Object.assign(r.result.cells['video.likes'], { value: '18446744073709551614' });
  Object.assign(r.result.cells['video.comments'], { value: '1' });
  r.result.derived['video.like_rate'].value = '100';
  r.result.derived['video.comment_rate'].value = '0';
  const view = inspector.parseStaticDetails(r, expectQuery('H0000000001', 'short'));
  assert.deepEqual([view.like_rate, view.comment_rate], [{ state: 'known', value: '100' }, { state: 'known', value: '0' }]);
  assert.equal(inspector.exactRate(18446744073709551614n, 18446744073709551615n), '100');
  assert.equal(inspector.exactRate(1n, 160000n), '0.000625');
  assert.equal(inspector.exactRate(1n, 16000000n), '0.000006', '0.00000625 → 6 places, ties to even');
  r.result.cells['video.views'].value = '18446744073709551616';
  contract(() => inspector.parseStaticDetails(r, expectQuery('H0000000001', 'short')), /unsigned decimal/);

  // A two-point history at the very top of the counter range, exactly one day apart.
  const h = op(historyLong), result = h.result;
  const [first, last] = [result.points.at(-2), result.points.at(-1)];
  first.views = '18446744073709551000'; last.views = MAX;
  first.likes = last.likes = null; first.comments = last.comments = null;
  first.recorded_at = later(last.recorded_at, -86400);
  result.points = [first, last];
  result.scanned_count = 2;
  result.series.views = { status: 'ready', reason: null, coverage: { point_count: 2, valid_count: 2, missing_count: 0, duplicate_timestamp_count: 0 },
    intervals: [{ from_at: first.recorded_at, to_at: last.recorded_at, actual_interval_seconds: '86400', delta: '615', per_day: '615', definition: 'known' }] };
  result.series.views.latest_interval = { ...result.series.views.intervals[0] };
  for (const metric of ['likes', 'comments']) {
    result.series[metric] = { status: 'ready', reason: null, coverage: { point_count: 2, valid_count: 0, missing_count: 2, duplicate_timestamp_count: 0 },
      intervals: [{ from_at: first.recorded_at, to_at: last.recorded_at, actual_interval_seconds: '86400', delta: null, per_day: null }], latest_interval: null };
  }
  const view2 = inspector.parseObservedHistory(h, expectFrozen());
  assert.deepEqual(view2.points.map((point) => point.views), ['18446744073709551000', MAX]);
  assert.equal(view2.latest_interval.views_per_day, '615');
  assert.equal(history.historyExactCount(MAX), '18,446,744,073,709,551,615');
  assert.equal(history.historyExactCount('615', true), '+615');
  assert.ok(history.videoHistoryGeometry(view2, 'cumulative', 'views').points.every((point) => Number.isFinite(point.y)));
  assert.equal(inspector.exactPerDay(-1n, 3n * 86_400_000_000n), '-0.33');
  assert.equal(inspector.exactPerDay(1n, 8n * 86_400_000_000n), '0.12', '0.125 → ties to even');
  assert.equal(inspector.exactPerDay(3n, 8n * 86_400_000_000n), '0.38', '0.375 → ties to even');
  assert.equal(inspector.secondsText(3_600_500_000n), '3600.5');

  // Launch views above 2^53 are classified exactly.
  const l = op(launchLong), target = l.result.horizons[0].target;
  target.views = '9007199254740993';
  target.comparison.classification = 'above_typical';
  target.comparison.multiplier = '352449493455.20';
  inspector.parseLaunchComparison(l, expectFrozen());
  target.comparison.multiplier = '352449493455.21';
  contract(() => inspector.parseLaunchComparison(l, expectFrozen()), /exact ratio/);
}
console.log('inspector counters: 2^64-1 strings, exact rates, per-day and ratios with ties to even');

// ---------------------------------------------------------------------------
// Honest states per type.
{
  // Static: a non-member of the current population (query opening) is a state, not an error.
  const absent = op(staticShort);
  Object.assign(absent.result, { membership: 'not_in_population', cells: null, derived: null, category: null, clocks: null });
  notMember(() => inspector.parseStaticDetails(absent, expectQuery('H0000000001', 'short')));
  notMember(() => inspector.parseStaticDetails(absent, expectQuery('H0000000001', 'short', { description: true })));
  absent.result.clocks = { counters_checked_at: null, metadata_checked_at: null };
  contract(() => inspector.parseStaticDetails(absent, expectQuery('H0000000001', 'short')), /missing values/);
  absent.result.clocks = null;
  assert.deepEqual(inspector.inspectorFailure(new inspector.InspectorNotInPopulation()), { kind: 'unavailable', notInPopulation: true });

  // An expired value is a state with no value; the derived count follows its source.
  const stale = op(staticLong);
  Object.assign(stale.result.cells['video.tags'], { value: null, state: 'expired' });
  Object.assign(stale.result.derived['video.tag_count'], { value: null, state: 'expired' });
  const staleView = inspector.parseStaticDetails(stale, expectFrozen());
  assert.deepEqual([staleView.tags, staleView.tag_count], [{ state: 'expired' }, { state: 'expired' }]);
  stale.result.derived['video.tag_count'].state = 'unknown';
  contract(() => inspector.parseStaticDetails(stale, expectFrozen()), /state differs/);

  // The description, read with static details when its field is enabled.
  const described = op(staticShort);
  described.result.cells['video.description'] = { value: 'Line one\nLine two', state: 'known',
    observed_at: SHORT_CLOCKS.metadata_checked_at, expires_at: later(SHORT_CLOCKS.metadata_checked_at, 696 * 3600) };
  assert.deepEqual(inspector.parseStaticDetails(described, expectQuery('H0000000001', 'short', { description: true })).description,
    { state: 'known', value: 'Line one\nLine two' });
  contract(() => inspector.parseStaticDetails(described, expectQuery('H0000000001', 'short')), /unknown key/);
  described.result.cells['video.description'] = { value: null, state: 'unknown', observed_at: null, expires_at: null };
  assert.deepEqual(inspector.parseStaticDetails(described, expectQuery('H0000000001', 'short', { description: true })).description, { state: 'unknown' });

  // History: limit, conflict and non-member states (explicit states instead of 503).
  const limited = op(historyLong);
  const empty = (reason) => ({ status: 'unavailable', reason, intervals: [], latest_interval: null,
    coverage: { point_count: 0, valid_count: 0, missing_count: 0, duplicate_timestamp_count: 0 } });
  Object.assign(limited.result, { status: 'unavailable', reason: 'observation_limit_exceeded', scanned_count: 501, points: [],
    series: { views: empty('observation_limit_exceeded'), likes: empty('observation_limit_exceeded'), comments: empty('observation_limit_exceeded') } });
  const limitedView = inspector.parseObservedHistory(limited, expectFrozen());
  assert.deepEqual([limitedView.status, limitedView.reason, limitedView.points.length], ['unavailable', 'observation_limit_exceeded', 0]);
  assert.equal(history.historyUnavailableText('views', limitedView.reason),
    'More than 500 stored views observations fall in this window; the history is not shown.');
  assert.equal(history.historyUnavailableText('likes', 'conflicting_simultaneous_observations'), 'Stored likes observations conflict at the same timestamp.');
  assert.equal(history.historyUnavailableText('views', 'endpoint_snapshot_missing'), 'Stored views history cannot be matched to this video’s catalogue observation.');
  const gone = clone(limited);
  Object.assign(gone.result, { membership: 'not_in_population', reason: 'video_not_in_population', scanned_count: 0,
    window: { ...gone.result.window, from_at: null, to_at: null },
    series: { views: empty('video_not_in_population'), likes: empty('video_not_in_population'), comments: empty('video_not_in_population') } });
  gone.result.status = 'unavailable';
  notMember(() => inspector.parseObservedHistory(gone, expectFrozen()));
  gone.result.membership = 'member';
  contract(() => inspector.parseObservedHistory(gone, expectFrozen()), /membership/);

  // A views interval across a definition change keeps both points and withholds the change.
  const changed = op(historyLong), r = changed.result, k = 30;
  r.points[k].views_definition_epoch = 'youtube.views.next.20261001';
  for (const index of [k - 1, k]) Object.assign(r.series.views.intervals[index], { delta: null, per_day: null, definition: 'incompatible_definition' });
  const changedView = inspector.parseObservedHistory(changed, expectFrozen());
  assert.deepEqual(changedView.intervals.slice(k - 1, k + 1).map((item) => [item.delta_views, item.definition]),
    [[null, 'incompatible_definition'], [null, 'incompatible_definition']]);
  const drawn = history.videoHistorySeries(changedView, 'views').intervals[k];
  assert.equal(history.historyDefinitionNote(drawn), 'Views definition changed across this interval.');
  assert.equal(history.historyDefinitionNote(history.videoHistorySeries(changedView, 'views').intervals[0]), null);
  r.points[k].views_definition_epoch = null;
  for (const index of [k - 1, k]) r.series.views.intervals[index].definition = 'unknown_definition';
  assert.equal(history.historyDefinitionNote(history.videoHistorySeries(inspector.parseObservedHistory(changed, expectFrozen()), 'views').intervals[k]),
    'Views definition not recorded at one end of this interval.');

  // A Stream's history starts no earlier than its end; the window may start later than now − 28 d.
  const streamHistory = op(historyLong);
  Object.assign(streamHistory.result, { video_id: 'S0000000005', video_type: 'stream' });
  streamHistory.result.window.start = 'actual_end_at_if_later';
  streamHistory.result.window.from_at = streamHistory.result.points[0].recorded_at;
  assert.equal(inspector.parseObservedHistory(streamHistory, expectFrozen('S0000000005', 'stream')).window.from_at, streamHistory.result.points[0].recorded_at);
  streamHistory.result.window.start = 'window';
  contract(() => inspector.parseObservedHistory(streamHistory, expectFrozen('S0000000005', 'stream')), /unexpected start/);

  // Launch: definition states, an unknown own epoch and a non-member.
  const mixed = op(launchLong), h0 = mixed.result.horizons[0];
  Object.assign(h0, { eligible_count: 0, quality: 'insufficient_data', counts: { below_typical: 0, typical: 0, above_typical: 0 },
    definition: { state: 'mixed_definition_epochs', epoch: null, unknown_definition_count: 0 } });
  h0.target.comparison = { status: 'mixed_definition_epochs', reference_count: 0, median: null, p25: null, p75: null,
    classification: null, multiplier: null, multiplier_reason: null };
  const mixedView = inspector.parseLaunchComparison(mixed, expectFrozen());
  assert.deepEqual([mixedView.horizons[0].definition_state, mixedView.horizons[0].target.comparison.status], ['mixed_definition_epochs', 'mixed_definition_epochs']);
  assert.equal(launch.launchComparisonNote(mixedView.horizons[0].target.comparison), 'Views definition changed among these observations. No assessment.');
  const unknownOwn = op(launchLong), u0 = unknownOwn.result.horizons[0];
  u0.target.definition_epoch = null;
  u0.target.comparison = { status: 'unknown_definition', reference_count: 56, median: null, p25: null, p75: null,
    classification: null, multiplier: null, multiplier_reason: null };
  assert.equal(launch.launchComparisonNote(inspector.parseLaunchComparison(unknownOwn, expectFrozen()).horizons[0].target.comparison),
    'This observation has no recorded views definition. No assessment.');
  assert.equal(launch.launchComparisonNote({ status: 'insufficient_data' }), null, 'the old sample-size text stays');
  const outsider = op(launchStream);
  outsider.result.membership = 'not_in_population';
  for (const horizon of outsider.result.horizons) horizon.target = null;
  notMember(() => inspector.parseLaunchComparison(outsider, expectQuery('S0000000005', 'stream')));
  // Streams are measured from the actual end; texts follow the type.
  assert.equal(launch.launchEvidence(launch.LAUNCH_HORIZONS[0], 'stream'), '22–24 hours after the stream ended');
  assert.equal(launch.launchEvidence(launch.LAUNCH_HORIZONS[1], 'long'), '6–7 days after publication');
  assert.deepEqual(['long', 'short', 'stream'].map(launch.launchTypeNoun), ['long-form videos', 'Shorts', 'streams']);
  assert.equal(launch.launchReferenceText('long'), 'All-time long-form publications');
  assert.match(launch.launchReferenceText('stream'), /actual end/);

  // Baseline: states (no statistics), per-type label.
  const unknownBase = op(baselineLong);
  Object.assign(unknownBase.result, { status: 'insufficient_data', sample_size: 0, missing_views: 138, median: null, p25: null, p75: null,
    definition: { state: 'mixed_definition_epochs', epoch: null, unknown_definition_epoch_count: 0 } });
  const unknownBaseView = inspector.parseLifetimeBaseline(unknownBase, expectFrozen());
  assert.equal(baseline.baselineDefinitionNote(unknownBaseView), 'Views definition changed among these videos, so no baseline is shown.');
  assert.equal(baseline.baselineDefinitionNote({ definition: 'no_known_values' }), null);
  assert.deepEqual(['long', 'short', 'stream'].map(baseline.baselineTypeLabel), ['Long-form', 'Shorts', 'Streams']);
  const small = op(baselineLong);
  Object.assign(small.result, { status: 'preliminary', sample_size: 5, missing_views: 133 });
  assert.equal(inspector.parseLifetimeBaseline(small, expectFrozen()).status, 'preliminary');
}
console.log('inspector states: not in population, expired values, description, limits, definition epochs, Streams, per-type texts');

// ---------------------------------------------------------------------------
// growth evidence → the Inspector's "Observed views".
{
  const row = clone(fixture.growth_row);
  const partial = inspector.growthFromCells(row.cells, 'row');
  const proof = row.cells['observation.growth_28d_status'].observation;
  assert.deepEqual(partial, { status: 'partial', delta_views: '400', tracked_days: 27, observed_start_at: proof.from_at,
    observed_end_at: proof.to_at, actual_interval_seconds: 2408399, average_views_per_day: '14.35',
    boundary_distance_seconds: null, reason: 'full_window_history_unavailable', time_zone: 'UTC' });
  assert.deepEqual(growthText.presentVideoInspectorGrowth(partial), { status: 'Partial history', delta: '+400 views',
    average: '~+14.35 views/day', start: proof.from_at, end: proof.to_at, interval: '27d 20h 59m 59s' });
  assert.equal(inspector.growthFromCells({}, 'row'), null, 'no growth field selected');
  // Only some growth fields selected: the evidence moves to the first one (growth evidence_carrier).
  const subset = { 'observation.views_gained_28d': { ...row.cells['observation.views_gained_28d'], observation: row.cells['observation.growth_28d_status'].observation } };
  assert.equal(inspector.growthFromCells(subset, 'row').delta_views, '400');
  const broken = (change) => { const value = clone(row.cells); change(value); return () => inspector.growthFromCells(value, 'row'); };
  contract(broken((c) => { c['observation.views_gained_28d'].value = '399'; }), /growth evidence/);
  contract(broken((c) => { c['observation.views_per_day_28d'].value = '14.34'; }), /growth evidence/);
  contract(broken((c) => { c['observation.growth_28d_tracked_days'].value = '26'; }), /growth evidence/);
  contract(broken((c) => { c['observation.views_gained_28d'].observation = c['observation.growth_28d_status'].observation; }), /one evidence/);
  contract(broken((c) => { c['observation.growth_28d_status'].observation = null; }), /one evidence/);
  contract(broken((c) => { c['observation.growth_28d_status'].observation.reason = 'tracking_started_today'; }), /reason/);
  contract(broken((c) => { c['observation.growth_28d_status'].observation.actual_interval_seconds = '2408400'; }), /inconsistent partial/);
  contract(broken((c) => { c['observation.growth_28d_status'].observation.score = 1; }), /unknown key/);
  contract(broken((c) => { c['observation.growth_28d_status'].value = 'ready'; }), /growth evidence/);

  const evidence = (fields) => ({ ...row.cells['observation.growth_28d_status'].observation, ...fields });
  const cellsOf = (status, values, fields) => {
    const cells = clone(row.cells);
    cells['observation.growth_28d_status'].value = status;
    cells['observation.growth_28d_status'].observation = evidence({ status, ...fields });
    for (const [field, value] of Object.entries(values)) {
      Object.assign(cells[field], { value, state: value === null ? 'unavailable' : 'known' });
    }
    return cells;
  };
  // ready: 28 days plus the boundary distance, no tracked days.
  const ready = inspector.growthFromCells(cellsOf('ready', {
    'observation.views_gained_28d': '400', 'observation.views_per_day_28d': '14.28', 'observation.growth_28d_interval_seconds': '2419800',
    'observation.growth_28d_tracked_days': null,
  }, { reason: null, from_at: '2026-09-26T21:50:00Z', to_at: '2026-10-24T22:00:00Z', actual_interval_seconds: '2419800',
    boundary_distance_seconds: '600', tracked_days: null }), 'row');
  assert.deepEqual([ready.status, ready.boundary_distance_seconds, ready.tracked_days, ready.average_views_per_day], ['ready', 600, null, '14.28']);
  // started_today: the zone of "today" is shown in the Inspector line.
  const today = inspector.growthFromCells(cellsOf('started_today', {
    'observation.views_gained_28d': null, 'observation.views_per_day_28d': null, 'observation.growth_28d_interval_seconds': '3600',
    'observation.growth_28d_tracked_days': null,
  }, { reason: 'tracking_started_today', from_at: '2026-10-24T21:00:00Z', to_at: '2026-10-24T22:00:00Z', actual_interval_seconds: '3600',
    boundary_distance_seconds: null, tracked_days: null, time_zone: 'Europe/Berlin' }), 'row');
  assert.equal(growthText.presentVideoInspectorGrowth(today).status, 'Tracking started today (Europe/Berlin)');
  assert.equal(growthText.presentVideoInspectorGrowth({ ...today, time_zone: undefined }).status, 'Tracking started today');
  // unavailable: both points kept as evidence, no comparison shown.
  const epochs = inspector.growthFromCells(cellsOf('unavailable', {
    'observation.views_gained_28d': null, 'observation.views_per_day_28d': null, 'observation.growth_28d_interval_seconds': null,
    'observation.growth_28d_tracked_days': null,
  }, { reason: 'incompatible_definition', actual_interval_seconds: null, tracked_days: null, definition_epoch: null,
    to_definition_epoch: 'youtube.views.next.20261001' }), 'row');
  assert.deepEqual([epochs.status, epochs.observed_start_at, epochs.delta_views], ['unavailable', null, null]);
  assert.equal(growthText.presentVideoInspectorGrowth(epochs).status, 'Growth unavailable');

  // Query opening: one current row with the growth fields; no row → not in the population.
  const opening = query('long', { kind: 'collection', id: '0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3' }, { timeZone: 'UTC' });
  const request = inspector.inspectorGrowthQuery(opening, 'a0000000001');
  assert.deepEqual(request.where, { field: 'video.id', op: 'eq', value: 'a0000000001' });
  assert.deepEqual(request.select, ['video.id', ...inspector.GROWTH_FIELDS]);
  assert.deepEqual([request.scope, request.time, request.page], [opening.scope, { mode: 'current', timezone: 'UTC' }, { size: 1 }]);
  const idCell = { value: 'a0000000001', state: 'known', provenance_ref: 'video.id:identity', observed_at: '2026-10-24T22:00:00Z',
    expires_at: '2026-11-19T00:00:00Z', observation: null };
  const provenance = Object.fromEntries([idCell, ...Object.values(row.cells)].map((cell) => [cell.provenance_ref,
    { origin: 'calculated', source_paths: [], method: 'video.observed_growth_28d.v1', definition_epoch: null }]));
  const result = {
    query_version: 1, query_fingerprint: 'a'.repeat(64),
    context: { source_id: 'public.videos.current.v1', source_schema_version: 1, registry_version: 9,
      lifecycle_profile: { id: 'p', version: 1, revision: 'r' }, preferences_revision: 'existing-v1',
      type_policy: { id: 't', version: 1, revision: 'r' }, evaluated_at: '2026-10-24T22:30:00Z', valid_before: '2026-10-24T22:32:00Z',
      consistency: 'current', observation_age_limit_seconds: null, time_zone: 'UTC' },
    columns: ['video.id', ...inspector.GROWTH_FIELDS], provenance,
    rows: [{ id: 'a0000000001', cells: { 'video.id': idCell, ...row.cells } }],
    coverage: { status: 'complete', basis: 'collection_members', channels_in_scope: 1, channels_complete: 1, eligible_count: null, reasons: {} },
    page: { has_more: false, next_cursor: null }, warnings: [],
  };
  const expect = { videoId: 'a0000000001', videoType: 'long', timeZone: 'UTC' };
  assert.equal(inspector.parseInspectorGrowth(clone(result), expect).delta_views, '400');
  notMember(() => inspector.parseInspectorGrowth({ ...clone(result), rows: [] }, expect));
  contract(() => inspector.parseInspectorGrowth(clone(result), { ...expect, timeZone: 'Europe/Berlin' }), /zone/);
  contract(() => inspector.parseInspectorGrowth({ ...clone(result), columns: ['video.id'] }, expect), /columns/);
  contract(() => inspector.parseInspectorGrowth({ ...clone(result), rows: [{ ...clone(result.rows[0]), id: 'b0000000001' }] }, expect), /another video/);
  contract(() => inspector.parseInspectorGrowth({ ...clone(result), page: { has_more: true, next_cursor: 'x' } }, expect), /one page/);
}
console.log('inspector growth: growth evidence for every status, carrier rule, zone, one-video query');

// ---------------------------------------------------------------------------
// Errors → panel states and busy retries.
{
  const failure = (code, status = 400) => Object.assign(new Error(code), { code, status });
  assert.deepEqual(inspector.inspectorFailure(failure('cursor_expired', 409)), { kind: 'inactive' });
  assert.deepEqual(inspector.inspectorFailure(failure('cursor_context_changed', 409)), { kind: 'inactive' });
  for (const code of ['policy_denied', 'grant_required', 'source_unavailable']) assert.deepEqual(inspector.inspectorFailure(failure(code)), { kind: 'unavailable' }, code);
  for (const code of ['query_busy', 'backend_unavailable', 'invalid_query', 'field_disabled']) assert.deepEqual(inspector.inspectorFailure(failure(code)), { kind: 'error' }, code);
  assert.deepEqual(inspector.inspectorFailure(new ExplorerContractError('x')), { kind: 'error' });
  assert.equal(inspector.inspectorFailure(Object.assign(new Error('stop'), { name: 'AbortError' })), null);

  const runs = async (script, name = staticLong, expectation = expectFrozen()) => {
    const calls = [], waits = [];
    const client = inspector.createInspectorClient(async (path, body) => {
      calls.push({ path, body });
      const step = script[Math.min(calls.length - 1, script.length - 1)];
      if (step instanceof Error) throw step;
      return op(step);
    }, { wait: async (ms) => { waits.push(ms); } });
    const signal = new AbortController().signal;
    const opening = expectation.mode === 'frozen' ? frozen(expectation.videoType) : query(expectation.videoType);
    try {
      const value = await client.staticDetails(opening, expectation.videoId, { description: false }, signal);
      return { value, calls, waits };
    } catch (error) {
      return { error, calls, waits };
    }
  };
  const busy = () => failure('query_busy', 429);
  let run = await runs([busy(), busy(), staticLong]);
  assert.equal(run.value.like_rate.value, '4.299504', 'busy reads are retried, not shown');
  assert.deepEqual(run.waits, [500, 1000]);
  assert.deepEqual(run.calls.map((call) => call.path), Array(3).fill('/research/operations/execute'));
  assert.deepEqual(run.calls[0].body, fixture.operations[staticLong].request);
  run = await runs([busy()]);
  assert.equal(run.error.code, 'query_busy');
  assert.deepEqual(run.waits, [...inspector.INSPECTOR_BUSY_DELAYS_MS]);
  run = await runs([failure('catalogue_preparing', 503)]);
  assert.deepEqual(run.waits, [2000, 2000]);
  assert.equal(run.error.code, 'catalogue_preparing');
  run = await runs([Object.assign(new Error('Source data expired during this read; retry.'), { status: 503 }), staticLong]);
  assert.deepEqual(run.waits, [inspector.INSPECTOR_EXPIRED_READ_DELAY_MS]);
  assert.ok(run.value);
  for (const code of ['cursor_expired', 'policy_denied', 'backend_unavailable', 'source_unavailable']) {
    run = await runs([failure(code, 409)]);
    assert.deepEqual([run.waits, run.calls.length], [[], 1], code);
  }
  run = await runs([historyLong]);
  assert.ok(run.error instanceof ExplorerContractError, 'a reply of another operation is incompatible');

  // A cancelled read stops waiting.
  const controller = new AbortController();
  const client = inspector.createInspectorClient(async () => { throw busy(); });
  const pending = client.history(frozen(), 'L0000000020', controller.signal);
  controller.abort();
  await assert.rejects(pending, (error) => error.name === 'AbortError' && inspector.inspectorFailure(error) === null);
}
console.log('inspector errors: inspector column, busy/preparing retries, cancellation');

// ---------------------------------------------------------------------------
// Gates and panel sources.
{
  const live = JSON.parse(read('./fixtures/research_capabilities_window_limits.json'));
  assert.deepEqual(inspector.inspectorGates(libraryGates(live, 'stream')),
    { staticDetails: true, baseline: true, launch: true, history: true, description: true, growth: true });
  const off = {
    ...live,
    operations: live.operations.filter((item) => item !== 'video.launch_comparison.v1'),
    fields: live.fields.map((field) => field.id === 'video.description' ? { ...field, enabled_video_types: ['long'] } : field),
  };
  assert.deepEqual(inspector.inspectorGates(libraryGates(off, 'short')),
    { staticDetails: true, baseline: true, launch: false, history: true, description: false, growth: true }, 'Launch disabled; per-format descriptions');
  assert.equal(inspector.inspectorGates(libraryGates(off, 'long')).description, true);
  const noStatic = { ...live, operations: live.operations.filter((item) => item !== 'video.static_details.v1') };
  assert.deepEqual(Object.values(inspector.inspectorGates(libraryGates(noStatic, 'long'))).slice(0, 5), [false, true, true, true, false]);

  const calls = [];
  const fake = new Proxy({}, { get: (_, name) => (...args) => { calls.push([name, ...args.slice(0, -1)]); return Promise.resolve(name); } });
  const all = { staticDetails: true, baseline: true, launch: true, history: true, description: true, growth: true };
  const page = inspector.inspectorSources(frozen('short'), 'H0000000001', all, fake);
  assert.deepEqual(Object.keys(page).sort(), ['baseline', 'description', 'history', 'launch', 'staticDetails']);
  assert.equal(page.description, true);
  assert.equal(page.growth, undefined, 'a Library page passes its row growth instead');
  assert.deepEqual([page.staticDetails.active, page.staticDetails.pending, page.staticDetails.videoType, page.staticDetails.mode], [true, false, 'short', 'frozen']);
  // Tokens differ per page; the session key is the identity.
  const next = inspector.inspectorSources(frozen('short', { frozenRef: 'another_token' }), 'H0000000001', all, fake);
  assert.equal(next.staticDetails.key, page.staticDetails.key);
  assert.notEqual(inspector.inspectorSources(frozen('short', { sessionKey: 'session-2' }), 'H0000000001', all, fake).staticDetails.key, page.staticDetails.key);
  assert.notEqual(inspector.inspectorSources(frozen('short', { checkedAt: later(CLOCKS.counters_checked_at, 600) }), 'H0000000001', all, fake).history.key, page.history.key);
  // while the session is prepared the panels wait instead of failing.
  assert.equal(inspector.inspectorSources(frozen('short', { pending: true }), 'H0000000001', all, fake).history.pending, true);
  assert.equal(inspector.inspectorSources(frozen('short', { frozenRef: '' }), 'H0000000001', all, fake).history.pending, true);
  await page.staticDetails.load(new AbortController().signal);
  assert.deepEqual(calls.at(-1), ['staticDetails', frozen('short'), 'H0000000001', { description: true }]);
  const collection = inspector.inspectorSources(query('stream', { kind: 'collection', id: '0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3' }, { timeZone: 'Asia/Tokyo' }),
    'S0000000005', { ...all, launch: false, description: false }, fake);
  assert.deepEqual(Object.keys(collection).sort(), ['baseline', 'description', 'growth', 'history', 'staticDetails']);
  assert.equal(collection.staticDetails.mode, 'query');
  await collection.staticDetails.load(new AbortController().signal);
  assert.deepEqual(calls.at(-1).slice(2), ['S0000000005', { description: false }]);
}
console.log('inspector gates and sources: server gates per type, session identity, pending sessions, collection opening');

// ---------------------------------------------------------------------------
// Texts and purity.
{
  const panel = read('../application/frontend/src/explorer/VideoHistoryPanel.tsx');
  assert.match(panel, /28 days up to now, ending at the catalogue observation/, 'window label');
  assert.doesNotMatch(panel, /Strictly the 28 days ending/, 'the misleading label is gone');
  const drawer = read('../application/frontend/src/explorer/VideoInspector.tsx');
  assert.match(drawer, /export function VideoInspector/, 'provider registry evidence names VideoInspector.tsx::VideoInspector');
  assert.match(drawer, /Source: stored public \{SOURCE_NOUNS\[videoType\]\} Explorer fields\./);
  const adapter = read('../application/frontend/src/explorer/libraryData/inspector.ts');
  // No React, browser globals or network module (".window." in wire paths and messages is data).
  assert.doesNotMatch(adapter, /from 'react'|[^.\w$]window\.[a-z]|[^.\w$]document\.[a-z]|from '\.\.\/api'/, 'the adapter stays pure');
  assert.doesNotMatch(adapter, /\bNumber\((?:point|item|interval|target)\.(?:views|value|delta)/, 'counters never become numbers');
}
console.log('inspector texts and purity: label, per-type source, pure adapter');
