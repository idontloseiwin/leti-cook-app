// Header notes: the profile's count notice and the unlisted note's list proof.
// Views by format come from the shared backend's channel.format_totals.v1 (test_library_overview.mjs).
// Payloads as build_channel_list_inventory and build_channel_profile produce them.
import assert from 'node:assert/strict';
import { parseExplorerListInventoryV1, parseExplorerProfileV1 } from '../application/frontend/src/explorer/contracts.ts';

// A passive profile notice describes only a count change, with its own expiry.
const profile = {
  contract_version: 1, generated_at: '2026-10-15T12:00:00Z',
  channel: { channel_id: 'c', title: 'Channel', subscribers: 1, views: 2, video_count: 99,
    country_code: null, channel_created_at: null, channel_metadata_checked_at: '2026-10-15T12:00:00Z' },
  freshness: { maximum_age_days: 29, channel_data_fresh: true },
  source: { kind: 'stored_channel_profile', youtube_requests: false, database_writes: false },
};
assert.equal(parseExplorerProfileV1(profile, 'c').public_video_count_notice, undefined);
const countNotice = { code: 'public_video_count_decreased', checked_at: '2026-10-15T12:00:00Z',
  source_expires_at: '2026-11-13T11:00:00Z' };
assert.deepEqual(parseExplorerProfileV1({ ...profile, public_video_count_notice: countNotice }, 'c')
  .public_video_count_notice, countNotice);
for (const notice of [{ ...countNotice, code: 'videos_deleted' }, { ...countNotice, source_expires_at: null }]) {
  assert.throws(() => parseExplorerProfileV1({ ...profile, public_video_count_notice: notice }, 'c'));
}

// Fresh list evidence is independent from hydrated view totals.
const { unlistedVideoCount } = await import('../application/frontend/src/explorer/unlistedVideos.ts');
const { captureSourceLifetime, sourceExpired } = await import('../application/frontend/src/sourceLifetime.ts');
const now = Date.parse('2026-10-09T12:00:00Z');
const expiry = '2026-10-10T12:00:00Z';
const list = (status, count) => ({status, listed_video_count: count, source_expires_at: expiry});
const listPayload = (total = 320, unique = 300) => ({
  contract_version: 1, channel_id: 'c', generated_at: '2026-10-09T12:00:00.000000Z',
  library: { status: 'ready', formats: Object.fromEntries(['long','short','stream'].map(kind => [kind,
    {status: 'complete', full_completed_at: '2026-10-09T12:00:00Z', absent_checked_at: null, source_expires_at: expiry}])) },
  inventory: {
    complete: true, unique_video_count: unique, youtube_video_count: total, source_expires_at: expiry,
    formats: {long: list('complete', 200), short: list('complete', 100), stream: list('complete', 10)},
  },
  source: {kind: 'stored_channel_list_inventory', youtube_requests: false, database_writes: false},
});
const parseList = value => parseExplorerListInventoryV1(value, 'c');

assert.equal(unlistedVideoCount(parseList(listPayload()), now), 20,
  'Fixture 320-300=20; typed list sum 310 overlaps: use distinct 300, never sum the format counts.');
const pending = listPayload(250, 250);
pending.inventory.complete = false;
pending.inventory.unique_video_count = null;
pending.inventory.formats.short = list('not_collected', null);
assert.equal(unlistedVideoCount(parseList(pending), now), null, 'Queued lists are unknown, not zero.');
const loaded = listPayload(250, 250);
loaded.inventory.formats = {long: list('complete', 200), short: list('complete', 50), stream: list('absent', 0)};
assert.equal(unlistedVideoCount(parseList(loaded), now), 0, 'A complete fixture has no note.');
const large = listPayload(31000, 20150);
large.inventory.formats = {long: list('complete', 19900), short: list('complete', 200), stream: list('complete', 50)};
assert.equal(unlistedVideoCount(parseList(large), now), null,
  'A channel over the 20,000 limit keeps the limit notice only.');
assert.equal(unlistedVideoCount(undefined, now), null, 'Without a list proof nothing is said.');
for (const status of ['not_collected', 'incomplete', 'stale']) {
  const uncertain = listPayload();
  uncertain.inventory.complete = false;
  uncertain.inventory.unique_video_count = null;
  uncertain.inventory.formats.stream = list(status, null);
  assert.equal(unlistedVideoCount(parseList(uncertain), now), null);
}
for (const change of [
  value => {value.inventory.youtube_video_count = null;},
  value => {value.inventory.source_expires_at = '2026-10-09T12:00:00Z';},
  value => {value.inventory.formats.stream.source_expires_at = '2026-10-09T12:00:00Z';},
]) {
  const expired = listPayload(); change(expired);
  assert.equal(unlistedVideoCount(parseList(expired), now), null, 'Expired/unknown channel or list evidence cannot produce a note.');
}
for (const change of [
  value => {value.inventory.unique_video_count = 311;},
  value => {value.inventory.unique_video_count = 199;},
  value => {value.inventory.formats.stream = list('absent', 10);},
  value => {value.inventory.formats.stream = list('not_collected', null);},
  value => {value.inventory.source_expires_at = null;},
]) {
  const invalid = listPayload(); change(invalid);
  assert.throws(() => parseList(invalid), /inconsistent/);
}
for (const change of [
  value => {delete value.inventory;},
  value => {value.channel_id = 'other';},
  value => {value.source.kind = 'derived_from_stored_typed_video_inventory';},
  value => {value.source.database_writes = true;},
  value => {value.generated_at = 'yesterday';},
]) {
  const invalid = listPayload(); change(invalid);
  assert.throws(() => parseList(invalid), /Explorer list inventory/);
}

const previousVersion = listPayload();
delete previousVersion.library;
assert.equal(parseList(previousVersion).library, undefined, 'an older server has no current coverage evidence');
const invalidCoverage = listPayload();
invalidCoverage.library.formats.short.full_completed_at = null;
assert.throws(() => parseList(invalidCoverage), /inconsistent/);

const lifetimes = listPayload();
lifetimes.source_read_at = '2026-10-09T12:00:00Z';
lifetimes.source_expires_at = '2026-10-09T12:01:00Z';
const captured = captureSourceLifetime(lifetimes, undefined, 1000);
assert.equal(sourceExpired(captured, now + 60001, 61001), true);
assert.equal(sourceExpired(captured.inventory, now + 60001, 61001), false,
  'An earlier envelope expiry cannot expire a separately witnessed inventory.');
assert.equal(unlistedVideoCount(captured, now + 60001), 20);
console.log('header notes contract and list evidence matrix: ok');
