// Home "Latest videos" adapter: the
// library-scope query, key continuation, strict page reading, per-row
// lifetimes and the server pace median. Offline: no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as home from '../application/frontend/src/explorer/libraryData/home.ts';
import { FIELDS, OPERATIONS, ROUTES } from '../application/frontend/src/explorer/libraryData/wire.ts';
import { captureSourceLifetime, freshSourceRows, sourceExpired } from '../application/frontend/src/sourceLifetime.ts';

const contract = (fn) => assert.throws(fn, ExplorerContractError);
const A = 'UC' + 'A'.repeat(22);
const EVALUATED = '2026-10-29T12:00:00.123456Z';

function cell(value, { state = 'known', observed = '2026-10-29T11:00:00Z', expires = '2026-11-27T11:00:00Z', ref = 'x' } = {}) {
  return state === 'known'
    ? { value, state, provenance_ref: ref, observed_at: observed, expires_at: expires, observation: null }
    : { value: null, state, provenance_ref: ref, observed_at: null, expires_at: null, observation: null };
}

function row(id, published, { views = '1000', pace = '24000', paceState = 'known', expires = '2026-11-27T11:00:00Z' } = {}) {
  return {
    id,
    cells: {
      'video.id': cell(id, { expires: '2026-11-28T00:00:00Z' }),
      'video.channel_id': cell(A, { expires: '2026-11-28T00:00:00Z' }),
      'video.title': cell(`Title ${id}`),
      'video.thumbnail_url': cell(`https://img.youtube.com/vi/${id}/hqdefault.jpg`),
      'video.published_at': cell(published),
      'video.views': views === null ? cell(null, { state: 'unknown' }) : cell(views, { expires }),
      'video.likes': cell('7'),
      'video.comments': cell('3'),
      'video.lifetime_views_per_day': paceState === 'known' ? cell(pace, { expires }) : cell(null, { state: paceState }),
    },
  };
}

function page(rows, { hasMore = false, reasons = {} } = {}) {
  return {
    query_version: 1, query_fingerprint: 'a'.repeat(64),
    context: { source_id: 'public.videos.current.v1', source_schema_version: 1, registry_version: 10,
      evaluated_at: EVALUATED, valid_before: '2026-10-29T12:02:00.123456Z', consistency: 'current', time_zone: 'UTC' },
    columns: [...home.HOME_SELECT], provenance: {}, rows,
    coverage: { status: Object.keys(reasons).length ? 'partial' : 'complete', basis: 'channel_inventory',
      channels_in_scope: 3, channels_complete: 3, eligible_count: null, reasons },
    page: { has_more: hasMore, next_cursor: hasMore ? 'server-cursor' : null },
    warnings: ['current_pages_may_change'],
  };
}

// Query: library scope, publication window from the server clock, newest first, 20 rows, current data.
{
  const first = home.homeFeedQuery('7d');
  assert.deepEqual(first.scope, { kind: 'library' });
  assert.deepEqual(first.video_types, ['long']);
  assert.deepEqual(first.where, { field: 'video.published_at', op: 'within_last_hours_v1', value: '168' });
  assert.deepEqual(first.order_by.map((item) => [item.field, item.direction]), [['video.published_at', 'desc'], ['video.id', 'desc']]);
  assert.equal(first.consistency, 'current');
  assert.deepEqual(first.page, { size: 20 });
  assert.ok(first.select.includes(FIELDS.lifetimeViewsPerDay));
  assert.equal(home.homeFeedQuery('24h').where.value, '24');
  assert.equal(home.homeFeedQuery('28d').where.value, '672');
  assert.deepEqual(home.homeFeedQuery('7d', undefined, 'short').video_types, ['short']);
  assert.throws(() => home.homeFeedQuery('1y'));
  assert.throws(() => home.homeFeedQuery('7d', undefined, 'clips'));
  const pace = home.homePaceRequest('28d');
  assert.equal(pace.operation, OPERATIONS.paceMedian);
  assert.deepEqual(pace.parameters, {});
  assert.deepEqual(pace.query, home.homeFeedQuery('28d'));
}
console.log('home.ts: library-scope query, server window, order and pace request passed.');

// Pages: strict rows, published_from from the server clock, per-row lifetimes, key continuation.
{
  const rows = [row('ccccccccccc', '2026-10-29T10:00:00Z'), row('bbbbbbbbbbb', '2026-10-29T09:00:00Z'),
    row('aaaaaaaaaab', '2026-10-29T09:00:00Z', { views: null, paceState: 'unknown', expires: '2026-11-01T00:00:00Z' })];
  const parsed = home.parseHomeFeedPage(page(rows, { hasMore: true }), '24h');
  assert.equal(parsed.generated_at, EVALUATED);
  assert.equal(parsed.published_from, '2026-10-28T12:00:00.123Z');
  assert.deepEqual(parsed.items.map((item) => item.video_id), ['ccccccccccc', 'bbbbbbbbbbb', 'aaaaaaaaaab']);
  assert.equal(parsed.items[0].views_per_day, '24000');
  assert.equal(parsed.items[0].checked_at, '2026-10-29T11:00:00Z');
  assert.equal(parsed.items[2].views, null);
  assert.equal(parsed.items[2].views_per_day, null);
  assert.equal(parsed.items[2].checked_at, null);
  // Each row lives by its own clocks, not by the 120-second page.
  assert.equal(parsed.items[0].source_expires_at, '2026-11-27T11:00:00Z');
  assert.equal(parsed.items[2].source_expires_at, '2026-11-27T11:00:00Z');
  assert.equal(parsed.partial, false);
  assert.equal(parsed.has_more, true);
  // The next page: everything at or before 09:00 except both videos already shown at 09:00.
  const next = home.homeFeedQuery('24h', parsed.next_cursor);
  assert.deepEqual(next.where, { all: [
    { field: 'video.published_at', op: 'within_last_hours_v1', value: '24' },
    { field: 'video.published_at', op: 'lte', value: '2026-10-29T09:00:00Z' },
    { not: { field: 'video.id', op: 'in', value: ['aaaaaaaaaab', 'bbbbbbbbbbb'] } },
  ] });
  // Ties that continue across pages keep every shown id.
  const second = home.parseHomeFeedPage(page([row('aaaaaaaaaaa', '2026-10-29T09:00:00Z')], { hasMore: true }), '24h', parsed.next_cursor);
  assert.deepEqual(home.decodeHomeKey(second.next_cursor).tied.sort(), ['aaaaaaaaaaa', 'aaaaaaaaaab', 'bbbbbbbbbbb']);
  const end = home.parseHomeFeedPage(page([row('aaaaaaaaaaa', '2026-10-29T09:00:00Z')]), '24h', parsed.next_cursor);
  assert.equal(end.next_cursor, null);
  // A partial library (an incomplete channel inventory) is read, not refused.
  assert.equal(home.parseHomeFeedPage(page(rows.slice(0, 1), { reasons: { inventory_incomplete: 1 } }), '24h').partial, true);
  // Lifetimes: rows keep their own deadline when the page (120 s) expires.
  const captured = captureSourceLifetime(page(rows), { get: (name) => ({
    'X-Source-Read-At': EVALUATED.replace('.123456', ''), 'X-Source-Expires-At': '2026-10-29T12:02:00Z' })[name] ?? null }, performance.now());
  const withClock = home.parseHomeFeedPage(captured, '24h');
  const later = Date.parse('2026-10-29T12:05:00Z');
  assert.equal(sourceExpired(withClock, later, Number.NEGATIVE_INFINITY), true);
  assert.equal(freshSourceRows(withClock.items, later, Number.NEGATIVE_INFINITY).length, 3);
  assert.equal(freshSourceRows(withClock.items, Date.parse('2026-11-02T00:00:00Z'), Number.NEGATIVE_INFINITY).length, 3);
}
console.log('home.ts: strict pages, server period start, per-row lifetimes and tie-safe continuation passed.');

// Strictness: anything outside the contract is incompatible, never a partial feed.
{
  const good = [row('ccccccccccc', '2026-10-29T10:00:00Z'), row('bbbbbbbbbbb', '2026-10-29T09:00:00Z')];
  contract(() => home.parseHomeFeedPage({ ...page(good), extra: 1 }, '24h'));
  contract(() => home.parseHomeFeedPage({ ...page(good), columns: [...home.HOME_SELECT].reverse() }, '24h'));
  contract(() => home.parseHomeFeedPage(page([good[1], good[0]]), '24h'));
  contract(() => home.parseHomeFeedPage(page([good[0], good[0]]), '24h'));
  contract(() => home.parseHomeFeedPage(page([row('ccccccccccc', '2026-10-28T11:59:59Z')]), '24h'));
  contract(() => home.parseHomeFeedPage(page([row('ccccccccccc', '2026-10-29T10:00:00Z', { pace: '-1' })]), '24h'));
  contract(() => home.parseHomeFeedPage(page([row('ccccccccccc', '2026-10-29T10:00:00Z', { views: '1.5' })]), '24h'));
  contract(() => home.parseHomeFeedPage({ ...page(good), page: { has_more: true, next_cursor: null } }, '24h'));
  contract(() => home.parseHomeFeedPage({ ...page([]), page: { has_more: true, next_cursor: 'x' } }, '24h'));
  contract(() => home.parseHomeFeedPage({ ...page(good), context: { ...page(good).context, consistency: 'frozen' } }, '24h'));
  const wrongImage = row('ccccccccccc', '2026-10-29T10:00:00Z');
  wrongImage.cells['video.thumbnail_url'] = cell('https://example.com/a.jpg');
  contract(() => home.parseHomeFeedPage(page([wrongImage]), '24h'));
  const unknownDate = row('ccccccccccc', '2026-10-29T10:00:00Z');
  unknownDate.cells['video.published_at'] = cell(null, { state: 'expired' });
  contract(() => home.parseHomeFeedPage(page([unknownDate]), '24h'));
  const cursor = home.parseHomeFeedPage(page(good, { hasMore: true }), '24h').next_cursor;
  contract(() => home.parseHomeFeedPage(page([row('bbbbbbbbbbb', '2026-10-29T09:00:00Z')]), '24h', cursor));
  contract(() => home.parseHomeFeedPage(page([row('ddddddddddd', '2026-10-29T11:00:00Z')]), '24h', cursor));
  contract(() => home.decodeHomeKey('2026-10-29T09:00:00Z|bbbbbbbbbbb'));
  contract(() => home.decodeHomeKey('home-key-v1:{"p":"2026-10-29T09:00:00Z","t":[]}'));
  // 18446744073709551615 stays a string.
  const huge = home.parseHomeFeedPage(page([row('ccccccccccc', '2026-10-29T10:00:00Z', { views: '18446744073709551615' })]), '24h');
  assert.equal(huge.items[0].views, '18446744073709551615');
}
console.log('home.ts: contract errors for shape, order, period, values and continuation passed.');

// Pace median: whole-period operation; ratio and marker inputs come from the server.
{
  const operation = (result, context = {}) => ({ version: 1, operation: OPERATIONS.paceMedian, operation_fingerprint: 'b'.repeat(64),
    context: { source_id: 'public.videos.current.v1', registry_version: 10, evaluated_at: EVALUATED,
      valid_before: '2026-10-29T12:40:00Z', preferences_revision: 'existing-v1', consistency: 'current',
      observation_age_limit_seconds: null, ...context },
    coverage: { status: 'complete', basis: 'channel_inventory', channels_in_scope: 3, channels_complete: 3, eligible_count: null, reasons: {} },
    result, provenance: { origin: 'calculated' }, warnings: ['current_population_may_change'] });
  const epoch = 'youtube.views.play_start.20260824';
  const result = { field: FIELDS.lifetimeViewsPerDay, method: 'median_of_lifetime_views_per_day_v1', population_count: 5,
    known_count: 4, missing: { unknown: 1, expired: 0, unavailable: 0 }, definition: { state: 'known', epoch, excluded_count: 0 }, median: '12000' };
  const median = home.parseHomePace(operation(result), '7d');
  assert.deepEqual([median.median, median.population, median.known, median.valid_before], ['12000', 5, 4, '2026-10-29T12:40:00Z']);
  assert.equal(home.parseHomePace(operation({ ...result, known_count: 0, missing: { unknown: 5, expired: 0, unavailable: 0 }, median: null }), '7d').median, null);
  // A value without a recorded views definition is left out and counted; two definitions withhold the median.
  assert.equal(home.parseHomePace(operation({ ...result, known_count: 3, definition: { state: 'known', epoch, excluded_count: 1 } }), '7d').known, 3);
  assert.equal(home.parseHomePace(operation({ ...result, known_count: 0, median: null,
    definition: { state: 'mixed_definition_epochs', epoch: null, excluded_count: 4 } }), '7d').median, null);
  contract(() => home.parseHomePace(operation({ ...result, known_count: 3 }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, definition: undefined }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, definition: { state: 'known', epoch: null, excluded_count: 0 } }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, definition: { state: 'mixed_definition_epochs', epoch: null, excluded_count: 0 } }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, definition: { state: 'known', epoch, excluded_count: 0, extra: 1 } }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, median: null }), '7d'));
  contract(() => home.parseHomePace(operation({ ...result, median: '-1' }), '7d'));
  contract(() => home.parseHomePace({ ...operation(result), operation: 'video.summary.v1' }, '7d'));
  contract(() => home.parseHomePace(operation(result, { valid_before: EVALUATED }), '7d'));
  assert.equal(home.paceRatio('24000', '12000'), 2);
  assert.equal(home.paceRatio('0', '12000'), 0);
  assert.equal(home.paceRatio(null, '12000'), undefined);
  assert.equal(home.paceRatio('24000', '0'), undefined);
  assert.equal(home.paceRatio('24000', null), undefined);
  assert.match(home.HOME_PACE_HELP, /median of all videos in this period/);
}
console.log('home.ts: pace median of the whole period and ratio inputs passed.');

// The HTTP read envelope (web/middleware.py adds source_read_at / source_expires_at to every
// research reply whose read registered a deadline) is accepted and checked, as the Inspector adapter does.
{
  const READ = '2026-10-29T12:00:01.5Z', VALID = '2026-10-29T12:02:00.123456Z';
  const feed = (extra) => ({ ...page([row('aaaaaaaaaaa', '2026-10-29T09:00:00Z')]), ...extra });
  assert.equal(home.parseHomeFeedPage(feed({ source_read_at: READ, source_expires_at: VALID }), '24h').items.length, 1);
  assert.equal(home.parseHomeFeedPage(feed({ source_read_at: READ, source_expires_at: '2026-10-29T12:01:00Z' }), '24h').items.length, 1);
  assert.equal(home.parseHomeFeedPage(feed({ source_read_at: null, source_expires_at: null }), '24h').items.length, 1);
  contract(() => home.parseHomeFeedPage(feed({ source_read_at: READ, source_expires_at: '2026-10-29T12:02:00.123457Z' }), '24h'));
  contract(() => home.parseHomeFeedPage(feed({ source_read_at: 'yesterday', source_expires_at: VALID }), '24h'));
  contract(() => home.parseHomeFeedPage(feed({ source_read_at: READ, source_expires_at: VALID, source_other: 1 }), '24h'));
  const pace = (extra) => ({ version: 1, operation: OPERATIONS.paceMedian, operation_fingerprint: 'b'.repeat(64),
    context: { source_id: 'public.videos.current.v1', registry_version: 10, evaluated_at: EVALUATED, valid_before: '2026-10-29T12:40:00Z',
      preferences_revision: 'existing-v1', consistency: 'current', observation_age_limit_seconds: null },
    coverage: { status: 'complete', basis: 'channel_inventory', channels_in_scope: 3, channels_complete: 3, eligible_count: null, reasons: {} },
    result: { field: FIELDS.lifetimeViewsPerDay, method: 'median_of_lifetime_views_per_day_v1', population_count: 1, known_count: 1,
      missing: { unknown: 0, expired: 0, unavailable: 0 }, definition: { state: 'known', epoch: 'e', excluded_count: 0 }, median: '24000' },
    provenance: { origin: 'calculated' }, warnings: ['current_population_may_change'], ...extra });
  assert.equal(home.parseHomePace(pace({ source_read_at: READ, source_expires_at: '2026-10-29T12:40:00Z' }), '7d').median, '24000');
  contract(() => home.parseHomePace(pace({ source_read_at: READ, source_expires_at: '2026-10-29T12:40:00.000001Z' }), '7d'));
  contract(() => home.parseHomePace(pace({ source_read_at: 5 }), '7d'));
}
console.log('home.ts: the HTTP read envelope of research replies is accepted and bounded by the result.');

// Requests go through the passed request function to the shared routes.
{
  const calls = [];
  const request = async (path, signal, init) => {
    calls.push({ path, body: JSON.parse(init.body), method: init.method });
    return path === ROUTES.execute ? page([row('ccccccccccc', '2026-10-29T10:00:00Z')])
      : { version: 1, operation: OPERATIONS.paceMedian, operation_fingerprint: 'b'.repeat(64),
        context: { evaluated_at: EVALUATED, valid_before: '2026-10-29T12:40:00Z' },
        coverage: { status: 'complete', reasons: {} },
        result: { field: FIELDS.lifetimeViewsPerDay, method: 'm', population_count: 1, known_count: 1,
          missing: { unknown: 0, expired: 0, unavailable: 0 }, definition: { state: 'known', epoch: 'e', excluded_count: 0 }, median: '24000' }, provenance: {} };
  };
  const feed = await home.loadHomeFeedPage(request, '7d', undefined);
  const pace = await home.loadHomePace(request, '7d');
  assert.equal(feed.items.length, 1);
  assert.equal(pace.median, '24000');
  assert.deepEqual(calls.map((call) => [call.path, call.method]), [[ROUTES.execute, 'POST'], [ROUTES.operations, 'POST']]);
  assert.deepEqual(calls[0].body, home.homeFeedQuery('7d'));
}
console.log('home.ts: requests through the shared routes passed.');

// Pure module: no React, window, document or api import at load time.
{
  const source = readFileSync(new URL('../application/frontend/src/explorer/libraryData/home.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /from 'react'|\bwindow\.|\bdocument\.|from '\.\.\/api'/);
  const component = readFileSync(new URL('../application/frontend/src/explorer/ExplorerRecentVideos.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(component, /loadExplorerRecentVideos|medianPace = median\(/, 'Home uses shared feed and population median');
  assert.match(component, /loadHomeFeedPage/);
  assert.match(component, /Load 20 more/);
  assert.match(component, /Live check every 30 seconds/);
}
// Real replies of the current server (tests/library_wire_fixtures_pg.py on the research_home_pg seed, temporary
// PostgreSQL 17): the browser builds the same requests and reads the replies, so a server shape change cannot leave
// this parser stale while the hand-made pages above stay green.
{
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/library_home_wire.json', import.meta.url), 'utf8'));
  // As requestJson delivers a reply: the source clock of the HTTP layer.
  const delivered = (response) => captureSourceLifetime(structuredClone(response), { get: (name) => (name === 'X-Source-Expires-At'
    ? response.context.valid_before : name === 'X-Source-Read-At' ? response.context.evaluated_at : null) }, performance.now());
  const first = fixture.feed_7d_first, next = fixture.feed_7d_next, pace = fixture.pace_7d;
  assert.deepEqual(home.homeFeedQuery('7d'), first.request);
  assert.deepEqual(home.homePaceRequest('7d'), pace.request);
  const page = home.parseHomeFeedPage(delivered(first.response), '7d');
  assert.equal(page.items.length, 20);
  assert.equal(page.has_more, true);
  assert.equal(page.generated_at, first.response.context.evaluated_at);
  assert.deepEqual(page.items.map((item) => item.video_id), first.response.rows.map((row) => row.id));
  assert.ok(page.items.every((item) => item.views_per_day === null || /^\d+(\.\d{1,6})?$/.test(item.views_per_day)));
  // "Load 20 more" after the last row and every shown row at its instant: the exact request the generator ran.
  assert.deepEqual(home.decodeHomeKey(page.next_cursor), next.key);
  assert.deepEqual(home.homeFeedQuery('7d', page.next_cursor), next.request);
  const more = home.parseHomeFeedPage(delivered(next.response), '7d', page.next_cursor);
  assert.equal(more.items.length, next.response.rows.length);
  assert.ok(more.items.every((item) => !page.items.some((shown) => shown.video_id === item.video_id)), 'no row twice');
  const median = home.parseHomePace(delivered(pace.response), '7d');
  assert.equal(median.median, pace.response.result.median);
  assert.ok(median.known > 0 && median.known <= median.population);
}
console.log('Home on real replies: the same requests, first page, "Load 20 more" and the pace median parsed.');

console.log('Library Home adapter passed.');
