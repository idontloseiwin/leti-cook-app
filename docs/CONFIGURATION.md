# Configuration

Edit these files inside your installation folder:

| File | Controls | When changes apply |
| --- | --- | --- |
| `config/config.ini` | Collection on/off, automatic interval and work limits | Next worker run; request concurrency needs a restart |
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
| `interval_minutes` | `30` | 6, 10, 15, 20, 30 | Interval between automatic pass start times, in minutes. |
| `handoff_grace_seconds` | `120` | 10–600 | Seconds allowed to finish current work when the next pass is due. |
| `manual_stats_quota_units` | `200` | 1–1,000,000 | Maximum estimated quota cost accepted for one manual statistics job. |
| `youtube_request_concurrency` | `8` | 1–50 | Maximum simultaneous YouTube requests across the installation. Requires restart. |

More concurrency does not increase the daily quota or reduce request costs.

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
| `stats_limit` | `20000` | 1–100,000 | Video checks per pass, including unsuccessful checks. |
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
for Long-form, Shorts and Streams. All three types are collected; there is no
per-type off switch.

| Video age | Counters: all types | Metadata: Long-form | Metadata: Shorts / finished Streams |
| --- | --- | --- | --- |
| Under 1 day | 1 hour | 6 hours | 7 days |
| 1–7 days | 2 hours | 6 hours | 7 days |
| 7–28 days | 6 hours | 1 day | 7 days |
| 28–90 days | 12 hours | 7 days | 7 days |
| 90 days – 1 year | 1 day | 7 days | 28 days |
| Over 1 year | 7 days | 28 days | 28 days |

Unlisted counters use 28 days; Long-form unlisted metadata also uses 28 days.
Finished Streams are aged from the end of the broadcast. Shorts and Streams
metadata checks are spread earlier than their deadline by up to six hours.

Use the comments and keys in the [schedule file](../config/collection-schedules.ini).
Intervals are whole seconds, up to 2,419,200 (28 days). Metadata jitter may be
zero and cannot exceed the smaller metadata interval for its type. These
intervals do not extend the [29-day data window](DATA.md#the-29-day-window).

Restart after editing. Existing refresh times stay in place; subsequent
scheduling uses your new intervals. A shorter interval does not immediately
reschedule the entire library. Waiting publications are checked again; a
scheduled start time alone does not prove that a video has started or finished.

### Unavailable videos

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
`[schedule.stream.inventory]` control discovery independently:

| Setting | Long-form default | Shorts / Streams default |
| --- | --- | --- |
| `missing_playlist_seconds` | 1 day | 21 days |
| `found_new_seconds` | 1 hour | 1 hour |
| `quiet_1_seconds` … `quiet_6_plus_seconds` | 2, 4, 8, 8, 8, 24 hours | 2, 4, 8, 24, 24, 24 hours |
| `error_0_seconds` … `error_5_plus_seconds` | 1, 2, 4, 8, 16, 24 hours | 1, 1, 2, 4, 8, 24 hours |
| `discovery_continuation_seconds` | 30 minutes | 0 |
| `reconciliation_seconds` | 21 days | 21 days |

`quiet` delays grow after checks without new entries; `error` delays govern
retries after technical failures. The last step repeats. A missing public
playlist is not a transport failure. Reconciliation scans the full available
list again.

Enter seconds in the file. Values must be between 1 and 2,419,200; continuation
also accepts 0, meaning no nominal delay. Quiet and error steps must not
decrease. Restart to apply changes; this does not abandon a scan or move an
already assigned check forward.

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
usage is too high. Collection waits when YouTube reports quota exhaustion.

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
| `restore-update-copy` | Restore an available, unexpired emergency update copy after confirmation. |
| `purge-update-copy` | Remove an expired update copy; keep an unexpired one. |
| `clear-sign-in-limit` | Clear the temporary block after repeated failed sign-ins. |
| `uninstall` | Permanently remove this installation after confirmation. |

Most commands first recover an interrupted update or uninstall. Follow
[Troubleshooting](TROUBLESHOOTING.md) if recovery cannot finish.

## Built-in limits

- Up to 10,000 channels per workspace.
- Manual additions: 20 per minute and 300 per 24 hours. Restarting does not reset
  these limits. Reconnecting restored channels does not use these counters.
- Manual channel statistics refresh: six hours after the last completed refresh.
- User-work backup: up to 16 MiB.
- Sign-in: up to five attempts per minute, including successful attempts.
