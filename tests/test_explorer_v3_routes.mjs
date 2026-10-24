import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as routes from '../application/frontend/src/explorerRoutes.ts';
import * as query from '../application/frontend/src/explorer/catalogueQuery.ts';
import { INITIAL_EXPLORER_FILTERS } from '../application/frontend/src/explorer/filters.ts';
import * as sourceLifetime from '../application/frontend/src/sourceLifetime.ts';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
function compiled(path, imports) {
  const { outputText } = ts.transpileModule(read(path), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}});
  const exports = {};
  new Function('require', 'exports', outputText)(name => {assert.ok(name in imports, name);return imports[name];}, exports);
  return exports;
}
const work = compiled('../application/frontend/src/explorer/savedWork.ts', {});
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
  const saved = compiled('../application/frontend/src/explorer/savedViews.ts', {'./api':{}, '../explorerRoutes':routes, './catalogueQuery':query, './savedWork':work});
  const collections = compiled('../application/frontend/src/explorer/savedCollections.ts', {'./api':{}, './contracts':{}, './savedWork':work, '../explorerRoutes':routes});
  assert.equal(routes.EXPLORER_V3_ROOT, expected);
  assert.equal(routes.explorerChannelPath(channel), `${expected}/channels/${channel}`);
  assert.equal(routes.parseExplorerChannelPath(`${expected}/channels/${channel}`),channel);
  for (const retired of ['/app-explorer-v2', '/app-explorer', '/app-next']) {
    assert.equal(routes.parseExplorerChannelPath(`${retired}/channels/${channel}`),undefined, retired);
    assert.deepEqual(saved.readExplorerDestination(`${retired}/health`),{kind:'home'}, retired);
  }
  for(const page of ['health','telemetry']) {
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
// A membership command that changed nothing keeps the revision it expected (T-08).
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

// A collection member may stay readable for the whole 29-day source window (R07a G9).
{
  globalThis.document = {documentElement: {dataset: {}}};
  const collections = compiled('../application/frontend/src/explorer/savedCollections.ts', {'./api':{}, './contracts':{parseStoredExplorerVideo: video => video}, './savedWork':work, '../explorerRoutes':routes});
  delete globalThis.document;
  const checked = '2026-10-08T10:00:00Z', video = 'aaaaaaaaaaa';
  const page = lifetime => ({contract_version: 1,
    collection: {id, kind: 'collection', name: 'Mix', revision: 1, member_count: 1, created_at: checked, updated_at: checked},
    members: [{video_id: video, added_at: checked, status: 'available', reason: null, channel_id: channel,
      video: {video_id: video, title: 'T', thumbnail_url: null, published_at: checked, checked_at: checked, duration: 'PT1M',
        views: 1, like_count: null, comment_count: null, category_id: null, category_name: null, duration_seconds: 60},
      source_expires_at: new Date(Date.parse(checked) + lifetime).toISOString()}],
    next_cursor: null, source_read_at: checked});
  assert.equal(collections.SOURCE_RETENTION_MS, 29 * 86400000);
  assert.equal(collections.parseMembersPage(page(collections.SOURCE_RETENTION_MS), id, undefined, 0).members.length, 1);
  assert.throws(() => collections.parseMembersPage(page(collections.SOURCE_RETENTION_MS + 1000), id, undefined, 0), /counter lifetime/);
}

// The chosen zone travels inside catalogue_filters; UTC travels as absent.
{
  const conditions = {version: 1, filters: {...INITIAL_EXPLORER_FILTERS}, facets: structuredClone(query.DEFAULT_CATALOGUE_FACETS)};
  const local = JSON.parse(query.catalogueRequestQuery(conditions, 0, 'Europe/Kyiv').catalogue_filters);
  assert.equal(local.time_zone, 'Europe/Kyiv');
  assert.equal(JSON.parse(query.catalogueRequestQuery(conditions, 0).catalogue_filters).time_zone, undefined);
  assert.equal(JSON.stringify(query.normalizeCatalogueRequestFilters(local)), query.catalogueRequestQuery(conditions, 0, 'Europe/Kyiv').catalogue_filters);
  for (const zone of ['UTC', 'Nowhere/../x', 5]) assert.throws(() => query.normalizeCatalogueRequestFilters({...local, time_zone: zone}));
}
console.log('Explorer V3 roots, Saved and deep links: passed');

// The recent view orders the whole library, not separate folder sublists.
{
  const sidebar = compiled('../application/frontend/src/explorer/sidebar.ts', {
    './imageHosts': {safeImageUrl: value => value},
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
}
