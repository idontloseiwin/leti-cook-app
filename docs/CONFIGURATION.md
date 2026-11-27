# Configuration

Edit these files inside your installation folder:

| File | Controls | When changes apply |
| --- | --- | --- |
| `config/config.ini` | Collection on/off, video formats, automatic interval and work limits | Next worker run; request concurrency needs a restart |
| `config/collection-schedules.ini` | Refresh intervals for each video type and its playlists | After a restart, when new refresh times are assigned |

Commands below use `/opt/leti-cook`; substitute your own folder. Keep generated
credentials and installation files unchanged.

Check your edits before using them:

```bash
sudo python3 /opt/leti-cook/manage.py check-settings
sudo python3 /opt/leti-cook/manage.py restart
```

The restart command validates both files before stopping the application. An
invalid configuration prevents new collection work until corrected. A running
pass keeps the limits it started with.

## Collection settings

In `config/config.ini`, `[collection]` contains:

| Setting | Default | Accepted values | Purpose |
| --- | --- | --- | --- |
| `automatic_collection` | `true` | `true`, `false` | Pause collection with `false`. Cleanup continues; collection jobs wait. |
| `interval_minutes` | `10` | 6, 10, 15, 20, 30 | Interval between automatic pass start times, in minutes. Existing configured values are preserved during updates. |
| `handoff_grace_seconds` | `120` | 10–600 | Seconds allowed to finish current work when the next pass is due. |
| `manual_stats_quota_units` | `200` | 1–1,000,000 | Maximum estimated quota cost accepted for one manual statistics job. |
| `youtube_request_concurrency` | `8` | 1–50 | Maximum simultaneous YouTube requests across the installation. Requires restart. |
| `websub_callback_url` | empty | empty or an `https://` address | Optional YouTube notifications (WebSub). Empty keeps them off. See [YouTube notifications](INSTALLATION.md#youtube-notifications-websub). Requires restart. |
| `formats` | `long, short` | `long, short` or `long, short, stream` | Video formats that are collected and shown. Streams are off by default. See [Video formats](#video-formats). |

More concurrency does not increase the daily quota or reduce request costs.

### Video formats

`formats` lists the video formats this installation collects and shows:
`long` (Long-form videos), `short` (Shorts) and `stream` (Streams). The
default is `formats = long, short`, so Streams are turned off. Without this
line the same default applies.

Write the names in lower case and separate them with commas. Spaces around a
name and the order of the names do not matter. Each name may appear once.
`long` and `short` are required: in this release only Streams can be turned
off. `check-settings` refuses anything else.

To turn Streams on, change the line, check the file and wait for the next
automatic pass; no restart is needed:

```ini
formats = long, short, stream
```

The next automatic pass collects Streams again. Library and the workshop show
them the next time a page loads; Home and Health summaries follow within about
20 seconds, the time they keep a reading. Removing `stream` turns Streams off
the same way. A pass
that is already running finishes with the formats it started with, so pages
can show the new setting a few minutes before collection follows it.

While a format is turned off:

- **Scheduled collection of its known videos is disabled.** Its playlists are
  not read, its videos' details and counters are not refreshed, its unavailable
  videos are not checked again, live and upcoming broadcasts are not checked, a
  channel's first video of that format is not looked for, and the work it would
  have needed is left out of pass limits and quota estimates.
  The following requests are possible while Streams are off:
  - The first load of a newly added channel reads all three playlists once.
    Streams found during this load stay hidden.
  - After playlist evidence expires, a retained video ID can have an unknown
    format. The [identity check](#unavailable-videos) may request its details,
    by default every 28 days (`identity_seconds`).
  - When discovery is active, it can check an unknown ID for membership in
    the Streams playlist after checking the enabled lists. A match identifies
    a disabled Stream. The check does not scan the full list or load counters.
- **Stored data is kept and hidden.** The format's tab says that it is turned
  off and where to turn it on. Home, Library and channel counts leave it out,
  and Health marks its queues as off. Research queries, saved views, canvas
  sources and agent ([MCP](MCP.md)) requests for it are refused with
  `format_turned_off` instead of returning an empty result. Saved views,
  collections, video-type decisions and other work for it remain.
- **The [29-day window](DATA.md#the-29-day-window) still applies.** The
  format's YouTube data is no longer refreshed, so it expires and is removed
  on its usual schedule.

When you turn a format on again, collection resumes from the stored state:
due playlists and videos are collected in the following passes, and data that
expired meanwhile is requested again. History that was not collected while
the format was off cannot be recreated.

### Limits per pass

In `[limits]`, the two main limits are independent and both default to 20,000:

```ini
[limits]
inventory_page_budget = 20000
stats_limit = 20000
```

A **playlist page** lists up to 50 video entries. A **video check** refreshes a
known video's metadata or counters. These are ceilings per pass across all
three types, not targets or limits on your library's size. Failed checks and
empty page responses still count.

| Setting | Default | Range | Purpose |
| --- | --- | --- | --- |
| `metadata_limit` | `1000` | 0–100,000 | Channel profiles refreshed per pass. |
| `inventory_page_budget` | `20000` | 0–100,000 | Playlist-page requests per pass. |
| `stats_limit` | `20000` | 1–100,000 | Video checks per pass for each quota bucket, including unsuccessful checks: up to this many with General (details and counters) and, separately, up to this many with Batch statistics (counters only). |
| `new_video_stats_limit` | `2500` | 0–100,000; no greater than `stats_limit` | Diagnostic sample size for newly found video IDs; does not limit collection. |
| `operator_chunk_size` | `50` | 1–250 | Videos in a manual statistics job chunk. |
| `inventory_pages_per_chunk` | `5` | 1–10 | Playlist pages in a job chunk. |
| `drain_max_chunks` | `128` | 1–512 | Job chunks allowed per job-worker run. |
| `drain_max_seconds` | `900` | 1–1200 | Seconds allowed per job-worker run. |

Zero disables the corresponding work or diagnostic sample; it never means
unlimited. Larger limits can use quota faster when work is waiting. Updates
preserve your chosen values. Leave the file's format marker unchanged.

## Video refresh intervals

`config/collection-schedules.ini` sets counters and metadata schedules separately
for Long-form, Shorts and Streams. Which formats are collected at all is set by
[`formats`](#video-formats) in `config/config.ini`; the intervals of a
turned-off format stay in this file and apply again when it is turned on.

| Video age | Counters: all types | Metadata: Long-form | Metadata: Shorts / finished Streams |
| --- | --- | --- | --- |
| Under 1 day | 1 hour | 1 day | 7 days |
| 1–7 days | 1 hour | 1 day | 7 days |
| 7–28 days | 1 day | 28 days | 7 days |
| 28–90 days | 1 day | 28 days | 7 days |
| 90 days – 1 year | 1 day | 28 days | 28 days |
| Over 1 year | 7 days | 28 days | 28 days |

Counters (views, likes, comments) are refreshed hourly in a video's first week,
daily up to a year and weekly after that. A daily snapshot keeps the exact
28-day growth available; a weekly one supports growth over 7 and 14 days.
Unlisted counters use 28 days; Long-form unlisted metadata also uses 28 days.
Shorts and Streams metadata currently has two age steps (90 days or less, and
older). Channel profiles and channel snapshots are refreshed hourly; that
interval is not yet part of this file.

A new installation writes these defaults into the file, and the file is read
exactly as written.
Finished Streams are aged from the end of the broadcast. Shorts and Streams
metadata checks are spread earlier than their deadline by up to six hours.

Use the comments and keys in the [schedule file](../config/collection-schedules.ini).
Intervals are whole seconds, up to 2,419,200 (28 days). Metadata jitter may be
zero and cannot exceed the smaller metadata interval for its type. These
intervals do not extend the [29-day data window](DATA.md#the-29-day-window).

Restart after editing. Existing refresh times stay in place; subsequent
scheduling uses your new intervals. A shorter interval does not immediately
reschedule the entire library.

A Long-form video or Short waiting for publication receives a metadata check
every 24 hours, or at the announced start if sooner. Each observation replaces
the planned date. A missing date or a start time that has passed keeps the
daily check. A fresh notification can bring one check forward.

During a live show the metadata check follows the format's counters interval
(hourly in the first week by default). A stored future start cannot postpone
an active Long-form or Shorts show. Actual dispatch depends on collector and
quota admission. These checks also run when Streams are off. For enabled
Streams the existing start and counters schedules apply, and collection of the
completed stream requires the actual end time.

### Unavailable videos

One YouTube response that leaves a video out is not enough to call it
unavailable. The first omission keeps the video's values and asks YouTube for
it again in the next collection pass (passes start every `interval_minutes`),
or in a later one when that pass's limits or quota are used up. Only a second
omission in a row starts an unavailable episode. A video that a statistics
response has just missed, or that is already in an episode, is marked at once.

Each type has a `[schedule.TYPE.unavailable]` section, where `TYPE` is `long`,
`short` or `stream`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `daily_seconds` | `86400` | Delay between early checks: one day. |
| `daily_window_seconds` | `604800` | Early checking period: seven days. |
| `weekly_seconds` | `604800` | Delay between later checks: seven days. |
| `stop_after_seconds` | `2505600` | Stop automatic checks after 29 days of this unavailable episode. |

Repeated missing responses do not restart that period. The stop deadline cannot
exceed 29 days. Under `[schedule.rechecks]`, `identity_seconds` controls checks
for retained IDs without current type evidence; it defaults to 28 days. Leave
`unavailable_long_max_seconds` unchanged; use the per-type sections above.

## Playlist intervals

`[schedule.long.inventory]`, `[schedule.short.inventory]` and
`[schedule.stream.inventory]` control ordinary playlist reads independently.
New installations use the defaults below; an update keeps the values in your
file.

| Setting | Long-form default | Shorts default | Streams default |
| --- | --- | --- | --- |
| `missing_playlist_seconds` | 1 day | 1 day | 1 day |
| `found_new_seconds` | 1 day | 1 day | 1 hour |
| `quiet_1_seconds` … `quiet_6_plus_seconds` | 1 day at every step | 1 day at every step | 2, 4, 8, 24, 24, 24 hours |
| `error_0_seconds` … `error_5_plus_seconds` | 1, 2, 4, 8, 16, 24 hours | 1, 1, 2, 4, 8, 24 hours | 1, 1, 2, 4, 8, 24 hours |
| `discovery_continuation_seconds` | 30 minutes | 0 | 0 |
| `full_coverage_max_age_days` | 7 days | 7 days | 21 days |

`quiet` delays grow after checks without new entries; `error` delays govern
retries after technical failures. The last step repeats. A missing public
playlist is not a transport failure. Reconciliation scans the full available
list again.

A missing playlist is one YouTube does not currently return, for example the
Shorts list of a channel that never posted a Short. Its normal retry follows
`missing_playlist_seconds`, independently of the full-coverage interval. A disabled
format is not checked; its configured interval only applies when collection for
that format is enabled. The time to find a channel's first video of a format is
not guaranteed.

Enter seconds in the file. Values must be between 1 and 2,419,200; continuation
also accepts 0, meaning no nominal delay. Quiet and error steps must not
decrease. Restart to apply changes; this does not abandon a scan or move an
already assigned check forward.

Full reconciliation is selected when a due source is claimed, using the start
of its last successfully completed full scan. Reducing its interval can turn
the next head check into a full scan even when that check's assigned time did
not change. A head, partial scan or single-video check does not renew full coverage.

The `[discovery]` section and `full_coverage_max_age_days` in each inventory
section are checked strictly. Do not change the format marker manually.

This version has no active discovery collector, and no setting or command turns
one on. Collection does its ordinary playlist work, and accepted WebSub
notifications only bring existing work forward: a new video's notification
makes the channel's playlists due, and a known video's notification makes that
video due. A missing playlist that already has a later check assigned is not
brought forward. An observed increase in a channel's video count makes its
playlist heads due, unless notifications already announced the whole increase.
In that case the first video of a format can wait for the ordinary
missing-playlist check, by default one day. A settings change does not move
dates that are already assigned.

`full_coverage_max_age_days` accepts 1–24,855 whole days. This is an integer
storage bound, not a recommended waiting period, and does not extend data
expiry. Use either this days key or `reconciliation_seconds` in a section, never
both. The file is read once at start, without environment overrides or a second
settings file.

The `[discovery]` section also holds the following settings. Only the discovery
collector uses them, so in this version they are checked but change nothing.

| Setting | Default (seconds) | Purpose |
| --- | --- | --- |
| `needs_review_seconds` | `604800` | Mark an unresolved discovery for review after seven days of effective waiting. |
| `review_retry_seconds` | `86400` | Minimum retry delay for unresolved evidence after the review threshold: one day. |
| `unresolved_pause_seconds` | `2592000` | Pause automatic discovery after 30 days of effective waiting, preserving its pending intent. |
| `technical_retry_first_seconds` | `120` | Initial delay after a technical failure in metadata or membership checking. |
| `technical_retry_max_seconds` | `21600` | Maximum base technical retry delay: six hours, before the review-age policy applies. |
| `activation_grace_seconds` | `172800` | Additional full-coverage age the discovery collector allows when it is first turned on: two days. |

The review delay must be at least `retry_sparse_seconds`, the pause threshold
must exceed `needs_review_seconds`, and the technical maximum must be at least
the technical first delay.

Request allowances are stored across restarts:

| Settings | Daily default | Window default | Applies to |
| --- | --- | --- | --- |
| `events_general_daily_limit`, `events_general_window_limit` | 2,500 | 100 | Additional General requests for discovery signals and retries. |
| `events_stats_daily_limit`, `events_stats_window_limit` | 500 | 50 | Additional Batch statistics requests for discovery loading. |
| `full_general_daily_limit`, `full_general_window_limit` | 1,000 | 25 | Every automatic full-scan page, including scheduled and continuing scans. |

Windows use `interval_minutes`; restarting the collector does not refill them.
The full-scan limits apply to every automatic full scan; the event limits are
used only by the discovery collector. Manual initial loading keeps its own
limits. These are local allowances, not Google's remaining quota.

WebSub subscription does not postpone the configured playlist checks.

## YouTube API quota

**Health** shows locally recorded use in the General Data API and Batch video
statistics buckets. Google Cloud Console is authoritative: other software using
the same project also spends its quota. Daily quota resets at midnight Pacific
time.

| Request | Recorded cost |
| --- | --- |
| Look up a channel or refresh up to 50 channel profiles | 1 General unit |
| Read a playlist page of up to 50 entries | 1 General unit |
| Fetch metadata and counters for up to 50 videos | 1 General unit |
| Refresh counters for up to 50 videos | 1 Batch statistics unit |
| Check a replacement API key | 1 General unit |
| Prepare the complete category reference | 1 General unit for the language list, plus 1 per returned language |

Failed and empty requests still cost quota. A missing required video counter
can require an additional Batch request. Local pass limits do not increase
YouTube's quota. Reduce collection limits or increase refresh intervals if
usage is too high. Collection waits when YouTube reports quota exhaustion;
Health and Home show this in yellow as waiting for quota, not as a failure,
and name the bucket when only one ran out. A sync that also recorded another
error shows as failed (see [Health and Home states](TROUBLESHOOTING.md#health-and-home-states)).

Each automatic pass refreshes video details and counters, then channel
profiles, then checks playlists for new videos. The General bucket serves
video details, channel profiles and playlist pages; the Batch statistics
bucket serves counters only. A problem stops only the work it affects:

| Problem | What stops | What continues | Until |
| --- | --- | --- | --- |
| General quota exhausted | Video details, channel profiles, playlist checks, and the availability and live-stream checks that ride on video details | Counters through Batch statistics, including counters of videos whose details now wait | Midnight Pacific |
| Batch statistics quota exhausted | Counter-only refreshes, and the extra counter request for a video whose details came without a view count | All General work; video details already carry counters | Midnight Pacific |
| Rate limit (HTTP 429) | The step it came from | The next step, after the pause YouTube asked for (at most 60 seconds) | The end of that step |
| Failed Batch statistics request (server error, timeout, unreadable answer) | Further counter requests in that pass's video refreshes | Channel profiles and playlist checks | The end of that pass |
| Database failure or an unconfirmed write | The whole pass | — | The next pass |

Once YouTube reports a bucket exhausted, collection remembers it until
midnight Pacific and sends that bucket no further requests. Requests already
on their way can still be refused, so a few refusals are recorded once, not in
every pass. A video whose details are due then waits for the reset, while its
counters keep their own schedule through Batch statistics. A counter refresh
that cannot use Batch statistics moves to the video's next details check, if
that comes before the reset. One-off jobs, such as loading a new channel, wait
for the reset. Health shows each exhausted bucket and when it
resumes. A pass that met any of these problems is recorded with its reason;
work that only waited for an exhausted bucket is not counted as failed
requests.

Every collector request to YouTube rechecks the stored quota control after
waiting for a request slot. A confirmed refusal is saved for its project, bucket
and Pacific day before the optional usage journal is finished. The same row
holds the local allowance counters; recording a refusal neither spends another
allowance nor resets those counters. A request sent under a local allowance
also rechecks that allowance after waiting: an expired window or a new Pacific
day requires a new allowance, and one allowance cannot send a second HTTP
attempt.
If control cannot be read, Health reports "Quota state is unavailable" and
requests wait for a successful read. This is separate from reported exhaustion
and from an incomplete usage journal. A failed refusal write keeps the process's
local stop and is retried; a crash before a confirmed write can lose that refusal.
These controls cover the collector process, including its one-off work, and do
not describe direct requests made by the web process. The standalone manual
worker uses the same stored stops; its work is not charged to the event or
full-scan allowances. Health uses the startup schedule snapshot to select the
corresponding observer; an unreadable snapshot remains unknown.

A [one-off API acquisition](DATA.md#one-off-api-acquisition) uses the same key
and quota and is recorded the same way. All acquisitions together may use at
most 500 General units and 100 Search queries calls per Pacific quota day, so
collection keeps the rest.

## The YouTube API key

```bash
sudo python3 /opt/leti-cook/manage.py set-key
```

Enter the new key at the hidden prompt. The app briefly stops while saving it.
A rejected key is not saved; the previous one remains. If YouTube reports quota
exhaustion, the key is saved without a successful check and collection waits
for reset. Before setup finishes, replacing the key restarts category preparation.

If YouTube later refuses your stored key, collection stops and Home and Health
show a notice. Fix its Google Cloud settings or replace it with this command.

## Library and research workshop

Library and the research workshop are always on; there is nothing to enable.
Owner access and the usual data-read checks still apply, and their summaries
and other calculations keep their own server permissions. Do not edit
generated environment files manually.

For a local agent, issue a separate research read-only credential using the
[MCP setup](MCP.md). Agent access uses the same data-read and calculation checks;
it cannot change these settings.

The installer turns on the base analytics: Overview, filter counts, the
Inspector's details, history and channel comparison, 28-day growth, observed
change and channel history. The Inspector's launch comparison is not enabled on
a standard installation. [Library behavior](DATA.md#channel-library) explains
the formats.

## Management commands

Run `sudo python3 /opt/leti-cook/manage.py COMMAND` in an interactive terminal.
For an interactive SSH command, use `ssh -t`.

| Command | Use |
| --- | --- |
| `setup-code` | New code for unfinished first-run access setup; not a password reset. |
| `set-key` | Replace the YouTube API key. |
| `check-settings` | Validate both collection files. |
| `restart` | Validate settings and restart the app; leave the database running. |
| `update --check`, `update` | Check for or apply an [update](INSTALLATION.md#updating-an-installation). |
| `update --build-local` | Apply an update with images built on this server from the verified package; see [updating](INSTALLATION.md#updating-an-installation). |
| `restore-update-copy` | Restore an available, unexpired emergency update copy after confirmation. |
| `purge-update-copy` | Remove an expired update copy; keep an unexpired one. |
| `clear-sign-in-limit` | Restart the shared wait after repeated wrong passwords; it does not stop whoever sends them. |
| `check-secrets` | Report where a YouTube API key may be stored in the database, without showing it. |
| `redact-secrets` | Replace API keys found in stored error texts with a code, after confirmation. |
| `agent-key-create`, `agent-key-list`, `agent-key-revoke` | Manage [agent keys](MCP.md#create-a-key). |
| `uninstall` | Permanently remove this installation after confirmation. |

Most commands first recover an interrupted update or uninstall. Follow
[Troubleshooting](TROUBLESHOOTING.md) if recovery cannot finish.

### Checking for stored API keys

Use these commands after an API key was exposed, together with revoking it in
Google Cloud and running `set-key`, or when you suspect that a key was stored
in the database.

`check-secrets` searches every text and JSON column of the application's
database for values shaped like a YouTube API key (`AIza` followed by 35
letters, digits, `-` or `_`) or an address with a `key=` parameter. It only
reads and does not stop collection. It prints each matching table and column
with up to 20 row keys, never the values. It also runs without a terminal, for
example from a script, and exits with 0 when nothing is found, 3 when it finds
possible keys and 4 when the check did not finish. An unfinished check, for
example after its 30-minute limit, is never reported as clean.

`redact-secrets` covers the five error columns that can hold YouTube's error
text, which may include the key from a request address. It shows how many
values match there and asks you to type
`REDACT` before replacing them with the code `redacted_secret` in one
transaction. It then checks the whole database like `check-secrets`. It
changes nothing else. If matches remain in other columns, do not delete those
rows by hand: they may hold your work.

## Telemetry

The pipeline event log is off in this version. `TELEMETRY_ENABLED` does not
turn it on, and no `pipeline-telemetry.jsonl` is written or read. Health and
collection do not depend on diagnostic recording.

**Telemetry → Discovery support journal** is a separate, small passive journal,
enabled by default for installations with a local account-state directory. It
sits at the bottom of the Telemetry page, closed; it is read only while it is
open. It
records fixed boundary facts, waits, unknown outcomes and recovery; it
does not record source payloads, titles, raw video/channel IDs, URLs or exception
messages. A local 32-byte key produces stable private identity references across
the backend and collector children. The key and journal have owner-only file
permissions. Do not share `discovery-support.key` with exported observations.

The journal has fixed technical bounds:

- One rotating 4 MiB file, with no retained archive files. Rotation briefly
  needs about 2 MiB of temporary space plus a bounded 32 KiB append batch.
  A hard interruption can leave a temporary file. Initialization and the next
  rotation remove this journal's abandoned private temporary files under the
  shared lock; unrelated files, links and another user's files are left alone.
  A cleanup failure disables recording or drops that write, without creating
  another rotation file or changing collection.
- At most 256 queued observations per process, 16 per write batch and 2 KiB
  per observation. Producers do no filesystem I/O; a bounded writer handles it.
- The owner-only panel reads at most 200 recent observations, on opening or
  **Refresh journal**. There is no support-journal event stream or background
  polling from a closed panel.
- Catalogue pages of at most 50 rows can carry an opaque read receipt.
  At most 128 receipts are kept in backend memory, expiring with the source
  result. Browser reports are capped at 2 KiB and 50 row indices, and duplicate
  received/rendered reports for a row are ignored.

The API records result preparation separately from completed response-body
delivery. Browser `received` and `rendered` entries are untrusted client reports;
they do not prove an actual paint or change freshness, visibility or exposure.
Writer confirmations describe the existing owner's operation; `writer_receipt`
specifically identifies an event settled against its exact writer attempt.
Inventory observations use one private source reference per channel and format.
A confirmed inventory claim is the page runner's acknowledgement, separate from
a confirmed page write. A lost progress acknowledgement remains unknown even
when the page write is already confirmed; lease and page tokens are never logged.
Projection observations confirm consumed refresh markers, not row visibility.
Missing, rotated or dropped observations never become success. The panel shows
reported loss, but a crashed process can lose its final loss report too.

For support, `LIC_SUPPORT_JOURNAL_ENABLED=false` in `backend.env` disables this
journal's recording and reads after backend restart. This optional flag is
preserved by the trusted updater; it is not an owner-settings key. Existing
journal/key files remain. A key, permissions or disk failure makes diagnostics
unavailable without changing collection, API results or their owners. Separate
quota and WebSub journals retain their existing purposes.

## Built-in limits

- Up to 10,000 channels per workspace.
- Up to 1,000 folders, and 1,000 custom labels besides the preset Monetized
  label.
- Up to 100,000 label assignments; one label on one channel counts once.
- Up to 1,000 items in Saved: views, queries, studies, canvases and collections
  together. Deleted items do not count.
- Up to 100,000 videos in all collections together.
- Video-type review: up to 1,000 videos with a decision or review history, and
  10,000 history entries.
- Manual additions: 20 per minute and 300 per 24 hours. Restarting does not reset
  these limits. Reconnecting restored channels does not use these counters.
- Manual channel statistics refresh: six hours after the last completed refresh.
- A user-work backup can hold up to 16 MiB. The database checks a 15 MiB
  budget for channels, folders, labels, saved selections and manual decisions.
  The remaining space covers up to 100 alert definitions, browser preferences,
  field choices and restore fingerprints.
- Sign-in: five wrong passwords in a row, then a wait before each further try
  that doubles from 1 second up to 5 minutes. A browser that signed in during
  the last 30 days has its own count; see [Security](../SECURITY.md#access).
  Setup codes have no limit.
- Library's browsing list: up to 50,000 videos, kept for 10 minutes.
- One-off API acquisition: one at a time, with the
  [limits per acquisition and per day](DATA.md#one-off-api-acquisition).
- A database transaction left open without activity is ended after 10 minutes,
  which releases its locks. This includes a `psql` session you open by hand in
  the database container. Updates apply this to existing installations.

The workspace limits match what a user-work backup holds, so your work can
always be backed up. A change that would pass one of them is refused; nothing
already saved is deleted. Channels restored from a backup count toward them
before they are reconnected.
