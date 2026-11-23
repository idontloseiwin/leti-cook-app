// Library adapter core: request builders, strict readers,
// frozen session identity, coverage notice, error table, gates and the
// translation client. Offline: no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as wire from '../application/frontend/src/explorer/libraryData/wire.ts';
import * as strict from '../application/frontend/src/explorer/libraryData/strict.ts';
import * as envelope from '../application/frontend/src/explorer/libraryData/envelope.ts';
import * as errors from '../application/frontend/src/explorer/libraryData/errors.ts';
import { libraryGates } from '../application/frontend/src/explorer/libraryData/capabilities.ts';
import * as conditions from '../application/frontend/src/explorer/libraryData/conditions.ts';
import * as saved from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
import { createInitialCatalogueConditions, normalizeCatalogueConditions } from '../application/frontend/src/explorer/catalogueQuery.ts';
import { INITIAL_EXPLORER_FILTERS } from '../application/frontend/src/explorer/filters.ts';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const contract = (fn) => assert.throws(fn, ExplorerContractError);
const channel = 'UC' + 'A'.repeat(22);
const fixture = JSON.parse(read('./fixtures/library_translation_cases.json'));
const several = fixture.cases.find((item) => item.name === 'several conditions in table order');
const translated = {
  query_version: 1, source: { id: wire.VIDEO_SOURCE }, scope: { kind: 'channel', id: channel },
  video_types: ['long'], where: several.expected.where, order_by: several.expected.order_by,
  time: { mode: 'current', timezone: 'UTC' },
};

// wire.ts: the frozen Library page keeps the translated membership and adds only presentation.
{
  const first = wire.libraryQuery(translated, { timeZone: 'Europe/Berlin', page: { anchorId: 'abcdefghijk' } });
  assert.deepEqual(first, {
    query_version: 1, source: { id: 'public.videos.current.v1' }, scope: { kind: 'channel', id: channel },
    video_types: ['long'], select: [...wire.LIBRARY_SELECT], where: several.expected.where,
    order_by: several.expected.order_by.map((item) => ({ field: item.field, direction: item.direction, nulls: 'last' })),
    time: { mode: 'current', timezone: 'Europe/Berlin' }, coverage: { mode: 'available', required_fields: [] },
    consistency: 'frozen', page: { size: 50, anchor_id: 'abcdefghijk' },
  });
  assert.ok(!first.select.includes('video.tags') && !first.select.includes('video.description'), 'omit large fields');
  assert.deepEqual(wire.libraryQuery(translated, { timeZone: 'UTC', page: { after: 'opaque' } }).page, { size: 50, after: 'opaque' });
  assert.deepEqual(wire.libraryQuery({ ...translated, where: undefined }, { timeZone: 'UTC' }).page, { size: 50 });
  assert.ok(!('where' in wire.libraryQuery({ ...translated, where: undefined }, { timeZone: 'UTC' })));
  const growth = wire.libraryQuery(translated, { timeZone: 'UTC', growthFields: [wire.FIELDS.viewsGained28d] });
  assert.equal(growth.select.at(-1), 'observation.views_gained_28d');
  assert.throws(() => wire.libraryQuery(translated, { timeZone: 'UTC', growthFields: ['video.id'] }), /Duplicate/);
  for (const change of [
    { query_version: 2 }, { source: { id: 'public.channels.current.v1' } }, { scope: { kind: 'folder', id: channel } },
    { scope: { kind: 'channel', id: 'UCshort' } }, { video_types: ['long', 'short'] }, { video_types: ['live'] }, { order_by: [] },
  ]) assert.throws(() => wire.libraryQuery({ ...translated, ...change }, { timeZone: 'UTC' }), JSON.stringify(change));
  for (const size of [0, 201, 1.5]) assert.throws(() => wire.libraryQuery(translated, { timeZone: 'UTC', page: { size } }));
  // Zones: a v1 translation stays at UTC (dates already instants); local-date fields need the same zone.
  const local = { ...translated, where: { field: wire.FIELDS.publishedDate, op: 'gte', value: '2026-03-29' } };
  assert.throws(() => wire.libraryQuery(local, { timeZone: 'Europe/Berlin' }), /time zone/);
  assert.equal(wire.libraryQuery({ ...local, time: { mode: 'current', timezone: 'Europe/Berlin' } }, { timeZone: 'Europe/Berlin' }).time.timezone, 'Europe/Berlin');
  assert.throws(() => wire.libraryQuery({ ...translated, time: { timezone: 'Asia/Tokyo' } }, { timeZone: 'Europe/Berlin' }), /time zone/);
  assert.equal(wire.usesLocalDateFields({ all: [{ any: [{ not: { field: 'video.published_year', op: 'eq', value: '2024' } }] }] }), true);
  assert.equal(wire.usesLocalDateFields(several.expected.where), false);
  // Operations: a frozen result travels only as parameters.population, under the server's key `frozen_ref`
  // (inspection.population_reference refuses any other key).
  assert.deepEqual(wire.operationRequest(wire.OPERATIONS.facets, first, { groups: ['views'] }, 'ref'),
    { version: 1, operation: 'video.facets.v1', query: first, parameters: { groups: ['views'], population: { kind: 'frozen', frozen_ref: 'ref' } } });
  assert.equal(JSON.stringify(wire.operationRequest(wire.OPERATIONS.facets, first, {}, 'ref')).includes('token'), false);
  // Extra frozen fields come after the card fields, then growth: the matched set does not change.
  assert.deepEqual(wire.libraryQuery(translated, { timeZone: 'UTC', fields: ['video.title_length'], growthFields: ['observation.views_gained_28d'] }).select,
    [...wire.LIBRARY_SELECT, 'video.title_length', 'observation.views_gained_28d']);
  assert.deepEqual(wire.operationRequest(wire.OPERATIONS.summary, first).parameters, {});
  assert.throws(() => wire.operationRequest(wire.OPERATIONS.summary, first, { population: { kind: 'query' } }, 'ref'));
  assert.deepEqual(wire.frozenIdsRequest('ref-1'), { version: 1, frozen_ref: 'ref-1' });
  for (const bad of ['', 'a b', 'x'.repeat(4097), 7]) assert.throws(() => wire.frozenIdsRequest(bad));
}
console.log('wire.ts: frozen page request, zones, operation population and frozen ids passed.');

// strict.ts: closed shapes, big numbers as strings, instants, cells.
{
  assert.deepEqual(strict.closedObject({ a: 1 }, 'x', ['a'], ['b']), { a: 1 });
  contract(() => strict.closedObject({ a: 1, c: 2 }, 'x', ['a'], ['b']));
  contract(() => strict.closedObject({}, 'x', ['a']));
  contract(() => strict.closedObject([], 'x', []));
  contract(() => strict.closedObject(new Date(), 'x', []));
  assert.equal(strict.uintString('18446744073709551615', 'v'), '18446744073709551615');
  for (const bad of ['18446744073709551616', '01', '-1', '1.5', '', 5, ' 1', '1e3']) contract(() => strict.uintString(bad, 'v'));
  for (const good of ['0', '-5', '12.25', '-0.5', '9007199254740993']) assert.equal(strict.signedDecimal(good, 'v'), good);
  for (const bad of ['-0', '-0.0', '1.', '.5', '+1', '1.1234567', '00', 1]) contract(() => strict.signedDecimal(bad, 'v'));
  assert.equal(strict.count(50000, 'n', 50000), 50000);
  for (const bad of [-1, 1.5, '3', 50001, Number.MAX_SAFE_INTEGER + 1]) contract(() => strict.count(bad, 'n', bad === Number.MAX_SAFE_INTEGER + 1 ? undefined : 50000));
  for (const good of ['2026-10-29T12:00:00Z', '2026-10-29T12:00:00.123456Z', '0001-01-01T00:00:00Z']) assert.equal(strict.utcInstant(good, 't'), good);
  for (const bad of ['2026-10-29T12:00:00+00:00', '2026-10-29T12:00:00.1234567Z', '2026-02-30T00:00:00Z', '2026-10-29 12:00:00Z', '2026-10-29T24:00:00Z', null]) {
    contract(() => strict.utcInstant(bad, 't'));
  }
  assert.equal(strict.instantMilliseconds('2026-10-29T12:00:00.123456Z'), Date.UTC(2026, 9, 29, 12, 0, 0, 123));
  assert.equal(strict.enumValue('frozen', 'c', ['current', 'frozen']), 'frozen');
  contract(() => strict.enumValue('live', 'c', ['current', 'frozen']));
  assert.equal(strict.videoId('abcdefghij_', 'id'), 'abcdefghij_');
  for (const bad of ['abc', '../unsafe/id', 'abcdefghijkl']) contract(() => strict.videoId(bad, 'id'));
  assert.deepEqual(strict.cellPresentation({ state: 'known', value: '18446744073709551615', observed_at: 'x' }, 'c'), { state: 'known', value: '18446744073709551615' });
  for (const state of ['unknown', 'expired', 'unavailable']) {
    assert.deepEqual(strict.cellPresentation({ state, value: null }, 'c'), { state });
    contract(() => strict.cellPresentation({ state, value: '5' }, 'c'));
  }
  contract(() => strict.cellPresentation({ state: 'known' }, 'c'));
  contract(() => strict.cellPresentation({ state: 'stale', value: null }, 'c'));
}
console.log('strict.ts: closed objects, 2^64-1 strings, instants, enumerations and cell states passed.');

// envelope.ts: one frozen session = fingerprint + evaluated_at + valid_before + total (tokens differ every time).
const page = (overrides = {}) => ({
  query_version: 1, query_fingerprint: '6cb1bdfde7af' + 'a'.repeat(52),
  context: { source_id: wire.VIDEO_SOURCE, evaluated_at: '2026-10-29T12:00:00Z', valid_before: '2026-10-29T12:10:00Z',
    consistency: 'frozen', time_zone: 'Europe/Berlin', frozen_ref: 'wRv-' + Math.random() },
  page: { has_more: true, next_cursor: 'n-' + Math.random(), previous_cursor: null, start_index: 0, total_count: 120, anchor_id: null },
  ...overrides,
});
{
  const first = envelope.frozenSessionIdentity(page());
  assert.deepEqual(first, { query_fingerprint: '6cb1bdfde7af' + 'a'.repeat(52), evaluated_at: '2026-10-29T12:00:00Z', valid_before: '2026-10-29T12:10:00Z', total_count: 120 });
  assert.equal(envelope.frozenSessionKey(first), `${'6cb1bdfde7af' + 'a'.repeat(52)}\n2026-10-29T12:00:00Z\n2026-10-29T12:10:00Z\n120`);
  envelope.requireSameFrozenSession(first, envelope.frozenSessionIdentity(page()));
  const ids = { version: 1, query_fingerprint: first.query_fingerprint, context: { evaluated_at: first.evaluated_at, valid_before: first.valid_before }, ids: [], total_count: 120 };
  assert.equal(envelope.frozenSessionKey(envelope.frozenSessionIdentity(ids)), envelope.frozenSessionKey(first));
  contract(() => envelope.requireSameFrozenSession(first, envelope.frozenSessionIdentity({ ...ids, total_count: 119 })));
  contract(() => envelope.requireSameFrozenSession(first, envelope.frozenSessionIdentity(page({ context: { ...page().context, evaluated_at: '2026-10-29T12:00:01Z' } }))));
  for (const broken of [
    page({ query_fingerprint: 'abc' }), page({ query_fingerprint: 'A'.repeat(64) }),
    page({ context: { ...page().context, valid_before: '2026-10-29T12:00:00Z' } }),
    page({ page: { ...page().page, total_count: 50001 } }), page({ context: { ...page().context, evaluated_at: '2026-10-29T12:00:00+00:00' } }),
  ]) contract(() => envelope.frozenSessionIdentity(broken));

  const notice = envelope.coverageNotice({ status: 'partial', basis: 'channel_inventory',
    reasons: { source_rows_unavailable: 2, source_rows_expiring: 1, rows_with_expired_values: 4, inventory_incomplete: 1, new_reason: 3, zero_reason: 0 } });
  assert.deepEqual(notice, { partial: true, withheldRows: 3, staleValueRows: 4, inventoryIncomplete: 1, other: [{ code: 'new_reason', count: 3 }] });
  assert.deepEqual(envelope.coverageNotice({ status: 'complete', reasons: {} }), { partial: false, withheldRows: 0, staleValueRows: 0, inventoryIncomplete: 0, other: [] });
  for (const bad of [{ status: 'done', reasons: {} }, { status: 'partial', reasons: { a: -1 } }, { status: 'partial', reasons: { 'Bad Code': 1 } }, { status: 'partial', reasons: [] }]) {
    contract(() => envelope.coverageNotice(bad));
  }

  assert.deepEqual(envelope.frozenPagePosition({ has_more: true, next_cursor: 'n', previous_cursor: 'p', start_index: 50, total_count: 120, anchor_id: 'abcdefghijk' }, 50),
    { hasMore: true, nextCursor: 'n', previousCursor: 'p', startIndex: 50, totalCount: 120, anchorId: 'abcdefghijk' });
  assert.equal(envelope.frozenPagePosition({ has_more: false, next_cursor: null, previous_cursor: 'p', start_index: 100, total_count: 120, anchor_id: null }, 20).hasMore, false);
  for (const [bad, rows] of [
    [{ has_more: true, next_cursor: null, previous_cursor: null, start_index: 0, total_count: 120, anchor_id: null }, 50],
    [{ has_more: false, next_cursor: null, previous_cursor: null, start_index: 0, total_count: 120, anchor_id: null }, 50],
    [{ has_more: false, next_cursor: null, previous_cursor: null, start_index: 0, total_count: 10, anchor_id: null }, 50],
    [{ has_more: false, next_cursor: null, previous_cursor: null, start_index: 50, total_count: 60, anchor_id: null }, 10],
    [{ has_more: false, next_cursor: null, previous_cursor: null, start_index: 0, total_count: 1, anchor_id: null, extra: 1 }, 1],
  ]) contract(() => envelope.frozenPagePosition(bad, rows));
}
console.log('envelope.ts: session key, same-session check, coverage notice with open reasons and page invariants passed.');

// errors.ts: the whole design table, code by surface.
{
  const expected = {
    cursor_expired: ['retry_once', 'expired', 'session_message', 'await_session', 'inactive', 'error'],
    cursor_context_changed: ['retry_once', 'expired_changed', 'session_message', 'await_session', 'inactive', 'retry_once'],
    query_busy: ['error', 'error', 'session_message', 'error', 'error', 'error'],
    catalogue_preparing: ['preparing', 'preparing', 'preparing', 'preparing', 'preparing', 'preparing'],
    backend_unavailable: ['error', 'error', 'session_message', 'error', 'error', 'error'],
    client_timeout: ['error', 'error', 'session_message', 'error', 'error', 'error'],
    query_budget_exceeded: ['too_large', 'error', 'session_message', 'error', 'error', 'error'],
    policy_denied: ['unavailable', 'error', 'session_message', 'unavailable', 'unavailable', 'unavailable'],
    grant_required: ['unavailable', 'error', 'session_message', 'unavailable', 'unavailable', 'unavailable'],
    field_disabled: ['blocked', 'error', 'session_message', 'error', 'error', 'error'],
    dependency_disabled: ['blocked', 'error', 'session_message', 'error', 'error', 'error'],
    source_unavailable: ['channel_unavailable', 'expired', 'session_message', 'error', 'unavailable', 'error'],
    invalid_query: ['invalid_field', 'error', 'session_message', 'error', 'error', 'error'],
    incomplete_source: ['incompatible', 'incompatible', 'session_message', 'incompatible', 'error', 'incompatible'],
    unsupported_field: ['incompatible', 'incompatible', 'session_message', 'incompatible', 'error', 'incompatible'],
    unsupported_operator: ['incompatible', 'incompatible', 'session_message', 'incompatible', 'error', 'incompatible'],
    hot_runtime_not_ready: ['runtime_not_ready', 'error', 'session_message', 'error', 'error', 'error'],
    something_new: ['error', 'error', 'session_message', 'error', 'error', 'error'],
  };
  class ApiError extends Error { constructor(code, path) { super(`server says ${code}`); this.code = code; this.status = 409; if (path) this.path = path; } }
  for (const [code, actions] of Object.entries(expected)) {
    errors.LIBRARY_ERROR_SURFACES.forEach((surface, index) => {
      assert.equal(errors.libraryErrorState(new ApiError(code), surface).action, actions[index], `${code} on ${surface}`);
    });
  }
  errors.LIBRARY_ERROR_SURFACES.forEach((surface, index) => {
    assert.equal(errors.libraryErrorState(new ExplorerContractError('bad'), surface).action, expected.incomplete_source[index]);
    assert.equal(errors.libraryErrorState(new Error('no code'), surface).action, expected.something_new[index]);
  });
  const abort = new Error('stop'); abort.name = 'AbortError';
  assert.deepEqual(errors.libraryErrorState(abort, 'videos_first'), { action: 'aborted', code: 'aborted' });
  assert.equal(errors.libraryErrorState(new ApiError('cursor_expired'), 'videos_more').message, 'This browsing session expired. Loaded videos and selection are kept.');
  assert.match(errors.libraryErrorState(new ApiError('cursor_context_changed'), 'videos_more').message, /Data or access changed\.$/);
  assert.equal(errors.libraryErrorState(new ApiError('cursor_expired'), 'select_all').message, 'The catalogue session expired. Reload before selecting all matching videos.');
  assert.equal(errors.libraryErrorState(new ApiError('query_busy'), 'select_all').message, 'server says query_busy');
  // One refusal code for three bounds; the server's `limit` picks the true sentence, none claims no number.
  const tooLarge = (limit) => errors.libraryErrorState(Object.assign(new ApiError('query_budget_exceeded'), limit === undefined ? {} : { limit }), 'videos_first');
  assert.equal(tooLarge('rows').message, 'This selection is too large to browse (more than 50,000 videos).');
  assert.equal(tooLarge('memory').message, 'This selection is too large to browse: its data exceeds the server’s memory limit. Narrow the filters.');
  assert.equal(tooLarge('time').message, 'This selection took too long to prepare. Narrow the filters or try again.');
  for (const limit of [undefined, 'bytes', 7]) {
    assert.equal(tooLarge(limit).message, 'This selection is too large to browse at once. Narrow the filters.', `limit ${limit}`);
    assert.doesNotMatch(tooLarge(limit).message, /50,000/);
  }
  assert.equal(tooLarge('memory').action, 'too_large');
  // these refusals carry a code and no text, so the error's own message is "…(HTTP 422).". Every surface
  // names them in the same fixed words; the action of each surface (table above) is unchanged.
  {
    const refused = (code, limit) => Object.assign(new Error('The request was not accepted (HTTP 422).'),
      { name: 'ExplorerApiError', status: 422, code }, limit === undefined ? {} : { limit });
    const words = [
      ['query_budget_exceeded', 'rows', 'This selection is too large to browse (more than 50,000 videos).'],
      ['query_budget_exceeded', 'memory', 'This selection is too large to browse: its data exceeds the server’s memory limit. Narrow the filters.'],
      ['query_budget_exceeded', 'time', 'This selection took too long to prepare. Narrow the filters or try again.'],
      // The request itself, or the answer, is too large.
      ['query_budget_exceeded', 'request', 'These conditions are too large or too complex for one read. Remove some conditions or values.'],
      ['query_budget_exceeded', 'response', 'This result is too large to send at once. Narrow the filters or choose fewer columns.'],
      ['query_budget_exceeded', undefined, 'This selection is too large to browse at once. Narrow the filters.'],
      ['query_budget_exceeded', 'bytes', 'This selection is too large to browse at once. Narrow the filters.'],
      ['field_disabled', undefined, 'A field in these filters is turned off in Collection fields. Remove that condition or turn the field on.'],
      ['dependency_disabled', undefined, 'Data needed for these filters is turned off in Collection fields.'],
    ];
    for (const [code, limit, text] of words) {
      errors.LIBRARY_ERROR_SURFACES.forEach((surface, index) => {
        const state = errors.libraryErrorState(refused(code, limit), surface);
        assert.equal(state.message, text, `${code}:${limit} on ${surface}`);
        assert.equal(state.action, expected[code][index], `${code} on ${surface} keeps its action`);
      });
    }
    // A field refusal never borrows a limit; another code with a limit keeps the error's own words.
    assert.equal(errors.libraryErrorState(refused('field_disabled', 'rows'), 'videos_more').message, words[7][2]);
    assert.equal(errors.libraryErrorState(Object.assign(new ApiError('query_busy'), { limit: 'rows' }), 'facets').message, 'server says query_busy');
  }
  assert.equal(errors.libraryErrorState(new ApiError('policy_denied'), 'overview').message, 'Baseline is unavailable on this server');
  assert.deepEqual(errors.libraryErrorState(new ApiError('invalid_query', 'where.all[1]'), 'videos_first'),
    { action: 'invalid_field', code: 'invalid_query', message: 'server says invalid_query', path: 'where.all[1]' });
  assert.equal(errors.libraryErrorState({ code: 'Not A Code' }, 'facets').code, 'unknown');
  assert.equal(errors.PREPARING_RETRIES, 2); assert.equal(errors.PREPARING_DELAY_MS, 2000);
  // The workshop's block pages retry preparation with the same budget.
  assert.match(read('../application/frontend/src/explorer/workshopModel.ts'), /error\.code !== 'catalogue_preparing' \|\| attempt >= 2/);
}
console.log('errors.ts: every error code maps to its state on every surface.');

// capabilities.ts: gates per type from /research/capabilities.
{
  const live = JSON.parse(read('./fixtures/research_capabilities_window_limits.json'));
  const today = libraryGates(live, 'long');
  assert.equal(today.overview, true, 'the four Overview operations exist');
  // The Overview gate checks its operations (descriptive_analytics), not the workshop's video.summary.v1.
  for (const missing of [wire.OPERATIONS.fingerprint, wire.OPERATIONS.rhythm, wire.OPERATIONS.years, wire.OPERATIONS.breakdown]) {
    assert.equal(libraryGates({ ...live, operations: live.operations.filter((id) => id !== missing) }, 'long').overview, false, missing);
  }
  assert.equal(libraryGates({ ...live, operations: live.operations.filter((id) => id !== wire.OPERATIONS.summary) }, 'long').overview, true);
  for (const id of ['video.fingerprint.v1', 'video.publication_rhythm.v1', 'video.publication_years.v1', 'video.content_breakdown.v1',
    'channel.counter_change.v1', 'channel.format_totals.v1', 'video.publication_calendar.v1']) {
    assert.ok(Object.values(wire.OPERATIONS).includes(id), id);
  }
  assert.ok(live.operations.includes(wire.OPERATIONS.breakdown), 'registered names, not working names');
  // Integration with (frozen), (facets, derived filters), (growth) and (history, launch).
  assert.deepEqual([today.facets, today.derivedFilters, today.growth, today.history, today.launch, today.frozen.available], [true, true, true, true, true, true]);
  assert.deepEqual(today.frozen, { available: true, ttlSeconds: 600, maxRows: 50000 });
  assert.equal(today.field('video.views'), true);
  const later = {
    ...live, consistency_modes: ['current', 'frozen'], frozen: { ttl_seconds: 300, max_rows: 1000, max_page_size: 200 },
    operations: [...live.operations, wire.OPERATIONS.facets, wire.OPERATIONS.observedHistory, wire.OPERATIONS.launchComparison],
    // The live fixture already lists the growth field for every type; replace it, do not add a twin.
    fields: [...live.fields.filter((f) => f.id !== wire.FIELDS.viewsGained28d && !wire.DERIVED_FIELDS.includes(f.id)),
      ...wire.DERIVED_FIELDS.map((id) => ({ id })),
      { id: wire.FIELDS.viewsGained28d, enabled_video_types: ['long', 'short'] }],
  };
  const long = libraryGates(later, 'long');
  assert.deepEqual([long.facets, long.derivedFilters, long.growth, long.history, long.launch], [true, true, true, true, true]);
  assert.deepEqual(long.frozen, { available: true, ttlSeconds: 300, maxRows: 1000 });
  assert.equal(libraryGates(later, 'stream').growth, false, 'a field limited to other types is off');
  assert.equal(libraryGates(later, 'short').growth, true);
  assert.equal(libraryGates({ ...later, frozen: { ttl_seconds: -1, max_rows: 'x' } }, 'long').frozen.ttlSeconds, 600);
}
console.log('capabilities.ts: gates from the real capabilities fixture and per-type fields passed.');

// conditions.ts: translation request, strict reply, blocking texts, cache.
{
  const v1 = normalizeCatalogueConditions({ version: 1, filters: { ...INITIAL_EXPLORER_FILTERS, q: 'a' }, facets: { version: 1 } });
  const body = conditions.translationRequest(channel, v1, 'Europe/Berlin');
  // The route requires the tab's type; a Saved v1 view and an old address are Long.
  assert.deepEqual(body, { channel_id: channel, conditions: v1, time_zone: 'Europe/Berlin', video_type: 'long' });
  assert.equal(conditions.translationRequest(channel, v1, 'UTC', 'short').video_type, 'short');
  for (const zone of ['utc', '+03:00', 'Mars/../x']) assert.throws(() => conditions.translationRequest(channel, v1, zone));
  assert.throws(() => conditions.translationRequest('UCbad', v1, 'UTC'));
  const reply = (translation) => ({ contract_version: 1, translation: { status: 'translated', time_zone: 'Europe/Berlin', missing: [], query: { ...translated }, ...translation } });
  assert.equal(conditions.parseTranslation(reply(), body).query.scope.id, channel);
  for (const change of [
    { time_zone: 'UTC' }, { missing: [{ capability: 'video.tag_count', conditions: ['facets.tag_count_buckets'] }] },
    { query: { ...translated, scope: { kind: 'channel', id: 'UC' + 'B'.repeat(22) } } }, { query: { ...translated, video_types: ['short'] } },
    { query: { ...translated, source: { id: 'public.channels.current.v1' } } }, { query: { ...translated, time: { timezone: 'Asia/Tokyo' } } },
    { status: 'partial' }, { extra: true },
  ]) contract(() => conditions.parseTranslation(reply(change), body));
  contract(() => conditions.parseTranslation({ ...reply(), contract_version: 2 }, body));
  const needs = { contract_version: 1, translation: { status: 'needs_capability', query: null, time_zone: 'Europe/Berlin',
    missing: [{ capability: 'observation.views_gained_28d', conditions: ['filters.sort_by'] }] } };
  assert.equal(conditions.parseTranslation(needs, body).status, 'needs_capability');
  contract(() => conditions.parseTranslation({ ...needs, translation: { ...needs.translation, missing: [] } }, body));
  contract(() => conditions.parseTranslation({ ...needs, translation: { ...needs.translation, query: translated } }, body));
  const blocked = conditions.translationBlockedMessage;
  assert.equal(blocked([{ capability: 'video.tag_count', conditions: ['facets.tag_count_buckets'] }, { capability: 'observation.views_gained_28d', conditions: ['filters.sort_by'] }]), conditions.BLOCKED_TEXT.staticFilters);
  assert.equal(blocked([{ capability: 'observation.views_gained_28d', conditions: ['facets.growth.full_history', 'filters.sort_by'] }]), conditions.BLOCKED_TEXT.growthFilters);
  assert.equal(blocked([{ capability: 'observation.views_gained_28d', conditions: ['filters.sort_by'] }]), conditions.BLOCKED_TEXT.growthSort);
  // Tracked days and full history need their own growth fields (GROWTH_FIELDS): still the growth text.
  assert.equal(blocked([{ capability: 'observation.growth_28d_status', conditions: ['facets.growth.full_history'] }]), conditions.BLOCKED_TEXT.growthFilters);
  assert.equal(blocked([{ capability: 'observation.growth_28d_interval_seconds', conditions: ['facets.growth.min_tracked_days'] }, { capability: 'observation.views_gained_28d', conditions: ['filters.sort_by'] }]), conditions.BLOCKED_TEXT.growthFilters);
  assert.equal(blocked([{ capability: 'query.where_leaf_budget', conditions: ['conditions'] }]), conditions.BLOCKED_TEXT.tooManyConditions);
  assert.match(blocked([{ capability: 'video.future_field', conditions: ['x'] }]), /video\.future_field/);
  assert.equal(blocked([]), '');
  // The Videos section shows these very texts (one wording, the Videos tab reads them from conditions.ts).
  const videos = read('../application/frontend/src/explorer/LibraryVideosSection.tsx');
  for (const key of ['staticFilters', 'growthFilters', 'growthSort']) assert.ok(videos.includes(`BLOCKED_TEXT.${key}`), key);
  assert.doesNotMatch(videos, /Static analysis filters are unavailable/, 'no second copy of the wording');

  let calls = 0; let fail = false;
  const client = conditions.createTranslationClient(async (path, signal, init) => {
    calls += 1;
    assert.equal(path, '/saved/views/translate'); assert.equal(init.method, 'POST');
    const sent = JSON.parse(init.body);
    if (fail) throw new Error('offline');
    return { contract_version: 1, translation: { status: 'translated', time_zone: sent.time_zone, missing: [], query: { ...translated } } };
  }, 2);
  await client.translate(channel, v1, 'Europe/Berlin');
  await client.translate(channel, structuredClone(v1), 'Europe/Berlin');
  assert.equal(calls, 1, 'same channel, conditions and zone: no second request');
  await client.translate(channel, v1, 'UTC');
  assert.equal(calls, 2);
  fail = true;
  await assert.rejects(client.translate(channel, createInitialCatalogueConditions(), 'UTC'), /offline/);
  await assert.rejects(client.translate(channel, createInitialCatalogueConditions(), 'UTC'), /offline/);
  assert.equal(calls, 4, 'failures are not cached');
  fail = false;
  await client.translate(channel, createInitialCatalogueConditions(), 'Asia/Tokyo');
  assert.equal(client.size(), 2, 'bounded cache');
}
console.log('conditions.ts: request body, strict reply, blocking texts and bounded cache passed.');

assert.equal(saved.LIBRARY_SURFACE, 'library');
assert.equal(saved.PRESENTATION_VERSION, 1);
const savedSource = read('../application/frontend/src/explorer/libraryData/savedLibrary.ts');
assert.ok(savedSource.indexOf('export function librarySavedDefinition(') < savedSource.indexOf('export function recognizeLibraryView('), 'saving and reopening have separate entry points');
// The core stays pure: no React, window or document at import time.
for (const name of ['wire', 'strict', 'envelope', 'errors', 'capabilities', 'presets', 'conditions', 'savedLibrary']) {
  const source = read(`../application/frontend/src/explorer/libraryData/${name}.ts`);
  assert.doesNotMatch(source, /from 'react'|\bwindow\.|\bdocument\.|from '\.\.\/api'/, name);
}
console.log('Library adapter core passed.');
