// The video card's numbers, dates and growth, written the way the YouTube site writes them
// (application/frontend/src/explorer/videoCardFormat.ts). Offline, no browser, no dependencies.
import assert from 'node:assert/strict';
import * as card from '../application/frontend/src/explorer/videoCardFormat.ts';

const MINUS = '−';

// 1. Counts: whole below 1,000, then K, M and B; one decimal below 10 of a unit, cut, never ".0".
{
  const cases = [
    [0, '0'], [312, '312'], [999, '999'], [1000, '1K'], [1050, '1K'], [1900, '1.9K'], [9412, '9.4K'], [9999, '9.9K'],
    [12000, '12K'], [12400, '12K'], [124300, '124K'], [999950, '999K'], [1240000, '1.2M'], [12000000, '12M'],
    [2000000, '2M'], [2400000000, '2.4B'], [999999999, '999M'],
  ];
  for (const [value, text] of cases) {
    assert.equal(card.cardCount(value), text, `${value} as a number`);
    assert.equal(card.cardCount(String(value)), text, `${value} as the row's decimal string`);
  }
  assert.equal(card.cardCount('18446744073709551615'), '18446744073B', 'uint64 stays exact');
  assert.equal(card.cardCount('0012'), '12');
  for (const value of [null, undefined, '', 'abc', 1.5, '1.5', Number.NaN, 2 ** 60, true]) {
    assert.equal(card.cardCount(value), null, `${String(value)} has no count`);
  }
  for (const [, text] of cases) assert.doesNotMatch(text, /\.0[KMB]?$/, 'no ".0"');
  assert.equal(card.exactCount(1240000), '1,240,000');
  assert.equal(card.exactCount('312'), '312');
  assert.equal(card.exactCount(null), null);
  assert.equal(card.cardCountWords(1240000), '1.2 million');
  assert.equal(card.cardCountWords(999950), '999 thousand');
  assert.equal(card.cardCountWords(2400000000), '2.4 billion');
  assert.equal(card.cardCountWords(312), '312');
  assert.equal(card.isSingleCount('1'), true);
  assert.equal(card.isSingleCount(11), false);
  console.log('Card counts: 312 · 1.9K · 9.4K · 12K · 124K · 1.2M · 12M · 2.4B; 999,950 → 999K; no ".0"; missing is null.');
}

// 2. Dates: relative and short, from "now"; weeks from 14 days, months from 30, years from 365.
{
  const now = Date.parse('2026-11-26T12:00:00Z');
  const ago = (days, hours = 0, minutes = 0) => new Date(now - ((days * 24 + hours) * 60 + minutes) * 60_000).toISOString();
  const cases = [
    [ago(0, 0, 0), 'just now'], [ago(0, 0, 42), '42min ago'], [ago(0, 5), '5h ago'], [ago(0, 23, 59), '23h ago'],
    [ago(1), '1d ago'], [ago(13), '13d ago'], [ago(14), '2w ago'], [ago(21), '3w ago'], [ago(29), '4w ago'],
    [ago(30), '1mo ago'], [ago(150), '5mo ago'], [ago(364), '12mo ago'], [ago(365), '1y ago'], [ago(4 * 365), '4y ago'],
  ];
  for (const [value, text] of cases) assert.equal(card.cardAge(value, now), text, value);
  assert.equal(card.cardAge(new Date(now + 30_000).toISOString(), now), 'just now', 'a clock a few seconds ahead');
  assert.equal(card.cardAge(new Date(now + 2 * 86_400_000).toISOString(), now), null, 'a future instant shows its date instead');
  assert.equal(card.cardAge('not a date', now), null);
  assert.equal(card.cardAge(undefined, now), null);
  assert.match(card.cardDateWords('2026-11-23T12:00:00Z'), /^November 2[234], 2026$/, 'the spoken date (viewer zone)');
  console.log('Card dates: 1d · 13d · 2w at 14 days · 1mo at 30 days · 5mo · 1y at 365 days · 4y; future and invalid fall back.');
}

// 3. Growth: always a sign and a period; tone by sign only; statuses without a number say so.
{
  const growth = (delta, seconds, status = 'ready') => ({
    status, delta_views: delta, tracked_days: null, observed_start_at: 'a', observed_end_at: 'b',
    actual_interval_seconds: seconds, boundary_distance_seconds: null, reason: null,
  });
  const full = 28 * 86400 + 3600;
  assert.deepEqual(card.cardGrowth(growth('48213', full)),
    { text: '+48K · 28d', tone: 'up', title: '+48,213 views over ~28 days', words: 'up 48 thousand over ~28 days' });
  const down = card.cardGrowth(growth('-1234', full));
  assert.equal(down.text, `${MINUS}1.2K · 28d`, 'a real minus sign');
  assert.equal(down.tone, 'down');
  assert.equal(down.title, `${MINUS}1,234 views over ~28 days`);
  const zero = card.cardGrowth(growth('0', full));
  assert.deepEqual(zero, { text: '0 · 28d', tone: 'flat', title: '0 views over ~28 days' }, 'zero has no sign and no spoken change');
  assert.equal(card.cardGrowth(growth('3100', 6 * 86400, 'partial')).text, '+3.1K · 6d', 'a shorter history names its own days');
  assert.equal(card.cardGrowth(growth('3100', 6 * 86400, 'partial')).tone, 'up', 'a partial window still reads as a rise');
  assert.equal(card.cardGrowth(growth('2801', 27.5 * 86400, 'partial')).text, '+2.8K · 27d', 'a partial window never reads 28d');
  assert.equal(card.cardGrowth(growth('2801', 27.5 * 86400, 'partial')).title, '+2,801 views over ~27.5 days');
  assert.equal(card.cardGrowth(growth('5', 3600, 'partial')).text, '+5 · <1d');
  assert.equal(card.cardGrowth(growth('1', 9 * 86400, 'partial')).title, '+1 view over 9 days');
  assert.equal(card.cardGrowth(growth(1200, full)).text, '+1.2K · 28d', 'a number delta');
  assert.deepEqual(card.cardGrowth(null), { text: 'Growth unavailable', tone: 'flat', title: 'Growth unavailable' });
  assert.equal(card.cardGrowth(growth(null, null, 'unavailable')).text, 'Growth unavailable');
  assert.equal(card.cardGrowth(growth(null, null, 'waiting_next_snapshot')).text, 'Waiting for next snapshot');
  assert.deepEqual(card.cardGrowth({ ...growth(null, 600, 'started_today'), time_zone: 'Europe/Berlin' }),
    { text: 'Tracking started today', tone: 'flat', title: 'Tracking started today (Europe/Berlin)' });
  assert.equal(card.cardGrowth(growth('12', null)).text, 'Growth unavailable', 'no period, no number');
  assert.equal(card.cardGrowth(growth('1.5', full)).text, 'Growth unavailable', 'not a whole count');
  console.log('Card growth: +48K · 28d · −1.2K · 28d · 0 · 28d · +3.1K · 6d; partial windows name their days; statuses.');
}

// 4. Duration and the spoken label.
{
  assert.equal(card.cardDuration(28), '0:28');
  assert.equal(card.cardDuration(1122), '18:42');
  assert.equal(card.cardDuration(3730), '1:02:10');
  assert.equal(card.cardDurationSeconds(1122), 1122);
  assert.equal(card.cardDurationSeconds('60'), 60);
  assert.equal(card.cardDurationSeconds('PT1H2M10S'), 3730);
  assert.equal(card.cardDurationSeconds('PT1M'), 60);
  for (const value of [null, '', 'PT', 'P1D', -1, 1.5]) assert.equal(card.cardDurationSeconds(value), null, String(value));
  assert.equal(card.cardDurationWords(3730), '1 hour 2 minutes 10 seconds');
  assert.equal(card.cardDurationWords(60), '1 minute');
  assert.equal(card.videoCardLabel({ type: 'long', title: 'Sourdough', views: '1240000', growthWords: 'up 48 thousand over ~28 days',
    durationSeconds: 1122, published: 'November 23, 2026' }),
  'Video: Sourdough. 1.2 million views, up 48 thousand over ~28 days. 18 minutes 42 seconds. Published November 23, 2026.');
  assert.equal(card.videoCardLabel({ type: 'short', title: 'Crust', views: null, viewsState: 'needs refresh', durationSeconds: null }),
    'Short: Crust. Views: needs refresh.');
  assert.equal(card.videoCardLabel({ title: 'One', views: 1, durationSeconds: 0 }), 'Video: One. 1 view.');
  assert.equal(card.videoCardLabel({ type: 'stream', title: 'Live', views: undefined, durationSeconds: 3600 }),
    'Stream: Live. Views: not reported. 1 hour.');
  console.log('Card duration and label: 0:28 · 18:42 · 1:02:10; the spoken summary names missing views by state.');
}
