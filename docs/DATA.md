# Data and backups

Your organised work and collected YouTube data have different lifetimes.

## What is stored

| Data | Retention |
| --- | --- |
| Your channel list, folders, labels, saved views, saved queries, research definitions, notes, collections and video-type decisions | Until you delete them; included in user-work backups |
| YouTube profiles, video details, current counters and playlist contents | Refreshed or removed within a rolling 29-day window |
| Historical statistics snapshots | Removed 29 days after collection |
| Category reference, collection runs, API request records and job details | 29 days; summary job rows remain |
| Answers of a [one-off API acquisition](#one-off-api-acquisition) | Not stored; only the API request records above |
| Account, sessions, API key and setup state | Stored locally in the installation |
| Known-browser mark: a cookie your browser keeps after you sign in, read only by sign-in ([Security](../SECURITY.md#access)) | 30 days after the last sign-in in that browser; a password reset ends it |
| Pipeline telemetry (the Telemetry event log) | Not recorded in this version |
| Discovery support journal | Recent boundary observations in a rotating 4 MiB `discovery-support.jsonl`, beside the local account database. Older entries are displaced by size, without an age guarantee. Video, channel and work identities use installation-specific keyed references; source payloads, titles, raw video/channel IDs, URLs and exception text are excluded. The separate `discovery-support.key` remains local and is not part of a user-work backup |

## Connections to other services

The application and database run on your server. These connections leave it
or your browser:

- **YouTube Data API.** Setup, collection, adding channels, the API key check
  and [one-off acquisitions](#one-off-api-acquisition) send requests with your
  API key to `www.googleapis.com`.
- **Clock check.** The backend sends one `HEAD https://www.googleapis.com/`
  request, without a key or parameters, to read Google's clock. It uses no
  quota. It runs when the backend starts and when an hour passes without a
  YouTube answer, which also carries Google's clock. While the clock is not
  confirmed, it repeats after five minutes, or up to an hour apart after
  failed checks. See [the server clock](TROUBLESHOOTING.md#the-server-clock).
- **Thumbnails and avatars.** Your browser loads them directly from YouTube's
  image hosts: `img.youtube.com`, `i.ytimg.com`, `yt3.ggpht.com` and
  `yt3.googleusercontent.com`. Google can see your browser's IP address and
  which images it loads; no referrer is sent. The app's pages load nothing else
  from other sites. Links to YouTube open only when you click them.
- **YouTube notifications (WebSub)**, only when
  [configured](INSTALLATION.md#youtube-notifications-websub). For each tracked
  channel, the server sends subscription requests to Google's hub,
  `pubsubhubbub.appspot.com`, with the channel's feed address, your public
  callback address and a signing secret. YouTube then sends notifications to
  that address.
- **Installation and updates** download files from GitHub and container images
  from registries. Building images on the server also downloads their base
  images and packages.
- **Agent programs** you connect through [MCP](MCP.md) receive query results
  and may send them to their model provider.

None of these upload your database or user-work backup.

## Video engagement observations

A statistics-bearing video profile can report an explicit zero for likes or comments.
An omitted field has no reported value. During a Batch refresh a zero following a
positive count, or a zero for a field omitted from the latest profile, leaves that
snapshot field empty. The catalogue keeps any still-current previous value until
its own observation expires. Views and the other engagement field refresh independently.
A first Batch zero can be recorded when the collector has no contrary evidence.

A WebSub entry for a new public, processed video can omit views. If the profile
explicitly reports no broadcast and every enabled public format list is checked
successfully without a match, the collector closes that notification. A new
notification for the same video triggers another check. The outcome records the
observed API facts only.

## Alerts

An alert watches future observations from normal collection. Its preview uses
the current Library selection. An existing match establishes the starting
value. A crossing requires two comparable measurements of the same object.
For a folder or label, each channel whose subscriber count crosses the threshold
gets its own notification, even when the collector checks the channels together.
Processing the same channel measurement again preserves the original notification.
Every observed crossing in the selected direction gets its own message. For an
upward threshold of 100,000, observations of 99,000, 101,000, 91,000 and
102,000 produce two messages, including when the worker catches up later.
The collector keeps the below-threshold samples used to establish each rise.
A paused worker retains eligible observations for the lifetime of their evidence.
The value on the threshold qualifies in the selected direction. Unknown or
hidden counters supply no measurement. Views from different definitions
cannot be compared. A notification records when the change was observed;
the exact moment between measurements is unknown.

New-publication alerts require a publication time after monitoring began and
confirmed public membership of the selected format. Availability alerts use
the collector's confirmed loss or restoration. A timeout, an absent playlist
and a change to a Library filter supply no evidence of removal.

Alerts work while the browser is closed and consume no additional YouTube
requests. While observations are waiting, the worker processes short batches
in separate transactions. It yields regularly for the collector and Library.
An empty queue is checked every five seconds. A single slow observation can
be deferred while other ready observations continue. Observation evidence follows the same lifetime as its source.

Use **Watch** on the Library videos tab to carry the selected channel, folder
or label, video format and supported duration filters into a rule.
Preview shows up to five items that match now. An item already beyond a threshold can trigger an alert after it returns
to the other side and crosses again.
Under **Alerts → Rules**, you can also select an individual video, pause a rule
or set a downward threshold. **Inbox** shows the two values and their observation times for a
crossing. The rule records a coverage gap for expired, missing or unreadable observation
evidence. Other observations can still be delivered. A timed-out observation is retained
for retry while its evidence is available.
When several rules watch the same video counter, each rule keeps its own
definition, scope and time of eligibility at capture. Processing can share the
source observation and the preceding comparable measurement. A later correction
between those measurements is considered if it is stored before evaluation.
Pausing or editing a rule preserves accepted observations. An observation
captured before a pause can reach the inbox while the rule is paused.
Moving a channel to the trash interrupts observation. Restoring it starts a
new observation period. Leaving a folder or label also breaks comparisons
for rules that watch that group. A replacement group with the same ID has
its own identity. User-work exports include rule definitions and pause settings.
Restored rules begin observing at import or when their source is reconnected.
Their first measurement establishes a baseline. Rules shows when a source
is missing. Pending observations and inbox messages stay with their original
installation. Repeated crossings produce separate inbox entries.
Deleting a rule immediately removes it and its messages from the Alerts screen.
The worker clears its stored messages and pending observations in batches.
Other rules continue to receive crossings during cleanup.

For an operator sample, run `psql -X -v ON_ERROR_STOP=1 -f tools/alert_queue_sample.sql`
with read access to the installation database. It returns aggregate PostgreSQL counters,
estimated pending rows, queue storage including indexes, and the age of the oldest job
with a known enqueue time, measured from the start of the capturing SQL statement
and including time before its transaction commits. Older pending jobs created before enqueue times were recorded
have a separate flag. To estimate rates, take two samples in the same statistics lifetime
and divide counter differences by the elapsed seconds. Discard intervals containing a
restart, statistics reset, relation replacement, disabled counters or decreasing
counters. PostgreSQL statistics can lag and can include work that rolled back.
Queue removals include expiry and rule deletion as well as worker completions.
Inbox inserts measure database activity. Read the inbox to verify deliveries.

## The 29-day window

YouTube's standard rules require the public API data used here to be refreshed
or deleted within 30 days. Leti Cook uses 29 days and automatically refreshes
and cleans up data while running. See
[YouTube's data-storage rules](https://developers.google.com/youtube/terms/developer-policies#e.-handling-youtube-data-and-content).

- **The collection date matters.** A video published years ago can remain in
  your catalogue while its data is refreshed. Its publication date does not
  determine expiry.
- **History is temporary.** Each statistics snapshot expires independently.
  A fresh counter cannot recreate expired history or observations missed while
  your server was off.
- **Growth is counted between snapshots.** A video's 28-day growth ends at its
  latest snapshot. It counts as full history when a snapshot from at least 28
  days before that one is still kept, or, if there is none, one taken at most
  three hours later than that point; the comparison then spans 27 days 21 hours
  to 29 days, and its actual interval is kept in the data details. Otherwise
  the actual number of tracked days is shown, for example "over ~27.6 days".
- **Expired fields are hidden.** They stop appearing even if cleanup has not
  yet removed them. Library requests current data automatically; if that
  fails, it offers a reload.
- **A passing YouTube failure keeps channel profiles.** If a profile refresh
  meets a YouTube server error, a timeout, a dropped connection, a rate limit
  or a quota wait, or collection stops in the middle of it, the channel keeps
  its last confirmed name, avatar and counters and is refreshed later. The
  profile still expires 29 days after it was collected. When YouTube no longer
  returns the channel, or answers with another error, the profile is hidden
  and removed; a channel that YouTube no longer returns keeps that
  mark through such passing failures.
- **Your work remains.** Folders, labels, saved references and your edits are
  retained, subject to permanent channel deletion below.

No cleanup can run while the server is off. After a long shutdown, the app
cleans up expired data before normal access to YouTube data resumes. Collection
may need time and quota to refill missing details.

Cleanup deletes in small batches and keeps its progress: data it removed stays
removed after a dropped connection or a restart, and failed batches are
retried. Expired fields stay hidden meanwhile. If cleanup keeps failing, Health
names the failing step and its cause, and YouTube data is shown again without
the expired fields while cleanup keeps retrying; see
[Data maintenance status](TROUBLESHOOTING.md#data-maintenance-status).

Cleanup also waits while the server clock is wrong. Stored YouTube data cannot
be read meanwhile; see [the server clock](TROUBLESHOOTING.md#the-server-clock).
An update's emergency copy has its own deadline; see
[Temporary copy during an update](#temporary-copy-during-an-update).

## Catalogue coverage

YouTube may omit unavailable, private, members-only or unlisted videos from
public lists or responses. For channels reporting more than 20,000 videos, the
app warns that uploads lists may contain only the latest 20,000 uploads; see
[the related YouTube issue](https://issuetracker.google.com/issues/166292064).
A channel's reported video count therefore need not match your collected catalogue.
This coverage warning is separate from your configurable limits per collection pass.

A video becomes unavailable in Leti Cook only after two YouTube video-data
responses in a row leave it out. The second request is made in the next
collection pass, or in a later one when that pass's limits or quota are used
up. Until the second one, it keeps its stored values, and the run's details say
how many videos wait for this check. See [Unavailable videos](CONFIGURATION.md#unavailable-videos).

When the collector rechecks a video already known to be unavailable and saves
the same result, that check counts as completed in Sync History. Other videos
in the same pass may still be waiting for confirmation.

A channel's first video of a format may appear after an earlier check found no
public list. Normal retries follow [Playlist intervals](CONFIGURATION.md#playlist-intervals).
Quota, channel permissions and ongoing work can delay it; no fixed time to
discover the first video is promised.

Records without current type evidence can remain as saved IDs while being absent
from a typed catalogue. Missing details do not necessarily mean your saved work
was deleted. Check collection status and quota before removing and re-adding
a channel.

Scheduled video requests admit healthy rows due within one minute. An already
started final batch for a provider operation may fill spare slots with eligible
rows due within ten minutes. Future work alone does not start a request.
The initial loader and the recurring collector interpret the same publication
evidence. A future start on its own keeps a video waiting.
Confirmed Long-form videos and Shorts return to ordinary collection after an
observed end, a completed broadcast, or an explicit non-live flag with a past
actual start once the processed profile is usable. A public, processed profile
with an explicit non-live flag and no future start also qualifies when the
provider omits both actual times.
An archived Stream requires an actual end time.

If a waiting Long-form video or Short has two successive, separately timed
profile reads that still leave publication unclear, the collection queue marks
it for review (`publication_evidence_needs_review`). Health counts that video
once. The daily profile check continues. A clear observation clears the marker.
Pending observations do not write counter snapshots.

## Channel Library

A channel's **Videos**, **Shorts** and **Streams** tabs show its Long-form
videos, Shorts and completed Streams. Each tab has the same **Overview** and
**Videos** sections and the same Inspector. Streams are turned off by default:
their tab says "Streams are turned off" and names the setting that turns them
on, [`formats`](CONFIGURATION.md#video-formats). A turned-off format's stored
videos are kept but are not shown or counted anywhere, until the format is
turned on again. Library reads only stored data,
through the same server rules as the [research workshop](#research-workshop).
It is always on; [nothing needs enabling](CONFIGURATION.md#library-and-research-workshop).
Opening it does not request YouTube data or change collection. That holds for
every read the page makes, including the next page, **Select all matching**
and the filter counts: the server refuses any YouTube request while it answers
them.

Overview and Videos show the eligible rows already in the selected format.
If a list check is unfinished, the page shows its status alongside those rows.

Dates, years and "today" count in your browser's time zone, or in UTC when you
choose **UTC** for Time in Overview. Library names the zone next to them.

### Videos

Videos shows the complete list of the channel's videos that match the filters, in the chosen order. One read of the channel returns the first 50 cards together with the ordered list of every matching video: filters and sorting apply to the whole channel, including when ordering by 28-day growth. Videos with equal values follow each other by video ID, and videos whose value is unknown come last in either direction; an unknown value is never counted as zero. The bar above the grid shows how many videos the list has. Counts in the filter panel describe the current query.

Further cards load as you scroll, 50 at a time, by their place in that list, so no video repeats or goes missing while the collector updates counters. Their values are read when they load. A video of the list that no longer matches the filters, or is no longer available, is left out, and a note counts such videos. Library requests `page.growth_lifetime = "page"` so that growth used only on cards is evaluated for the displayed page. The default API contract keeps its scope-wide lifetime.

Changing the sort, its direction, a filter or the format opens the new list at its top. **Reload** and **Refresh order** read the same list again, at the video you were viewing, or from the top when you were at the top, so newly matching videos there are in view; earlier videos load as you scroll up, and **Show from the beginning** returns to the top. If that video no longer matches, Library opens the top of the list and says so. The list never restarts by itself, and a loaded card never disappears by itself: it stays in its place for as long as the page is open, together with your selection, the open Inspector and the scroll position. Once the read of the list has expired the bar says what time its order is from.

While the Videos tab is on screen, Library reads the loaded cards again in the background and puts newer numbers in place of older ones on the same cards. These reads go to the program's own storage; nothing is requested from YouTube. A full pass over the loaded cards runs every 5 minutes. When you return to the browser tab, or a shown value reaches the end of its lifetime, the cards around the one you are viewing are read at once, at most once a minute. Values that would end before the next pass, such as 28-day growth, are read ahead of time, so they rarely show **Needs refresh** while the page is open. The order of the list, the loaded part and the count stay as they are until you use **Refresh order** or **Reload**, or change the sort or a filter. Up to 200 cards are read at a time, one read after another. A very long loaded list is updated in turns, the cards around the one you are viewing first, so its far end can be a few passes behind. While you scroll and further cards load, a pass is put off: it goes on at most once a minute, with the cards read longest ago, and the cards around the one you are viewing are read once in 5 minutes. A new attempt after a failed one keeps the same minute. Nothing is read while the browser tab is hidden or another tab of the channel page is open.

If a background read fails, every card keeps the values it had and a note says so. Library tries again after 5, 10, 20 and 30 minutes and then stops until the list is read again. A card whose video no longer matches the filters, or is no longer available, also stays with its last values until then, and a note counts such cards.

A list of more than 200,000 videos is not held as one list. Library shows its count, loads it in its order as you scroll and says that a video whose counters change meanwhile can move between pages and that the whole list cannot be selected at once; narrower filters keep the complete list. After **Reload** at a remembered video of such a list, earlier videos do not load by scrolling up; **Show from the beginning** opens the top.

**Select all matching** selects every video of the list, in its order, without another read. **Open in research workshop** requests a fixed list of the same query and order at the time of the action, with the fixed-list limit of 50,000 rows. A refused action leaves the visible cards and existing selection available. Sending individually selected cards uses their IDs.

Each card shows the thumbnail with the duration, the title in up to two lines,
the views, the 28-day view growth, the time since publication, likes and
comments, and a link that opens the video on YouTube. Counts are shortened the
way YouTube writes them: 312, 1.9K, 12K, 1.2M, 2.4B. The digits are cut, not
rounded, so 999,950 views read 999K; hover a number for its exact value. Growth
always carries its sign and period: +48K · 28d, −1.2K · 28d or 0 · 28d, and the
actual days when the stored history is shorter, such as +3.1K · 6d. The date
reads 1d ago, 3w ago, 5mo ago or 4y ago; hover it for the exact time in your
time zone. **Compact** puts smaller cards in each row, shows the date next to
the views and leaves out likes and comments. The number of columns follows the
width of the page, and every card in the grid has the same height whatever its
title or values. A video without a length yet, such as a live stream, shows a
dash for its duration.

In the **Shorts** tab the cards are narrower and stand edge to edge: where a
row fits n long-form videos, it fits 2n − 1 Shorts, and never fewer than two.
Each Short has a tall 2:3 thumbnail: the middle of the YouTube thumbnail for
the video, only scaled, so the black bars beside an upright picture stay
outside the card. The views stand in their own row under the title, then the
growth; **Large** adds likes and comments, and the date with the YouTube link.
**Compact** keeps the views with the link and the growth, and leaves out the
date, likes and comments; hover the title for the exact publication time.

A missing value is never shown as 0, and an out-of-date number is never shown
as current. When a shown value reaches the end of its lifetime and the program
has nothing newer yet, the card stays and that value shows a dash with
**Needs refresh**; hover it for the explanation. The other values of the card
stay, and the number returns as soon as newer data has been collected. A note
counts such videos and any withheld rows. A value YouTube no longer returns
shows a dash with **Unavailable**, and the card is dimmed. Likes or comments
that are not reported show a dash; hover it for the reason.

Search looks for text in titles, ignoring letter case and repeated spaces.
**Filters** offers presets for views, duration, likes, comments, title length,
tag count and publication years, plus the official category. Each choice
shows how many videos it would match together with the search and the other
filters. Shorts and Streams have their own duration presets, from "Under 15
sec" to "Over 3 min" and from "Under 30 min" to "4+ hours". The 28-day growth
filters and the **Views gained** sort apply to the whole list. **More filters**
offers Captions, Description contains and Declared paid promotion when those
fields are collected for the format, and Stream ended for Streams.
**Collection fields** opens [field settings](#choosing-collected-fields) in the
research workshop.

Each format keeps its own selection; opening another channel clears it.
**Save view** keeps the search, filters, sort and grid density in Saved, and
the view opens again in the tab of its format; see
[Saved views, queries and studies](#saved-views-queries-and-studies). **Add to
collection** works in all three formats.

**Open in research workshop** opens a new study on a fixed list of Library's
query and order, made at that moment; the workshop shows its total and time.
**Send selected to workshop** sends up to 1,000 selected videos as selected
IDs; after **Select all matching**, the whole list goes as the fixed list of
the query, and only if that list still holds exactly the selected videos.
Otherwise nothing is sent and Library says how many videos were selected and
how many match now.

### Inspector

The Inspector shows a video's stored details, its 28-day history up to now and
how it compares with the channel. Opened from Videos, it reads the current stored data for that video in the selected channel and format. A description appears only when Description is
collected for that format; otherwise the Inspector says where to turn it on.
The launch comparison is not enabled on a standard installation, so the
Inspector does not show it.

### Overview

Overview summarises the public videos of the format published in the chosen
period (7D, 28D, 3M, 1Y or MAX): Fingerprint, Publishing rhythm, publication
years and the content breakdown. The server counts all matching videos, not
just the loaded page. A video whose counters or details need refresh stays in
the count; its value is unknown, and Coverage shows how many under **Values
need refresh**. Rhythm and years use the same videos as Fingerprint. YouTube
topic shares are counted among videos whose topics were collected. Each format
has its own duration buckets; choosing a bucket opens Videos of the same format
with that filter.

### Channel header and Home

The channel header shows totals by format and the 7D and 28D change of the
channel's own counters. Home's video counts and the channel totals include
only the formats that are turned on. The change names its actual observed interval. When
stored history cannot give the comparison, for example because no observation
is close enough to the period boundary, it shows a dash with the reason. A note
says when YouTube counts more videos than the channel's public lists contain;
it compares list counts only.

Opening Overview or Videos reads the stored catalogue for the selected format.
A new list check, a failed attempt or the expiry of the first loading report
leaves eligible videos visible. The coverage notice tells you when the list is
still being checked or its completeness is unknown. A confirmed missing public
playlist has its own notice. A completed empty playlist has a full check date
and zero listed videos.

**Verify full list** requests a full check of the selected format through the
[discovery collector](#discovery-collector). That collector is not active in
this version, so Library shows "Discovery is paused." and does not queue a check.

The footer gives the profile check time and the last confirmed full check
for each enabled format in the selected time zone. A first-page check or a
failed request cannot advance the full check date. An absence confirmation
has its own date. The header shows a view total only when the format has
current list coverage and the counters needed for the total are available.
Reload reads the application's stored data.

Home **Latest videos** lists Long-form uploads of your visible channels. Each
video's pace ("2× median") is compared with the median of the whole period,
which the server calculates; **Load 20 more** does not change it.

Library has no list of upcoming or live broadcasts.

## Stored values in Library and the workshop

The optional [MCP connection](MCP.md) reads this same catalogue. It preserves
field states, source clocks, completeness and permissions in its responses.
A request for a [turned-off format](CONFIGURATION.md#video-formats) is refused
with `format_turned_off`, as in Library and the workshop.
It stores no results and makes no YouTube calls. Your agent provider may retain
what it receives; the application's cleanup cannot remove that external copy.

Available fields include video metadata and counters, captions, tags, topics,
lifecycle dates and related channel fields. These are existing stored values;
showing a field does not start additional collection. Unknown values differ
from zero, false and empty lists. Expired and unavailable values are explicit.
State conditions let you include missing values; scalar missing values sort last.
Video and channel fields have independent refresh and expiry clocks.

**Data details** explains a field's source and observation time. Video type comes
from recorded playlist membership and the application's conflict resolution,
not duration alone. Details distinguish membership, a manual decision and an
actually applied automatic rule; only the last includes its version and threshold.
Streams must have a recorded actual end. A scheduled time is not evidence of
completion. A live or late broadcast is checked hourly in its first week, so
its end and first counters usually appear about an hour after it finishes; see
[Video refresh intervals](CONFIGURATION.md#video-refresh-intervals). Thumbnail
and avatar references do not guarantee an image exists.

For channels, folders and labels, completeness describes their stored inventory,
not all of YouTube. Collections select only saved video references, not the whole
channel. An unfinished channel scan does not make a collection incomplete when
its saved members are resolved. Missing references or required values can still
make it partial. A channel hidden by you is identified separately from an
unavailable reference. **Require complete data** rejects incomplete results.
Changing source membership, ownership or visibility ends a list or continuation.

A background task prepares searchable values on startup. "Preparing stored
catalogue" retries twice, then offers a retry; continuing collection does not
prevent readiness. Heavy reads run two at a time and otherwise wait in a
bounded queue. Up to two fixed lists can be built at once. Further builds
wait in arrival order, up to 16 waiting requests. The build queue holds
no database connections. A new list has an 8-second wait budget. Once a
build can start, it waits at most 4 seconds for a shared read slot within
that budget. It then has 8 seconds to read and prepare the list. Other
reads have 8 seconds in total, including their wait. Busy or timed-out queries ask you to retry or narrow the selection
rather than show a false empty result. The browser remembers only column and
layout preferences and, while the tab is open, your unsaved canvas; never
YouTube rows.

## Removing a channel

Select the channel and choose **Remove selected channel**. It stops being
collected and moves to **Deleted channels**, where you can restore it for 29 days.
While it waits there, Leti Cook does not ask YouTube about it, including videos
whose stored data expires meanwhile; if you restore it, those videos are
requested again. After the 29 days, routine cleanup permanently deletes its
stored data, every video it collected and its associated saved work, usually
within minutes; collection of your other channels continues meanwhile.
Your unrelated channels and workspace organisation remain.

## A channel YouTube no longer returns

Collection refreshes each tracked channel's profile from YouTube. When YouTube
no longer returns a channel, for example because it was deleted or terminated,
Leti Cook stops showing its stored name, picture and counters at once. Its
videos leave the catalogue as collection finds them unavailable. The channel
stays in your list, and Leti Cook asks YouTube about it again about once a day.

- **Channel list and Home.** The channel is listed last, under any sort, as
  **No longer on YouTube** with its channel ID.
- **Its entry and its page** say so, with the day YouTube first stopped
  returning it, the date of the last check and the date by which its stored
  YouTube data is removed under the 29-day rule. Your folders, labels and saved
  work for it stay.
- **Home** says how many channels are no longer on YouTube and the earliest of
  these dates. **Show them** lists only those channels in the channel list.

The removal date is the latest 29-day deadline among the channel's stored
YouTube data: its country, video counts and history, and its videos' details,
counters, descriptions, history and catalogue entries. Each piece stops being
shown at its own deadline, and cleanup then deletes it; see
[The 29-day window](#the-29-day-window). The stored name, picture and counters
are not counted: they are hidden already and removed by the next cleanup.
Records of Leti Cook's own checks that found nothing follow the same rule from
their own dates.

The first day is the day of the check that first found the channel missing.
Later checks keep it, including checks that fail for another reason, such as a
quota wait or a YouTube error. It is cleared when YouTube returns the channel.

If YouTube returns the channel again, the next check restores its profile and
its usual place in the list. History that was not collected meanwhile cannot be
recreated. To stop tracking the channel, remove it as described above.

## Backing up your work

Use **Backup and restore** to download a JSON backup. Keep a copy before an
update, migration or removal of your installation.

The backup preserves your organised work and saved references. It excludes
YouTube metadata and statistics history, account credentials, sessions, API keys
and installation settings. It is not encrypted and may reveal private research
interests, so store it privately. The maximum supported file size is 16 MiB.

Leti Cook gives each channel, folder, label, saved item, collection and video
type decision a random ID (a version 4 UUID), and the backup keeps these IDs.
An item with any other ID can come only from a change made directly in the
database, for example manual SQL or a data migration. Such an item stops the
backup: nothing is downloaded, and the message names the item, by channel ID
for a channel and by name for a folder, label, saved item or collection, and
counts any others. See [Backup is refused](TROUBLESHOOTING.md#backup-is-refused).

### Restoring

Restore into an empty workspace, including no active or deleted channels,
folders, custom labels, saved views, collections or video-type review history.
Previously imported work also makes a workspace non-empty. For a new
installation, finish setup first: access setup, then the category-reference
preparation, which needs an API key that YouTube accepts, then continue without
selecting starter channels. **Backup and restore** opens only after setup has
finished.

Restoring the file itself makes no YouTube requests. After import, use
**Reconnect all** to collect current data in the background. This uses your API
quota and continues after its reset if necessary. Channels kept in Deleted
channels are not reconnected by that action.

See [Moving to a new installation](INSTALLATION.md#moving-to-a-new-installation).

## Temporary copy during an update

The updater makes an encrypted emergency database copy for rollback on the
same installation. It contains the database, including collected YouTube data;
it does not copy the separate application-state volume. It is not a portable
backup of your work.

The copy is removed after a successful update or rollback. If recovery cannot
finish, it is kept until its deadline and then removed automatically. The
deadline is 24 hours after the copy was made, or 30 days minus one hour after
its oldest YouTube data was collected, whichever comes first. A copy is made
only when all stored YouTube data is at most 29 days old, so data in the copy
can reach an age of 29 days 23 hours. Retrying or restarting does not extend
its deadline. Expired copies cannot be restored.

A video disappearing from a playlist after a complete scan does not by itself
prevent an update. The copy check verifies its earlier type evidence and the
completed scan confirming its absence. A scan awaiting a retry before its first
page uses its recorded failure receipt as evidence of that attempt. Neither
case makes missing data current, changes the catalogue or extends data expiry.
Missing or inconsistent evidence still prevents the recovery copy and update.

Keep the installation and its adjacent `.update` and `.update-keys` folders
together. Follow [update recovery](TROUBLESHOOTING.md#an-update-was-interrupted)
instead of moving or restoring these files yourself.

## Limits of cleanup

The live database is not encrypted by the application. A server administrator
with root access can read its data and secrets. Deleting records or files is
not a guarantee of forensic secure erasure.

Database dumps, copied volumes, disk snapshots and exports you create outside
the application are not managed by its cleanup. Their storage and deletion
remain your responsibility. Use the built-in user-work backup for moving your
research without carrying YouTube statistics history.

## Observations in the workshop

The workshop's field **Views observed near age 24 hours** shows a
real stored view count observed when a video was between 22 and 24 hours old.
It does not estimate the count at exactly 24 hours.

Where the server admits analytics operations, observed change compares the
current counter's matching snapshot with the last snapshot at or before an
hour earlier (up to 30 additional minutes), or a day earlier (up to two
additional hours) when you choose a one-day window. Only videos refreshed at
least that often under the collection schedule are offered: with the default
schedule, up to 7 days old for one hour and up to a year old for one day. The
limit applies whenever a change is used, also when it appears only in a
condition. Each result reports the applied limit, and the workshop's result
blocks state it; older videos are left out of the query instead of showing an
empty change. If the schedule refreshes no video that often, that window is
unavailable and the workshop says so; the other window and the rest of the
study keep working. Average views/hour uses the actual elapsed interval. Negative corrections remain negative. Data details
show source times, metric definition and the reason when evidence is missing.
Library's 28-day growth is a separate measure, described in
[the 29-day window](#the-29-day-window).

Each snapshot records the view definition by the date it was taken. A snapshot
without that record is read with the same date rule. A recorded
definition always wins, and different definitions are never combined. These columns use
already collected data; opening them does not trigger YouTube requests. Results
expire with their input observations.

Observation pages behave like other current pages. Each page is consistent at
its reported evaluation time, and collection may continue while it is read. A
later observation does not invalidate the next page; values shown on later pages
may be newer. Visibility and source membership are checked again before
returning a page. No page is silently restarted.

## YouTube notifications (WebSub)

With [notifications](INSTALLATION.md#youtube-notifications-websub) on, the
database keeps one subscription row per tracked channel (state, lease, next
renewal), each received notification (channel ID, video ID, upload/change or
removal, YouTube's timestamps, what it did) and a journal of subscription
requests, YouTube's confirmations and deliveries (outcomes and timings).
Titles, descriptions and the notification XML are not stored. Callback keys and
signing secrets are derived from a key in the installation's state volume and
are not stored in the database.

A notification records a hint for discovery, refresh or an availability check;
it does not establish the video's type, availability or catalogue readiness.
The callback stores the accepted event before waking the collector. A lost wake
does not erase that event. The backend handles stored notifications by
bringing existing playlist or video work forward; see
[Discovery collector](#discovery-collector). Processing the notification makes
no YouTube request.

Notifications come from YouTube's notification service; later Data API reads
follow the retention rules above. Processed notifications and the subscription
journal are deleted after 90 days. Pending work is not deleted merely because
its discovery or initial loading is unfinished.

The Health page reads only totals from these tables: subscriptions by state,
notifications and subscription requests of the last 24 hours by outcome, the
last delivery time, and whether YouTube asked to pause. No channel, video or
address is read for it.

## Discovery collector

This version has no active discovery collector, and no setting or command turns
one on. Its `[discovery]` settings are checked but change nothing; see
[Playlist intervals](CONFIGURATION.md#playlist-intervals). New videos are found
by the ordinary playlist checks.

An accepted WebSub notification only brings existing work forward. A
notification about a video that is not in the catalogue yet makes the channel's
enabled available playlists due for the next collection run. A missing playlist
with a future assigned check is not brought forward. A notification about a
video already in the catalogue, an edit or a removal, makes that video due for
a refresh. Each such notification is closed when it is handled.

An observed increase in a channel's video count also makes its enabled
playlist heads due, including a previously missing playlist. The one exception
is an increase that the channel's verified subscription announced in full
within the last 24 hours. In that case the first video of a format can wait for
the normal missing-playlist check. The default for that check is one day for
every format; explicitly configured values and already assigned dates are kept.
Disabled formats are not checked. All of this runs through the normal
collector, and the time to discovery depends on when collection can run.

The time attached to a provider result is captured when the provider request
completes, before its collector continuation or database settlement. A complete
metadata response can be reused with that original time and request identity;
reuse does not advance freshness. Backend completion does not prove when a
browser displayed the card, and a large channel's initial load can still delay
visibility.

Request admission is stored separately from the observe-only quota journal.
Restarting does not refill a current budget window. Every automatic full-scan
page uses the full-scan allowance, including scheduled scans and their
continuations; the actual request is recorded once. Manual initial loading
retains its own limits. See [request allowances](CONFIGURATION.md#playlist-intervals).
Due heads can still be read while a full scan waits for its allowance. A head
read uses the same source owner and preserves the full scan's cursor and debt.
It records positive membership only: it cannot finish full coverage or declare
unseen videos absent. Its own due time and retry backoff prevent reading it on
every pass. A full scan's first page also advances the head clock.

Confirmed quota exhaustion blocks only that project's affected bucket until
the next Pacific quota day. If operational quota state cannot be read, sending
pauses and Health shows "Quota state is unavailable". One transient failure of
the read-only admission check may retry once within the original deadline;
writes and provider requests are not retried by this mechanism. An incomplete usage
journal alone does not grant or revoke permission to send.

The backend checks discovery's quota and repair records hourly, including on
installations with WebSub turned off. Each cleanup stage processes up to 200
eligible records. Completed allowance records age out after 90 days. Current
quota-day controls, live allowances, outstanding repairs and their spend are
preserved. Video history has its own expiry rules.

If YouTube returns HTTP 404 with the reason `playlistNotFound` on the first
page of a full Long-form scan, the collector can close a scan that has read
zero pages. A verified first-page absence for the current epoch satisfies the outstanding full-scan request for that generation. The source keeps its configured missing-list recheck. A failure after a page was read cannot establish
that the whole playlist is absent.

A scheduled check of the first page can also confirm absence for the current
channel and format. If a later lookup finds a video in that playlist, the
earlier absence no longer counts. An ambiguous API error or HTTP 500 cannot
confirm absence.

The [discovery support journal](CONFIGURATION.md#telemetry) connects passive
signal, claim, provider, writer, projection, API and browser observations using
private references. It also records waits, unknown outcomes and receipt-based
recovery. Browser entries are client reports, not independent proof of a paint,
current visibility or server-side exposure. Entries can be dropped or rotated;
their absence remains unknown. Collection receipts, freshness and exposure keep
their existing database owners. The pipeline event log is off; quota and
subscription journals retain their separate roles.

### Playlist check report

The collector records the reason and General HTTP cost of a playlist page in its existing receipt. Both Long-form and Shorts use this path, as do enabled Streams. To compare the daily checks and the full reconciliation interval, run this command in the installation's backend container:

```sh
python ops/list_check_report.py
```

The default report covers the preceding seven days. Use `--start` and `--end` with UTC offsets to select a half-open interval of up to 28 days within the 29-day observation window. Every page belongs to one primary reason. A manual request joining a running scan is marked as shared. Signal-driven work, first loads and explicitly targeted work are reported separately from the normal schedule. A receipt without a recorded reason or cost is counted as unknown.

- **Scheduled head** counts IDs first inserted into the catalogue, including how many already had a recorded WebSub notification when discovered.
- **Absent-list recheck** separates repeat absences from newly observed public lists. The appearance gap measures the time between checks. The change could have happened anywhere in that interval.
- **Scheduled full scan** distinguishes first-page and deeper finds, confirmed removals from the list and completed scans with unchanged membership. A failed or partially read list cannot establish a removal.
- **Cost and delay.** Each group reports HTTP attempts, General units and recorded retries. A provider quota refusal counts when sent. A local quota wait sends no request. Zero finds retain their cost and have no cost-per-find value. Delay uses the earliest retained matching notification received before discovery. Missing notifications leave the delay unknown. General units are the installation's local accounting of dispatched requests. Google Cloud Console remains authoritative for project usage. The independent request ledger counts attempts at dispatch, while receipts use the response time. A discrepancy is shown even if a request crosses the window boundary or a receipt is missing.
- **Coverage.** The report includes the number and share of eligible lists with overdue or missing full coverage at report time. Lists with a current confirmation of absence are shown separately. This is a current observation even when the selected period is historical.

To keep comparable weekly totals beyond receipt rotation, run the following in the backend container once per completed UTC week. The default archive covers the previous Monday-to-Monday interval. An explicit UTC midnight `--end` allows weeks anchored to the installation date. Each file contains aggregate counts and timestamps and preserves the first successful export for that interval.

```sh
python ops/list_check_report.py --archive-week /var/lib/leti-cook/list-check-reports
```

Routine collection supplies the evidence. Exporting a report reads PostgreSQL and makes zero requests to YouTube. Several complete weeks of ordinary operation show how the three intervals suit your channels.

## Saved views, queries and studies


Saved keeps four kinds of definitions in one list:

- **Library views**, from **Save view** in a channel's Videos, Shorts or Streams.
- **Queries**: search conditions for one source; they open in the research
  workshop.
- **Canvases**, from **Save** in the research workshop.
- **Studies**: research definitions with one source; they open on the canvas.

Each keeps its source, video format, conditions, sorting and display choices.
A row names its channel, or a study's folder, label or collection, for example
"Folder · Music"; only the kind shows when that source was deleted. A canvas
names its first two sources and counts the rest, and shows Research canvas
instead of a sort.
Opening one reads current permitted data; no result rows or YouTube statistics
are saved in the definition. Source deletion and missing capabilities do not
silently broaden it. Rename and delete use the existing Saved revisions;
a repeated create with the same command ID confirms its original receipt.
An item this version of the page cannot read, for example one saved by a
newer version, stays in the list as a row with its name, if it can be read,
and "This item cannot be opened by this version of the page." It has no Open,
Rename or Delete: reload the page, and if it stays, open Saved in the version
that saved it. The other items and **Load more saved views** keep working.

Opening a Library view first checks its conditions with the server for the
view's format. It opens on its channel's tab of that format with the same
filters. If Library filters cannot show its conditions, or its channel is
unavailable, it opens in the research workshop with the same query. Every
Library condition keeps its Library meaning: title length, tags, like and
comment rates, the 28-day growth filters and the Views gained sort included.
A condition the server cannot run is named and nothing runs; the view is kept.
Dates and years count in your current time zone as calendar dates and years.
When a view's dates were saved in another zone, Saved shows one note before it
opens. Views saved in version 0.1.0 and restored from its backup open the same
way; one whose duration presets belong to Shorts or Streams opens in that tab.

A saved query opens in the research workshop as a study and runs once; a study
opens as it was saved. See [Research workshop](#research-workshop).

Channel definitions bind to the channel's user-work identity during backup/restore,
not to an unrelated channel that happens to be available at import time.

Collections show their videos of all three formats as Library cards and open
the Inspector. Every card in a collection uses the long-video layout, so a
collection that mixes formats keeps one even grid, and collection cards show
no growth. A video that cannot be shown names one reason, for example that
it is scheduled or live, its stream has not finished, or its channel is in
Deleted channels. A video saved before the app tracked it is linked to its
channel once it appears in the channel's list; the collection's next change
stores that link.

User-work backup format 5 includes saved views and queries, research definitions, notes,
layouts and field choices. Restore also reads formats 1 and 2; version 0.1.0
writes format 2. Import restores definitions without acquiring YouTube data,
enabling optional fields, or granting analytics permission. Unknown optional
field IDs are retained as inactive definitions and rejected by execution until
supported. Backups do not include query results, cursor tokens or credentials.

The portable file limit remains 16 MiB. User-work writes reserve 1 MiB for the
portable envelope, browser preferences and restore receipts; the remaining
15 MiB is measured from bounded user-work projections once, when a writing
transaction commits, so a large restore is checked once rather than after every
statement. User edits serialize this admission check so concurrent writes
cannot both use the same remaining space. Ordinary video counters, metadata
updates and collected type-review evidence without a user decision do not
acquire this user-work lock. Existing data is not deleted by this admission rule.

## Choosing collected fields

Collection fields, in the research workshop (`/app-explorer-v3/research?settings=fields`
opens it directly), supports Description and Declared paid promotion. Both are off until you review and apply their collection settings.
Choose a workspace default and, when needed, an override for each video format.
On a narrow screen, such as a phone, the table of fields scrolls sideways inside
its frame; the page keeps the screen's width.
Required identity, type, availability, publication and freshness evidence cannot
be disabled. Other existing fields keep their existing collection behavior.

Preview lists affected saved queries and estimates storage from the actual
catalogue and a bounded sample. The upper bound excludes row/index overhead and
write-ahead logging. Applying settings does not contact YouTube or start a
historical backfill; current values arrive on the normal metadata schedule.
Description shares the existing snippet response and is discarded when off.
The paid-placement response part is omitted unless a format needs it. An absent
flag is unknown; a reported false value means No.

A shown Description or Declared paid promotion column is read only for the rows
on the displayed page, and its values expire on their own clocks. Searching,
filtering or sorting by the field reads it for the whole selection, so in a very
large selection (hundreds of thousands of videos) such a query takes longer.

Off hides the field immediately and prevents later in-flight replies from
storing it under old settings. It does not renew or stop the independent expiry
clock. Retained values expire normally. To remove them sooner, tick the
field's **Delete retained … now in bounded batches** box, available when the
field is off for all formats, and apply the settings. Re-enabling does not
restore missed history. A query using a disabled field stays saved but cannot
run until the field is enabled or removed from that query.

Imported field choices are held for review and never enable collection by
themselves. The supported storage profile remains the public 29-day lifecycle;
unsupported explicit profiles are rejected before schema migration. This is
not an option to disable cleanup or extend permitted retention.

## Channels and selection overviews in the workshop

Choose Channels in a Source node to inspect a channel, folder or label
as a table of reported channel facts. A channel does not need stored videos to
appear. Compare up to four displayed channels side by side; this does not assign
ranks or performance scores. Subscriber counts can be rounded by YouTube. Missing
profiles and expired fields stay unknown. Channel history is a separate source
from video observations.

The Fingerprint and Publishing calendar nodes use the rows of their branch: its
source, format and conditions. Where admitted by the server, Fingerprint shows totals, averages,
quantiles and fixed distributions; Publishing calendar shows dates, hours and
weekdays in the node's time zone. These calculations include all matching current rows before
pagination, not just the displayed page. Long-form, Shorts and completed Streams
use the same modules; live and upcoming broadcasts are not included.
When an observed-change field restricts the selection to videos refreshed often
enough for its window, the summary and calendar report and display that age limit,
including when the field appears only in a condition or an earlier node of the branch.

Unknown values are excluded independently per metric with known/missing counts.
A views aggregate uses a counter only with recorded evidence of its metric
definition: a counter without it is left out and counted, and two different
definitions withhold that aggregate with an explanation; raw counters remain
available. The view-pace median on Home follows the same rule. Channel history
shows real observations, with the last point per UTC bucket and no interpolated
values or inferred growth. All outputs expire with their input data. Enabling
an interface does not grant analytics permissions.

## Research workshop

Open **Research workshop** from the navigation; its address is
`/app-explorer-v3/research`. The workshop is a canvas: nodes connected by wires,
the palette on the left, the selected node's settings on the right and results
below. A new canvas has a Source still to be chosen, feeding Cards; New canvas
starts another one.

Nodes come in four groups, named on every node and in the palette:

- **Sources**: Videos (stored videos of one format: Long-form, Shorts or
  completed Streams) or Channels, from a channel, folder, label or collection
  (collections load 50 at a time, with More and Refresh; channels have no
  collections). Each Source reads its own scope; a canvas can have several.
  Require complete data and the observed change window (1 hour or 1 day,
  offered when a change field is used, with the schedule's age limit) are set
  on the Source and apply to every branch after it.
- **Processing**: Filter (conditions on real fields), Sort (up to three keys;
  missing values last, the ID breaks ties; the last Sort on a branch sets its
  whole order) and Selected items (only, or all but, up to 1,000 picked IDs).
- **Calculations**: Fingerprint and Publishing calendar of the rows, and
  Channel history of one channel, offered only when the server grants them.
- **Views**: Cards (thumbnails and the familiar video facts) and Table (up to 50
  columns you choose); both set their rows per page.

Search in the palette finds nodes and fields: "duration" offers Filter videos by
Duration and Sort videos by Duration. A palette node is added after the selected
node; a Filter, Sort or Selected items node goes between that node and its only
result. Dragging a wire from an output into empty space opens a menu of the
nodes that can follow. The menu takes the focus: the arrow keys, Home and End
move through it, Enter chooses, and Escape closes it and returns to the node.
Without a mouse, Tab to a node and press Enter or Space to select it; **Connect
to…** in its settings lists, by name, the nodes it can feed. A node already fed
by another one says which wire the new one replaces (a node has one input).
Choosing one makes the same wire as dragging it, by the same rules, as one edit
that Undo takes back. A connection that cannot work is refused with its
reason: a view feeds nothing, a source has no input, there are no loops, and
Cards show videos only. Branches are independent: a Filter narrows only the rows
after it.

**Start from a template** in the palette opens a new canvas on the selected (or
first) Source's channel, folder, label or collection and format. Every template
is listed once. Videos: Latest uploads, Most viewed, Thumbnail gallery, Overview
of this selection (fingerprint and publishing calendar, offered only when the
server grants them) and Observations near age 24 hours. Channels: Channel list,
Compare channels and Channel history. Videos or channels: Evidence and notes,
which keeps the Source's rows and starts the notes with a research question.

A node without a path to a source is a draft. Drafts stay on the canvas, are
saved, and are skipped by a run; deleting a node keeps the nodes after it as
drafts. Undo and Redo (Ctrl+Z, and Ctrl+Shift+Z or Ctrl+Y, outside text fields)
step through the edits of this tab; one deletion is one step. Ctrl+D duplicates
the selected node. The mouse wheel scrolls the page; Ctrl + wheel or a pinch
zooms the canvas, and the controls fit the whole canvas into view.

A canvas holds up to 16 nodes and 6 result nodes, and up to 16 KiB (16,384
bytes) including notes and picked IDs. Notes take up to 8,192 bytes: 8,192
Latin letters, or about 4,096 Cyrillic letters, which take 2 bytes each. Below
the notes the canvas shows the bytes they use, and the size of the whole canvas
once it passes three quarters of its limit. An edit that would make the canvas
too large to save is not made: the canvas says how many bytes it would take and
what to shorten. An oversized canvas in tab history still opens; edits
that make it smaller are made, and it saves once it fits.
There are no cycles, scripts or user SQL. **Run** checks
current permissions and enabled fields and reads every connected branch; Stop
cancels outstanding reads. Each read has a bounded statement budget; a run has a
30-second execution budget and a 4 MiB result budget. Every node shows its
state: Ready to run, Running, the rows shown, Changed: run again (after an edit
to its branch), or Error with its reason. A failed node shows its reason rather
than an empty success. Visibility and source membership are checked again
before results return.

Results appear below the canvas, one tab per result node, named by its branch
("Cards after Filter → Sort"). Cards and tables show their read time, Data
details for every row, and Previous / Next pages of their own query. Current
pages are live: updates may move rows between pages. Tick cards (up to 1,000,
kept across pages) and choose **Continue with selected**: a Selected items node
with exactly those IDs is added on the same input, with Cards after it. In a
table, Keep as comparison does the same with the rows you ticked. If the picked
IDs do not fit into the canvas's 16 KiB, nothing is added, and the message next
to the button says how many fit.

A canvas reads current data: each branch is read when it runs, possibly at
slightly different times. **Open in research workshop** in Library opens a
canvas with that channel and format, Library's conditions (a Filter), its sorting
(a Sort) and its columns (Cards or a table), and runs it once. That result shows
Library's own fixed list: the same rows, order and total, marked "Library's fixed
list", with Previous / Next through that list. Any other branch, or this one after
you change its conditions, sorting, columns or page size, reads current data, and
the canvas says so. The list lives up to 10 minutes and only in this tab's memory:
when it expires, or its data or your access changes, the canvas says so and the
next run reads current data; it is never replaced silently, and a canvas never
fixes a list of its own. **Send selected to workshop** takes up to 1,000 selected
videos as a Selected items node and reads them as current data.

**Save** keeps the canvas in Saved: its name, sources, nodes, wires, parameters,
picked IDs, notes and layout. Fetched rows, counters, thumbnails, cursors and
permission grants are never saved. Every Source must name what it reads before
saving; drafts are saved as they are. The top bar shows Saved or Unsaved
changes. Unsaved changes are any change to what Save keeps, the name
included: a new name alone counts (surrounding spaces do not, and an empty name
saves as "Untitled canvas"). Saving again updates the same item. The tab
remembers which saved version its edits started from, so a newer save from
another tab (of the canvas or only of its name) is never overwritten: Save, or
reopening the canvas, says the canvas was saved elsewhere, keeps your changes in
this tab and offers **Save mine as a new canvas** (the other version stays as it
is), **Open the saved version** (in a new history entry; Back returns to your
changes) and **Replace the saved version with mine** (after you confirm). If the
other save holds exactly what this tab holds, nothing is asked. A canvas without
unsaved changes reopens as its newest saved version, name included. A saved
canvas opens by address
(`?saved=<id>`) as it was saved, without running. If a source was deleted, the
canvas still opens and says so; choose another one in that Source node and save.

Studies open on the canvas with the same meaning:
their source becomes a Source node (with its completeness rule and change
window), their conditions a Filter, their sorting a Sort, and their blocks keep
their columns and notes. Saving one creates a new
canvas and keeps the study. A saved catalogue query opens the same way. Library
views open in their channel's Library; see
[Saved views, queries and studies](#saved-views-queries-and-studies).

Each browser history entry keeps its canvas and its name in that tab: Reload,
Back and Forward return to them, unsaved changes included, up to the last edit.
The tab also keeps a second copy in its session storage, written when you leave
the page, so a missing, older or damaged history copy does not lose the work;
if neither copy can be read, the canvas says so and opens without it. New canvas
and a template open a new entry, so Back returns to the previous canvas. Closing
the tab discards unsaved changes, so while a canvas has them (a new empty canvas
does not count), the browser asks before the tab is closed or reloaded. If your
password session ends meanwhile, the page keeps them in the tab and goes to
sign-in; after you sign in you return to the same page, and the canvas says
"Restored your unsaved canvas.". Signing out ends the tab's copies. Results,
cursors and Library's list reference are never kept in the tab.
Export downloads the canvas
definition as JSON, without fetched data. Import opens such a file, or a study
file, on the canvas as one edit that Undo takes back.
Built-in user-work backup includes saved canvases. Collection fields opens from Collection settings below the notes
and from Library's advanced filters (`/app-explorer-v3/research?settings=fields`).

## One-off API acquisition

The owner can ask YouTube for a bounded, one-time answer through HTTP routes
under `/sources/`. The routes require installation owner access. POST routes check the request origin.

| Route | What it does |
| --- | --- |
| `GET /sources/registry` | Lists the YouTube read methods, their fields and support states; `?method=` selects one. |
| `POST /sources/acquisitions/preview` | Checks one request and returns its plan: parts, field mask, pages and worst-case quota cost. Sends nothing to YouTube and spends no quota. |
| `POST /sources/acquisitions/execute` | Runs the previewed request. It needs the `X-Saved-Work: 1` header, the preview's plan fingerprint and its worst-case cost as the confirmed cost. |
| `GET /sources/acquisitions/status` | Shows today's use of the daily caps and the limits. |

Limits:

- One acquisition runs at a time.
- One acquisition may use up to 10 requests, 10 General units, 10 Batch
  statistics units, 5 calls of YouTube's Search queries bucket, 5,000 rows,
  2 MiB per response and 60 seconds. A request can lower these limits, not
  raise them.
- All acquisitions together may use 500 General units and 100 Search queries
  calls per Pacific quota day, so collection keeps the rest of your quota.
  Beyond that, an acquisition is refused with `daily_cap_reached` until midnight
  Pacific time. If today's use cannot be read, acquisitions are refused.
- Video counters come only from YouTube's batch statistics method, never from
  video details.
- A continuation is valid for 10 minutes after the next request becomes eligible.
  Live Chat returns the waiting time requested by YouTube, rounded up to seconds.
  Restarting the server invalidates continuations.
- For `search.list`, an explicit `maxResults: 0` requests a single page with no hits.
  When YouTube returns `pageInfo.totalResults`, the page includes `estimated_total_results`.
  Find channels requests pages of videos or channels.

Nothing is stored. The answer exists only in the HTTP response; it is not cached,
written to the database or logged. Each request to YouTube is recorded in the
local quota journal, like collection's requests, and spends the same API key's
quota. Closing the connection cancels the acquisition. An acquisition is refused
while the app is not ready to contact YouTube, for example during an update.

Methods that need the channel owner's Google sign-in (for example captions,
memberships, live broadcasts, YouTube Analytics and Reporting) answer 409
`owner_auth_unavailable`, because this version has no such sign-in. Methods the
registry marks as not executable answer `method_not_executable`. The acquisition preview returns the estimated quota cost before execution.

## Find channels

In the Explorer navigation, **Find channels** searches public YouTube videos by topic or video category, or channels by keywords. The date filter refers to video publication in UTC. Each click requests one page of up to 50 hits. The screen shows the original YouTube results in order and groups the same hits by author in a separate list. Subscriber and view thresholds filter only the loaded results. Hidden or missing counts are shown as unknown and cannot satisfy a numeric threshold.

| Owner action | Maximum Search | Maximum General | Maximum Stats |
| --- | ---: | ---: | ---: |
| Open, type, filter, or preview cost | 0 | 0 | 0 |
| Find a page or load the next page | 1 | 1 | 1 when video views are selected |
| Retry missing details | 0 | 1 if profiles are missing | 1 if video statistics are missing |

Search uses the installation key. The local allowance covers this installation's one-off requests. The Google project may also be used by other clients. A result alone never enrolls a channel in collection. **Add to Library** and **Restore to Library** use the normal channel-management operation, which has its own acquisition costs.

`POST /sources/channel-search/preview` takes `{"mode":"topic","q":"miniatures","stats":true}`, or a previous action ID and `"action":"more"` or `"retry"`. It returns `action_id` and `max_cost`. For a direct channel search use `mode:"channel"`, a query and `stats:false`. Category search takes a numeric category ID as text instead of a query. Video searches accept an optional RFC 3339 `after` date-time. Execute at `POST /sources/channel-search/execute` with the same action ID, `confirmed_cost` equal to the quoted cost, and header `X-Saved-Work: 1`. The response includes hits, profile fields, view counts, Library membership, expiry and actual attempts by step. Repeating a completed action returns its receipt. If the response to the first search is interrupted, the tab keeps the action ID and approved cost, including across a reload. Resume uses that same action. If its outcome cannot be recovered or its ticket expired, discard it before explicitly starting a new paid search. Closing the tab ends browser recovery. The browser saves no search terms or YouTube results for recovery. Expired actions, a restart, and owner changes require a new preview. Results expire after ten minutes from the first preview. Search is available to the installation owner.

## Not available yet

- Signing in as the channel owner with a Google account. Owner-only YouTube
  methods, YouTube Analytics and YouTube Reporting therefore cannot run.
- A general-purpose screen for all one-off acquisition methods, and the streamed live chat method.
- A list of upcoming and live broadcasts in Library.
- The Inspector's launch comparison on a standard installation.
