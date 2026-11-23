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

// Owner session: only a refusal or explicit sign-out ends a mounted session. The end keeps this history entry, tells
// the page first (the canvas keeps its unsaved work) and goes to sign-in with the way back.
const page = '/app-explorer-v3/research', query = '?saved=11111111-1111-4111-8111-111111111111';
const back = '/auth?return=' + encodeURIComponent(page + query);
function session(responses, pathname = page, search = query) {
  const log = {redirects: [], replaced: [], cleared: 0, mounts: 0, interval: null, events: []};
  const element = {replaceChildren: () => { log.cleared += 1; }};
  const window = {
    location: {pathname, search, replace: path => { log.redirects.push(path); log.replaced.push(path); }, assign: path => log.redirects.push(path)},
    setInterval: callback => { log.interval = callback; },
    addEventListener: () => {},
    dispatchEvent: event => { log.events.push([event.type, log.cleared]); return true; },
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
  assert.deepEqual([log.redirects, log.cleared > 0], [[back], true], 'an explicit sign-out ends the session');
  assert.deepEqual(log.events, [['leti-cook:session-ended', 0]], 'the page is told before it is cleared');
  assert.deepEqual(log.replaced, [], 'the history entry stays (assign, not replace)');
}
{
  const log = session([{status: 200, body: {authenticated: true}}, {status: 401}]);
  await tick(); log.interval(); await tick();
  assert.deepEqual([log.redirects, log.events.length], [[back], 1], 'a refusal ends the session');
}
{
  const log = session([{status: 503}]);
  await tick();
  assert.deepEqual([log.mounts, log.redirects, log.events], [0, [back], []], 'the first check still requires access');
}
{
  const log = session([{status: 200, body: {authenticated: true}}, {status: 401}], '/setup', '');
  await tick(); log.interval(); await tick();
  assert.deepEqual(log.redirects, ['/auth'], 'only app pages are returned to');
}
// One return pattern in three places: ownerSession.ts, auth.js (and self_hosted_auth.py, checked by the Python tests).
{
  const owner = compiled('../application/frontend/src/ownerSession.ts', {window: {}, document: {}, fetch: null},
    source => source.replace('import.meta.env.VITE_LIC_DEPLOYMENT_PROFILE', "'self-hosted'"));
  const auth = read('../application/self_hosted_ui/auth.js');
  const patterns = new Function('return ' + /const RETURN_PATHS = (\[.*\]);/.exec(auth)[1])();
  assert.equal(patterns[1].source, owner.APP_RETURN.source, 'auth.js and ownerSession.ts share the app pattern');
  const accepted = ['/app-explorer-v3', '/app-explorer-v3/', '/app-explorer-v3/research', page + query, page + query + '&settings=fields',
    '/app-explorer-v3/channel/UCAAAAAAAAAAAAAAAAAAAAAA', '/app-explorer-v3/research?settings=fields'];
  const refused = ['//evil.example', '/app-explorer-v3//evil.example', '/app-explorer-v3/../auth', '/app-explorer-v3/./research', '/\\evil.example',
    'https://evil.example/app-explorer-v3', '/app-explorer-v3@evil.example', '/app-explorer-v3x', '/app-explorer-v3/research#top',
    '/app-explorer-v3/research?x=<b>', '/app-explorer-v3/research?x="', '/app-explorer-v3/ x', '/auth', '/setup', '', '/app-explorer-v3/' + 'a'.repeat(2048),
    '/app-explorer-v3/%2e%2e/auth', '/app-explorer-v3/%2E%2E', '/app-explorer-v3/%2F%2Fevil.example', '/app-explorer-v3/.hidden'];
  for (const value of accepted) {
    assert.equal(owner.signInAddress({pathname: value.split('?')[0], search: value.includes('?') ? value.slice(value.indexOf('?')) : ''}), '/auth?return=' + encodeURIComponent(value), value);
    assert.ok(patterns.some(pattern => pattern.test(value)), value);
  }
  for (const value of refused) {
    assert.equal(owner.APP_RETURN.test(value) && value.length <= 2048, false, value);
    assert.equal(patterns[1].test(value) && value.length <= 2048, false, value);
  }
  assert.ok(patterns[0].test('/oauth/authorize?client_id=x'), 'the Allow page is still resumed');
  assert.ok(/clearCanvasCopies\(\); window\.location\.replace\('\/auth'\)/.test(auth), 'signing out ends the tab copies');
  assert.ok(auth.includes("startsWith('leti-cook.workshop.')"));
}
console.log('Frontend resilience: session checks passed');

// User-work bookmarks: a channel removed for good leaves no identity, so its
// waiting bookmark is dropped instead of breaking every later backup.
{
  const store = new Map();
  const storage = {getItem: key => store.has(key) ? store.get(key) : null, setItem: (key, value) => store.set(key, String(value)), removeItem: key => store.delete(key)};
  let bookmarks = new Set();
  const stubs = {
    './catalogueQuery.ts': {normalizeCatalogueConditions: value => value, serializeCatalogueConditions: () => ''},
    './savedViews': {normalizeSavedConditions: value => value},
    './researchFieldPreferences': compiled('../application/frontend/src/explorer/researchFieldPreferences.ts'),
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
  storage.setItem(work.PENDING_BOOKMARKS_STORAGE_KEY, JSON.stringify([bound, waiting, gone]));
  assert.deepEqual(work.readPendingBookmarks(storage), [bound, waiting, gone]);
  const status = {channel_mappings: [{id: bound, channel_id: 'UC' + 'a'.repeat(22)}], user_channel_refs: [bound, waiting]};
  assert.equal(work.resolveUserWorkBookmarks(status, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), [waiting]);
  assert.deepEqual([...bookmarks], ['UC' + 'a'.repeat(22)]);
  storage.setItem(work.PENDING_BOOKMARKS_STORAGE_KEY, JSON.stringify([gone]));
  assert.equal(work.resolveUserWorkBookmarks({channel_mappings: [], user_channel_refs: []}, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), []);
  assert.equal(storage.getItem(work.PENDING_BOOKMARKS_STORAGE_KEY), '[]');
  assert.equal(work.resolveUserWorkBookmarks(status, storage), true);
  assert.deepEqual(work.readPendingBookmarks(storage), [], 'completed work stays completed on the next read');
  assert.deepEqual([...bookmarks], ['UC' + 'a'.repeat(22)], 'clearing pending work preserves resolved bookmarks');
}

// Backup format 2 lists the preset label as kept, renamed or not at all.
{
  const stubs = {
    './catalogueQuery.ts': {normalizeCatalogueConditions: value => value, serializeCatalogueConditions: () => ''},
    './savedViews': {normalizeSavedConditions: value => value},
    './researchFieldPreferences': compiled('../application/frontend/src/explorer/researchFieldPreferences.ts'),
    './savedWork.ts': {savedId: value => value, savedRevision: value => value, savedTimestamp: value => value},
    './cataloguePreferences.ts': {catalogueDensityPreference: {isValid: () => true}},
    '../state/browserPreferences.ts': {readBrowserPreference: () => 'large', writeBrowserPreference: () => {}},
    './bookmarks.ts': {EXPLORER_BOOKMARKS_STORAGE_KEY: 'b', loadExplorerBookmarks: () => new Set(), saveExplorerBookmarks: () => true},
    './folderCollapse.ts': {COLLAPSED_FOLDERS_STORAGE_KEY: 'f', loadCollapsedFolders: () => new Set(), saveCollapsedFolders: () => {}},
  };
  const {outputText} = ts.transpileModule(read('../application/frontend/src/explorer/userWorkBackup.ts'), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const work = {};
  new Function('require', 'exports', outputText)(name => stubs[name], work);
  const file = (version, labels) => ({format: 'leti-cook-user-work', version, created_at: '2026-09-20T00:00:00Z',
    user_work: {folders: [], labels, channels: [], collections: [], reviews: []},
    browser_preferences: {catalogue_density: 'large', bookmarked_channel_refs: [], collapsed_folder_ids: []}});
  const preset = {label_key: 'monetized', name: 'Paid', label_type: 'preset', created_at: '2026-09-01T00:00:00Z'};
  const parsed = work.parseUserWorkBackup(file(2, [preset]));
  assert.equal(parsed.version, 2);
  assert.equal(parsed.format, 'leti-cook-user-work');
  assert.throws(() => work.parseUserWorkBackup({...file(2, []), format: 'user-work'}));
  assert.throws(() => work.parseUserWorkBackup({...file(2, []), format: 'leti-cook-user-work-v2'}));
  assert.deepEqual(parsed.user_work.labels.map(row => [row.label_key, row.name, row.label_type]), [['monetized', 'Paid', 'preset']]);
  assert.equal(work.parseUserWorkBackup(file(1, [])).version, 1);
  assert.throws(() => work.parseUserWorkBackup(file(1, [preset])));
  assert.throws(() => work.parseUserWorkBackup(file(3, [])));
}

// Another tab's bookmark or folder change reloads them here; unrelated keys do not.
{
  const stubs = {'./bookmarks.ts': {EXPLORER_BOOKMARKS_STORAGE_KEY: 'b'},
    './folderCollapse.ts': {COLLAPSED_FOLDERS_STORAGE_KEY: 'f'}};
  const {outputText} = ts.transpileModule(read('../application/frontend/src/explorer/preferenceSync.ts'), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const sync = {};
  new Function('require', 'exports', outputText)(name => { if (!stubs[name]) throw new Error(`unexpected import ${name}`); return stubs[name]; }, sync);
  const listeners = new Set();
  const target = {addEventListener: (type, listener) => { assert.equal(type, 'storage'); listeners.add(listener); },
                  removeEventListener: (type, listener) => listeners.delete(listener)};
  let reloads = 0;
  const stop = sync.watchSharedPreferences(target, () => { reloads += 1; });
  for (const key of ['b', 'f', null, 'other']) listeners.forEach(listener => listener({key}));
  assert.equal(reloads, 3);
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
  // The separate fetch wrappers name a lost or unreadable answer in the page's words.
  const lost = text.noAnswerError(new TypeError('Failed to fetch'));
  assert.deepEqual([lost.name, lost.message], ['ExplorerNetworkError', 'No connection to the Leti Cook server.']);
  assert.equal(text.noAnswerError(new TypeError('NetworkError when attempting to fetch resource.'), true).message,
    'No connection to the Leti Cook server. The result is not confirmed; refresh status before retrying.');
  const abort = Object.assign(new TypeError('aborted'), { name: 'AbortError' }), own = new Error('The response is too large.');
  assert.equal(text.noAnswerError(abort, true), abort, 'a cancel is not a lost connection');
  assert.equal(text.noAnswerError(own, true), own, "the wrapper's own words pass");
  assert.equal(text.answerReadError(new SyntaxError("Unexpected token '<', \"<html>\" is not valid JSON")).message, 'The server answer could not be read.');
  assert.equal(text.answerReadError(new SyntaxError('Unexpected end of JSON input'), true).message,
    'The server answer could not be read. The result is not confirmed; refresh status before retrying.');
  assert.equal(text.answerReadError(new TypeError('terminated')).message, 'No connection to the Leti Cook server.');
  assert.equal(text.unreadableAnswerError(new TypeError('The encoded data was not valid for encoding utf-8')).message, 'The server answer could not be read.');
  // The same sentence as requestJson's ExplorerNetworkError (explorer/api.ts).
  assert.match(read('../application/frontend/src/explorer/api.ts'), new RegExp(`super\\('${text.TRANSPORT_TEXT.noConnection.replace(/\./g, '\\.')}'`));
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

// Numbers show the precision a reader can use: whole counts from 1,000,
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

// "1 video", not "1 videos".
{
  const withoutImports = source => source.replace(/^import [^;]+;$/gm, '');
  const format = compiled('../application/frontend/src/explorer/format.ts', {formatYouTubeDuration: () => ''}, withoutImports);
  for (const one of [1, '1', 1n]) assert.equal(format.videoNoun(one), 'video', String(one));
  for (const many of [0, 2, '1,000', 11, undefined, null]) assert.equal(format.videoNoun(many), 'videos', String(many));
}

// Moments are shown in the viewer's own time zone, never labelled UTC.
{
  const local = compiled('../application/frontend/src/explorer/localTime.ts');
  const value = '2026-09-26T07:16:48.568910Z', date = new Date(value), pad = n => String(n).padStart(2, '0');
  assert.equal(local.formatExactLocal(value), `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`);
  assert.equal(local.formatExactLocal('not a time'), 'Not recorded');
  assert.equal(local.formatTimestampLocal(null, 'recently'), 'recently');
  for (const text of [local.formatTimestampLocal(value), local.formatDateLocal(value)]) assert.doesNotMatch(text, /UTC/);
}

// Local mode has no account: the sign-out link needs the access mode.
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

// An unknown list is never zero; the independent inventory proof owns the difference.
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

// Browser display values are written and read under their current keys only; a corrupt value
// reads as empty or the default, and blocked storage never stops the page.
{
  const store = new Map();
  const writes = [];
  const storage = {getItem: key => store.has(key) ? store.get(key) : null,
    setItem: (key, value) => { writes.push(key); store.set(key, String(value)); }, removeItem: key => store.delete(key)};
  const bookmarks = compiled('../application/frontend/src/explorer/bookmarks.ts');
  const folders = compiled('../application/frontend/src/explorer/folderCollapse.ts');
  const preference = compiled('../application/frontend/src/state/browserPreferences.ts');
  const density = compiled('../application/frontend/src/explorer/cataloguePreferences.ts').catalogueDensityPreference;
  bookmarks.saveExplorerBookmarks(['UCsaved'], storage);
  folders.saveCollapsedFolders(new Set(['unassigned']), storage);
  preference.writeBrowserPreference(density, 'compact', storage);
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(storage)], ['UCsaved']);
  assert.deepEqual([...folders.loadCollapsedFolders(storage)], ['unassigned']);
  assert.equal(preference.readBrowserPreference(density, storage), 'compact');
  assert.deepEqual(writes, [bookmarks.EXPLORER_BOOKMARKS_STORAGE_KEY, folders.COLLAPSED_FOLDERS_STORAGE_KEY, density.key]);
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
  const blocked = {getItem() {throw new Error('blocked');}, setItem() {throw new Error('blocked');}};
  assert.deepEqual([...bookmarks.loadExplorerBookmarks(blocked)], []);
  assert.deepEqual([...folders.loadCollapsedFolders(blocked)], []);
  assert.equal(preference.readBrowserPreference(density, blocked), 'large');
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
