# Installation

Leti Cook runs on your Linux machine with Docker and a private PostgreSQL
database. You need your own YouTube Data API key.

Signed public downloads are not available yet. Use
[installation from source](#from-source-development) for now.

## Requirements

- Linux with `sudo`, Python 3.10 or newer, and OpenSSL 3.
- Docker Engine and Docker Compose 2.24.4 or newer.
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

From a trusted checkout, run `sudo bash install.sh`. Once this repository is
public, you can obtain that checkout with:

```bash
git clone https://github.com/idontloseiwin/leti-cook-app.git
cd leti-cook-app
sudo bash install.sh
```

This builds the images and creates a separate installation. Running it again
does not update an existing installation.

### Signed downloads

When published downloads are available, obtain `install-release.py` from the
[project's releases](https://github.com/idontloseiwin/leti-cook-app/releases).
Only run an installer you trust, especially with `sudo`.

```bash
python3 ./install-release.py --check
sudo python3 ./install-release.py
```

The default selects the latest stable version. Add `--pre` to both commands for
a prerelease. Use `--version VERSION` to select a specific published version;
prereleases also require `--pre`. Use `--build-local` to build verified source
locally. Other CPU architectures than amd64 use a local build automatically.

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

Open a channel's **Overview** and **Videos** to explore Long-form videos. Shorts
and Streams are also collected, but their dedicated interface tabs are not yet
available. Use **Folders and labels** to organise channels, **Saved** for saved
views and collections, and **Health** to check collection and quota notices.

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

## Updating an installation

Download a [backup of your work](DATA.md#backing-up-your-work) first. Use the
installed management command; you do not need to clone or pull the repository.

```bash
sudo python3 /opt/leti-cook/manage.py update --check
sudo python3 /opt/leti-cook/manage.py update
```

For prereleases, add `--pre` to both commands. The updater checks the download,
compatibility and the current installation before applying it. Owner access,
the API key and supported configuration settings are preserved. Your explicit
settings are not replaced by new defaults.

The application is briefly unavailable during an update. A temporary recovery
copy supports rollback if it fails. That copy expires and is not a substitute
for your own backup. Do not edit or move installation files during an update.

To use a downloaded signed package, keep its archive, `release.json`,
`SHA256SUMS` and `SHA256SUMS.sig` together, then add
`--package /absolute/path/to/archive.tar.gz` to both commands. This also selects
a specific version. Downgrades and incompatible versions are refused.

Check the installed version with:

```bash
sudo python3 /opt/leti-cook/manage.py --version
```

If an update stops, follow [update recovery](TROUBLESHOOTING.md#an-update-was-interrupted).
Do not bypass a refusal by replacing files manually.

## Moving to a new installation

Download a user-work backup, create a separate installation, complete its access
setup without starter channels, and restore the backup into the empty workspace.
Use **Reconnect all** to collect current YouTube data again. Verify your work
there before removing the old installation.

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
