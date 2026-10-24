# Security

Leti Cook is intended for one owner on a machine they control. It is not a
public or multi-user hosting service.

## Access

- Keep the app on its configured `127.0.0.1` address. Use an SSH tunnel for
  access from another computer; do not expose it through a public proxy.
- Choose a password account on a shared server. Without an account, anyone who
  can reach the address has full access, including other server users and
  people with access to its tunnels.
- The access mode is chosen once during setup. A setup code lasts 15 minutes
  and does not reset an existing password.
- PostgreSQL has no published host port. Each installation uses its own
  database and application state.

## Protecting your data and credentials

Keep `installation.env`, `backend.env`, setup codes and API keys private. The
generated environment files are readable only by root. Passwords are stored as
salted hashes; the API key is encrypted in local application state.

The server's root administrator can still read all data and secrets. The live
database is not encrypted by the application. User-work backups contain private
research information and are not encrypted.

If an API key is exposed, revoke it in Google Cloud and replace it with
`manage.py set-key`. If database credentials in the generated environment files
are exposed, there is no built-in rotation command: move your work to a separate
new installation, verify it, then remove the old one. See
[moving an installation](docs/INSTALLATION.md#moving-to-a-new-installation).

Data cleanup applies to the running installation, not to external dumps or disk
snapshots. It is not a guarantee of secure erasure. See [Data and backups](docs/DATA.md).

## Release signatures

Signed installation and update packages are checked against the trusted public
key already held by the installer or installation. Missing or invalid signatures
stop the operation. Do not bypass these checks.

Obtain the first installer from a trusted project source before running it with
`sudo`. Its embedded key cannot establish trust in that initial download.
Signatures do not protect against full compromise of the publisher's account or
signing process. If compromise is suspected, stop installation and updates until
the installer and key can be verified independently with the maintainer.

## Browser extension

Install the optional extension only from trusted files. It accesses YouTube and
the local app address you configure, using the app's existing session.
Chromium's host permission can cover multiple ports, so review the permission
prompt. The extension validates the exact configured app address.

It stores no password, session cookie or YouTube API key and sends no telemetry.
Checking whether a channel is already tracked does not add it or make YouTube
API requests. Channel addition requires a click. The separately downloaded
extension is not verified or installed by the backend updater.

## Reporting a vulnerability

Do not post exploit details, credentials, private logs or database dumps in
public issues or pull requests.

Use **Security → Advisories → Report a vulnerability** on GitHub when that
option is available. Otherwise, ask the maintainer for a private reporting
channel without posting sensitive details. The GitHub `noreply` commit address
is not a contact inbox.
