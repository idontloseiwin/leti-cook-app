import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as routes from '../application/frontend/src/explorerRoutes.ts';
import * as query from '../application/frontend/src/explorer/catalogueQuery.ts';
import * as sourceLifetime from '../application/frontend/src/sourceLifetime.ts';
import * as savedLibrary from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
function compiled(path, imports) {
  const { outputText } = ts.transpileModule(read(path), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const exports = {};
  new Function('require', 'exports', outputText)(name => {assert.ok(name in imports, name);return imports[name];}, exports);
  return exports;
}
const work = compiled('../application/frontend/src/explorer/savedWork.ts', {});
const research = compiled('../application/frontend/src/explorer/researchQuery.ts', {'./api':{}, '../sourceLifetime':sourceLifetime});
const definition = compiled('../application/frontend/src/explorer/savedQueryDefinition.ts', {'./researchQuery':research, './savedWork':work, './timeZones':compiled('../application/frontend/src/explorer/timeZones.ts', {})});
const operations=compiled('../application/frontend/src/explorer/researchOperations.ts',{'./api':{},'../sourceLifetime':sourceLifetime});
const graph=compiled('../application/frontend/src/explorer/researchGraph.ts',{'./api':{},'./savedWork':work,'./savedQueryDefinition':definition,'./researchQuery':research,'./researchOperations':operations,'../sourceLifetime':sourceLifetime});
const id = '44444444-4444-4444-8444-444444444444';
const channel = 'UC' + 'a'.repeat(22);
// Hidden identities are a bounded, read-only list independent from review cases.
const hidden = compiled('../application/frontend/src/videoTypeReviews.ts', { './serverText.ts': { serverErrorMessage() {} }, './sourceLifetime': sourceLifetime });
const hiddenRow = { video_id: 'a'.repeat(11), channel_id: channel };
assert.deepEqual(hidden.parseHiddenVideoIdentities({ contract_version: 1, count: 1, videos: [hiddenRow], next_cursor: null }),
  { count: 1, videos: [hiddenRow], next_cursor: null });
for (const changes of [{ count: -1 }, { count: null }, { videos: [hiddenRow, hiddenRow] }, { next_cursor: 'b'.repeat(11) },
  { videos: [{ video_id: '../unsafe', channel_id: channel }] }, { videos: Array(101).fill(hiddenRow) }]) {
  assert.throws(() => hidden.parseHiddenVideoIdentities({ contract_version: 1, count: 1, videos: [hiddenRow], next_cursor: null, ...changes }));
}
const expected = '/app-explorer-v3';
// An acknowledged zero is quiet on the channel overview; missing/failed data
// must remain visible and the full review list must keep its refresh controls.
{
  const { outputText } = ts.transpileModule(read('../application/frontend/src/components/HiddenVideoIdentities.tsx'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  });
  const render = ({ count, countOnly = true, loading = false, error = false }) => {
    const state = [count === undefined ? undefined : { count, videos: [], next_cursor: null }, [undefined], error, loading, 0];
    const jsx = (type, props) => ({ type, props });
    const imports = {
      react: { useState: () => [state.shift(), () => {}], useEffect: () => {} },
      'react/jsx-runtime': { jsx, jsxs: jsx }, '../videoTypeReviews': {},
    };
    const exports = {};
    new Function('require', 'exports', outputText)(name => { assert.ok(name in imports, name); return imports[name]; }, exports);
    return exports.HiddenVideoIdentities({ channelId: channel, countOnly });
  };
  assert.equal(render({ count: 0 }), null);
  assert.notEqual(render({ count: 1 }), null);
  assert.notEqual(render({ loading: true }), null);
  assert.match(JSON.stringify(render({ error: true })), /Retry/);
  assert.match(JSON.stringify(render({ count: 0, countOnly: false })), /Refresh hidden videos/);
}
// V3 is the only served workspace: navigation must not depend on the HTML entry.
for (const declared of [undefined, '/app-explorer-v3', '/app-explorer-v2', '/evil']) {
  if (declared === undefined) delete globalThis.document;
  else globalThis.document = {documentElement: {dataset: {explorerRoot: declared}}};
  const saved = compiled('../application/frontend/src/explorer/savedViews.ts', {'./researchGraph':graph,'./api':{}, '../explorerRoutes':routes, './catalogueQuery':query, './savedWork':work, './savedQueryDefinition':definition, './timeZones':compiled('../application/frontend/src/explorer/timeZones.ts', {}), './libraryData/savedLibrary.ts':savedLibrary});
  const collections = compiled('../application/frontend/src/explorer/savedCollections.ts', {'./api':{}, './contracts':{}, './savedWork':work, '../explorerRoutes':routes});
  assert.equal(routes.EXPLORER_V3_ROOT, expected);
  assert.equal(routes.explorerChannelPath(channel), `${expected}/channels/${channel}`);
  assert.equal(routes.parseExplorerChannelPath(`${expected}/channels/${channel}`),channel);
  for (const retired of ['/app-explorer-v2', '/app-explorer', '/app-next']) {
    assert.equal(routes.parseExplorerChannelPath(`${retired}/channels/${channel}`),undefined, retired);
    assert.deepEqual(saved.readExplorerDestination(`${retired}/health`),{kind:'home'}, retired);
  }
  for(const page of ['health','telemetry','agents']) {
    assert.equal(routes.explorerOperationalPath(page),`${expected}/${page}`);
    assert.deepEqual(saved.readExplorerDestination(`${expected}/${page}`),{kind:page});
  }
  assert.equal(saved.SAVED_VIEWS_PATH,`${expected}/saved`);
  assert.equal(collections.COLLECTIONS_INDEX,`${expected}/saved?tab=collections`);
  assert.equal(collections.collectionDetailPath(id),`${expected}/saved/collections/${id}`);
  assert.deepEqual(saved.readExplorerDestination(`${expected}/saved/collections/${id}`),{kind:'saved',collectionId:id});
  assert.deepEqual(saved.readExplorerDestination(`${expected}/saved`,'?tab=collections'),{kind:'saved',tab:'collections'});
  const url=saved.savedViewUrl({channel_id:channel,conditions:query.createInitialCatalogueConditions()},'http://localhost');
  assert.equal(url.pathname,`${expected}/channels/${channel}`);
}
delete globalThis.document;
// Retired navigation helpers and version switches stay removed.
assert.deepEqual(Object.keys(routes).sort(), ['EXPLORER_V3_ROOT', 'explorerChannelPath', 'explorerOperationalPath',
  'parseExplorerChannelPath', 'parseExplorerOperationalPath']);
// A membership command that changed nothing keeps the revision it expected.
{
  globalThis.document = {documentElement: {dataset: {}}};
  const collections = compiled('../application/frontend/src/explorer/savedCollections.ts', {'./api':{}, './contracts':{}, './savedWork':work, '../explorerRoutes':routes});
  delete globalThis.document;
  const request = '55555555-5555-4555-8555-555555555555';
  const command = {request_id: request, expected_revision: 2, video_ids: ['aaaaaaaaaaa', 'bbbbbbbbbbb']};
  const at = '2026-09-26T10:00:00Z';
  const reply = (revision, changed, applied) => ({contract_version: 1,
    collection: {id, kind: 'collection', name: 'Mix', revision, member_count: 2, created_at: at, updated_at: at},
    mutation: {request_id: request, replayed: false, requested_count: 2, changed_count: changed, applied_revision: applied}});
  assert.equal(collections.parseCollectionMutation(reply(2, 0, 2), id, command).mutation.applied_revision, 2);
  assert.equal(collections.parseCollectionMutation(reply(3, 1, 3), id, command).mutation.applied_revision, 3);
  for (const [revision, changed, applied] of [[3, 0, 3], [2, 2, 2]]) {
    assert.throws(() => collections.parseCollectionMutation(reply(revision, changed, applied), id, command), /not confirmed/);
  }
}

// A collection member may stay readable for the whole 29-day source window.
{
  globalThis.document = {documentElement: {dataset: {}}};
  const collections = compiled('../application/frontend/src/explorer/savedCollections.ts', {'./api':{}, './contracts':{parseStoredExplorerVideo: video => video}, './savedWork':work, '../explorerRoutes':routes});
  delete globalThis.document;
  const checked = '2026-10-08T10:00:00Z', video = 'aaaaaaaaaaa';
  const page = lifetime => ({contract_version: 1,
    collection: {id, kind: 'collection', name: 'Mix', revision: 1, member_count: 1, created_at: checked, updated_at: checked},
    members: [{video_id: video, added_at: checked, status: 'available', reason: null, channel_id: channel,
      video: {video_id: video, video_type: 'short', title: 'T', thumbnail_url: null, published_at: checked, checked_at: checked, duration: 'PT1M',
        views: 1, like_count: null, comment_count: null, category_id: null, category_name: null, duration_seconds: 60},
      source_expires_at: new Date(Date.parse(checked) + lifetime).toISOString()}],
    next_cursor: null, source_read_at: checked});
  assert.equal(collections.SOURCE_RETENTION_MS, 29 * 86400000);
  assert.equal(collections.parseMembersPage(page(collections.SOURCE_RETENTION_MS), id, undefined, 0).members.length, 1);
  assert.throws(() => collections.parseMembersPage(page(collections.SOURCE_RETENTION_MS + 1000), id, undefined, 0), /counter lifetime/);
  // Every video type is a member; its type travels with the shown values.
  // A recent clock keeps these checks independent of the fixed fixture date.
  const recent = new Date(Date.now() - 60000).toISOString(), fresh = () => {
    const value = page(0); value.collection.created_at = value.collection.updated_at = recent; value.source_read_at = recent;
    Object.assign(value.members[0], {added_at: recent, source_expires_at: new Date(Date.now() + 3600000).toISOString()});
    value.members[0].video.published_at = value.members[0].video.checked_at = recent;
    return value;
  };
  for (const type of ['long', 'short', 'stream']) {
    const typed = fresh(); typed.members[0].video.video_type = type;
    assert.equal(collections.parseMembersPage(typed, id, undefined, 0).members[0].video.video_type, type);
  }
  for (const type of [undefined, 'live', null]) {
    const typed = fresh(); if (type === undefined) delete typed.members[0].video.video_type; else typed.members[0].video.video_type = type;
    assert.throws(() => collections.parseMembersPage(typed, id, undefined, 0));
  }
  // One reason per unavailable member, as the shared collection scope names them.
  const reasons = ['channel_hidden', 'channel_unavailable', 'inventory_missing', 'source_mismatch', 'type_conflict', 'type_unverified',
    'source_missing', 'source_unavailable', 'source_unpublished', 'stream_incomplete', 'source_stale'];
  assert.deepEqual(Object.keys(collections.COLLECTION_SOURCE_REASONS).sort(), [...reasons, 'api_paused'].sort());
  for (const reason of [...reasons, 'type_unsupported']) {
    const missing = fresh();
    missing.members[0] = {video_id: video, added_at: checked, status: 'unavailable', reason, channel_id: null, video: null, source_expires_at: null};
    if (reason === 'type_unsupported') assert.throws(() => collections.parseMembersPage(missing, id, undefined, 0));
    else assert.equal(collections.parseMembersPage(missing, id, undefined, 0).members[0].reason, reason);
  }
}
console.log('Explorer V3 roots, Saved and deep links: passed');

// The research workshop: one address, a plain link that navigates without a reload,
// ?saved= and ?settings=fields keep the research destination; Library hands off through memory.
{
  globalThis.document = {documentElement: {dataset: {}}};
  const saved = compiled('../application/frontend/src/explorer/savedViews.ts', {'./researchGraph':graph,'./api':{}, '../explorerRoutes':routes, './catalogueQuery':query, './savedWork':work, './savedQueryDefinition':definition, './timeZones':compiled('../application/frontend/src/explorer/timeZones.ts', {}), './libraryData/savedLibrary.ts':savedLibrary});
  delete globalThis.document;
  for (const search of ['', '?saved=11111111-1111-4111-8111-111111111111', '?settings=fields', '?saved=a&saved=b'])
    for (const path of [`${expected}/research`, `${expected}/research/`]) assert.deepEqual(saved.readExplorerDestination(path, search), {kind: 'research'});
  assert.deepEqual(saved.readExplorerDestination(`${expected}/workshop`), {kind: 'home'}, 'no second address');
  assert.equal(saved.RESEARCH_WORKSHOP_PATH, `${expected}/research`);
  const app = read('../application/frontend/src/explorer/ExplorerApp.tsx');
  const link = app.slice(app.indexOf("<a href={rootPath + '/research'}"), app.indexOf('<span>Research workshop</span></a>'));
  assert.match(link, /title="Research workshop" data-label="Research workshop"/);
  assert.match(link, /if \(event\.button !== 0 \|\| event\.ctrlKey \|\| event\.metaKey \|\| event\.shiftKey \|\| event\.altKey\) return;\s*event\.preventDefault\(\); showWorkshop\(\);/);
  assert.match(link, /<Workflow size=\{17\} aria-hidden="true" \/>/);
  assert.doesNotMatch(app, /ResearchCatalogue|>Catalogue</);
  assert.match(app, /pageBoundary\('Research workshop', <Suspense fallback=\{<LoadingState label="Loading research workshop…" \/>\}><ResearchWorkshopPage /);
  assert.match(app, /handoffFor=\{\(entry\) => workshopHandoff\.current\?\.entry === entry \? workshopHandoff\.current\.handoff : undefined\}/);
  assert.match(app, /onOpenWorkshop=\{openWorkshop\}/);
  assert.match(app, /showWorkshop\(\{ state: writeWorkshopHistory\(null, \{ version: 1, entry, handoff \}\) \}\)/);
}
console.log('Research workshop navigation, address and Library hand-off wiring: passed');

// The recent view orders the whole library, not separate folder sublists.
{
  const localTime = compiled('../application/frontend/src/explorer/localTime.ts', {});
  const gone = compiled('../application/frontend/src/explorer/goneChannels.ts', {'./localTime': localTime});
  const sidebar = compiled('../application/frontend/src/explorer/sidebar.ts', {
    './imageHosts': {safeImageUrl: value => value}, './goneChannels': gone,
  });
  const channels = [
    {channel_id: 'old', title: 'A oldest', folder_id: 'a', added_at: '2026-10-10T00:00:00Z'},
    {channel_id: 'undated', title: 'A undated', folder_id: 'a', added_at: null},
    {channel_id: 'second', title: 'B second', folder_id: null, added_at: '2026-10-15T09:00:00Z'},
    {channel_id: 'new', title: 'Z newest', folder_id: 'z', added_at: '2026-10-16T09:00:00Z'},
    {channel_id: 'bad', title: 'B bad', added_at: 'invalid'},
  ];
  const folders = [{id: 'a', name: 'A folder'}, {id: 'z', name: 'Z folder'}];
  const recent = {metric: 'added_at', direction: 'desc'};
  const groups = sidebar.groupExplorerSidebarChannels(channels, folders, recent);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].label, 'Recently added');
  assert.deepEqual(groups[0].channels.map(c => c.channel_id), ['new', 'second', 'old', 'undated', 'bad']);
  assert.equal(channels[0].channel_id, 'old', 'Sorting must not mutate the source payload.');
  assert.deepEqual(sidebar.nextExplorerSidebarSort({metric: 'name', direction: 'asc'}, 'added_at'), recent);
  const oldest = sidebar.nextExplorerSidebarSort(recent, 'added_at');
  assert.deepEqual(sidebar.sortExplorerSidebarChannels(channels, oldest).map(c => c.channel_id),
    ['old', 'second', 'new', 'undated', 'bad']);
  assert.deepEqual(sidebar.groupExplorerSidebarChannels(channels, folders, recent, 'second')[0].channels.map(c => c.channel_id), ['second']);
  assert.equal(sidebar.groupExplorerSidebarChannels(channels, folders, {metric: 'name', direction: 'asc'}).length, 3);
  const moved = sidebar.assignExplorerSidebarChannelFolder(channels, 'new', 'a');
  assert.equal(sidebar.groupExplorerSidebarChannels(moved, folders, recent)[0].channels[0].channel_id, 'new');
  const retained = compiled('../application/frontend/src/sidebarUserState.ts', {'./sourceLifetime.ts': sourceLifetime});
  const local = retained.retainSidebarUserState({channels: [{...channels[0], title: 'API title', subscribers: 123}], folders: [], labels: []});
  assert.equal(local.channels[0].added_at, channels[0].added_at);
  assert.equal(local.channels[0].title, undefined);
  assert.equal(local.channels[0].subscribers, undefined);

  // Channels YouTube no longer returns (docs/DATA.md): last under every sort and
  // direction, named plainly and never by a stored title, with their removal date.
  const now = Date.parse('2026-11-05T12:00:00Z');
  const goneRow = {channel_id: 'UCgone', title: null, channel_metadata_status: 'channel_not_found',
    channel_metadata_checked_at: '2026-11-05T09:00:00Z', youtube_data_expires_at: '2026-12-01T10:00:00Z', added_at: '2026-10-17T00:00:00Z',
    subscribers: null, video_count: null};
  const goneWithTitle = {...goneRow, channel_id: 'UCaaagone', title: 'Aardvark (stored)', youtube_data_expires_at: null};
  const mixed = [goneRow, goneWithTitle, ...channels, {channel_id: 'UCloading', title: null, added_at: null}];
  for (const metric of ['name', 'video_count', 'subscribers', 'added_at']) {
    for (const direction of ['asc', 'desc']) {
      const order = sidebar.sortExplorerSidebarChannels(mixed, {metric, direction}).map(c => c.channel_id);
      assert.deepEqual(order.slice(-2).sort(), ['UCaaagone', 'UCgone'], `${metric} ${direction}`);
    }
  }
  assert.deepEqual(sidebar.groupExplorerSidebarChannels(mixed, folders, recent)[0].channels.slice(-2).map(c => c.channel_id).sort(),
    ['UCaaagone', 'UCgone']);
  assert.deepEqual(sidebar.groupExplorerSidebarChannels(mixed, folders, {metric: 'name', direction: 'asc'}, 'no longer on')
    .flatMap(g => g.channels).map(c => c.channel_id).sort(), ['UCaaagone', 'UCgone']);
  assert.equal(gone.channelDisplayName(goneRow), 'No longer on YouTube');
  assert.equal(gone.channelDisplayName({channel_id: 'UCloading'}), 'UCloading');
  assert.equal(gone.channelChoiceName(goneRow), 'No longer on YouTube (UCgone)');
  assert.equal(gone.goneChannelRemovalLine(goneRow, now), `Data removed by ${localTime.formatDateLocal(goneRow.youtube_data_expires_at)}`);
  assert.equal(gone.goneChannelRemovalLine({...goneRow, youtube_data_expires_at: null}, now), 'Stored data expired');
  assert.equal(gone.goneChannelRemovalLine(goneRow, Date.parse('2026-12-02T00:00:00Z')), 'Stored data expired');
  assert.equal(gone.goneChannelRemovalLine({...goneRow, youtube_data_expires_at: undefined}, now), '');
  const notice = gone.goneChannelNotice(goneRow, now);
  assert.equal(notice, `YouTube no longer returns this channel (last checked ${localTime.formatDateLocal(goneRow.channel_metadata_checked_at)}). `
    + `Its stored YouTube data is removed by ${localTime.formatDateLocal(goneRow.youtube_data_expires_at)} under the 29-day data rule. `
    + 'Your folders, labels and saved work for it stay.');
  assert.doesNotMatch(gone.goneChannelNotice({...goneRow, youtube_data_expires_at: undefined}, now), /removed by|expired/);
  // The first missing day; null or absent: unknown, the notice names only the last check.
  const since = '2026-11-02T06:00:00Z';
  assert.equal(gone.goneChannelNotice({...goneRow, channel_metadata_not_found_since: since}, now),
    `YouTube has not returned this channel since ${localTime.formatDateLocal(since)} (last checked ${localTime.formatDateLocal(goneRow.channel_metadata_checked_at)}). `
    + `Its stored YouTube data is removed by ${localTime.formatDateLocal(goneRow.youtube_data_expires_at)} under the 29-day data rule. `
    + 'Your folders, labels and saved work for it stay.');
  assert.equal(gone.goneChannelNotice({...goneRow, channel_metadata_not_found_since: null}, now), notice);
  assert.equal(gone.goneChannelNotice({...goneRow, channel_metadata_not_found_since: 'not a date'}, now), notice);
  assert.match(gone.goneChannelNotice({...goneRow, channel_metadata_not_found_since: since, channel_metadata_checked_at: null}, now),
    new RegExp(`^YouTube has not returned this channel since ${localTime.formatDateLocal(since)}\\. Its stored`));
  const summary = gone.goneChannelSummary([...mixed, {...goneRow, channel_id: 'UClater', youtube_data_expires_at: '2026-12-20T00:00:00Z'}], now);
  assert.deepEqual(summary, {count: 3, earliest: goneRow.youtube_data_expires_at});
  assert.match(gone.goneChannelSummaryText(summary), /^YouTube no longer returns them\. Their stored YouTube data is removed under the 29-day data rule, the first by /);
  assert.equal(gone.goneChannelSummaryText({count: 1}), 'YouTube no longer returns it. Its stored YouTube data has expired under the 29-day data rule. Your folders, labels and saved work stay.');
  assert.deepEqual(gone.goneChannelSummary(channels, now), {count: 0});
  // A deadline the server did not confirm (no field, or not a date) is unknown, never "expired".
  const unconfirmed = {...goneRow, youtube_data_expires_at: undefined};
  const lone = gone.goneChannelSummary([unconfirmed], now);
  assert.deepEqual(lone, {count: 1, unknown: 1});
  assert.equal(gone.goneChannelSummaryText(lone),
    'YouTube no longer returns it. The date for its stored YouTube data is not confirmed yet. Your folders, labels and saved work stay.');
  const invalid = gone.goneChannelSummary([{...goneRow, youtube_data_expires_at: 'soon'}, unconfirmed], now);
  assert.deepEqual(invalid, {count: 2, unknown: 2});
  assert.doesNotMatch(gone.goneChannelSummaryText(invalid), /expired|removed/);
  const partly = gone.goneChannelSummary([goneRow, {...goneRow, youtube_data_expires_at: null}, unconfirmed], now);
  assert.deepEqual(partly, {count: 3, earliest: goneRow.youtube_data_expires_at, unknown: 1});
  assert.equal(gone.goneChannelSummaryText(partly), 'YouTube no longer returns them. Their stored YouTube data is removed under the 29-day data rule, '
    + `the first confirmed one by ${localTime.formatDateLocal(goneRow.youtube_data_expires_at)}. For 1 of them the date is not confirmed yet. `
    + 'Your folders, labels and saved work stay.');
  const expiredAndUnknown = gone.goneChannelSummaryText(gone.goneChannelSummary([{...goneRow, youtube_data_expires_at: null}, unconfirmed], now));
  assert.doesNotMatch(expiredAndUnknown, /has expired/);
  // When a row's source fields expire, the notice's own clocks stay; others keep none.
  const kept = retained.retainSidebarUserState({channels: [{...goneRow, channel_metadata_not_found_since: since},
    {...channels[0], channel_metadata_checked_at: '2026-11-05T09:00:00Z'}], folders: [], labels: []});
  assert.equal(kept.channels[0].youtube_data_expires_at, goneRow.youtube_data_expires_at);
  assert.equal(kept.channels[0].channel_metadata_checked_at, goneRow.channel_metadata_checked_at);
  assert.equal(kept.channels[0].channel_metadata_not_found_since, since);
  assert.equal('channel_metadata_not_found_since' in retained.retainSidebarUserState({channels: [goneRow], folders: [], labels: []}).channels[0], false);
  assert.equal('channel_metadata_checked_at' in kept.channels[1], false);
  // Wiring: list rows, Home cards and summary, the workshop picker and the channel page use these helpers.
  const app = read('../application/frontend/src/explorer/ExplorerApp.tsx');
  assert.match(app, /<strong>\{name\}<\/strong>\s*\{gone \? <>\s*<small className="explorer-channel-gone-id">\{channelId\}<\/small>/);
  assert.match(app, /title=\{gone \? goneNotice : name\}/);
  assert.match(app, /<strong>\{channelDisplayName\(channel\)\}<\/strong>/);
  assert.match(app, /goneChannelSummaryText\(gone\)/);
  assert.match(app, /<button type="button" onClick=\{onShowGone\}>Show \{gone\.count === 1 \? 'it' : 'them'\}<\/button>/);
  assert.match(app, /if \(onlyGone && !channelGoneFromYouTube\(channel\)\) return false;/);
  assert.match(app, /name: channelChoiceName\(c\)/);
  assert.doesNotMatch(app, /channel\.title \|\| channel\.channel_id|channel\.title \|\| channelId/);
  const header = read('../application/frontend/src/explorer/ChannelHeader.tsx');
  assert.match(header, /const untitled = gone \? CHANNEL_GONE_LABEL : channelId;/);
  assert.match(header, /\{goneNotice \? \(\s*<p className="explorer-v2-unlisted-note explorer-channel-gone-note" role="note">/);
}
console.log('Channels YouTube no longer returns: order, names, notices and Home summary: passed');
