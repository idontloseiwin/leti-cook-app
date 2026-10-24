// Views by format: the browser accepts public-only totals and still rejects
// counts that do not add up (RR-02). Payload produced by build_channel_format_totals.
import assert from 'node:assert/strict';
import { parseExplorerFormatTotalsV1, parseExplorerProfileV1 } from '../application/frontend/src/explorer/contracts.ts';

const payload = () => JSON.parse(JSON.stringify({
  contract_version: 1, channel_id: 'c', generated_at: '2026-10-09T12:00:00.000000Z',
  formats: {
    long: {source_expires_at: '2026-11-07T12:00:00Z', status: 'ready', views: 100,
      scanned_inventory_count: 2, known_view_count: 1, missing_view_count: 0},
    short: {source_expires_at: '2026-11-07T12:00:00Z', status: 'ready', views: 30,
      scanned_inventory_count: 2, known_view_count: 1, missing_view_count: 0,
      coverage: {scan_limit: 1000000, inventory_complete: true, inventory_ready: true,
        unavailable_excluded_count: 1, product_exposure_ready: true, transactional_snapshot: false,
        type_resolution_withheld: 0, stale_inventory_count: 0, availability_withheld_count: 0,
        missing_video_row_count: 0, stream_completion_withheld: 0, stale_counter_count: 0,
        stale_metadata_count: 0, unknown_view_count: 0}},
    stream: {status: 'not_collected', views: null, reason: 'inventory_not_completed'},
  },
  coverage: {scan_limit: 1000000, inventory_complete: true, unavailable_excluded_count: 1,
    missing_video_row_count: 0, unknown_view_count: 0, stale_counter_count: 0,
    stale_inventory_count: 0, availability_withheld_count: 0, transactional_snapshot: false},
  source: {kind: 'derived_from_stored_typed_video_inventory', youtube_requests: false,
    database_writes: false, snapshots_used: false, freshness_enforced: true},
}));

const accepted = parseExplorerFormatTotalsV1(payload(), 'c');
assert.equal(accepted.formats.long.views, 100, 'A video no longer public leaves the long total without hiding it.');
assert.equal(accepted.formats.short.views, 30, 'Shorts follow the same public-only rule.');

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

const longMismatch = payload();
longMismatch.coverage.unavailable_excluded_count = 0;
assert.throws(() => parseExplorerFormatTotalsV1(longMismatch, 'c'), /inconsistent/,
  'Long counts must still add up: known + missing + excluded = scanned.');

const shortMismatch = payload();
shortMismatch.formats.short.coverage.unavailable_excluded_count = 0;
assert.throws(() => parseExplorerFormatTotalsV1(shortMismatch, 'c'), /inconsistent/,
  'Typed counts must still add up.');

const invalidCount = payload();
invalidCount.coverage.unavailable_excluded_count = -1;
assert.throws(() => parseExplorerFormatTotalsV1(invalidCount, 'c'), /unavailable_excluded_count/,
  'A negative excluded count is rejected.');

const legacy = payload();
delete legacy.coverage.unavailable_excluded_count;
legacy.formats.long.known_view_count = 2;
legacy.formats.long.views = 107;
assert.equal(parseExplorerFormatTotalsV1(legacy, 'c').formats.long.views, 107,
  'A response without the new field keeps the previous meaning.');



// RR03/RR18: fresh list evidence is independent from hydrated view totals.
const { unlistedVideoCount } = await import('../application/frontend/src/explorer/unlistedVideos.ts');
const { captureSourceLifetime, sourceExpired } = await import('../application/frontend/src/sourceLifetime.ts');
const now = Date.parse('2026-10-09T12:00:00Z');
const expiry = '2026-10-10T12:00:00Z';
const list = (status, count) => ({status, listed_video_count: count, source_expires_at: expiry});
const listPayload = (total = 314, unique = 302) => ({...payload(), inventory: {
  complete: true, unique_video_count: unique, youtube_video_count: total, source_expires_at: expiry,
  formats: {long: list('complete', 200), short: list('complete', 100), stream: list('complete', 10)},
}});
const parseList = value => parseExplorerFormatTotalsV1(value, 'c');

const absentPlaylist = payload();
absentPlaylist.formats.stream = {status: 'absent', reason: 'playlist_not_found', views: null, source_expires_at: expiry};
assert.equal(parseList(absentPlaylist).formats.stream.status, 'absent');
for (const change of [{source_expires_at: null}, {reason: 'inventory_not_completed'}, {views: 0}]) {
  const invalid = structuredClone(absentPlaylist);
  Object.assign(invalid.formats.stream, change);
  assert.throws(() => parseList(invalid), /returned/);
}
const partiallyRead = payload();
partiallyRead.formats.stream = {status: 'incomplete', reason: 'inventory_not_completed', views: null};
assert.equal(parseList(partiallyRead).formats.stream.status, 'incomplete');

const missingCounter = listPayload();
missingCounter.formats.short.status = 'incomplete';
missingCounter.formats.short.views = null;
missingCounter.formats.short.known_view_count = 0;
missingCounter.formats.short.missing_view_count = 1;
missingCounter.formats.short.coverage.unknown_view_count = 1;
assert.equal(unlistedVideoCount(parseList(missingCounter), now), 12,
  'Yevgeniy fixture: 314-302=12 despite one missing counter; no guessed members-only classification.');
assert.equal(unlistedVideoCount(parseList(listPayload()), now), 12,
  'Typed list sum310 overlaps: use distinct302, never sum the format counts.');

const pending = listPayload(272, 272);
pending.inventory.complete = false;
pending.inventory.unique_video_count = null;
pending.inventory.formats.short = list('not_collected', null);
assert.equal(unlistedVideoCount(parseList(pending), now), null, 'Queued Mark Rober lists are unknown, not zero.');
const loaded = listPayload(272, 272);
loaded.inventory.formats = {long: list('complete', 200), short: list('complete', 72), stream: list('absent', 0)};
assert.equal(unlistedVideoCount(parseList(loaded), now), 0, 'Complete Mark Rober fixture has no note.');
const bbc = listPayload(32798, 20243);
bbc.inventory.formats = {long: list('complete', 19900), short: list('complete', 300), stream: list('complete', 43)};
assert.equal(unlistedVideoCount(parseList(bbc), now), null,
  'BBC keeps existing 20000 limit notice only.');
assert.equal(unlistedVideoCount(parseList(payload()), now), null, 'Legacy responses cannot substitute unknown lists with zero.');
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

const lifetimes = listPayload();
lifetimes.source_read_at = '2026-10-09T12:00:00Z';
lifetimes.source_expires_at = '2026-10-09T12:01:00Z';
const captured = captureSourceLifetime(lifetimes, undefined, 1000);
assert.equal(sourceExpired(captured, now + 60001, 61001), true);
assert.equal(sourceExpired(captured.inventory, now + 60001, 61001), false,
  'Earlier counter expiry cannot expire a separately witnessed inventory.');
assert.equal(unlistedVideoCount(captured, now + 60001), 12);
console.log('format totals contract and RR03/RR18 evidence matrix: ok');

// PREMIERE-01: type-independent raw observations, separate from catalogue totals.
const { parseChannelPublications, publicationStatusLabel, publicationTypeLabel } = await import('../application/frontend/src/explorer/publicationStatus.ts');
const publication = (index = 1, changes = {}) => ({
  video_id: String(index).padStart(11, '0'), video_type: 'long', title: 'Waiting', publication_status: 'upcoming',
  scheduled_start_at: null, checked_at: '2026-10-16T12:00:00Z', source_expires_at: '2026-11-14T12:00:00Z', ...changes,
});
const publicationsPage = (videos = [publication()], next_cursor = null) => ({
  contract_version: 1, channel_id: 'c', videos, next_cursor,
  source: { kind: 'stored_publication_observations', youtube_requests: false, database_writes: false },
});
for (const type of ['long', 'short', 'stream', null]) {
  const page = parseChannelPublications(publicationsPage([publication(1, { video_type: type })]), 'c');
  assert.equal(page.videos[0].video_type, type);
  assert.equal(page.videos[0].scheduled_start_at, null, 'Missing API date stays unknown.');
}
assert.equal(publicationTypeLabel(null), 'Type unconfirmed', 'Unknown type never becomes Long.');
for (const date of ['2026-10-01T12:00:00Z', '2027-12-01T12:00:00Z']) {
  const parsed = parseChannelPublications(publicationsPage([publication(1, { scheduled_start_at: date })]), 'c');
  assert.equal(parsed.videos[0].scheduled_start_at, date);
  assert.equal(publicationStatusLabel(parsed.videos[0].publication_status), 'Upcoming', 'Date alone never changes stage.');
}
for (const changes of [{ publication_status: 'finished' }, { video_type: 'premiere' }, { scheduled_start_at: '2026-02-30T12:00:00Z' },
  { source_expires_at: null }, { title: {} }]) {
  assert.throws(() => parseChannelPublications(publicationsPage([publication(1, changes)]), 'c'));
}
assert.throws(() => parseChannelPublications(publicationsPage([publication(2), publication(1)]), 'c'));
assert.throws(() => parseChannelPublications(publicationsPage(), 'wrong-channel'));
assert.throws(() => parseChannelPublications(publicationsPage([publication(1)], '00000000001'), 'c'));
const fullPublicationPage = publicationsPage(Array.from({ length: 50 }, (_, index) => publication(index + 1)), '00000000050');
assert.equal(parseChannelPublications(fullPublicationPage, 'c').next_cursor, '00000000050');
assert.throws(() => parseChannelPublications(publicationsPage(Array.from({ length: 51 }, (_, index) => publication(index + 1))), 'c'));
const sourceAt = Date.parse('2026-10-16T12:00:00Z');
const publicationLifetime = parseChannelPublications(captureSourceLifetime({ ...publicationsPage(),
  source_read_at: '2026-10-16T12:00:00Z', source_expires_at: '2026-11-14T12:00:00Z' }, undefined, 100), 'c');
assert.equal(sourceExpired(publicationLifetime.videos[0], sourceAt + 29 * 86400000, 100), true,
  'Parsed publication metadata keeps its source lifetime.');
console.log('publication status contract, pagination, no clock inference and source expiry: ok');
