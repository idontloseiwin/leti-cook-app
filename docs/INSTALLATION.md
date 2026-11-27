# Installation

Leti Cook runs on your Linux machine with Docker and a private PostgreSQL
database. You need your own YouTube Data API key.

Leti Cook is in active development. No releases have been published. Install
[from source](#from-source-development).

## Requirements

- Linux with `sudo`, Python 3.10 or newer, and OpenSSL 3.
- Docker Engine 28.3.3 or newer and Docker Compose 2.24.4 or newer. On older
  Docker Engines, other computers on the local network can reach ports
  published on `127.0.0.1`, so the installer and `manage.py update` refuse
  them. Other `manage.py` commands still run; most print a warning.
- A systemd host with automatic clock synchronisation enabled.
- Disk space and memory for the application, database and your collected data.
- Outbound HTTPS for YouTube, GitHub, container images and build dependencies.

The installer checks the host but does not install missing system packages.
Check the clock with `timedatectl`; it should report
`System clock synchronized: yes`. See
[clock troubleshooting](TROUBLESHOOTING.md#the-server-clock) if it does not.

### Getting a YouTube API key

In [Google Cloud Console](https://console.cloud.google.com/), create or select a
project, enable **YouTube Data API v3**, then create an API key under
**Credentials**. Restrict it to YouTube Data API v3. You can also restrict it to
your server's public IP address. Keep the key private.

## Install

### From source (development)

Clone the repository, then run the installer from that checkout:

```bash
git clone https://github.com/idontloseiwin/leti-cook-app.git
cd leti-cook-app
sudo bash install.sh
```

This builds the images and creates a separate installation. Running it again
does not update an existing installation. Without `sudo` it stops before the
first question with `root_required_run_with_sudo`.

### Folder, port and setup code

The installer asks for a new folder, a port and your API key. The suggested
folder is `/opt/leti-cook`; the port is the first available one from 8002.
Press Enter to accept a suggestion. The key prompt hides your input.

The folder must not already exist. Its parent directories must belong to root
and must not be writable by other users or groups. Symlinks and shared temporary
folders are not accepted. The adjacent `.update` and `.update-keys` folders must
also be absent. Existing installations are never overwritten.

At the end, the installer prints a browser address and a one-time setup code,
valid for 15 minutes. If it expires before you finish access setup:

```bash
sudo python3 /opt/leti-cook/manage.py setup-code
```

Use your installation folder in place of `/opt/leti-cook` in every command.

## First run

Open the printed `/auth` address and enter the setup code. Choose either:

- **Create an account:** a username and a passphrase of 15–128 characters.
- **Continue without an account:** anyone who can reach the application has full
  access, including other users of the server or its SSH tunnels.

Choose an account on a shared server. The access mode cannot be changed later,
and `setup-code` does not reset an existing password.

Setup prepares the YouTube category reference, then offers optional starter
channels. Select any or none. Initial loading continues in the background while
the server runs; you can close the browser and return to `/setup`. Follow any
quota or large-channel confirmation shown there.

### Using the workspace

Open **Library → Add channel** and paste a channel link, handle, channel ID or
video link. Loading runs in the background. Large channels may need confirmation.

**Find channels** shows the maximum cost before each search page. YouTube may
return an empty page with more results available. Choose **Load next page** to
continue. An empty page uses one Search call and makes no requests for channel
profiles or video statistics.

Open a channel to explore its [Library](DATA.md#channel-library). Its
**Videos**, **Shorts** and **Streams** tabs each have an **Overview** and a
**Videos** grid with filters, selection, Save view and the Inspector.
**Research workshop** in the navigation is a canvas of connected nodes: sources
(channels, folders, labels or collections), filters, sorting, picked items,
cards, tables and calculations; Library can open its list there. Library and the
workshop are always on; [nothing needs enabling](CONFIGURATION.md#library-and-research-workshop).
Summaries, the publication calendar and channel history are part of the base
analytics that a new installation enables.

Use **Folders and labels** to organise channels, **Saved** for existing saved
views and collections, and **Health** for collection and quota notices.

Use **Backup and restore** to save your organised work. The optional
[browser extension](../extensions/chromium/README.md) adds channels from YouTube.

## Opening it from another computer

The app listens on the server's `127.0.0.1` only. Open an SSH tunnel from your
computer, using the installation's port on both sides:

```bash
ssh -N -L 127.0.0.1:8002:127.0.0.1:8002 user@server
```

Keep the tunnel open and visit `http://127.0.0.1:8002/auth`. Other host names or
ports are refused. Public internet hosting is not supported by this installer.

## YouTube notifications (WebSub)

Optional. YouTube can notify the installation when a tracked channel uploads a
video or changes a video's title or description. Accepted notifications are
stored as hints for collection. A hint only makes existing work due earlier: a
new video makes the channel's playlists due for the next collection run, and a
known video becomes due for a refresh. See
[YouTube notifications in Data and backups](DATA.md#youtube-notifications-websub).
Notifications themselves use no YouTube API quota; fetching the video's data
does. Normal playlist checks continue independently, and there is no fixed
promise of how soon a notification becomes a visible video.

YouTube must reach the installation over HTTPS from the internet, so this needs
a public HTTPS address that you control, for example a Cloudflare Tunnel or a
reverse proxy with a certificate. Forward **only** the `/websub/` path and keep
everything else private. Example for nginx behind such an address, with this
installation on port 8002 and `hooks.example.com` as the public name:

```nginx
location ~ "^/leti-cook(?<websub_path>/websub/UC[A-Za-z0-9_-]{22}/[A-Za-z0-9_-]{43})$" {
    if ($request_method !~ ^(?:GET|POST)$) { return 405; }
    access_log off;
    client_max_body_size 256k;
    proxy_set_header Host hooks.example.com;
    proxy_pass http://127.0.0.1:8002$websub_path$is_args$args;
}
location /leti-cook/ { return 404; }
```

Keep the named capture: a regular expression inside `if` resets numbered
captures such as `$1`, and nginx then answers 500. The forwarded Host must be
the public name. The installation answers only the
exact `/websub/<channel>/<key>` paths for that name and refuses everything else
from it before owner access. Then set in `config/config.ini`:

```ini
websub_callback_url = https://hooks.example.com/leti-cook
```

and run `manage.py restart`. The Health page shows subscriptions, the last
notification and what notifications did. Subscriptions are requested one at a
time with the pauses YouTube requests. The first run starts with one channel,
then ten, then the rest as confirmations arrive. If YouTube explicitly denies
a subscription, another channel can take its place. The denied channel can be
requested again after 24 hours with a fresh callback address. Removing the
setting and restarting stops new requests;
existing subscriptions expire on their own within about 10 days.

## Updating an installation

No releases have been published, so there are no published updates to install.
The built-in updater uses signed release packages; it does not update from Git
commits. Pulling this repository or rerunning `install.sh` does not update an
existing installation. A release package contains the built web app; the web
app's source for the same version is in the project repository.

The commands below are a reference for signed-package updates. Download a
[backup of your work](DATA.md#backing-up-your-work) before applying a package.

```bash
sudo python3 /opt/leti-cook/manage.py update --check
sudo python3 /opt/leti-cook/manage.py update
```

For prereleases, add `--pre` to both commands. The updater checks the download,
compatibility and the current installation before applying it. Owner access,
the API key and supported configuration settings are preserved. Your explicit
settings are not replaced by new defaults.

A release can add a setting to `config/config.ini`. Your file is kept as it is,
without the new line, and the setting's default applies; add the line after the
update if you want another value. Do not add it before updating: the installed updater
rejects unknown keys.

The application is briefly unavailable during an update. A temporary recovery
copy supports rollback if it fails. That copy expires and is not a substitute
for your own backup. Do not edit or move installation files during an update.

To use a downloaded signed package, keep its archive, `release.json`,
`SHA256SUMS` and `SHA256SUMS.sig` together, then add
`--package /absolute/path/to/archive.tar.gz` to both commands. This also selects
a specific version. Downgrades and incompatible versions are refused.

An update normally downloads the release's published images, checked by their
digests. With `--build-local`, `manage.py update` builds the database and
application images on this server from the verified package files instead.
Use it when the published images cannot be downloaded, or when you want images
built on your own server. The build still downloads base images and packages,
and takes longer. A server that is not amd64 always builds its images this way.

Check the installed version with:

```bash
sudo python3 /opt/leti-cook/manage.py --version
```

If an update stops, follow [update recovery](TROUBLESHOOTING.md#an-update-was-interrupted).
Do not bypass a refusal by replacing files manually.

### Coming from version 0.1.0

Version 0.2.0 starts a new database history, so an installation of version
0.1.0 (schema 38) cannot be updated to it. The updater of a 0.1.0 installation
refuses the 0.2.0 package during its checks, before it stops anything, with
`update_compatibility_unsupported`. The 0.1.0 installation, its data and its
containers stay as they were. Pulling the repository or rerunning `install.sh`
does not update it either.

To keep your work, move it to a new 0.3.0 installation:

1. In the 0.1.0 installation, open **Backup and restore** and download a
   user-work backup. Version 0.1.0 writes backup format 2; version 0.3.0 reads
   it.
2. [Install](#install) version 0.3.0 into a new folder. The folder of the 0.1.0
   installation cannot be reused, and the installer suggests the next free port.
3. Restore the backup into the new, empty workspace as described in
   [Moving to a new installation](#moving-to-a-new-installation), then use
   **Reconnect all**.
4. Check your work there, then remove the 0.1.0 installation with its own
   `manage.py uninstall`.

The backup carries your organised work only. YouTube statistics history
collected by 0.1.0 is not carried over: the new installation collects current
data again from the time you reconnect the channels.

## Moving to a new installation

Download a user-work backup, then create a separate installation. The new
installation needs a YouTube API key that YouTube accepts before the backup can
be restored. A backup is restored only after setup has finished, and setup
prepares the YouTube category reference with that key, which spends a small
part of the [daily quota](CONFIGURATION.md#youtube-api-quota). Until then the
app opens only its setup page, so **Backup and restore** is not available. If
YouTube refuses the key, replace it with `manage.py set-key`. The new
installation may use the same key as the old one; until you remove the old
one, both spend its daily quota.

Complete access setup and the preparation, continue without starter channels,
and restore the backup into the empty workspace. Use **Reconnect all** to
collect current YouTube data again. Verify your work there before removing the
old installation. Format 6 includes your alert rules and their pause settings.
Restored rules start a new observation period. A rule whose source is missing
stays visible in Alerts. The importer also reads formats 1, 2 and 5.

The backup does not transfer account credentials, the API key, configuration or
YouTube statistics history. See [Data and backups](DATA.md).

## Uninstalling

Download your backup before running:

```bash
sudo python3 /opt/leti-cook/manage.py uninstall
```

The command asks you to type this installation's project name. Confirmation
permanently removes its containers, database, application state, recovery copy
and installed files. This cannot be undone. Other installations and shared
container images are retained. Save the recovery command it prints in case
removal is interrupted.

An installation that did not finish is removed with the same command; see
[The installer stopped](TROUBLESHOOTING.md#the-installer-stopped).
