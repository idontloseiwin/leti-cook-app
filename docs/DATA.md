# Data and backups

Your organised work and collected YouTube data have different lifetimes.

## What is stored

| Data | Retention |
| --- | --- |
| Your channel list, folders, labels, saved views, collections and video-type decisions | Until you delete them; included in user-work backups |
| YouTube profiles, video details, current counters and playlist contents | Refreshed or removed within a rolling 29-day window |
| Historical statistics snapshots | Removed 29 days after collection |
| Category reference, collection runs, API request records and job details | 29 days; summary job rows remain |
| Account, sessions, API key and setup state | Stored locally in the installation |

The application and database run on your server. Collection contacts YouTube;
installation and updates can download files from GitHub and container registries.
These operations do not upload your database or user-work backup.

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
  days before that one is still kept; the comparison then spans 28 to 29 days.
  Otherwise the actual number of tracked days is shown.
- **Expired fields are hidden.** They stop appearing even if cleanup has not
  yet removed them. The main Explorer requests current data automatically;
  if that fails, it offers a reload.
- **Your work remains.** Folders, labels, saved references and your edits are
  retained, subject to permanent channel deletion below.

No cleanup can run while the server is off. After a long shutdown, the app
cleans up expired data before normal access to YouTube data resumes. Collection
may need time and quota to refill missing details.

## Catalogue coverage

YouTube may omit unavailable, private, members-only or unlisted videos from
public lists or responses. For channels reporting more than 20,000 videos, the
app warns that uploads lists may contain only the latest 20,000 uploads; see
[the related YouTube issue](https://issuetracker.google.com/issues/166292064).
A channel's reported video count therefore need not match your collected catalogue.
This coverage warning is separate from your configurable limits per collection pass.

Records without current type evidence can remain as saved IDs while being absent
from a typed catalogue. Missing details do not necessarily mean your saved work
was deleted. Check collection status and quota before removing and re-adding
a channel.

## Experimental folder catalogue

Development builds include a separate preview at `/app-explorer-v3/research`.
It is off by default. For an API process you run directly during development,
set `LIC_RESEARCH_QUERY_PREVIEW=1` in that process's environment before starting
it. Remove the variable and restart that process to disable the preview.
Owner sign-in and the usual data-read checks still apply.

The managed Docker installation does not yet provide a supported preview switch.
Do not add this variable to its generated `backend.env` or `config.ini`: those
files are validated by the updater. A Docker container restart does not load
changes to its environment. The preview switch is not an agent credential.

Choose a folder, one format, title text, an optional minimum view count and a
sort order, then select **Refresh**. The table returns 25 stored videos per page.
Long-form, Shorts and completed Streams use the same query rules. Type comes
from recorded YouTube playlist membership and the application's conflict
resolution; a short duration alone does not make a video a Short.

Unknown, expired and unavailable values are shown explicitly and sort last.
Coverage describes the channels' stored inventory, not all videos on YouTube.
**Next page** continues a live selection: updates can move rows between pages.
A continuation expires within two minutes, possibly sooner when a source field
expires. An expired page is cleared; select **Refresh** to start again. Switching
filters or format also clears the previous result immediately.

Every page is computed from the whole folder. A background task prepares stored
type checks, duration and title search keys to speed up these reads. On startup,
the preview can show **Preparing the catalogue** until that task has caught up
with the changes waiting at startup; videos collected meanwhile do not hold it
back. Leave the server running; the page retries twice, then offers **Refresh**.
A failed preparation step is retried within seconds. Only repeated failures are
shown as unavailable, never as an empty list.

After initial preparation, changed videos are checked directly against stored
source data until their prepared values catch up. Results stay current, but
pages can be slower. Heavy catalogue reads wait in a limited queue; a busy
response asks you to try again shortly. A query that exceeds its time limit
asks you to narrow the selection. Neither condition returns a partial page as
if the query had succeeded.

The preview reads the local database without requesting YouTube data or changing
collection schedules. It requires the installation's existing owner access and
data-read readiness. Existing channel pages remain available. Saving queries,
gallery views and the complete Shorts/Streams overview are not part of this preview.

## Removing a channel

Select the channel and choose **Remove selected channel**. It stops being
collected and moves to **Deleted channels**, where you can restore it for 29 days.
After that, routine cleanup permanently deletes its stored data, every video it
collected and its associated saved work, usually within minutes; collection of
your other channels continues meanwhile.
Your unrelated channels and workspace organisation remain.

## Backing up your work

Use **Backup and restore** to download a JSON backup. Keep a copy before an
update, migration or removal of your installation.

The backup preserves your organised work and saved references. It excludes
YouTube metadata and statistics history, account credentials, sessions, API keys
and installation settings. It is not encrypted and may reveal private research
interests, so store it privately. The maximum supported file size is 16 MiB.

### Restoring

Restore into an empty workspace, including no active or deleted channels,
folders, custom labels, saved views, collections or video-type review history.
Previously imported work also makes a workspace non-empty. For a new
installation, finish access setup without selecting starter channels.

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
finish, it is available for at most 24 hours, and less when its oldest API data
is close to expiry. Retrying or restarting does not extend its deadline.
Expired copies cannot be restored.

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
