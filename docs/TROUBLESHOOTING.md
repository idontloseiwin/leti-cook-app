# Troubleshooting

Start with **Health** for collection and quota notices, and **Telemetry → Sync
history** for recorded runs. A past error in Sync history does not by itself
mean that the same problem is still occurring. **Telemetry → Discovery support
journal** provides recent boundary observations using private references. The
pipeline event log is off; Health and Sync history use their own state.

Commands below use `/opt/leti-cook`; substitute your installation folder.

## The page will not open

Use the exact `http://127.0.0.1:PORT` address printed by the installer. If the app
is on another computer, keep its [SSH tunnel](INSTALLATION.md#opening-it-from-another-computer)
open with the same port on both sides. Check that the server and Docker are running.

`invalid_host` means the address or port does not match the installation. Use
its configured loopback address rather than a custom hostname or public proxy.

If the page opens but data does not load, check Health, then retry the failed
view. Do not delete the database or re-add channels to fix a page error.

PostgreSQL error `53100` can also mean the container's shared-memory area is
full, even with free disk space. The standard database container allows 256 MiB
of shared memory for concurrent queries; normal updates apply this setting.
Keep the recorded operation and error code when reporting a failure. A healthy
server does not guarantee that every individual screen request succeeded.

## Setup code or sign-in

Setup codes expire after 15 minutes. Before access setup is complete, request
a new one:

```bash
sudo python3 /opt/leti-cook/manage.py setup-code
```

After access setup, open `/auth` and use the mode you chose. The setup-code
command then stops with `owner_already_created` and issues no code; it does not
reset a password or change that mode.

### Signed out while working

With an account, a session ends after 30 minutes without any request from the
browser, and always 12 hours after sign-in. The page then opens sign-in. After
you sign in you return to the page you were on; a research canvas with unsaved
changes is restored in the same tab ("Restored your unsaved canvas."). Only
this app's own pages are returned to. **Sign out** ends the canvas copies kept
in that tab.

Setting the server clock back while the app runs does not sign you out:
session times move back with the clock, so a session neither ends early nor
lasts longer. A sign-in wait moves the same way: it still ends when the page
said it would. Setting the clock forward is not undone, because the server may
really have been asleep, so sessions can end sooner. If the clock was set back
while the app was stopped, the app cannot see the change: browsers used
shortly before it sign in again, and other sessions, or a sign-in wait that
was running, can last longer by up to the size of the change.

### Sign-in says to wait

After five wrong passwords in a row, each further try waits longer: 1 second,
then 2, 4 and so on, up to 5 minutes. The page says how long. Signing in, or
an hour without tries, starts the count again.

A browser where you signed in during the last 30 days has its own count, so
wrong passwords sent by anyone else never make it wait. If a new browser keeps
having to wait although you typed nothing wrong, someone else is sending
passwords to the app's port. Sign in from a browser you used before and find
who else can reach the port (see [Security](../SECURITY.md#access)). The server
owner can restart the shared count; it does not stop the sender:

```bash
sudo python3 /opt/leti-cook/manage.py clear-sign-in-limit
```

## Collection has stopped

| Notice or symptom | What to do |
| --- | --- |
| Collection paused | Set `automatic_collection = true` in `config/config.ini` when ready. Cleanup continues while paused. |
| API key refused | Check the key's API and IP restrictions in Google Cloud, then run `manage.py set-key`. |
| Quota exhausted (**Stopped: waiting for quota**, yellow) | **Health** shows which bucket ran out and when it resumes: midnight Pacific time; collection resumes by itself. Work of the other bucket continues meanwhile (see [what stops what](CONFIGURATION.md#youtube-api-quota)). When only one bucket ran out, Health and Home name it, for example *General quota is exhausted; counters keep updating through Batch statistics.* Follow any **Continue loading** action shown during setup. |
| YouTube rate limit (**Stopped: rate limited**, yellow) | No action: collection resumes on the next sync. |
| **No full pass in 24 h of running time** | Work is accumulating. Check quota use in Health and the number of channels; lower the limits or remove channels if quota runs out every day. |
| **No sync has finished for … while the server was running** | Check that automatic collection is on and the server is running; record the Health signals and Telemetry, and report it if it persists. |
| Invalid settings or `scheduler_unavailable` | Run `manage.py check-settings`, correct the indicated file and validate again. |
| Large-channel confirmation | Review and confirm the estimated load in the app. |
| Worker failure or repeated timeouts | Record the affected run and error from Health and Telemetry → Sync history; report it if it persists. |

A timed-out or partial pass can still have completed useful work. Its completed
updates remain; the status does not mean the whole pass was discarded.

See [Configuration](CONFIGURATION.md) for collection controls and quota limits.

### Health and Home states

Health → **Latest recorded sync** labels how the latest sync ended; Telemetry →
**Sync history** uses the same labels for each run:

| Label | Meaning | Health and Home |
| --- | --- | --- |
| Complete | All selected work finished. | Healthy |
| Partial · work carried over | The sync ended without errors and left some work to the next sync: it gave its time to the next one, or checks and counters are still waiting. Its saved changes remain. | Healthy; this is normal with many channels. |
| Stopped: waiting for quota | YouTube's daily quota ran out, and nothing else failed in that sync. | Yellow until the reset at midnight Pacific |
| Stopped: rate limited | YouTube asked to slow down, and nothing else failed in that sync. | Yellow until the next sync |
| Failed | The sync recorded another error, also when quota or a rate limit stopped it as well. | Health **Critical**, Home **Error** |

A sync records how many of its errors were requests refused for quota or a
rate limit. It shows as a quota or rate-limit stop only when every recorded
error is such a refusal; any other error, such as a server error while
reading channel profiles, makes it Failed. Sync records without a per-error breakdown
lack that count: they show as a stop only if they recorded no error,
otherwise as Failed.

Health **Last sync** and Home **Sync … ago** show the latest sync without
errors, complete or partial. Health adds when the last full pass finished; Home
shows it on hover.

Time while the server was off is not counted: each warning below is measured
from the later of the event and the server's start. After more than an hour
without collection, Health says when the server started and how long
collection did not run. Each restart restarts the count.

- **No full pass in 24 h of running time**: no sync finished all its work for
  24 hours of running time, so work is accumulating.
- **No sync has finished for … while the server was running**: no sync finished
  for 90 minutes, or three collection intervals if longer, while automatic
  collection is on.

Queue tiles show due work in a neutral colour. They turn yellow only for
recorded failures or expired leases.

### Understanding a partial or slow sync

Expand a run in **Telemetry → Sync history** to see its recorded reasons and
phase durations, including time spent waiting within each phase. Older runs
may only have a total duration; missing measurements cannot be reconstructed.

A missing statistics response can require a later metadata check. It does not
by itself mean a video was deleted. If the metadata response also omits the
video, the app can record it as unavailable via the API, without knowing why.
The requested retry delay is not a guarantee that the next sync starts then.

For a recent discovery, open **Telemetry**, scroll to **Discovery support
journal** at the bottom and open it. Select a private video or channel
reference and use **Refresh journal**. Its bounded
sample can connect an accepted signal, claim, provider attempt, writer
confirmation, projection refresh, API delivery and browser report. A wait
explains deferred work; an unknown outcome remains unresolved until the owner
confirms recovery. An absent next step may also mean a dropped or rotated
observation. Check the reported loss and Health before drawing a conclusion.

`writer_receipt` means the event was settled against the existing writer's exact
attempt. Other writer confirmations describe that owner's operation and do not
by themselves prove fresh source content. Projection entries confirm consumed
refresh markers. API `prepared` precedes response delivery; a subsequent
`delivered` records the completed response body. Browser `received` and
`rendered` are client reports, not independent proof of a paint, current
visibility or server-side exposure. No diagnostic entry overrides stored
receipts, source expiry or access rules.

If the journal is unavailable, collection can still work. Its file/key or disk
may be unavailable; do not reset collection state to repair diagnostics. See
[journal settings and limits](CONFIGURATION.md#telemetry). Run history describes
past work, not a video's current availability.

## Data maintenance status

Startup cleanup can delay access after downtime: stored YouTube data that
expired while the server was off is removed first. The time grows with the
length of the downtime and the size of the catalogue. Leave the server running
and check Health for progress. Pausing collection does not
pause cleanup.

Cleanup removes data in small batches, each in its own short database
transaction, and keeps going after interruptions:

- **Removed data stays removed.** After a dropped connection, a database
  restart or an application restart, cleanup continues with the data still
  left; work already done is not repeated.
- **Batches fit the database's time limit.** A batch the database cancels for
  taking too long is retried at half its size, and grows back once batches are
  quick again.
- **Failures are retried.** A failed batch is retried after 5 seconds, then
  10, 20 and 40 seconds. When the same step fails five times in a row with the
  same cause, or ten times with changing causes, Health shows **Data cleanup
  keeps failing while …** with the step and its latest cause, for example
  `lock_timeout`. Retries continue at a slower pace, at least every 10
  minutes, and the notice clears after the next success. A single failure
  shows no notice.
- **The startup check is not required.** If the check of stored data at
  startup cannot finish, cleanup runs at full pace anyway and the check is
  retried.

Expired values are never shown while cleanup is late: every read hides each
value past its own 29-day deadline. While data that expired during downtime is
being removed, or the startup check has not finished yet, YouTube data waits;
a failed batch does not end that wait. If cleanup keeps failing, YouTube data
becomes available again with the notice above, still without expired values,
and cleanup keeps retrying.

YouTube data stays unavailable whenever the application cannot verify the
database, its schema and read protection, or the [server clock](#the-server-clock).
Settings, Health, your collections and backups remain available.

| Cause | Meaning | What to do |
| --- | --- | --- |
| `statement_timeout` | A database request took too long and was cancelled. | Usually a busy server or slow disk; check the load. |
| `lock_timeout` | Another database session holds a lock that cleanup needs. | End a long-running manual session or transaction on the database. |
| `transaction_conflict` | Cleanup conflicted with other database work. | Usually clears by itself. |
| `connection_terminated`, `connection_lost`, `server_restarting`, `pool_unavailable` | The database connection was ended, failed or could not be opened in time. | Check that the database container is running. |
| `insufficient_resources` | The database server ran out of memory, disk space or connections. | Free disk space or memory. |
| `query_failed`, `invalid_result`, `files_unavailable`, `unknown` | A request failed in an unexpected way. | Report it with the cause and installed version. |

If the app reports database unavailability, a schema error or maintenance
failure, retain the error code and installed version. Check for an interrupted
update below. Do not change the schema or delete data manually to clear a notice.

## YouTube notifications on Health

With [notifications](INSTALLATION.md#youtube-notifications-websub) on, the
**YouTube notifications (WebSub)** section of Health shows active
subscriptions, the last notification and what notifications did in the last
24 hours.

- **Subscribing** for more than a few minutes: check that the public address
  forwards only `/websub/` to this installation with its public name.
- **Paused by YouTube** or **Stopped after failures**: requests resume on their
  own; after repeated failures one request is retried after 6 hours.
- **Unavailable**: the status could not be read. The section retries by itself,
  and notifications and normal checks continue meanwhile. If it stays
  unavailable, report it with the installed version.

## The server clock

Collection and expiry depend on an accurate clock. Check synchronisation:

```bash
timedatectl
sudo timedatectl set-ntp true
```

Wait until `System clock synchronized: yes`. If NTP is unsupported, install or
enable a time service such as systemd-timesyncd or chrony.

A confirmed clock mismatch, more than 60 seconds from Google's clock, pauses
collection, reads of stored YouTube data and cleanup. The app shows "YouTube
data and collection are paused. Check the server clock." Library, the research
workshop, Home and agents cannot read stored YouTube data meanwhile, and
expired data is not deleted until the clocks agree again. Your organised work
is kept, and its backup still works. While the mismatch lasts, the clock is
checked again every five minutes; everything resumes when a check agrees.

A notice that the external clock check could not complete is different: the
app keeps working. Verify outbound HTTPS and local time synchronisation. Do not
manually shift the clock to change data expiry.

## Missing videos or statistics

Check whether initial loading has finished, quota is available and the channel
is active. Public YouTube lists do not necessarily include every video shown
in a channel's reported total. Videos can also become unavailable or lack
current type evidence.

Details disappear when their API data expires. Fresh collection can restore
current details, but it cannot recreate expired statistics history. New
installations and recently added channels need time to accumulate observations.
See [Data and backups](DATA.md).

A channel listed last as **No longer on YouTube** is one YouTube no longer
returns; see [A channel YouTube no longer returns](DATA.md#a-channel-youtube-no-longer-returns).

No Streams anywhere, and a Streams tab that says "Streams are turned off":
Streams are not collected or shown by default. Add `stream` to `formats` in
`config/config.ini`; see [Video formats](CONFIGURATION.md#video-formats). Stored
streams reappear on the next page load (Home and Health within about 20 seconds)
and collection resumes in the next pass.
If `check-settings` reports `formats must list…`, correct the spelling: lower-case
`long`, `short` and `stream`, separated by commas, each once.

## Library and workshop messages

The size, time and turned-off-field messages below read the same wherever
Library reads: the first page, the next page, **Select all matching**, the
group counts and Overview.

| Message or symptom | What it means and what to do |
| --- | --- |
| "This browsing session expired." | Library's 10-minute list ended, or the channel or your access changed. Choose **Reload catalogue**; loaded videos and the selection stay. |
| "This selection is too large to browse (more than 50,000 videos)." | The list is over Library's limit. Narrow the filters. |
| "This selection is too large to browse: its data exceeds the server's memory limit." | Fewer than 50,000 videos, but their stored values do not fit the 10-minute list. Narrow the filters. |
| "This selection took too long to prepare." | The read reached its time limit: 8 seconds of work for Library's list, counted from the moment its read starts. Narrow the filters or try again. |
| "These conditions are too large or too complex for one read." | The search and filters hold too many conditions or values, or nest too deep. Remove some conditions or values. |
| "This result is too large to send at once." | The answer, for example one page with long tag lists, is over the server's response limit. Narrow the filters or choose fewer columns. |
| "The server is busy with other reads or lists. Try again shortly." | Heavy reads run two at a time, and this one waited its limit for a turn: up to 4 seconds for Library's list, up to 8 seconds in total for other reads. A third list prepared at the same moment is refused at once. Nothing was read or changed. Try again in a moment. |
| "This selection is too large to browse at once." | The server refused the list as too large without naming the limit. Narrow the filters. |
| "A field in these filters is turned off in Collection fields." | A condition uses a field that is not collected for this format. Remove that condition, or turn the field on in **Collection fields**. |
| "Data needed for these filters is turned off in Collection fields." | The filters need data whose collection is turned off. Turn it on in **Collection fields**, or remove the filter. |
| "No connection to the Leti Cook server." | The browser got no answer: the network is down or the server is not running. Loaded videos stay; use the retry next to the message when the connection is back. Removing or restoring a channel, user-work backup and video type reviews use the same words. |
| "The server answer could not be read." | An answer arrived but is not the page's data, usually an error page of a proxy in front of Leti Cook. Check the proxy and the server, then retry. |
| "… The result is not confirmed; refresh status before retrying." | A change (removing or restoring a channel, a restore or reconnection, a video type decision) lost its answer, so it may have been made. Refresh the list or status first, then retry only what is missing. |
| "Preparing stored catalogue…" | The server is still preparing searchable values after a start. It retries twice, then offers a retry. Collection keeps running. |
| "Stored data is temporarily unavailable." | Startup cleanup, an update or a [server clock mismatch](#the-server-clock) holds data reads. Check Health and retry later. |
| "Group counts are unavailable." | This server does not run the base analytics. Filters still work without counts. |
| "Group counts could not load." | The count read failed; the reason follows. Counts show "—" until **Retry counts** succeeds. Filters still work. |
| "Notes are limited to 8,192 bytes…" | A Cyrillic letter takes 2 bytes, so notes hold about 4,096 Cyrillic letters. Shorten the notes; the counter below them shows the bytes in use. |
| "A canvas is limited to 16,384 bytes…" | Notes, picked IDs and nodes together would exceed what Save accepts, so the edit was not made. Shorten the notes, pick fewer items or remove a node. |
| "These … items do not fit…" | **Continue with selected** would make the canvas too large. Pick at most the number shown, or shorten the notes. |
| "This page's unsaved canvas could not be reopened…" | Both of the tab's copies of that canvas were damaged or come from another version. The page opens without it; open the saved canvas from Saved, or rebuild it. |
| "This canvas was saved elsewhere after you started editing here." | Another tab saved the canvas after your edits began, so Save did not overwrite it. Your changes stay in this tab: save them as a new canvas, open the saved version (Back returns to yours), or replace the saved version after confirming. |
| "Streams are turned off." (or Shorts, Long-form videos) | That format is not in `formats` in `config/config.ini`. Its stored data is kept but not shown; queries, saved views, canvas sources and agent requests for it answer `format_turned_off`. Add it to `formats` to show it again; see [Video formats](CONFIGURATION.md#video-formats). |
| No launch comparison in the Inspector | It is not enabled on a standard installation. |
| Saved names a condition and opens nothing | The server cannot run that condition. The saved view is kept. |

See [Channel Library](DATA.md#channel-library) and
[Research workshop](DATA.md#research-workshop).

### One-off acquisition is refused

| Answer | What to do |
| --- | --- |
| 409 `owner_auth_unavailable` | The method needs the channel owner's Google sign-in, which this version does not have. Use a method that works with the installation's API key. |
| 429 `daily_cap_reached` | Today's cap for one-off acquisitions is used. Wait for the `Retry-After` time (midnight Pacific). |
| 409 `acquisition_busy` | Another acquisition is running. Retry when it ends. |
| 409 `plan_changed` or `cost_not_confirmed` | Execute did not match the preview. Preview again and send its fingerprint and cost. |
| 503 `daily_cap_unavailable` | The server could not read today's use, so it refuses. Retry later. |

See [One-off API acquisition](DATA.md#one-off-api-acquisition).

## Backup is refused

"This backup exceeds the supported size or item limit." means that the file is
larger than 16 MiB, or that the work or the file holds more of something than
the [built-in limits](CONFIGURATION.md#built-in-limits) allow. Every such
refusal answers HTTP 413 with the code `user_work_limit_exceeded`, whichever
limit it is. Nothing was downloaded or restored.

"Backup stopped: … has an internal ID that Leti Cook did not create" names an
item of your work whose ID is not a version 4 UUID: a channel by its channel ID
(`UC…`), a folder, label, saved item or collection by its name, or a video type
decision or a restored channel awaiting reconnection by its kind. Leti Cook
creates only version 4 IDs; any other ID comes from a change made directly in
the database, for example manual SQL or a data migration. A backup cannot hold
it, so nothing was downloaded. When several items are affected, the message
names the first and counts the others; the answer of `POST /user-work/backup`
(code `unsupported_user_work_identity`, HTTP 422) lists up to 10 of them.

Undo that database change if you can. Otherwise delete the named folder,
label, saved item or collection and create it again in Leti Cook, which gives
the new one a version 4 ID, then download the backup again. A channel, a video
type decision and a restored channel awaiting reconnection keep their IDs for
as long as Leti Cook stores them, a removed channel also during its 29 days in
**Deleted channels**, so they cannot be recreated this way: undo the change that
created them, or [report the problem](#reporting-a-problem). Such a channel
stays in **Deleted channels** like any other: it is listed, and **Restore
channel** works until its recovery period ends.

## The installer stopped

Keep its `stage` and `reason` codes. Correct the reported requirement, such as
Docker, clock synchronisation, permissions, disk space or network access.
Installation does not reuse an existing database or overwrite a partial folder;
retry with a new folder after resolving the cause.

If the installer stopped after it created the installation folder, its
containers may keep running. It prints the command that removes this
unfinished installation, for example:

```bash
sudo python3 /opt/leti-cook/manage.py uninstall
```

That command lists the containers, volumes, networks and images of the
installation, asks for its project name and removes them together with its
folders. An unfinished installation has no work to back up. If the installer
stopped before it created any Docker resources, uninstall removes nothing and
prints the command that removes the folders. Other management commands in that
folder only answer that the installation did not finish. While the installer is
still running, uninstall for its folder stops with
`uninstall_installation_in_progress`; wait until the installer stops.

From a source checkout, `install.sh` first copies the project's files into a
temporary folder, normally under `/tmp`, and installs from there. It removes
that folder when installation succeeds and when you stop it with Ctrl+C. After
a failure, or if the terminal connection drops, it keeps the folder and prints
`Release bundle kept for diagnosis:` with its path. The folder holds only the project's files, no key or password;
remove it when you no longer need it.

`root_required_run_with_sudo` means the installer was started without root. It
stops before asking for a folder; run the same command again with `sudo`, for
example `sudo bash install.sh`.

`docker_engine_28_3_3_required` means Docker Engine is older than 28.3.3; the
line before it names the version found. On older Engines, other computers on
the local network can reach ports published on `127.0.0.1`, so they could open
Leti Cook. Install Docker Engine 28.3.3 or newer from
[Docker's repository](https://docs.docker.com/engine/install/), then run the
installer again. `docker_engine_version_unknown` means `docker info` reported
a version the installer cannot read, such as a development build; install a
released Docker Engine 28.3.3 or newer.

For `docker_network_address_pool_exhausted`, ask the host administrator to review
unused networks or Docker address-pool settings. Do not remove networks used by
other applications. Remove the unfinished installation with its uninstall
command, as above.

## An update is refused

Run the installed command first:

```bash
sudo python3 /opt/leti-cook/manage.py update --check
```

It checks the download, compatibility, Docker, clock, current runtime/database
readiness and eligibility for a recovery copy. Applying the update additionally
prepares images and checks copy space and file replacement. A successful check
does not reserve resources or guarantee these later steps.

| Reason | Next step |
| --- | --- |
| No eligible version | Check whether you intended to include prereleases with `--pre`. |
| Signature or checksum failure | Stop and obtain the files again from the trusted project source. Do not bypass verification. |
| Local file changes or incompatible version | Preserve the error and request help; do not replace installed files manually. |
| Invalid configuration | Correct the file reported by `manage.py check-settings`. |
| `docker_engine_28_3_3_required` or `docker_engine_version_unknown` | Docker Engine is older than 28.3.3, or its version cannot be read. Install Docker Engine 28.3.3 or newer as described under [the installer stopped](#the-installer-stopped), then run the update again. Other management commands keep working; most print a warning until then. |
| Disk, image download or build failure | Resolve the host or network problem, then retry the management command. |
| Expired API data blocks a recovery copy | Let normal cleanup finish and retry. If cleanup fails, resolve that failure first. |
| `update_lock_busy` | Another management command, update or removal is running for this installation. Wait until it finishes, then run the command again. |
| `update_compatibility_unsupported` | The installed updater cannot apply this package's formats. A 0.1.0 installation reports it for version 0.2.0, which it cannot be updated to; nothing was changed. Move your work with a user-work backup to a new installation; see [Coming from version 0.1.0](INSTALLATION.md#coming-from-version-010). |
| `update_schema_history_changed` | The new version's database history does not continue the installed one. It cannot be applied to this installation; nothing was changed. Move your work with a user-work backup to a new installation; see [Moving to a new installation](INSTALLATION.md#moving-to-a-new-installation). |

A refusal before the update starts ends with "No update was started." The
installed version keeps running and no update journal is involved.

### Why an update stopped

An update that stopped after it started prints one fixed cause code and what
to do, just before its final line:

```text
Cause: update_insufficient_disk_space. A disk is too full for the update and its recovery copy. ...
Command stopped: update_failed_previous_version_recovered.
```

The same code is kept as `failure_code` in the update journal,
`/opt/leti-cook.update/owner/update-journal.json`. It is never error text from
Docker, the database or the application. A later management command that
recovers an interrupted update prints the cause again.

If the update stopped before the application was paused, it says "Update
stopped before the application was paused." and "The installed version kept
running."; no pause is reported because there was none.

| Cause | Next step |
| --- | --- |
| `update_insufficient_disk_space` | Free space on the disks that hold the installation folder and Docker's data, then run the update again. `update --check` does not measure this space. |
| `update_preparation_failed` | Images or files of the new version could not be prepared, for example a download or build failed. Check network access and free disk space, then run the update again. |
| `update_collection_stop_unconfirmed`, `update_collection_stop_timeout` | Collection did not stop in time. Run the update again when no collection pass is running. |
| `update_backend_stop_not_clean`, `update_backend_stop_failed`, `update_db_stop_not_clean`, `update_db_stop_failed` | The application or its database did not stop cleanly. Run the update again; if it repeats, keep the code and request help. |
| `update_backend_start_failed` | The new version did not start. Often another program took the installation's port: free it, then run the update again. |
| `update_schema_not_completed`, `update_schema_step_failed` | The new version's database step did not finish. Keep the code and both versions and request help before retrying. |
| `update_runtime_health_failed` | The new version did not pass its health check in time. Keep the code and both versions and request help before retrying. |
| `update_stopped_by_user` | The update was stopped from the terminal, for example with Ctrl+C. Run it again when ready. |
| `update_unexpected_error` | Keep the code and the installed version and request help. |
| Any other code | Follow the printed hint. Without one, keep the code and the installed version and request help. |

## An update was interrupted

A command names the update journal ("The update journal records any recovery
still needed.") only when an update or its recovery stopped part-way and the
journal still records unfinished work.

Rerun the installed management command. Most management commands first attempt
to recover an interrupted operation, even when invoked with `update --check`
or `--version`. Wait for that recovery before making further changes.

If automatic recovery cannot finish and an unexpired copy is available:

```bash
sudo python3 /opt/leti-cook/manage.py restore-update-copy
```

Read the confirmation before accepting. The copy is for this installation only
and expires at its original deadline. Do not manually start a different database
volume or alter that deadline.

To remove an expired copy when automatic cleanup could not finish:

```bash
sudo python3 /opt/leti-cook/manage.py purge-update-copy
```

An expired copy cannot be restored. If recovery is unavailable, keep the files
and error details for diagnosis. A user-work backup can be imported into a new
installation; it cannot restore YouTube history.

If uninstall was interrupted after you confirmed it, its message names the saved
removal journal; use the recovery command printed before removal. A refusal
before confirmation names no removal journal and removes nothing.

Containers of other programs that end while uninstall runs do not stop it.
`uninstall_docker_command_failed` means Docker could not answer for a container
that still exists; check that Docker is healthy, then run uninstall again.

## Reporting a problem

Include the installed version, operating system, Docker/Compose versions,
steps to reproduce, relevant error codes and when the problem occurred. A
screenshot of the affected view is useful. Remove private research details.

Never publish API keys, setup codes, passwords, environment files or database
dumps. Use [Security](../SECURITY.md#reporting-a-vulnerability) for sensitive reports.
