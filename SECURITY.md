# Security

Leti Cook is intended for one owner on a machine they control. It is not a
public or multi-user hosting service.

## Access

- Keep the app on its configured `127.0.0.1` address. Use an SSH tunnel for
  access from another computer; do not expose it through a public proxy.
- Docker Engine older than 28.3.3 lets other computers on the local network
  reach ports published on `127.0.0.1`. The installer and updates refuse such
  an Engine. Docker's `DOCKER_INSECURE_NO_IPTABLES_RAW=1` setting removes the
  same protection on newer Engines and cannot be detected by the installer;
  do not set it.
- Choose a password account on a shared server. Without an account, anyone who
  can reach the address has full access, including other server users and
  people with access to its tunnels.
- **Known limitation, not fixed yet: the sign-in cookie is shared with other
  local sites.** Browsers do not separate cookies by port. While you are signed
  in to `http://127.0.0.1:<port>`, or are completing access setup there, your
  browser also sends the Leti Cook session cookie to every other site it opens
  on `127.0.0.1` with another port: a development server, a notebook server,
  another self-hosted program or another SSH tunnel. Such a site can use Leti
  Cook as you until the session ends, after 30 minutes without requests or at
  most 12 hours after sign-in. Until this is fixed, do not open untrusted web
  servers or tunnels on `127.0.0.1` in the browser profile you use for Leti
  Cook while you are signed in. Use a separate browser profile for Leti Cook,
  or **Sign out** first. A separate address for each installation, so that
  its cookie is not shared, is planned.
- The access mode is chosen once during setup. A setup code lasts 15 minutes
  and does not reset an existing password. Setup codes are not rate limited:
  each is a random 256-bit code, a wrong code is refused at once, and wrong
  codes never delay the right one.
- Sign-in allows five wrong passwords in a row. After that each further try
  waits longer, from 1 second doubling up to 5 minutes; a try sent during the
  wait is refused at once, without checking the password. An hour without tries
  starts the count again. No request is held on the server while it waits.
  The refusal names the wait, and the next try is checked once that time has
  passed; setting the server clock back while the app runs neither shortens
  nor lengthens it.
- A browser that signed in during the last 30 days keeps a signed mark that
  only sign-in reads. It has its own count, so wrong passwords sent from
  anywhere else never make it wait. The mark opens nothing by itself, holds no
  name or password, stays after you sign out and stops working when the
  password is reset.
- Browsers without that mark (a new browser, a private window, cleared
  cookies) share one count with everyone who reaches the port: while someone
  keeps sending wrong passwords, they wait up to 5 minutes between tries. Sign
  in from a browser you used before. `manage.py clear-sign-in-limit` restarts
  the shared count but does not stop the sender. The app cannot tell callers
  apart by address: requests through the published port can all arrive from
  the same one.
- A flood of requests to the port slows every page, sign-in included, while it
  lasts; it leaves no lock behind.
- With an account, a session ends after 30 minutes without use and always 12
  hours after sign-in. Setting the server clock back while the app runs neither
  ends nor lengthens sessions, including for a request that was already waiting
  when the clock changed; setting it forward can end them sooner. A clock set
  back while the app was stopped can lengthen sessions, and a sign-in wait
  already running, by up to that change.
- PostgreSQL has no published host port. Each installation uses its own
  database and application state.
- On the installation's internal network, only the application's own database
  login can connect, and only to its database. The database superuser can sign
  in only from inside the database container. Updates apply this to existing
  installations.
- The app serves no interactive API documentation or OpenAPI schema
  (`/docs`, `/redoc`, `/openapi.json`).

## Protecting your data and credentials

Keep `installation.env`, `backend.env`, setup codes and API keys private. The
generated environment files are readable only by root. Passwords are stored as
salted hashes.

The YouTube API key is stored in an encrypted settings file in the
application-state volume, but the key that decrypts it lies in the same folder.
Anyone who can read that volume can read the API key, so the encryption does
not protect it there. What protects it are file permissions: the folder and
both files are readable only by the application's unprivileged user inside the
container, and on the server only by root and anyone allowed to run Docker.
User-work backups and the update's emergency copy, which holds only the
database, do not include these files.

The server's root administrator can still read all data and secrets. The live
database is not encrypted by the application. User-work backups contain private
research information and are not encrypted.

Both containers run with reduced rights and cannot gain new ones. The
application runs as an unprivileged user on a read-only file system without
Linux capabilities. The database keeps only the five capabilities its start
needs to hand its data folder to the database user before switching to it.
Each container has a process limit: 512 for the database and 1,024 for the
application. Leti Cook sets no memory limit. Updates apply these limits to
existing installations.

If an API key is exposed, revoke it in Google Cloud and replace it with
`manage.py set-key`. `manage.py check-secrets` reports whether a key is stored
in the database, and `manage.py redact-secrets` replaces keys in stored error
texts; see [checking for stored API keys](docs/CONFIGURATION.md#checking-for-stored-api-keys).
If database credentials in the generated environment files
are exposed, there is no built-in rotation command: move your work to a separate
new installation, verify it, then remove the old one. See
[moving an installation](docs/INSTALLATION.md#moving-to-a-new-installation).

Pipeline telemetry is off in this version, so no diagnostic journal with video
or channel IDs is written; see [what is stored](docs/DATA.md#what-is-stored).

Data cleanup applies to the running installation, not to external dumps or disk
snapshots. It is not a guarantee of secure erasure. See [Data and backups](docs/DATA.md).
[Connections to other services](docs/DATA.md#connections-to-other-services)
lists what the server and your browser send to other sites.

## Research agent credentials

The optional [MCP adapter](docs/MCP.md) uses a separate, expiring, revocable
`research.read` key. Only hashes are stored. Bearer-authenticated requests have
an exact read-route allowlist and never receive owner permissions or fall back
to a browser session. Password reset invalidates these keys too.

The key covers the visible research library, not a selected folder. Trusted-local
mode still gives owner access to any process that reaches the address without
a key; it is not a sandbox. Use password mode for separate HTTP identities.
The adapter accepts a fixed loopback origin only and does not follow redirects.
Keep the client-side key file private and out of backups intended for sharing.
Review the agent provider's data handling before sending it research results.

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
