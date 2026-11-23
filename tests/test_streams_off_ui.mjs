// The interface's words and readers for a video format turned
// off in config.ini. Offline: no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as off from '../application/frontend/src/explorer/formatsOff.ts';
import { libraryErrorState } from '../application/frontend/src/explorer/libraryData/errors.ts';
import { parseExplorerHomeSummary } from '../application/frontend/src/explorer/contracts.ts';

// The stub: what is off and where to turn it on.
assert.equal(off.formatTurnedOffTitle('stream'), 'Streams are turned off.');
assert.match(off.formatTurnedOffHint('stream'), /add stream to formats under \[collection\] in config\/config\.ini/);
assert.equal(off.formatTurnedOffTitle('short'), 'Shorts are turned off.');

// Capabilities: absent key means nothing is off; anything malformed is refused.
assert.deepEqual(off.turnedOffTypes(undefined), []);
assert.deepEqual(off.turnedOffTypes(['stream']), ['stream']);
for (const bad of [['video'], ['stream', 'stream'], 'stream', ['long', 'short', 'stream'], [1]]) {
  assert.equal(off.turnedOffTypes(bad), null);
}

// One wording of the refusal: researchQuery.ts keeps its own copy (it imports no new module).
const textOf = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
  .match(/FORMAT_TURNED_OFF_TEXT = ('[^']*'\s*\+\s*'[^']*')/)[1];
assert.equal(textOf('../application/frontend/src/explorer/researchQuery.ts'),
  textOf('../application/frontend/src/explorer/formatsOff.ts'));

// Every Library surface names the refusal in fixed words, never an HTTP status.
for (const surface of ['videos_first', 'videos_more', 'select_all', 'facets', 'inspector', 'overview']) {
  const state = libraryErrorState({ code: 'format_turned_off' }, surface);
  assert.equal(state.message, off.FORMAT_TURNED_OFF_TEXT, surface);
}

// Home: a turned-off format is counted as 0 and named; anything else is a contract error.
const home = (saved) => ({
  contract_version: 1, last_successful_sync_at: null, last_sync_without_errors_at: null,
  last_data_change_at: null, saved_videos: saved,
  source: { kind: 'stored_typed_video_inventory', format_source: 'video_inventory.video_type', scope: 'visible_channels',
    youtube_requests: false, database_writes: false },
});
const parsed = parseExplorerHomeSummary(home({ total: 5, long: 3, short: 2, stream: 0, turned_off: ['stream'] }));
assert.deepEqual(parsed.saved_videos.turned_off, ['stream']);
parseExplorerHomeSummary(home({ total: 6, long: 3, short: 2, stream: 1 }));
for (const saved of [{ total: 6, long: 3, short: 2, stream: 1, turned_off: ['stream'] },
  { total: 5, long: 3, short: 2, stream: 0, turned_off: ['video'] },
  { total: 5, long: 3, short: 2, stream: 0, turned_off: 'stream' }]) {
  assert.throws(() => parseExplorerHomeSummary(home(saved)));
}

// The channel page swaps the turned-off tab's sections for the stub and sends them no read.
const workspace = readFileSync(new URL('../application/frontend/src/explorer/ExplorerWorkspace.tsx', import.meta.url), 'utf8');
assert.match(workspace, /const overviewVisible = activeSection === 'overview' && !formatOff;/);
assert.match(workspace, /const videosVisible = activeSection === 'videos' && !formatOff;/);
assert.match(workspace, /formatTurnedOffTitle\(/);

console.log('streams off ui: ok');
