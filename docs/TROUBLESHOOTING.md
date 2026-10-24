# Troubleshooting

Start with **Health** for collection and quota notices, and **Telemetry** for
recent request failures. A past error in Sync history does not by itself mean
that the same problem is still occurring.

Commands below use `/opt/leti-cook`; substitute your installation folder.

## The page will not open

Use the exact `http://127.0.0.1:PORT` address printed by the installer. If the app
is on another computer, keep its [SSH tunnel](INSTALLATION.md#opening-it-from-another-computer)
open with the same port on both sides. Check that the server and Docker are running.

`invalid_host` means the address or port does not match the installation. Use
its configured loopback address rather than a custom hostname or public proxy.

If the page opens but data does not load, check Health and Telemetry, then retry
the failed view. Do not delete the database or re-add channels to fix a page error.

## Setup code or sign-in

Setup codes expire after 15 minutes. Before access setup is complete, request
a new one:

```bash
sudo python3 /opt/leti-cook/manage.py setup-code
```

After access setup, open `/auth` and use the mode you chose. The setup-code
command does not reset a password or change that mode.

### Sign-in answers `rate_limited`

Sign-in allows five attempts per minute, including successful attempts. Wait
one minute before retrying. If needed, the server owner can
clear the temporary block:

```bash
sudo python3 /opt/leti-cook/manage.py clear-sign-in-limit
```

## Collection has stopped

| Notice or symptom | What to do |
| --- | --- |
| Collection paused | Set `automatic_collection = true` in `config/config.ini` when ready. Cleanup continues while paused. |
| API key refused | Check the key's API and IP restrictions in Google Cloud, then run `manage.py set-key`. |
| Quota exhausted | Wait for midnight Pacific time. Follow any **Continue loading** action shown during setup. |
| Invalid settings or `scheduler_unavailable` | Run `manage.py check-settings`, correct the indicated file and validate again. |
| Large-channel confirmation | Review and confirm the estimated load in the app. |
| Worker failure or repeated timeouts | Record the affected run and error from Health/Telemetry; report it if it persists. |

A timed-out or partial pass can still have completed useful work. Its completed
updates remain; the status does not mean the whole pass was discarded.

See [Configuration](CONFIGURATION.md) for collection controls and quota limits.

### Understanding a partial or slow sync

Expand a run in **Telemetry → Sync history** to see its recorded reasons and
phase durations, including time spent waiting within each phase. Older runs
may only have a total duration; missing measurements cannot be reconstructed.

A missing statistics response can require a later metadata check. It does not
by itself mean a video was deleted. If the metadata response also omits the
video, the app can record it as unavailable via the API, without knowing why.
The requested retry delay is not a guarantee that the next sync starts then.

Search **Event log** by Video ID to inspect retained observations and their
run IDs, endpoints and write acknowledgements. The browser sample and diagnostic
journal are bounded; an absent event is not proof that a check did not happen.
Run history describes past work, not the video's current availability.

## Data maintenance status

Startup cleanup can delay access after downtime. Leave the server running and
check Health for progress. Pausing collection does not pause cleanup.

If the app reports database unavailability, a schema error or maintenance
failure, retain the error code and installed version. Check for an interrupted
update below. Do not change the schema or delete data manually to clear a notice.

## The server clock

Collection and expiry depend on an accurate clock. Check synchronisation:

```bash
timedatectl
sudo timedatectl set-ntp true
```

Wait until `System clock synchronized: yes`. If NTP is unsupported, install or
enable a time service such as systemd-timesyncd or chrony.

A confirmed clock mismatch pauses collection. A notice that the external clock
check could not complete is different: verify outbound HTTPS and local time
synchronisation. Do not manually shift the clock to change data expiry.

## Missing videos or statistics

Check whether initial loading has finished, quota is available and the channel
is active. Public YouTube lists do not necessarily include every video shown
in a channel's reported total. Videos can also become unavailable or lack
current type evidence.

Details disappear when their API data expires. Fresh collection can restore
current details, but it cannot recreate expired statistics history. New
installations and recently added channels need time to accumulate observations.
See [Data and backups](DATA.md).

## The installer stopped

Keep its `stage` and `reason` codes. Correct the reported requirement, such as
Docker, clock synchronisation, permissions, disk space or network access.
Installation does not reuse an existing database or overwrite a partial folder;
retry with a new folder after resolving the cause.

For `docker_network_address_pool_exhausted`, ask the host administrator to review
unused networks or Docker address-pool settings. Do not remove networks used by
other applications. Use the installation's uninstall command to remove an
unwanted partial installation.

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
| Disk, image download or build failure | Resolve the host or network problem, then retry the management command. |
| Expired API data blocks a recovery copy | Let normal cleanup finish and retry. If cleanup fails, resolve that failure first. |

## An update was interrupted

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

If uninstall was interrupted, use the recovery command printed before removal.

## Reporting a problem

Include the installed version, operating system, Docker/Compose versions,
steps to reproduce, relevant error codes and when the problem occurred. A
screenshot of the affected view is useful. Remove private research details.

Never publish API keys, setup codes, passwords, environment files or database
dumps. Use [Security](../SECURITY.md#reporting-a-vulnerability) for sensitive reports.
