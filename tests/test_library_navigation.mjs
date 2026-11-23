// Library addresses: `videos=` keeps its conditions, Long writes no `type`, and the channel
// shell mounts the same Library sections for every format. Offline: no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as query from '../application/frontend/src/explorer/catalogueQuery.ts';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const channel = 'UC' + 'a'.repeat(22);
const base = `https://example.test/app-explorer-v3/channels/${channel}`;
const conditions = query.normalizeCatalogueConditions({ version: 1,
  filters: { ...query.createInitialCatalogueConditions().filters, q: 'guide', min_views: '10', sort_by: 'views', sort_order: 'desc' },
  facets: { version: 1, view_buckets: [], duration_buckets: ['under_5m'], published_years: [2025], published_older_than: null, cohort: null } });
const videos = encodeURIComponent(query.serializeCatalogueConditions(conditions));


// 1. A saved address keeps its `videos=` conditions: read and written back unchanged.
{
  const saved = new URL(`${base}?section=videos&overviewPeriod=max&videos=${videos}`);
  const opened = query.readCatalogueUrl(saved);
  assert.equal(query.serializeCatalogueConditions(opened.conditions), query.serializeCatalogueConditions(conditions));
  const written = query.writeCatalogueUrl(new URL(base), { section: 'videos', overviewPeriod: 'max', conditions: opened.conditions });
  assert.equal(written.searchParams.get('videos'), saved.searchParams.get('videos'));
  assert.deepEqual(query.readCatalogueUrl(written), opened);
  console.log('Saved address: videos= conditions read and written back unchanged.');
}

// 2. Navigation writes: Long never writes `type` (version 0.1.0 addresses stay as they were); Shorts and Streams do.
{
  const state = { section: 'videos', overviewPeriod: 'max', conditions };
  const long = query.writeCatalogueUrl(new URL(base), state);
  assert.equal(long.searchParams.has('type'), false);
  assert.equal(query.writeCatalogueUrl(new URL(base), { ...state, type: 'long' }).href, long.href);
  assert.equal(query.writeCatalogueUrl(new URL(base), { ...state, type: 'short' }).searchParams.get('type'), 'short');
  assert.equal(query.writeCatalogueUrl(new URL(`${base}?type=stream`), { ...state, type: 'long' }).searchParams.has('type'), false);
  // A Shorts or Streams duration bucket opens in its own tab: the address codec takes every format's keys.
  for (const [type, key] of [['short', 'under_15s'], ['stream', '4h_plus']]) {
    const typed = query.applyCataloguePreset(query.createInitialCatalogueConditions(), 'duration', key);
    const url = query.writeCatalogueUrl(new URL(base), { ...state, type, conditions: typed });
    assert.deepEqual(query.readCatalogueUrl(url).conditions.facets.duration_buckets, [key]);
    assert.equal(query.readCatalogueUrl(url).type, type);
  }
  console.log('Navigation: Long addresses without type, Shorts/Streams with type, each format\'s duration buckets accepted.');
}

// 3. The shell: one Library for every format.
{
  const shell = read('../application/frontend/src/explorer/ExplorerWorkspace.tsx');
  assert.equal((shell.match(/<LibraryOverviewSection /g) ?? []).length, 1, 'one Overview section, mounted for every format');
  assert.equal((shell.match(/<LibraryVideosSection /g) ?? []).length, 1, 'one Videos section, mounted for every format');
  // A format turned off in config.ini shows its stub instead of the sections.
  assert.match(shell, /const videosVisible = activeSection === 'videos' && !formatOff;/);
  assert.match(shell, /const overviewVisible = activeSection === 'overview' && !formatOff;/);
  // Every media tab controls a panel that exists: the active one with both sections, the others empty and hidden.
  assert.match(shell, /id=\{`explorer-media-panel-\$\{activeMedia\}`\}\s*aria-labelledby=\{`explorer-media-\$\{activeMedia\}`\}/);
  assert.match(shell, /mediaTabs\.filter\(\(tab\) => tab\.id !== activeMedia\)\.map\(\(tab\) => \(\s*<section\s*key=\{tab\.id\}\s*className="explorer-v2-media-panel"\s*role="tabpanel"\s*id=\{`explorer-media-panel-\$\{tab\.id\}`\}\s*aria-labelledby=\{`explorer-media-\$\{tab\.id\}`\}\s*hidden\s*\/>/);
  assert.match(shell, /aria-controls=\{`explorer-media-panel-\$\{tab\.id\}`\}/);
  assert.match(shell, /aria-controls=\{`explorer-section-panel-\$\{tab\.id\}`\}/);
  for (const id of ['explorer-section-panel-overview', 'explorer-section-panel-videos']) assert.equal((shell.match(new RegExp(`id="${id}"`, 'g')) ?? []).length, 1, id);
  for (const tab of ["{ id: 'videos', type: 'long', label: 'Videos', detail: 'long-form' }", "{ id: 'shorts', type: 'short', label: 'Shorts', detail: 'short-form' }",
    "{ id: 'streams', type: 'stream', label: 'Streams', detail: 'live' }"]) assert.ok(shell.includes(tab), tab);
  // The /research address opens the research workshop.
  const app = read('../application/frontend/src/explorer/ExplorerApp.tsx');
  assert.match(app, /destination\.kind === 'research' \? \(\s*pageBoundary\('Research workshop', <Suspense fallback=\{<LoadingState label="Loading research workshop…" \/>\}><ResearchWorkshopPage/);
  assert.match(read('../application/frontend/src/explorer/savedViews.ts'), /=== `\$\{EXPLORER_V3_ROOT\}\/research`\) return \{ kind: 'research' \}/);
  console.log('Shell: one Overview and one Videos section for Long, Shorts and Streams; valid tab panels; /research opens the workshop.');
}
