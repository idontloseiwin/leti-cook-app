// Library Overview and channel header adapter:
// Six operations feed the existing Overview and header components, for
// Long, Shorts and Streams; per-type buckets from capabilities;
// States; the 7D/28D interval and partial status; format
// totals with the completeness and Stream end rules; data-clock
// lifetimes; per-type hydration readiness; the bucket jump into Videos.
// Offline: 's real wire examples, no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as contracts from '../application/frontend/src/explorer/contracts.ts';
import * as source from '../application/frontend/src/sourceLifetime.ts';
import * as overview from '../application/frontend/src/explorer/libraryData/overview.ts';
import { presetsFor } from '../application/frontend/src/explorer/libraryData/presets.ts';
import { libraryErrorState } from '../application/frontend/src/explorer/libraryData/errors.ts';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
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
const { formatCount } = moduleWith('../application/frontend/src/explorer/format.ts', { '../format': sharedFormat, '../format.ts': sharedFormat });
const contract = (fn, pattern) => assert.throws(fn, (error) => error instanceof ExplorerContractError
  && (!pattern || pattern.test(error.message)), String(pattern ?? 'contract error'));
const clone = (value) => structuredClone(value);
const fixture = JSON.parse(read('./fixtures/library_overview_wire.json'));
const capabilitiesFixture = JSON.parse(read('./fixtures/research_capabilities_window_limits.json'));
const wire = (name) => clone(fixture.operations[name].response);
const MAIN = 'UCMainOverviewChannel000';
const KYIV = 'Europe/Kyiv';
// Instants follow the clock of the run that generated the fixture (tests/library_wire_fixtures_pg.py): they are read
// from it; counts, values and their relations stay literal.
const PERIOD = fixture.operations.fingerprint.response.result.period;
const CHANGE_7D = fixture.operations.counter_change.response.result.windows['7d'].metrics.views;
const later = (iso, seconds) => {
  const [whole, fraction = ''] = iso.replace('Z', '').split('.');
  return new Date(Date.parse(whole + 'Z') + seconds * 1000).toISOString().slice(0, 19) + (fraction ? '.' + fraction : '') + 'Z';
};
const capabilities = overview.parseOverviewCapabilities(capabilitiesFixture);
const presetsOf = (type, caps = capabilities) => (group) => overview.overviewPresets(caps, type, group);
const fingerprint = (raw = wire('fingerprint'), type = 'long', period = '28d') => overview.parseFingerprint(raw, {
  videoType: type, period, presets: presetsOf(type),
});

// ---------------------------------------------------------------------------
// Requests: 's shapes. Every Overview read carries the viewer zone.
{
  const ops = overview.OVERVIEW_OPERATIONS;
  const zoned = (name) => {
    const request = clone(fixture.operations[name].request);
    request.query.time ??= { mode: 'current', timezone: KYIV };
    return request;
  };
  assert.deepEqual(overview.overviewRequest(ops.fingerprint, MAIN, 'long', KYIV, { period: '28d' }), zoned('fingerprint'));
  assert.deepEqual(overview.overviewRequest(ops.rhythm, MAIN, 'long', KYIV, { period: '28d' }), fixture.operations.rhythm.request);
  assert.deepEqual(overview.overviewRequest(ops.years, MAIN, 'long', KYIV), fixture.operations.years.request);
  assert.deepEqual(overview.overviewRequest(ops.breakdown, MAIN, 'long', KYIV, { period: 'max' }), zoned('breakdown'));
  assert.deepEqual(overview.overviewRequest(ops.years, MAIN, 'stream', 'UTC', { year: 2024 }).parameters, { year: 2024 });
  assert.deepEqual(overview.overviewRequest(ops.years, MAIN, 'short', 'UTC').query.video_types, ['short']);
  assert.deepEqual(overview.headerRequest(overview.HEADER_OPERATIONS.counterChange, 'UCCounterFiveDayIntrvl00'),
    fixture.operations.counter_change.request);
  assert.deepEqual(overview.headerRequest(overview.HEADER_OPERATIONS.formatTotals, 'UCCleanHeaderTotals00000'),
    fixture.operations.format_totals.request);
  assert.throws(() => overview.overviewRequest(ops.years, MAIN, 'long', KYIV, { period: '28d' }), /no period/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, MAIN, 'long', KYIV, { year: 2024 }), /Only the publication calendar/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, MAIN, 'long', KYIV), /period is required/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, MAIN, 'long', KYIV, { period: '2w' }), /period is required/);
  assert.throws(() => overview.overviewRequest(ops.years, MAIN, 'long', KYIV, { year: 0 }), /calendar year/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, 'UCshort', 'long', KYIV, { period: '7d' }), /one channel/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, MAIN, 'clip', KYIV, { period: '7d' }), /video type/);
  assert.throws(() => overview.overviewRequest(ops.rhythm, MAIN, 'long', '../etc', { period: '7d' }), /time zone/);
  assert.throws(() => overview.headerRequest(overview.HEADER_OPERATIONS.formatTotals, 'channel'), /one channel/);
}
console.log('overview requests: Overview shapes, zone in every Overview read, header channel source');

// ---------------------------------------------------------------------------
// Real wire → presentation.
{
  const view = fingerprint();
  assert.equal(view.video_type, 'long');
  assert.deepEqual(view.period, { key: '28d', label: '28D', kind: 'rolling', days: 28,
    start_at: PERIOD.start_at, end_at: PERIOD.end_at });
  assert.equal(PERIOD.start_at, later(PERIOD.end_at, -28 * 86400), 'exactly 28 days');
  assert.deepEqual(view.sample, { matched_video_count: 56, scanned_inventory_count: 252, inventory_complete: true });
  assert.deepEqual(view.coverage, {
    scanned: 252, admitted: 231, matched: 56, publication_unknown_excluded: 7, stale_count: 10, stale_counter_count: 3,
    stale_metadata_count: 7, partial: true, inventory_incomplete: 0,
    rejected: { identity: 1, source_missing: 2, source_mismatch: 0, source_stale: 4, source_unavailable: 11, source_unpublished: 3, stream_incomplete: 0 },
  });
  assert.deepEqual(view.statistics.views, {
    state: 'known', unavailable_reason: null, known_count: 55, missing_count: 1, excluded_count: 0,
    definition_epoch: 'youtube.views.play_start.20260824', total: '21635132', mean: '393366.04', median: '42198', q1: '771',
    q3: '229368', min: '12', max: '3103852',
  });
  // title_length keeps the component's old key; the buckets are the type's set, in order.
  assert.equal(view.statistics.title_length_characters.median, '17');
  assert.deepEqual(view.distributions.duration_seconds.map((bucket) => bucket.key), presetsFor('long', 'duration').map((preset) => preset.key));
  assert.deepEqual(view.distributions.views.map((bucket) => [bucket.key, bucket.count]),
    [['under_1k', 15], ['1k_10k', 10], ['10k_100k', 10], ['100k_1m', 12], ['1m_10m', 8], ['10m_plus', 0]]);
  assert.deepEqual(view.distribution_coverage.likes, { known_count: 52, missing_count: 4, excluded_count: 0 });
  assert.deepEqual(view.publication_cadence, { known_publication_count: 56, unknown_publication_count: 0 });

  const rhythm = overview.parseRhythm(wire('rhythm'), { period: '28d', timeZone: KYIV });
  assert.equal(rhythm.time_zone, KYIV);
  assert.equal(rhythm.total, 56);
  assert.equal(rhythm.byHour.length, 24);
  // Weekdays and calendar days follow the generating run's clock: the parsed view equals the reply.
  assert.deepEqual(rhythm.byWeekday, fixture.operations.rhythm.response.result.weekdays.map((item) => item.count));
  assert.equal(rhythm.byWeekday.reduce((sum, count) => sum + count, 0), 56);
  assert.equal(rhythm.grid.length, 7);
  assert.deepEqual(rhythm.grid[0], fixture.operations.rhythm.response.result.grid[0].hours);
  assert.equal(rhythm.first_published_at, fixture.operations.rhythm.response.result.first_published_at);
  assert.ok(rhythm.first_published_at >= PERIOD.start_at && rhythm.first_published_at <= PERIOD.end_at);

  const years = overview.parseYears(wire('years'), { timeZone: KYIV });
  assert.deepEqual([years.default_year, years.year, years.available_years], [2026, 2026, [2026, 2025, 2024, 2023, 2022, 2021, 2020]]);
  assert.deepEqual(years.days[0], fixture.operations.years.response.result.days[0]);
  assert.match(years.days[0].date, /^2026-/);

  const breakdown = overview.parseBreakdown(wire('breakdown'), { period: 'max' });
  assert.equal(breakdown.period.start_at, null);
  assert.equal(breakdown.sections.youtube_topics.share_basis, 'collected_rows', 'topics share the collected rows');
  assert.deepEqual([breakdown.sections.youtube_topics.assigned_count, breakdown.sections.youtube_topics.empty_count], [172, 22]);
  assert.deepEqual(breakdown.sections.captions.groups.map((group) => [group.key, group.label, group.count, group.share_percent]),
    [['unavailable', 'No captions', 102, '55.14'], ['available', 'Captions available', 83, '44.86']]);
  assert.equal(breakdown.sections.official_categories.omitted_group_count, 0);

  // One Overview from four replies; the calendar lists the loaded years.
  const payload = overview.overviewPayload(view, rhythm, years, overview.parseBreakdown((() => {
    const raw = wire('breakdown');
    raw.result.period = clone(fixture.operations.fingerprint.response.result.period);
    raw.context.evaluated_at = raw.result.period.end_at;
    raw.result.population = { admitted: 231, matched: 56, publication_unknown_excluded: 7 };
    for (const key of ['official_categories', 'youtube_topics', 'audio_languages', 'metadata_languages', 'captions', 'catalogue_age']) {
      const section = raw.result[key];
      section.sample_count = 56; section.known_count = 0; section.missing_count = 56; section.groups = [];
      if (key === 'youtube_topics') { section.assigned_count = 0; section.empty_count = 0; }
    }
    return raw;
  })(), { period: '28d' }), [{ ...years, year: 2024, days: [{ date: '2024-02-29', count: 1 }] }]);
  assert.deepEqual(payload.publication_calendar.years.map((item) => item.year), [2026, 2024]);
  assert.equal(payload.publication_calendar.timezone, KYIV);
  assert.equal(payload.generated_at, view.evaluated_at);
  contract(() => overview.overviewPayload(view, rhythm, years, breakdown), /different periods/);
  contract(() => overview.overviewPayload(view, rhythm, { ...years, time_zone: 'UTC' }, breakdown), /different (periods|time zones)/);

  const change = overview.parseCounterChange(wire('counter_change'), 'UCCounterFiveDayIntrvl00');
  assert.deepEqual(change.periods['7d'].metrics.views, {
    status: 'partial', coverage: 'partial', start_value: '700000', end_value: '770000', absolute_change: '70000',
    percent_change: '10', observed_start_at: CHANGE_7D.observed_start_at, observed_end_at: CHANGE_7D.observed_end_at,
    actual_interval_seconds: 436056, reason_code: 'start_after_window_boundary',
    reason: 'Partial window: the earliest stored observation is later than the window start.',
  });
  assert.deepEqual([change.periods['7d'].window_seconds, change.periods['28d'].window_seconds, change.periods['7d'].tolerance_seconds],
    [604800, 2419200, 86400]);

  const totals = overview.parseFormatTotals(wire('format_totals'), 'UCCleanHeaderTotals00000');
  assert.deepEqual([totals.formats.long.status, totals.formats.long.views, totals.formats.short.views], ['ready', '6000', '4000']);
  assert.deepEqual([totals.formats.stream.status, totals.formats.stream.views, totals.formats.stream.missing_view_count], ['incomplete', null, 1]);
  assert.equal(totals.formats.stream.excluded.stream_completion_withheld, 1, 'an unfinished Stream withholds the sum');
  assert.equal(overview.formatTotalDetail(totals.formats.stream), '1 stream has not ended or its end is not confirmed yet.');
  assert.equal(overview.formatTotalDetail(totals.formats.long), '');
}
console.log('overview wire: fingerprint, rhythm, years, breakdown, counter change and format totals parsed');

// ---------------------------------------------------------------------------
// Overview values arrive as decimal strings; the compact Fingerprint text keeps the old
// Overview's precision (its JSON numbers through the same Intl rule): 34.5 stays "34.5", never "34".
{
  assert.equal(formatCount('34.5'), '34.5');
  assert.equal(formatCount('59.77'), '59.8');
  assert.equal(formatCount('3.25'), '3.3');
  assert.equal(formatCount('0.5'), '0.5');
  assert.equal(formatCount('-0.5'), '-0.5');
  assert.equal(formatCount('1234.5'), '1.2K');
  assert.equal(formatCount('999.96'), '1K');
  for (const value of ['34.5', '59.77', '3.25', '0.15', '12', '1000000', '393366.04', '2.333333', '0.004']) {
    assert.equal(formatCount(value), formatCount(Number(value)), `${value}: formats identically as a number`);
  }
  // Whole counters keep their exact BigInt path (beyond 2^53), and the exact mode keeps two places below 1,000.
  assert.equal(formatCount('18446744073709551615', { compact: false }), '18,446,744,073,709,551,615');
  assert.equal(formatCount('34.5', { compact: false }), '34.5');
  assert.equal(formatCount('1234.5', { compact: false }), '1,235');
  // Every mean and median of the real Fingerprint reply reads as its number did.
  const view = fingerprint();
  for (const [metric, summary] of Object.entries(view.statistics)) {
    for (const key of ['mean', 'median', 'q1', 'q3']) {
      const value = summary?.[key];
      if (typeof value === 'string') assert.equal(formatCount(value), formatCount(Number(value)), `${metric}.${key} ${value}`);
    }
  }
}
console.log('Compact decimal strings preserve the displayed precision (34.5, 59.8); whole counters stay exact.');

// ---------------------------------------------------------------------------
// the preloaded Overview chunk normally arrives within 0.2 s; its placeholder then never flashes.
// It shows only when loading lasts longer (a cold or slow load keeps "Loading channel analysis…").
{
  const React = await import('../application/frontend/node_modules/react/index.js');
  const jsxRuntime = await import('../application/frontend/node_modules/react/jsx-runtime.js');
  const { renderToStaticMarkup } = await import('../application/frontend/node_modules/react-dom/server.node.js');
  const tsx = (path) => ts.transpileModule(read(path), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  new Function('require', 'exports', tsx('../application/frontend/src/explorer/ModuleState.tsx'))((name) => ({
    react: React, 'react/jsx-runtime': jsxRuntime, 'lucide-react': new Proxy({}, { get: () => () => null }) })[name], exports);
  assert.equal(exports.LOADING_TEXT_DELAY_MS, 300);
  const first = renderToStaticMarkup(React.createElement(exports.DelayedLoadingState, { label: 'Loading channel analysis…' }));
  assert.match(first, /aria-hidden="true"/);
  assert.match(first, /visibility:hidden/, 'the box is kept, so the layout does not move when the text appears');
  assert.doesNotMatch(first, /role="status"/, 'nothing is announced during a normal load');
  const slow = renderToStaticMarkup(React.createElement(exports.DelayedLoadingState, { label: 'Loading channel analysis…', delayMs: 0 }));
  assert.match(slow, /role="status"/);
  assert.doesNotMatch(slow, /visibility:hidden|aria-hidden/);
  assert.equal(renderToStaticMarkup(React.createElement(exports.LoadingState, { label: 'x' })).includes('role="status"'), true, 'other loading rows are unchanged');
  const section = read('../application/frontend/src/explorer/LibraryOverviewSection.tsx');
  assert.match(section, /<DelayedLoadingState label="Loading channel analysis…" \/>/);
  assert.match(section, /void overviewPanels\.start\(\)/, 'the chunk is still requested at once (cold load)');
}
console.log('the Overview placeholder waits 300 ms (no flash on a normal load), the slow load keeps its text.');

// ---------------------------------------------------------------------------
// Strictness: envelopes, keys, enumerations and invariants.
{
  const cases = {
    fingerprint: (raw) => fingerprint(raw),
    rhythm: (raw) => overview.parseRhythm(raw, { period: '28d', timeZone: KYIV }),
    years: (raw) => overview.parseYears(raw, { timeZone: KYIV }),
    breakdown: (raw) => overview.parseBreakdown(raw, { period: 'max' }),
    counter_change: (raw) => overview.parseCounterChange(raw, 'UCCounterFiveDayIntrvl00'),
    format_totals: (raw) => overview.parseFormatTotals(raw, 'UCCleanHeaderTotals00000'),
  };
  for (const [name, parse] of Object.entries(cases)) {
    parse(wire(name));
    const broken = (edit, pattern) => { const raw = wire(name); edit(raw); contract(() => parse(raw), pattern); };
    broken((raw) => { raw.extra = 1; }, /unknown key/);
    broken((raw) => { raw.context.extra = 1; }, /unknown key/);
    broken((raw) => { raw.result.extra = 1; }, /unknown key/);
    broken((raw) => { raw.coverage.extra = 1; }, /unknown key/);
    broken((raw) => { raw.provenance.extra = 1; }, /unknown key/);
    broken((raw) => { raw.operation = 'video.summary.v1'; }, /expected/);
    broken((raw) => { raw.provenance.method = 'video.summary.v1'; }, /provenance.method/);
    broken((raw) => { raw.context.source_id = name.startsWith('co') || name.startsWith('fo') ? 'public.videos.current.v1' : 'public.channels.current.v1'; }, /source/);
    broken((raw) => { raw.context.consistency = 'frozen'; }, /consistency/);
    broken((raw) => { raw.context.valid_before = raw.context.evaluated_at; }, /end after/);
    broken((raw) => { raw.operation_fingerprint = 'abc'; }, /fingerprint/);
    broken((raw) => { raw.source_expires_at = '2099-01-01T00:00:00Z'; }, /outlives/);
    broken((raw) => { delete raw.warnings; }, /missing key/);
    // The middleware's read clock is accepted at the top.
    const timed = wire(name);
    timed.source_read_at = timed.context.evaluated_at;
    timed.source_expires_at = timed.context.valid_before;
    parse(timed);
  }

  const fp = (edit, pattern, type = 'long', period = '28d') => {
    const raw = wire('fingerprint'); edit(raw.result, raw);
    contract(() => fingerprint(raw, type, period), pattern);
  };
  fp((result) => { result.metrics.views.buckets[0].count += 1; }, /add up/);
  fp((result) => { result.metrics.likes.missing_count += 1; }, /cover the matched/);
  fp((result) => { result.metrics.likes.state = 'no_known_values'; }, /known count|expected one of/);
  fp((result) => { result.metrics.likes.state = 'mixed_definition_epochs'; }, /expected one of/);
  fp((result) => { result.metrics.likes.excluded_count = 1; result.metrics.likes.missing_count -= 1; }, /definition/);
  fp((result) => { result.metrics.views.q1 = '50000'; }, /out of order/);
  fp((result) => { result.metrics.views.mean = '1'; }, /outside/);
  fp((result) => { result.metrics.views.median = '42198.123'; }, /decimal summary/);
  fp((result) => { result.metrics.views.buckets[1].key = '1k_9k'; }, /expected 1k_10k/);
  fp((result) => { result.metrics.views.buckets[1].label = '1K-10K'; }, /expected 1K/);
  fp((result) => { result.metrics.views.buckets.pop(); }, /bucket set/);
  fp((result) => { result.metrics.views.definition_epoch = null; }, /names its definition/);
  fp((result) => { delete result.metrics.views.unknown_definition_epoch_count; }, /missing key/);
  fp((result) => { result.metrics.likes.unknown_definition_epoch_count = 0; }, /unknown key/);
  fp((result) => { result.period.end_at = later(PERIOD.end_at, -1); }, /evaluation time/);
  fp((result) => { result.period.start_at = later(PERIOD.start_at, -1); }, /24 hours/);
  fp((result) => { result.period.key = '7d'; }, /expected 28d/);
  fp((result) => { result.period.label = '4W'; }, /expected 28D/);
  fp((result) => { result.uploads.known_count -= 1; }, /cover the matched/);
  fp((result) => { result.uploads.known_count -= 1; result.uploads.unknown_count = 1; }, /known dates/);
  fp((result) => { result.population.stale_count = 1; }, /stale/);
  fp((result) => { result.population.rejected.source_missing = 1000; }, /than scanned/);
  fp((result) => { delete result.population.rejected.identity; }, /missing key/);
  fp((result) => { result.population.matched = 1000; }, /population/);
  fp((result) => { result.metrics.unknown_metric = {}; }, /unknown key/);
  fp(() => {}, /expected under_15s|bucket set/, 'short');

  const rh = (edit, pattern, zone = KYIV) => {
    const raw = wire('rhythm'); edit(raw.result);
    contract(() => overview.parseRhythm(raw, { period: '28d', timeZone: zone }), pattern);
  };
  rh(() => {}, /another time zone/, 'UTC');
  rh((result) => { result.hours.pop(); }, /24 items/);
  rh((result) => { [result.hours[0], result.hours[1]] = [result.hours[1], result.hours[0]]; }, /out of order/);
  rh((result) => { result.hours[0].count += 1; }, /add up/);
  rh((result) => { // Move one upload of the first weekday to the next hour: the totals hold, the rings do not.
    const hours = result.grid[0].hours, from = hours.findIndex((count) => count > 0);
    hours[from] -= 1; hours[(from + 1) % 24] += 1;
  }, /disagrees with the rings/);
  rh((result) => { // Any other hour (a later tie or a lower one) is not the peak.
    const counts = result.hours.map((item) => item.count), max = Math.max(...counts), first = counts.indexOf(max);
    const hour = counts.lastIndexOf(max) !== first ? counts.lastIndexOf(max) : (first + 1) % 24;
    result.peak_hour = { hour, count: max };
  }, /first maximum/);
  rh((result) => { result.peak_weekday = null; }, /expected an object/);
  rh((result) => { result.first_published_at = '2026-09-01T00:00:00Z'; }, /outside the period/);
  rh((result) => { result.known_count = 57; }, /add up|more publications/);
  rh((result) => { delete result.grid; }, /missing key/);

  const yr = (edit, pattern, expect = { timeZone: KYIV }) => {
    const raw = wire('years'); edit(raw.result);
    contract(() => overview.parseYears(raw, expect), pattern);
  };
  yr((result) => { result.available_years = [2025, 2026]; }, /newest first/);
  yr((result) => { result.available_years = [2025]; }, /always offered/);
  yr((result) => { result.default_year = 2025; }, /current year/);
  yr(() => {}, /another year/, { timeZone: KYIV, year: 2024 });
  yr((result) => { result.days[1].date = result.days[0].date; }, /ordered dates/);
  yr((result) => { result.days[0].date = '2025-12-31'; }, /ordered dates/);
  yr((result) => { result.days[0].date = '2026-02-30'; }, /ordered dates/);
  yr((result) => { result.days[0].count = 0; }, /has publications/);
  yr((result) => { result.known_count = 1; }, /known dates/);

  const bd = (edit, pattern) => {
    const raw = wire('breakdown'); edit(raw.result);
    contract(() => overview.parseBreakdown(raw, { period: 'max' }), pattern);
  };
  bd((result) => { result.official_categories.groups[0].share_percent = '20'; }, /disagrees with the count/);
  bd((result) => { result.youtube_topics.share_basis = 'known_rows'; }, /share basis/);
  bd((result) => { result.captions.groups[0].label = 'None'; }, /fixed group/);
  bd((result) => { result.catalogue_age.groups[0].key = 'decade'; }, /fixed group/);
  bd((result) => { result.official_categories.omitted_group_count = 3; }, /below the limit/);
  bd((result) => { result.audio_languages.sample_count = 230; }, /another sample/);
  bd((result) => { result.youtube_topics.empty_count += 1; }, /collected rows/);
  bd((result) => { result.metadata_languages.groups.reverse(); }, /largest groups first/);
  bd((result) => { result.official_categories.groups[1].key = result.official_categories.groups[0].key; }, /distinct key/);
  bd((result) => { delete result.official_categories.label_expires_at; }, /missing key/);
  bd((result) => { result.captions.label_expires_at = null; }, /unknown key/);
}
console.log('overview strictness: unknown keys, enumerations and broken invariants are contract errors');

// ---------------------------------------------------------------------------
// Views withheld by its definition carry a reason, not zeros; stale videos stay and are counted.
{
  const raw = wire('fingerprint');
  const views = raw.result.metrics.views;
  Object.assign(views, { state: 'mixed_definition_epochs', definition_epoch: null, known_count: 0, missing_count: 1, excluded_count: 55,
    unknown_definition_epoch_count: 0, total: null, mean: null, median: null, q1: null, q3: null, min: null, max: null });
  views.buckets = views.buckets.map((bucket) => ({ ...bucket, count: 0 }));
  const view = fingerprint(raw);
  assert.equal(view.statistics.views.state, 'mixed_definition_epochs');
  assert.equal(view.statistics.views.unavailable_reason, 'Views definition changed in this period');
  assert.equal(view.statistics.views.median, null);
  assert.deepEqual(view.distribution_coverage.views, { known_count: 0, missing_count: 1, excluded_count: 55 });
  views.state = 'unknown_definition_epoch'; views.unknown_definition_epoch_count = 55;
  assert.equal(fingerprint(raw).statistics.views.unavailable_reason, 'Views without a recorded definition');
  // One undefined value is left out and counted, the summary stays.
  const one = wire('fingerprint');
  Object.assign(one.result.metrics.views, { known_count: 54, excluded_count: 1, unknown_definition_epoch_count: 1 });
  one.result.metrics.views.buckets[0].count -= 1;
  assert.equal(fingerprint(one).statistics.views.excluded_count, 1);
  one.result.metrics.views.unknown_definition_epoch_count = 0;
  contract(() => fingerprint(one), /undefined values/);
  assert.equal(overview.METRIC_STATE_TEXT.no_known_values, 'No known values');
}
console.log('States: reasons instead of summaries; one undefined value excluded and counted');

// ---------------------------------------------------------------------------
// Per-type buckets from capabilities; fallback to the bundled set.
{
  const shortDuration = overview.overviewPresets(capabilities, 'short', 'duration');
  assert.deepEqual(shortDuration.map((preset) => preset.key), ['under_15s', '15s_30s', '30s_60s', '1m_3m', 'over_3m']);
  assert.deepEqual(shortDuration.map((preset) => [preset.min, preset.max]), [[null, 15], [15, 30], [30, 60], [60, 181], [181, null]]);
  assert.deepEqual(overview.overviewPresets(capabilities, 'stream', 'duration').map((preset) => preset.key),
    ['under_30m', '30m_60m', '1h_2h', '2h_4h', '4h_plus']);
  assert.deepEqual(overview.overviewPresets(capabilities, 'short', 'views'), overview.overviewPresets(capabilities, 'long', 'views'));
  // The bundled shared JSON is the same registry (fallback when a server sends no presets).
  for (const type of ['long', 'short', 'stream']) {
    for (const group of ['views', 'duration', 'likes', 'comments', 'title_length', 'tag_count']) {
      assert.deepEqual(overview.overviewPresets(capabilities, type, group), presetsFor(type, group), `${type} ${group}`);
      assert.deepEqual(overview.overviewPresets(null, type, group), presetsFor(type, group));
    }
  }
  // A Shorts fingerprint (derived from the real Long reply: Shorts duration set) parses only as Shorts.
  const derived = (type) => {
    const raw = wire('fingerprint');
    const known = raw.result.metrics.duration_seconds.known_count;
    const set = overview.overviewPresets(capabilities, type, 'duration');
    raw.result.metrics.duration_seconds.buckets = set.map((preset, index) => ({ key: preset.key, label: preset.label,
      count: index === 2 ? known : 0 }));
    return raw;
  };
  assert.deepEqual(fingerprint(derived('short'), 'short').distributions.duration_seconds.map((bucket) => bucket.count), [0, 0, 56, 0, 0]);
  assert.equal(fingerprint(derived('stream'), 'stream').video_type, 'stream');
  contract(() => fingerprint(derived('short'), 'long'), /expected under_5m|bucket set/);
  contract(() => fingerprint(derived('short'), 'stream'), /expected under_30m/);
  // Capabilities: only the parts the Overview reads; broken presets are refused.
  assert.ok(overview.overviewAvailable(capabilities), 'BASELINE on: the four Overview operations');
  const off = overview.parseOverviewCapabilities({ ...capabilitiesFixture, operations: capabilitiesFixture.operations.filter((id) => id !== 'video.publication_rhythm.v1') });
  assert.equal(overview.overviewAvailable(off), false);
  assert.equal(overview.parseOverviewCapabilities({ ...capabilitiesFixture, presets: undefined }).presets, null);
  contract(() => overview.parseOverviewCapabilities({ ...capabilitiesFixture, source_id: 'public.channels.current.v1' }), /source/);
  const badPresets = clone(capabilitiesFixture);
  badPresets.presets.groups.views.buckets.short[1].key = 'under_1k';
  contract(() => overview.parseOverviewCapabilities(badPresets), /unique buckets/);
  const noShort = clone(capabilitiesFixture);
  delete noShort.presets.groups.duration.buckets.short;
  contract(() => overview.parseOverviewCapabilities(noShort), /missing key/);
}
console.log('per-type buckets: capabilities presets, bundled fallback, Shorts/Streams sets enforced');

// ---------------------------------------------------------------------------
// Header: 7D/28D and format totals.
{
  const parse = (raw) => overview.parseCounterChange(raw, 'UCCounterFiveDayIntrvl00');
  // A full 7D window: start one hour before the boundary, inside the tolerance.
  const full = wire('counter_change');
  const fullViews = full.result.windows['7d'].metrics.views;
  Object.assign(fullViews, { status: 'ready', coverage: 'full', observed_start_at: later(CHANGE_7D.observed_end_at, -(7 * 86400 + 3600)),
    actual_interval_seconds: 7 * 86400 + 3600, reason_code: null, percent_change: '10' });
  const ready = parse(full).periods['7d'].metrics.views;
  assert.deepEqual([ready.status, ready.coverage, ready.reason, ready.actual_interval_seconds], ['ready', 'full', null, 608400]);
  const broken = (edit, pattern) => { const raw = wire('counter_change'); edit(raw.result.windows['7d'].metrics.views, raw); contract(() => parse(raw), pattern); };
  broken((metric) => { metric.actual_interval_seconds += 1; }, /interval disagrees/);
  broken((metric) => { metric.absolute_change = '1'; }, /change disagrees/);
  broken((metric) => { metric.percent_change = '-10'; }, /wrong sign/);
  broken((metric) => { metric.reason_code = null; }, /reason does not match/);
  broken((metric) => { metric.coverage = 'full'; }, /coverage does not match/);
  broken((metric) => { metric.status = 'ready'; metric.coverage = 'full'; metric.reason_code = null; }, /does not match the window/);
  broken((metric) => { metric.status = 'unavailable'; }, /expected one of/);
  broken((metric) => { metric.observed_end_at = later(metric.observed_end_at, 1); }, /interval disagrees|different snapshots/);
  broken((metric, raw) => { raw.context.evaluated_at = later(raw.context.evaluated_at, 86400); raw.context.valid_before = later(raw.context.evaluated_at, 3600); }, /stale end/);
  broken((metric, raw) => { raw.result.definition_epoch = 'x'; }, /no definition/);
  broken((metric) => { metric.start_value = '-1'; }, /unsigned/);
  // Statuses without a comparison carry only their end.
  const stale = wire('counter_change');
  stale.context.evaluated_at = later(stale.context.evaluated_at, 86400);
  stale.context.valid_before = later(stale.context.evaluated_at, 3600);
  for (const window of Object.values(stale.result.windows)) {
    for (const metric of Object.values(window.metrics)) {
      Object.assign(metric, { status: 'stale_current', coverage: 'unavailable', start_value: null, absolute_change: null,
        percent_change: null, observed_start_at: null, actual_interval_seconds: null, reason_code: 'latest_observation_too_old' });
    }
  }
  const staleView = parse(stale).periods['28d'].metrics.subscribers;
  assert.deepEqual([staleView.status, staleView.end_value, staleView.reason], ['stale_current', '4400', 'The current stored observation is stale.']);
  const none = wire('counter_change');
  for (const window of Object.values(none.result.windows)) {
    for (const metric of Object.values(window.metrics)) {
      Object.assign(metric, { status: 'not_reported', coverage: 'unavailable', start_value: null, end_value: null, absolute_change: null,
        percent_change: null, observed_start_at: null, observed_end_at: null, actual_interval_seconds: null, expires_at: null,
        reason_code: 'no_observation' });
    }
  }
  assert.equal(parse(none).periods['7d'].metrics.views.reason, 'No stored channel observation is available.');

  const totals = (edit, pattern) => {
    const raw = wire('format_totals'); edit(raw.result.formats, raw.result);
    if (pattern) contract(() => overview.parseFormatTotals(raw, 'UCCleanHeaderTotals00000'), pattern);
    return () => overview.parseFormatTotals(raw, 'UCCleanHeaderTotals00000');
  };
  // Largest decimal counter stays a string and is formatted exactly.
  const big = totals((formats) => { formats.long.views = '18446744073709551615'; })();
  assert.equal(big.formats.long.views, '18446744073709551615');
  assert.equal(formatCount(big.formats.long.views, { compact: false }), '18,446,744,073,709,551,615');
  // A gated Shorts list: absent, no counts.
  const absent = totals((formats) => {
    formats.short = { status: 'absent', views: null, reason_code: 'playlist_not_found', scanned_inventory_count: null,
      canonical_inventory_count: null, known_view_count: null, missing_view_count: null, excluded: null, expires_at: later(PERIOD.end_at, 600 * 3600) };
  })();
  assert.deepEqual([absent.formats.short.status, absent.formats.short.excluded], ['absent', null]);
  assert.equal(overview.formatTotalDetail(absent.formats.short), 'YouTube reports no public list of this format.');
  // Stale clocks only → stale; any other gap → incomplete; the server status must agree.
  const staleOnly = totals((formats) => {
    Object.assign(formats.short, { status: 'stale', views: null, known_view_count: 3, missing_view_count: 1 });
    formats.short.excluded.stale_counter_count = 1;
  })();
  assert.equal(staleOnly.formats.short.status, 'stale');
  assert.equal(overview.formatTotalDetail(staleOnly.formats.short), '1 video needs refreshed values.');
  totals((formats) => { formats.stream.status = 'stale'; }, /list coverage/);
  totals((formats) => { formats.long.views = null; }, /unsigned/);
  totals((formats) => { formats.stream.views = '1'; }, /no total/);
  totals((formats) => { formats.long.scanned_inventory_count = null; }, /gated list has no counts/);
  totals((formats) => { formats.short.reason_code = 'inventory_sources_incomplete'; }, /list size|gate/);
  totals((formats) => { formats.long.excluded.type_resolution_withheld = 1; }, /proves the type/);
  totals((formats) => { formats.long.missing_view_count = 2; }, /disagrees with the classes|cover the list/);
  totals((formats, result) => { result.list_limit = 5; }, /list coverage/);
  totals((formats) => { formats.short.status = 'not_collected'; }, /not absent/);
}
console.log('header: 7D/28D evidence (full window within tolerance, partial interval), format totals and gates');

// ---------------------------------------------------------------------------
// a reply lives by its data clocks (context.valid_before), not 120 s.
{
  const raw = wire('fingerprint');
  const readAt = raw.context.evaluated_at.replace(/\.\d+Z$/, 'Z');
  const expiresAt = raw.context.valid_before;
  raw.source_read_at = readAt;
  raw.source_expires_at = expiresAt;
  const headers = { get: (name) => ({ 'X-Source-Read-At': readAt, 'X-Source-Expires-At': expiresAt })[name] ?? null };
  const started = 1000;
  const captured = source.captureSourceLifetime(raw, headers, started);
  const view = fingerprint(captured);
  const read = Date.parse(readAt);
  const lifetime = Date.parse(expiresAt) - read;
  assert.ok(lifetime > 5 * 3600 * 1000, 'about six hours on this reply');
  for (const part of [view, view.statistics, view.distributions.views, view.coverage]) {
    assert.equal(source.sourceExpired(part, read + 121_000, started + 121_000), false, 'no 120-second recalculation');
    assert.equal(source.sourceExpired(part, read + lifetime + 1, started + lifetime + 1), true);
  }
}
console.log('Overview parts expire at the reply\'s valid_before, never after 120 seconds');

// ---------------------------------------------------------------------------
// Client: the error table of the "overview" surface.
{
  const wait = async () => {};
  const failure = (code, status = 503) => Object.assign(new Error(code), { code, status });
  const run = async (errors, op = (client, signal) => client.rhythm(MAIN, 'long', KYIV, '28d', signal)) => {
    const calls = [];
    const client = overview.createOverviewClient(async (path, signal, init) => {
      calls.push({ path, body: init && JSON.parse(init.body) });
      if (errors.length) throw errors.shift();
      return wire('rhythm');
    }, { wait });
    return { result: await op(client, new AbortController().signal).catch((error) => error), calls };
  };
  let outcome = await run([failure('catalogue_preparing'), failure('catalogue_preparing')]);
  assert.equal(outcome.result.total, 56);
  assert.equal(outcome.calls.length, 3, 'two automatic retries while the catalogue prepares');
  assert.equal(outcome.calls[0].path, '/research/operations/execute');
  assert.equal(outcome.calls[0].body.operation, 'video.publication_rhythm.v1');
  outcome = await run([failure('catalogue_preparing'), failure('catalogue_preparing'), failure('catalogue_preparing')]);
  assert.equal(outcome.result.code, 'catalogue_preparing');
  outcome = await run([failure('cursor_context_changed', 409)]);
  assert.equal(outcome.result.total, 56, 'one repeat after a context change');
  outcome = await run([failure('cursor_context_changed', 409), failure('cursor_context_changed', 409)]);
  assert.equal(outcome.result.code, 'cursor_context_changed');
  outcome = await run([failure('query_busy', 429)]);
  assert.equal(outcome.result.code, 'query_busy', 'busy: error and Retry, no automatic repeat');
  assert.equal(outcome.calls.length, 1);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(overview.createOverviewClient(async () => wire('rhythm')).rhythm(MAIN, 'long', KYIV, '28d', controller.signal),
    (error) => error.name === 'AbortError');
  // Capabilities read through the same client (GET, no body).
  const calls = [];
  const caps = await overview.createOverviewClient(async (path, signal, init) => { calls.push([path, init]); return capabilitiesFixture; })
    .capabilities(new AbortController().signal);
  assert.deepEqual(calls, [['/research/capabilities', undefined]]);
  assert.ok(overview.overviewAvailable(caps));
  // The table's texts for the Overview.
  assert.equal(libraryErrorState(failure('policy_denied', 403), 'overview').message, 'Baseline is unavailable on this server');
  // A size or time refusal reads in plain words, not as the error's "HTTP 422".
  const refused = (code, limit) => Object.assign(new Error('The request was not accepted (HTTP 422).'), { code, status: 422 }, limit ? { limit } : {});
  assert.equal(libraryErrorState(refused('query_budget_exceeded', 'time'), 'overview').message,
    'This selection took too long to prepare. Narrow the filters or try again.');
  assert.equal(libraryErrorState(refused('query_budget_exceeded'), 'overview').message, 'This selection is too large to browse at once. Narrow the filters.');
  assert.equal(libraryErrorState(refused('dependency_disabled'), 'overview').message, 'Data needed for these filters is turned off in Collection fields.');
  assert.equal(libraryErrorState(new ExplorerContractError('x'), 'overview').action, 'incompatible');
  // Every client call parses its own reply strictly.
  const header = overview.createOverviewClient(async (path, signal, init) => (JSON.parse(init.body).operation === 'channel.format_totals.v1'
    ? wire('format_totals') : wire('counter_change')));
  assert.equal((await header.formatTotals('UCCleanHeaderTotals00000', new AbortController().signal)).formats.long.views, '6000');
  assert.equal((await header.counterChange('UCCounterFiveDayIntrvl00', new AbortController().signal)).periods['28d'].metrics.views.status, 'partial');
}
console.log('client: preparing retries, one repeat after a context change, busy shown, abort, capabilities');

// ---------------------------------------------------------------------------
// Readiness per type and the strict hydration check of formats[type].
{
  const channel = 'UC' + 'h'.repeat(22);
  const counts = { known_count: 5, hydrated_count: 4, unavailable_count: 0, awaiting_completion_count: 1, pending_count: 0,
    missing_queue_count: 0, identity_unresolved_count: 0, failed_count: 0 };
  const hydration = (formats = {}, root = {}) => ({
    contract_version: 2, channel_id: channel, status: 'ready', phase: 'ready', current_profile_ready: true, inventory_ready: true,
    progress: { processed: 10, total: 10, percent: 100, discovered: 10 }, estimate: null, confirmation_required: false,
    action_required: null, job: null, format_readiness_version: 1, ...root,
    formats: {
      long: { video_type: 'long', scope: 'initial_long_hydration', status: 'ready', phase: 'ready', inventory_ready: true,
        current_profile_ready: true, initial_check_complete: true, progress: { total: 10, processed: 10 } },
      // A scheduled Stream waits: the type's check is complete, the profile flag is not (counts awaiting_completion).
      short: { video_type: 'short', status: 'checked', inventory_status: 'completed', inventory_ready: true, current_profile_ready: false,
        initial_check_complete: true, runtime_enabled: true, counts_available: true, ...counts },
      stream: { video_type: 'stream', status: 'not_available', inventory_status: 'playlist_not_found', inventory_ready: true,
        current_profile_ready: true, initial_check_complete: true, runtime_enabled: true, counts_available: true,
        ...counts, known_count: 0, hydrated_count: 0, awaiting_completion_count: 0 },
      ...formats,
    },
  });
  assert.equal(contracts.parseExplorerHydration(hydration(), channel).status, 'ready');
  // Strict: unknown keys, another type, a ready profile without its list.
  const refuse = (edit) => { const value = hydration(); edit(value); assert.throws(() => contracts.parseExplorerHydration(value, channel), ExplorerContractError); };
  refuse((value) => { value.formats.short.extra = 1; });
  refuse((value) => { value.formats.short.video_type = 'stream'; });
  refuse((value) => { value.formats.short.status = 'maybe'; });
  refuse((value) => { value.formats.short.inventory_ready = 'yes'; });
  refuse((value) => { value.formats.short.inventory_ready = false; value.formats.short.current_profile_ready = true; });
  refuse((value) => { delete value.formats.stream; });
  refuse((value) => { value.format_readiness_version = 2; });
  refuse((value) => { delete value.format_readiness_version; });
}
console.log('historical hydration contract: type, status and keys validated independently');

// ---------------------------------------------------------------------------
// The bucket jump into Videos: the type's preset within the exact period of the reply.
{
  const view = fingerprint();
  const conditions = overview.overviewBucketConditions(view.period, 'views', '1m_10m', overview.overviewPresets(capabilities, 'long', 'views'));
  assert.deepEqual(conditions.facets.view_buckets, ['1m_10m']);
  assert.deepEqual(conditions.facets.cohort, { kind: 'overview', published_from: PERIOD.start_at,
    published_through: PERIOD.end_at });
  const max = overview.overviewBucketConditions({ start_at: null, end_at: view.period.end_at }, 'duration', 'under_5m',
    overview.overviewPresets(capabilities, 'long', 'duration'));
  assert.equal(max.facets.cohort.published_from, null, 'MAX: no start, unknown publication dates stay (is_unknown or lte)');
  assert.equal(overview.overviewBucketConditions(view.period, 'views', 'nope', overview.overviewPresets(capabilities, 'long', 'views')), null);
  assert.equal(overview.overviewBucketConditions(undefined, 'views', '1m_10m', overview.overviewPresets(capabilities, 'long', 'views')), null);
  // Shared groups open for Shorts too, and the Shorts and Streams duration buckets open their own Videos tab.
  assert.deepEqual(overview.overviewBucketConditions(view.period, 'likes', '1k_10k', overview.overviewPresets(capabilities, 'short', 'likes'))
    .facets.like_buckets, ['1k_10k']);
  assert.deepEqual(overview.overviewBucketConditions(view.period, 'duration', '15s_30s', overview.overviewPresets(capabilities, 'short', 'duration'))
    .facets.duration_buckets, ['15s_30s']);
  assert.deepEqual(overview.overviewBucketConditions(view.period, 'duration', '4h_plus', overview.overviewPresets(capabilities, 'stream', 'duration'))
    .facets.duration_buckets, ['4h_plus']);
  assert.equal(overview.overviewBucketConditions(view.period, 'duration', 'under_5m', overview.overviewPresets(capabilities, 'short', 'duration')), null,
    'a Long key is not a Shorts bucket');
}
console.log('bucket jump: preset of the type (Long, Shorts, Streams), exact server period, MAX without a start');

// ---------------------------------------------------------------------------
// Words by type: Long rows keep the long-form labels.
assert.deepEqual(overview.OVERVIEW_FORMAT_WORDS.long, { adjective: 'long-form', plural: 'long-form videos', tab: 'Videos' });
assert.deepEqual([overview.OVERVIEW_FORMAT_WORDS.short.plural, overview.OVERVIEW_FORMAT_WORDS.stream.plural], ['Shorts', 'Streams']);

// The adapter stays pure: no React, window, document or api.ts at import time.
{
  const text = read('../application/frontend/src/explorer/libraryData/overview.ts');
  assert.doesNotMatch(text, /from 'react'|\bwindow\.|\bdocument\.|from '\.\.\/api'/);
}
// The header reads list-inventory once per channel load. A read made before the profile was
// ready is repeated once it is only when it could not prove a complete inventory (the unlisted note needs one).
{
  const { reactHookHost, flush } = await import('./react_hook_host.mjs');
  const stub = new Proxy({}, { get: (_, key) => (key === '__esModule' ? true : () => undefined) });
  const header = (calls, host) => {
    const { outputText } = ts.transpileModule(read('../application/frontend/src/explorer/ChannelHeader.tsx'), { fileName: 'ChannelHeader.tsx',
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
    const never = () => new Promise(() => {});
    const imports = {
      react: host.React, '../sourceLifetimeReact': { useSourceExpiry() {} }, '../sourceLifetime.ts': { sourceExpired: () => false },
      './api': { isAbortError: (error) => error?.name === 'AbortError', loadExplorerProfile: never, requestJson: never,
        loadExplorerListInventory: (channelId, signal) => new Promise((resolve, reject) => calls.push({ channelId, signal, resolve, reject })) },
      './libraryData/overview': { createOverviewClient: () => ({ formatTotals: never, counterChange: never }), formatTotalDetail: () => '' },
    };
    const exports = {};
    const page = { addEventListener() {}, removeEventListener() {}, setTimeout: () => 0, clearTimeout() {} };
    new Function('require', 'exports', 'document', 'window', outputText)((name) => imports[name] ?? stub, exports, page, page);
    return exports;
  };
  const inventory = (complete) => ({ inventory: { complete, youtube_video_count: 30, unique_video_count: 10, formats: {}, source_expires_at: null } });
  const run = async (steps, initial = {}) => {
    const host = reactHookHost(), calls = [];
    const module = header(calls, host);
    let props = { channelId: MAIN, ready: false, reload: 0, totals: 0, ...initial };
    const context = () => ({ channelId: props.channelId, channel: undefined, currentProfileReady: props.ready, profileReloadToken: 0, setProfileReloadToken() {},
      formatTotalsReloadToken: props.totals, setFormatTotalsReloadToken() {}, listInventoryReloadToken: props.reload,
      counterDynamicsReloadToken: 0, setCounterDynamicsReloadToken() {}, setAnnouncement() {} });
    host.mount(() => module.useChannelHeader(context(), { folders: [], labels: [] }, undefined), null); await flush();
    for (const step of steps) { await step({ calls, set: (next) => { props = { ...props, ...next }; host.update(null); } }); await flush(); }
    host.unmount();
    return calls;
  };
  // A channel already loaded: the profile becomes ready after the first read answered complete.
  let calls = await run([({ calls }) => calls[0].resolve(inventory(true)), ({ set }) => set({ ready: true })]);
  assert.equal(calls.length, 1, 'one read per channel load');
  // Readiness arrives while the first read runs: it is neither cancelled nor repeated.
  calls = await run([({ set }) => set({ ready: true }), ({ calls }) => { assert.equal(calls[0].signal.aborted, false); calls[0].resolve(inventory(true)); }]);
  assert.equal(calls.length, 1);
  // An old report may settle after the list. Only a real job transition or Reload triggers another read.
  for (const settle of [call => call.resolve(inventory(false)), call => call.reject(new Error('HTTP 500'))]) {
    calls = await run([({ calls }) => settle(calls[0]), ({ set }) => set({ ready: true })]);
    assert.equal(calls.length, 1);
  }
  // A read made after readiness is final, even incomplete; Reload and a new channel read again.
  calls = await run([({ calls }) => calls[0].resolve(inventory(false)), ({ set }) => set({ reload: 1 }),
    ({ calls }) => calls[1].resolve(inventory(true)), ({ set }) => set({ channelId: 'UCOtherOverviewChannel00' })], { ready: true });
  assert.deepEqual(calls.map((call) => call.channelId), [MAIN, MAIN, 'UCOtherOverviewChannel00']);
  console.log('list-inventory once per channel load; historical job readiness alone does not repeat it.');

  // as the shell drives the header: the first hydration answer of a channel makes the profile ready and, in
  // the same update, bumps the format-totals token (the shell's own rule, hydrationInitialBecameComplete(undefined, …)).
  // The list followed that token, so opening a loaded channel read it 2 times, or 3 when the first read was incomplete.
  const hydration = moduleWith('../application/frontend/src/explorer/hydration.ts', {});
  const loaded = { status: 'ready', current_profile_ready: true, inventory_ready: true, all_initial_checks_complete: true };
  assert.equal(hydration.hydrationInitialBecameComplete(undefined, loaded), true, 'the first answer bumps the totals token');
  const firstAnswer = ({ set }) => set({ ready: hydration.hydrationProfileReady(loaded),
    totals: hydration.hydrationInitialBecameComplete(undefined, loaded) ? 1 : 0 });
  // The read answered (complete) before the first hydration answer: still one read.
  calls = await run([({ calls }) => calls[0].resolve(inventory(true)), firstAnswer]);
  assert.equal(calls.length, 1, 'one read when the channel opens');
  // The first hydration answer while the read runs: the read is neither cancelled nor repeated.
  calls = await run([firstAnswer, ({ calls }) => { assert.equal(calls[0].signal.aborted, false); calls[0].resolve(inventory(true)); }]);
  assert.equal(calls.length, 1);
  // The totals' own Retry or refresh reads the totals only.
  calls = await run([({ calls }) => calls[0].resolve(inventory(true)), ({ set }) => set({ totals: 7 })], { ready: true });
  assert.equal(calls.length, 1);
  // Reload (the shell's reloadCurrentView) bumps the list's own token, so Reload still reads it again (above).
  const workspace = ts.createSourceFile('ExplorerWorkspace.tsx', read('../application/frontend/src/explorer/ExplorerWorkspace.tsx'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let reload;
  const visit = (node) => { if (ts.isVariableDeclaration(node) && node.name.getText(workspace) === 'reloadCurrentView') reload = node.getText(workspace); ts.forEachChild(node, visit); };
  visit(workspace);
  assert.ok(reload, 'reloadCurrentView');
  const setters = ['setProfileReloadToken', 'setFormatTotalsReloadToken', 'setListInventoryReloadToken', 'setCounterDynamicsReloadToken',
    'setBaselineReloadToken', 'setCatalogueReloadToken'], bumped = [];
  const shell = {};
  new Function('exports', ...setters, 'overviewVisible', 'videosVisible', ts.transpileModule(`export const ${reload};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText)(shell, ...setters.map((name) => () => bumped.push(name)), false, false);
  shell.reloadCurrentView();
  assert.deepEqual(bumped, ['setProfileReloadToken', 'setFormatTotalsReloadToken', 'setListInventoryReloadToken', 'setCounterDynamicsReloadToken']);
  console.log('opening a channel reads list-inventory once; Reload reads it again.');
}
console.log('Library Overview and header adapter passed.');
