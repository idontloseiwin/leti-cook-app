import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
function compiled(path, globals = {}, replace = source => source) {
  const { outputText } = ts.transpileModule(replace(read(path)), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const exports = {};
  new Function('require', 'exports', ...Object.keys(globals), outputText)(name => { throw new Error(`unexpected import ${name}`); }, exports, ...Object.values(globals));
  return exports;
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

// Publishing rhythm: no argument spread over catalogue-sized arrays.
const zones = compiled('../application/frontend/src/explorer/timeZones.ts');
const rhythm = compiled('../application/frontend/src/explorer/publishingRhythm.ts',
  {zonedParts: zones.zonedParts, zonedDayStart: zones.zonedDayStart}, source => source.replace(/^import [^;]+;$/gm, ''));
const count = 400_000;
const start = Date.UTC(2000, 0, 3);
const points = Array.from({length: count}, (_, index) => ({
  video_id: `v${index}`, title: '', published_at: new Date(start + index * 3_600_000).toISOString(),
}));
const summary = rhythm.buildPublishingRhythm(points);
assert.deepEqual([summary.observations.length, summary.maxGapMs], [count, 3_600_000]);
const mondays = Array.from({length: count}, (_, index) => ({
  weekday: 0, epochMs: start + (index % 2086) * 7 * 86_400_000, hour: index % 2 ? 9 : 11, minute: 0,
  year: new Date(start + (index % 2086) * 7 * 86_400_000).getUTCFullYear(),
}));
const comparison = rhythm.weekdayEdgeYearSlotComparison(mondays, 0);
assert.deepEqual([comparison.fromYear, comparison.toYear, Math.abs(comparison.hours) < 1], [2000, 2039, true]);

// Owner session: only a refusal or explicit sign-out ends a mounted session.
function session(responses) {
  const log = {redirects: [], cleared: 0, mounts: 0, interval: null};
  const element = {replaceChildren: () => { log.cleared += 1; }};
  const window = {
    location: {replace: path => log.redirects.push(path)},
    setInterval: callback => { log.interval = callback; },
    addEventListener: () => {},
  };
  const document = {hidden: false, getElementById: () => element, addEventListener: () => {}};
  const fetch = async () => {
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return {ok: next.status < 400, status: next.status, json: async () => next.body};
  };
  const module = compiled('../application/frontend/src/ownerSession.ts', {window, document, fetch},
    source => source.replace('import.meta.env.VITE_LIC_DEPLOYMENT_PROFILE', "'self-hosted'"));
  module.mountWithOwnerAccess(() => { log.mounts += 1; log.mode = module.ownerAccessMode(); });
  return log;
}
{
  const log = session([{status: 200, body: {authenticated: true}}, {status: 503}, {status: 500},
                       new Error('offline'), {status: 200, body: {authenticated: false}}]);
  await tick();
  assert.deepEqual([log.mounts, log.redirects], [1, []]);
  for (let check = 0; check < 3; check += 1) { log.interval(); await tick(); }
  assert.deepEqual([log.redirects, log.cleared], [[], 0], 'server errors and network failures keep the workspace');
  log.interval(); await tick();
  assert.deepEqual([log.redirects, log.cleared > 0], [['/auth'], true], 'an explicit sign-out ends the session');
}
{
  const log = session([{status: 200, body: {authenticated: true}}, {status: 401}]);
  await tick(); log.interval(); await tick();
  assert.deepEqual(log.redirects, ['/auth'], 'a refusal ends the session');
}
{
  const log = session([{status: 503}]);
  await tick();
  assert.deepEqual([log.mounts, log.redirects], [0, ['/auth']], 'the first check still requires access');
}
console.log('Frontend resilience: large rhythm inputs and session checks passed');

// User-work bookmarks: a channel removed for good leaves no identity, so its
// waiting bookmark is dropped instead of breaking every later backup.
{
  const store = new Map();
  const storage = {getItem: key => store.has(key) ? store.get(key) : null, setItem: (key, value) => store.set(key, String(value)), removeItem: key => store.delete(key)};
  let bookmarks = new Set();
  const stubs = {
    './catalogueQuery.ts': {normalizeCatalogueConditions: value => value, serializeCatalogueConditions: () => ''},
    './savedWork.ts': {savedId: value => value, savedRevision: value => value, savedTimestamp: value => value},
    './cataloguePreferences.ts': {catalogueDensityPreference: {isValid: () => true}},
    '../state/browserPreferences.ts': {readBrowserPreference: () => 'large', writeBrowserPreference: () => {}},
    './bookmarks.ts': {EXPLORER_BOOKMARKS_STORAGE_KEY: 'b', loadExplorerBookmarks: () => new Set(bookmarks), saveExplorerBookmarks: next => { bookmarks = new Set(next); return true; }},
    './folderCollapse.ts': {COLLAPSED_FOLDERS_STORAGE_KEY: 'f', loadCollapsedFolders: () => new Set(), saveCollapsedFolders: () => {}},
  };
  const {outputText} = ts.transpileModule(read('../application/frontend/src/explorer/userWorkBackup.ts'), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const work = {};
  new Function('require', 'exports', outputText)(name => { if (!stubs[name]) throw new Error(`unexpected import ${name}`); return stubs[name]; }, work);
  const bound = '11111111-1111-4111-8111-111111111111', waiting = '22222222-2222-4222-8222-222222222222', gone = '33333333-3333-4333-8333-333333333333';
  storage.setItem('let-it-cook.user-work.pending-bookmarks.v1', JSON.stringify([bound, waiting, gone]));
  assert.deepEqual(work.readPendingBookmarks(storage), [bound, waiting, gone]);
  const status = {channel_mappings: [{id: bound, channel_id: 'UC' + 'a'.repeat(22)}], user_channel_refs: [bound, waiting]};
  assert.equal(work.resolveUserWorkBookmarks(status, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), [waiting]);
  assert.deepEqual([...bookmarks], ['UC' + 'a'.repeat(22)]);
  storage.setItem(work.PENDING_BOOKMARKS_STORAGE_KEY, JSON.stringify([gone]));
  assert.equal(work.resolveUserWorkBookmarks({channel_mappings: [], user_channel_refs: []}, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), []);
  assert.equal(storage.getItem(work.PENDING_BOOKMARKS_STORAGE_KEY), '[]');
  assert.deepEqual(JSON.parse(storage.getItem('let-it-cook.user-work.pending-bookmarks.v1')), [bound, waiting, gone]);
  assert.equal(work.resolveUserWorkBookmarks(status, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), [], 'completed legacy work stays completed on the next read');
  assert.deepEqual([...bookmarks], ['UC' + 'a'.repeat(22)], 'clearing pending work preserves resolved bookmarks');
}

// Backup format 2 lists the preset label as kept, renamed or not at all (T-06).
{
  const stubs = {
    './catalogueQuery.ts': {normalizeCatalogueConditions: value => value, serializeCatalogueConditions: () => ''},
    './savedWork.ts': {savedId: value => value, savedRevision: value => value, savedTimestamp: value => value},
    './cataloguePreferences.ts': {catalogueDensityPreference: {isValid: () => true}},
    '../state/browserPreferences.ts': {readBrowserPreference: () => 'large', writeBrowserPreference: () => {}},
    './bookmarks.ts': {EXPLORER_BOOKMARKS_STORAGE_KEY: 'b', loadExplorerBookmarks: () => new Set(), saveExplorerBookmarks: () => true},
    './folderCollapse.ts': {COLLAPSED_FOLDERS_STORAGE_KEY: 'f', loadCollapsedFolders: () => new Set(), saveCollapsedFolders: () => {}},
  };
  const {outputText} = ts.transpileModule(read('../application/frontend/src/explorer/userWorkBackup.ts'), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const work = {};
  new Function('require', 'exports', outputText)(name => stubs[name], work);
  const file = (version, labels) => ({format: 'let-it-cook-user-work', version, created_at: '2026-09-20T00:00:00Z',
    user_work: {folders: [], labels, channels: [], collections: [], reviews: []},
    browser_preferences: {catalogue_density: 'large', bookmarked_channel_refs: [], collapsed_folder_ids: []}});
  const preset = {label_key: 'monetized', name: 'Paid', label_type: 'preset', created_at: '2026-09-01T00:00:00Z'};
  const parsed = work.parseUserWorkBackup(file(2, [preset]));
  assert.equal(parsed.version, 2);
  assert.equal(parsed.format, 'leti-cook-user-work');
  assert.deepEqual(work.parseUserWorkBackup({...file(2, [preset]), format: 'leti-cook-user-work'}), parsed);
  assert.throws(() => work.parseUserWorkBackup({...file(2, []), format: 'leti-cook-user-work-v2'}));
  assert.deepEqual(parsed.user_work.labels.map(row => [row.label_key, row.name, row.label_type]), [['monetized', 'Paid', 'preset']]);
  assert.equal(work.parseUserWorkBackup(file(1, [])).version, 1);
  assert.throws(() => work.parseUserWorkBackup(file(1, [preset])));
  assert.throws(() => work.parseUserWorkBackup(file(3, [])));
}

// Another tab's bookmark or folder change reloads them here; unrelated keys do not.
{
  const stubs = {'./bookmarks.ts': {EXPLORER_BOOKMARKS_STORAGE_KEY: 'b', LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY: 'old-b'},
    './folderCollapse.ts': {COLLAPSED_FOLDERS_STORAGE_KEY: 'f', LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY: 'old-f'}};
  const {outputText} = ts.transpileModule(read('../application/frontend/src/explorer/preferenceSync.ts'), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const sync = {};
  new Function('require', 'exports', outputText)(name => { if (!stubs[name]) throw new Error(`unexpected import ${name}`); return stubs[name]; }, sync);
  const listeners = new Set();
  const target = {addEventListener: (type, listener) => { assert.equal(type, 'storage'); listeners.add(listener); },
                  removeEventListener: (type, listener) => listeners.delete(listener)};
  let reloads = 0;
  const stop = sync.watchSharedPreferences(target, () => { reloads += 1; });
  for (const key of ['b', 'f', 'old-b', 'old-f', null, 'other']) listeners.forEach(listener => listener({key}));
  assert.equal(reloads, 5);
  stop();
  assert.equal(listeners.size, 0);
}

// Server text reaches the page only as a plain sentence; known codes use fixed wording.
{
  const text = compiled('../application/frontend/src/serverText.ts');
  const key = 'AIza' + 's'.repeat(35);
  assert.equal(text.serverErrorMessage(503, 'hot_runtime_not_ready'), 'The application is starting or running maintenance. Try again in a moment.');
  assert.equal(text.serverErrorMessage(401, 'authentication_required', 'ignored'), 'Your session has ended. Sign in again to continue.');
  assert.equal(text.serverErrorMessage(422, undefined, 'min_views must not exceed max_views.'), 'min_views must not exceed max_views.');
  assert.equal(text.serverErrorMessage(429, 'channel_add_rate_limited', 'Too many channel additions. Try again in 2 minutes.'),
    'Too many channel additions. Try again in 2 minutes.');
  assert.equal(text.serverErrorMessage(503, 'operator_job_settings_invalid', 'Durable operator job configuration is incomplete.'),
    'Durable operator job configuration is incomplete.');
  assert.equal(text.serverErrorMessage(500, undefined, 'Database is locked.'), 'The server could not complete the request (HTTP 500).');
  for (const leak of [`Request to https://www.googleapis.com/youtube/v3/videos failed`, `failed: /videos?id=1&key=${key}`, `bad key ${key}`,
    'Traceback (most recent call last): boom', 'token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9eyJyb2xlIjoic2VydmljZV9yb2xlIn0', 'x'.repeat(361)]) {
    assert.equal(text.plainServerText(leak), undefined, leak);
    assert.equal(text.serverErrorMessage(422, 'some_code', leak), 'The request was not accepted (HTTP 422).', leak);
  }
  assert.equal(text.plainServerText('  Channel   not found  '), 'Channel not found');
  assert.equal(text.plainServerText('UC' + 'a'.repeat(22) + ' was removed.'), 'UC' + 'a'.repeat(22) + ' was removed.');
}

// A UTC time written with +00:00 (as PostgREST renders timestamptz) is the same instant as with Z.
{
  const contracts = compiled('../application/frontend/src/explorer/contracts.ts');
  const micro = contracts.utcTimestampMicroseconds;
  assert.equal(micro('2026-09-26T07:16:48.56891+00:00'), micro('2026-09-26T07:16:48.568910Z'));
  assert.equal(micro('2026-09-26T07:16:48+00:00'), micro('2026-09-26T07:16:48Z'));
  assert.notEqual(micro('2026-09-26T07:16:48.56891Z'), null);
  for (const other of ['2026-09-26T07:16:48+01:00', '2026-09-26T07:16:48-00:00', '2026-09-26T07:16:48', '2026-02-30T00:00:00Z']) {
    assert.equal(micro(other), null, other);
  }
}

// Creating a folder or label whose name exists reuses it; the flag must be a boolean when present.
{
  const contracts = compiled('../application/frontend/src/explorer/contracts.ts');
  assert.equal(contracts.parseExplorerFolder({id: 'f1', name: 'Music', created: false}).created, false);
  assert.equal(contracts.parseExplorerFolder({id: 'f1', name: 'Music'}).created, undefined);
  assert.throws(() => contracts.parseExplorerFolder({id: 'f1', name: 'Music', created: 'no'}), /created flag/);
  const label = {label_key: 'custom:1', name: 'Fav', label_type: 'custom'};
  assert.equal(contracts.parseExplorerLabel({...label, created: true}).created, true);
  assert.throws(() => contracts.parseExplorerLabel({...label, created: 1}), /created flag/);
}

// Numbers show the precision a reader can use (T-05): whole counts from 1,000,
// two places below, two-place rates and whole-second ages.
{
  const withoutImports = source => source.replace(/^import [^;]+;$/gm, '');
  const shared = compiled('../application/frontend/src/format.ts', {}, withoutImports);
  const format = compiled('../application/frontend/src/explorer/format.ts', {formatYouTubeDuration: shared.formatYouTubeDuration}, withoutImports);
  const count = value => format.formatCount(value, {compact: false});
  assert.equal(count('15669962.5'), '15,669,963');
  assert.equal(count('511295.2'), '511,295');
  assert.equal(count('999.995'), '1,000');
  assert.equal(count('3.456'), '3.46');
  assert.equal(count('-0.35'), '-0.35');
  assert.equal(count('-1234.5'), '-1,235');
  assert.equal(count('0.001'), '0');
  assert.equal(count('8013940'), '8,013,940');
  assert.equal(count(40.793), '40.79');
  assert.equal(count(12345.6), '12,346');
  assert.equal(format.formatCount('15669962.5'), '15.7M');
  const contracts = compiled('../application/frontend/src/explorer/contracts.ts');
  const launch = compiled('../application/frontend/src/explorer/videoLaunch.ts',
    {ExplorerContractError: contracts.ExplorerContractError, utcTimestampMicroseconds: contracts.utcTimestampMicroseconds, formatCount: format.formatCount},
    withoutImports);
  assert.equal(launch.launchAgeLabel('10106.850261'), '2h 48m 26s');
  assert.equal(launch.launchAgeLabel('90061.5'), '1d 1h 1m 1s');
  assert.equal(launch.launchAgeLabel('0.25'), '<1s');
  assert.equal(launch.launchAgeLabel('0'), '0s');
}

// "1 video", not "1 videos" (T-20).
{
  const withoutImports = source => source.replace(/^import [^;]+;$/gm, '');
  const format = compiled('../application/frontend/src/explorer/format.ts', {formatYouTubeDuration: () => ''}, withoutImports);
  for (const one of [1, '1', 1n]) assert.equal(format.videoNoun(one), 'video', String(one));
  for (const many of [0, 2, '1,000', 11, undefined, null]) assert.equal(format.videoNoun(many), 'videos', String(many));
}

// Moments are shown in the viewer's own time zone, never labelled UTC (T-18).
{
  const local = compiled('../application/frontend/src/explorer/localTime.ts');
  const value = '2026-09-26T07:16:48.568910Z', date = new Date(value), pad = n => String(n).padStart(2, '0');
  assert.equal(local.formatExactLocal(value), `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`);
  assert.equal(local.formatExactLocal('not a time'), 'Not recorded');
  assert.equal(local.formatTimestampLocal(null, 'recently'), 'recently');
  for (const text of [local.formatTimestampLocal(value), local.formatDateLocal(value)]) assert.doesNotMatch(text, /UTC/);
}

// Local mode has no account: the sign-out link needs the access mode (T-18).
{
  for (const mode of ['local', 'password']) {
    const log = session([{status: 200, body: {authenticated: true, access_mode: mode}}]);
    await tick();
    assert.equal(log.mode, mode);
  }
  const log = session([{status: 200, body: {authenticated: true}}]);
  await tick();
  assert.equal(log.mode, undefined);
}

// Publication analysis counts hours, weekdays and months in the chosen zone (local by default).
{
  const check = (zone, epochMs) => {
    const parts = zones.zonedParts(epochMs, zone);
    const wall = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric',
      day: 'numeric', hour: 'numeric', minute: 'numeric', weekday: 'short'}).formatToParts(new Date(epochMs)).map(p => [p.type, p.value]));
    assert.deepEqual([parts.year, parts.month + 1, parts.day, parts.hour, parts.minute, parts.weekday],
      [Number(wall.year), Number(wall.month), Number(wall.day), Number(wall.hour) % 24, Number(wall.minute),
       ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(wall.weekday)], `${zone} ${new Date(epochMs).toISOString()}`);
  };
  // Across both 2026 daylight-saving changes and a half-hour offset.
  for (const zone of ['America/New_York', 'Europe/Kyiv', 'Asia/Kolkata', 'Australia/Adelaide', 'UTC']) {
    for (let t = Date.UTC(2026, 0, 1); t < Date.UTC(2027, 0, 1); t += 7 * 3_600_000 + 13 * 60_000) check(zone, t);
  }
  assert.equal(new Date(zones.zonedDayStart(2026, 2, 8, 'America/New_York')).toISOString(), '2026-03-08T05:00:00.000Z');
  assert.equal(new Date(zones.zonedDayStart(2026, 9, 25, 'Europe/Kyiv')).toISOString(), '2026-10-24T21:00:00.000Z');
  // 23:30 UTC on 31 December is already the next year, and a Thursday 01:30, in Kyiv.
  const late = [{video_id: 'v1', title: '', published_at: '2026-12-31T23:30:00Z'}];
  const utc = rhythm.buildPublishingRhythm(late), kyiv = rhythm.buildPublishingRhythm(late, 'Europe/Kyiv');
  assert.deepEqual([utc.observations[0].year, utc.observations[0].hour, utc.peakWeekday], [2026, 23, 3]);
  assert.deepEqual([kyiv.observations[0].year, kyiv.observations[0].hour, kyiv.peakWeekday], [2027, 1, 4]);
  assert.deepEqual(kyiv.months.map(month => month.key), ['2027-01']);
}

// RR18: an unknown list is never zero; the independent inventory proof owns the difference.
{
  const unlisted = compiled('../application/frontend/src/explorer/unlistedVideos.ts');
  const expiry = new Date(Date.now() + 86400000).toISOString();
  const complete = {complete: true, unique_video_count: 149, youtube_video_count: 195, source_expires_at: expiry,
    formats: {long: {status: 'complete', source_expires_at: expiry},
      short: {status: 'absent', source_expires_at: expiry}, stream: {status: 'absent', source_expires_at: expiry}}};
  assert.equal(unlisted.unlistedVideoCount({inventory: complete}), 46);
  assert.equal(unlisted.unlistedVideoCount({formats: {long: {status: 'ready', scanned_inventory_count: 149},
    short: {status: 'not_collected'}, stream: {status: 'not_collected'}}}), null);
}

// U0: exact old browser keys remain read-only fallbacks; a present new key wins,
// including an empty/corrupt value, so an old copy cannot resurrect deleted work.
{
  const store = new Map();
  const writes = [];
  const storage = {getItem: key => store.has(key) ? store.get(key) : null,
    setItem: (key, value) => { writes.push(key); store.set(key, String(value)); }, removeItem: key => store.delete(key)};
  const bookmarks = compiled('../application/frontend/src/explorer/bookmarks.ts');
  const folders = compiled('../application/frontend/src/explorer/folderCollapse.ts');
  const preference = compiled('../application/frontend/src/state/browserPreferences.ts');
  const density = compiled('../application/frontend/src/explorer/cataloguePreferences.ts').catalogueDensityPreference;
  store.set(bookmarks.LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY, '["UClegacy"]');
  store.set(folders.LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY, JSON.stringify({version: 1, collapsed: ['unassigned']}));
  store.set(density.legacyKey, JSON.stringify({version: 1, value: 'compact'}));
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(storage)], ['UClegacy']);
  assert.deepEqual([...folders.loadCollapsedFolders(storage)], ['unassigned']);
  assert.equal(preference.readBrowserPreference(density, storage), 'compact');
  assert.deepEqual(writes, [], 'reading legacy values never migrates or writes');
  bookmarks.saveExplorerBookmarks([], storage);
  folders.saveCollapsedFolders(new Set(), storage);
  preference.writeBrowserPreference(density, 'large', storage);
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(storage)], []);
  assert.deepEqual([...folders.loadCollapsedFolders(storage)], []);
  assert.equal(preference.readBrowserPreference(density, storage), 'large');
  assert.ok(writes.every(key => key.startsWith('leti-cook.')));
  for (const key of [bookmarks.EXPLORER_BOOKMARKS_STORAGE_KEY, folders.COLLAPSED_FOLDERS_STORAGE_KEY, density.key]) store.set(key, '{broken');
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(storage)], []);
  assert.deepEqual([...folders.loadCollapsedFolders(storage)], []);
  assert.equal(preference.readBrowserPreference(density, storage), 'large');
  assert.ok(store.has(bookmarks.LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY), 'legacy values are not deleted');
  const blocked = {getItem() {throw new Error('blocked');}, setItem() {throw new Error('blocked');}};
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(blocked)], []);
  assert.deepEqual([...folders.loadCollapsedFolders(blocked)], []);
  assert.equal(preference.readBrowserPreference(density, blocked), 'large');
}

// Only the previous catalogue-age source marker is accepted as a legacy alias.
{
  const {parseBaselineV1} = compiled('../application/frontend/src/explorer/contracts.ts');
  const deadline = '2026-10-10T12:00:00Z';
  const definitions = [
    ['official_categories', 'single_label', 'youtube.video.snippet.categoryId', 'known_rows'],
    ['youtube_topics', 'multi_label', 'youtube.video.topicDetails.topicCategories', 'sample_rows'],
    ['audio_languages', 'single_label', 'youtube.video.snippet.defaultAudioLanguage', 'known_rows'],
    ['metadata_languages', 'single_label', 'youtube.video.snippet.defaultLanguage', 'known_rows'],
    ['captions', 'single_label', 'youtube.video.contentDetails.caption', 'known_rows'],
    ['catalogue_age', 'single_label', 'leti_cook.video_age_from_published_at', 'known_rows'],
  ];
  const buckets = ['views', 'likes', 'comments', 'lifetime_views_per_day', 'likes_per_1000_views',
    'comments_per_1000_views', 'duration_seconds', 'publication_years', 'publication_quarters',
    'video_age_days', 'publication_gap_days', 'publication_weekdays_utc', 'publication_hours_utc',
    'title_length_characters', 'tag_count', 'categories'];
  const payload = {
    contract_version: 1, video_type: 'long', format: 'long', generated_at: '2026-10-09T12:00:00Z',
    source_expires_at: deadline, sample: {matched_video_count: 0}, statistics: {}, publication_cadence: {},
    publication_calendar: {timezone: 'UTC', default_year: 2026, available_years: [2026], years: [{year: 2026, days: []}]},
    content_breakdown: Object.fromEntries(definitions.map(([key, kind, source, share_basis]) => [key,
      {kind, source, share_basis, sample_count: 0, known_count: 0, missing_count: 0, groups: [],
        source_expires_at: deadline, ...(key === 'youtube_topics' ? {assigned_count: 0, empty_count: 0} : {})}])),
    distributions: Object.fromEntries(buckets.map(key => [key, []])),
    distribution_coverage: Object.fromEntries(buckets.map(key => [key, {known_count: 0, missing_count: 0, excluded_count: 0}])),
    freshness: {}, methods: {publication_timezone: 'UTC', video_age: 'generated_at_minus_published_at',
      lifetime_views_per_day: 'views_divided_by_exact_checked_at_minus_published_at_minimum_24_hours',
      per_1000_views: 'stored_counter_divided_by_positive_views_times_1000',
      publication_gaps: 'between_consecutive_matched_publications_chronological',
      title_length: 'unicode_code_points', tag_count: 'stored_string_list_length_empty_list_is_zero'},
    source: {kind: 'derived_from_stored_current_youtube_fields', format_source: 'video_inventory.video_type',
      youtube_requests: false, database_writes: false, snapshots_used: false, availability_history_used: false,
      trends_used: false, performance_scores_used: false, freshness_enforced: true},
  };
  const current = parseBaselineV1(payload);
  const legacy = structuredClone(payload);
  legacy.content_breakdown.catalogue_age.source = 'let_it_cook.video_age_from_published_at';
  const before = structuredClone(legacy);
  const parsed = parseBaselineV1(legacy);
  assert.deepEqual(parsed, current, 'legacy source normalizes to the same validated payload');
  assert.deepEqual(legacy, before, 'normalization does not mutate caller data');
  assert.equal(parsed.content_breakdown.catalogue_age.source_expires_at, deadline);
  assert.equal(parsed.source_expires_at, deadline);
  for (const mutate of [
    value => { value.content_breakdown.catalogue_age.source = 'let_it_cook.unrecognized'; },
    value => { value.content_breakdown.captions.source = 'let_it_cook.video_age_from_published_at'; },
    value => { value.content_breakdown.catalogue_age.known_count = 1; },
    value => { value.source.youtube_requests = true; },
  ]) {
    const invalid = structuredClone(legacy); mutate(invalid);
    assert.throws(() => parseBaselineV1(invalid), 'legacy alias must not weaken other invariants');
  }
}

// Product version is received from the server, including prereleases.
{
  const {parseExplorerSystemSummary} = compiled('../application/frontend/src/explorer/contracts.ts');
  const payload = {status: 'ok', warnings: [], latest_run: null, recent_runs: []};
  assert.deepEqual(parseExplorerSystemSummary(payload), payload, 'old status payloads remain readable');
  for (const version of ['0.1.0', '0.1.0-rc.2', '10.20.30-rc.10+build.001']) {
    assert.equal(parseExplorerSystemSummary({...payload, version}).version, version);
  }
  for (const version of [null, {}, 'v0.1.0', '0.1', '01.1.0', '0.1.0-rc.01', '0.1.0+build..1', '0.1.0\n', '<script>']) {
    assert.throws(() => parseExplorerSystemSummary({...payload, version}));
  }
}
