# Connect an agent through MCP

Leti Cook serves a read-only **MCP endpoint** at its own address followed by
`/mcp`, for example `http://127.0.0.1:8002/mcp`. Examples here use port 8002,
the installer's first suggestion; use your installation's port. A compatible
agent program on your computer, such as Claude Code, Codex, Cursor or VS Code,
connects to it and queries the stored library using the same backend as
Library and Research Workshop. The program either signs in and you click
**Allow** in the browser, or it uses an agent key. A separate skill is not
required: the tools describe their arguments and discover the installation's
current fields and operations. Claude Desktop starts local programs only and
connects through the included bridge.

The MCP endpoint supports read-only operations. It does not collect new YouTube data, spend
YouTube quota, change settings, add channels, save studies or modify collections.
The key permits research reads across the visible library; it is not restricted
to a particular folder. Data access still follows the installation's freshness,
format, collection-field and analytics permissions. Agent keys support current
reads only: creating or using a frozen list is refused by the backend, including
Canvas runs and Inspector/facet references. All agent keys together share at most
one heavy read slot. With the normal two-slot read admission, the other slot is
available to the owner; a smaller database pool cannot provide that reservation.

## Sign in with Allow

This is the recommended way: nothing is copied except the address. In the
workspace, open **Agents** in the sidebar. Under **Connect an agent**, keep
**Sign in with Allow**, choose the program and copy its settings. For Claude Code:

```bash
claude mcp add --transport http --scope user leti-cook http://127.0.0.1:8002/mcp
```

Then type `/mcp` in Claude Code, choose `leti-cook` and **Authenticate**. Codex
uses `codex mcp login leti-cook` after its settings entry; Cursor and VS Code
offer to sign in themselves. The program opens this installation's **Allow**
page in your browser. Sign in first if the installation uses a password; the
page opens again afterwards. It shows the name the program gave itself and the
full return address: a port on this computer or the program's own address. Choose
how long access lasts (7, 30, 90 or 365 days) and click **Allow read access**,
or **Deny**. Allow only a request you started yourself just now: any program on
this computer can claim a name.

The program then keeps its own access: an access token valid for one hour that
it renews automatically, up to the lifetime you chose. **Connected programs** on
**Agents** lists each allowed program with its lifetime and last use; **Revoke**
ends its access at once. Each sign-in that is allowed appears separately.

Programs register themselves without a password, so the installation accepts
only HTTP callbacks on this computer (`127.0.0.1`, `localhost`, `[::1]`) or the
desktop schemes `cursor`, `vscode` and `vscode-insiders`. Other schemes and web
callbacks are refused. Return addresses must be ASCII URIs; non-ASCII path and
query text must be percent-encoded by the client. A code from the Allow page
works once, within two minutes, and only together with the proof the program
created when it started (PKCE). A renewal token used twice ends the connection.
Access is bound to this installation's `/mcp` address: after changing the
configured origin, programs must sign in again.

Registration is limited to ten new clients per minute across the installation.
If another local program exhausts this allowance, new sign-ins receive a retry
response; existing connections and keys keep working. An open Allow request or
an unexpired code protects its registration from cleanup.

This is a local profile of MCP authorization: plain HTTP on this computer and
answers to loopback ports or application schemes. It is not a public HTTPS
deployment, and the installation should not be exposed as one.

## Create a key

Use a key for Claude Desktop, scripts or programs that cannot sign in. On
**Agents**, enter a name, one per agent program, and a lifetime of 1–365 days
(30 by default), then **Create key**.

The new key appears once, hidden. **Show** reveals it and **Copy key** copies it.
After **Done**, reloading or leaving the page it cannot be shown again: only its
hash is stored. If you lose it, create another key and revoke the old one. Paste
the key into the agent program's settings or a private key file, never into a
chat, prompt, issue, screenshot or Git file. Only the owner can create, list and
revoke keys; an agent key cannot reach these actions.

An installation keeps up to 32 agent keys; **Keys** shows how many are in use
("1 of 32"), as the server reports them. At the limit, **Create key** is refused
with the server's limit in the message: revoke a key you no longer need first.

If **Create key** gets no answer (the connection drops or the request times
out), the page says it is not known whether the key was created and reads the
list again. A key you do not need that appears in it cannot be shown: revoke it
and create another. When the server answers but its answer cannot be read, the
key was created and is revoked the same way. When the key list or **Connected
programs** cannot be read, that section shows "—" and "Could not be read" rather
than an empty list; **Refresh** reads both again.

The same actions remain available on the installation host:

```bash
sudo python3 /path/to/installation/manage.py agent-key-create --name "Desktop research" --days 30
```

That command shows the secret once in the interactive terminal. Until access
setup is finished, the `agent-key-*` commands stop with
`owner_access_not_configured`: finish setup at `/auth` first.

## Connect an agent program with a key

**Connect an agent** with **With a key** shows ready settings for Claude Code,
Codex, Cursor, VS Code and Claude Desktop with this installation's address. Right
after creating a key they contain it: hidden until **Show**, included by **Copy
with key**. Otherwise replace `YOUR_KEY`. For Claude Code, for example:

```bash
claude mcp add --transport http --scope user leti-cook http://127.0.0.1:8002/mcp --header "Authorization: Bearer YOUR_KEY"
```

Codex, Cursor and VS Code use an HTTP server entry with the same address and an
`Authorization: Bearer` header. Keep these entries in user-level settings
(`~/.codex/config.toml`, `~/.cursor/mcp.json`, VS Code's user MCP configuration),
not in a project folder that may be committed.

The address must exactly match the installation's configured origin, as the
browser shows it, including `127.0.0.1` versus `localhost`. It works where the
workspace opens: on the installation's computer, or through the SSH tunnel you
already use for it. claude.ai, ChatGPT and Claude Desktop connectors run in the
provider's cloud and cannot reach a local address; this is not a public remote
endpoint. The endpoint answers JSON over plain POST, keeps no session and opens
no server stream.

### Claude Desktop

Claude Desktop starts local (stdio) programs only, and the bridge cannot sign
in: it uses a key. Save the key alone in a text
file outside the repository and shared folders, readable only by your account
(Linux/macOS: `chmod 600`; Windows: restrict its permissions to your account and
administrators). Copy `integrations/mcp/server.py` and `client.py` from the
matching source tree or update package. The bridge needs Python 3.12+ and no
packages. Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "leti-cook": {
      "command": "python3",
      "args": [
        "-I", "-B", "/path/to/leti-cook/integrations/mcp/server.py",
        "--base-url", "http://127.0.0.1:8002",
        "--token-file", "/private/path/leti-cook-agent.key"
      ]
    }
  }
}
```

On Windows use `"command": "py"` and put `"-3.12"` first in `args`. Replace every
example path and port. The bridge accepts loopback addresses only, rejects
redirects, ignores proxy settings and rereads the key file for every message.

## What the agent can do

| Tool | Purpose |
| --- | --- |
| `find_sources` | Resolve folder, label, collection or channel names to real IDs. |
| `describe_catalogue` | Discover fields, units, operators, allowed calculations, query schema and a Canvas example. |
| `query_catalogue` | Select columns, combine conditions, sort and page through videos or channels. |
| `calculate` | Use an allowed backend operation over the full matching population, including details and history. |
| `run_canvas` | Validate or run a supplied Canvas definition without saving it. |

For example: “In my 3D folder, find Shorts up to 60 seconds long, sort by views,
and show the first 20 with links. Mention any missing or incomplete data.”

The short path is source lookup followed by a catalogue query. The agent asks
for field help only when needed. Video queries select one format at a time:
`long`, `short` or `stream`. Channel queries do not have a video format. A
format that is turned off in [`formats`](CONFIGURATION.md#video-formats) (by
default Streams) is refused with `format_turned_off`, and summaries and canvases
leave it out.

In a large library, a query over the whole library (`{kind: library}`) can
reach the read's time limit; the tool then fails with
`query_budget_exceeded:time`. The `query_catalogue` description therefore asks
the agent to narrow such a query to a folder, a channel or a publication window
(`video.published_at` with `within_last_hours_v1`, `gte` or `lte`) first.

Numeric filter values are decimal strings; duration is in seconds. Conditions
support `all`, `any` and `not`. The backend validates field IDs, operators,
permissions and query limits. No SQL or arbitrary provider request is accepted.

Results retain their original field states, provenance, coverage, expiry and
pagination. Unknown values are not zero. Current pages may change during a sync;
reuse the same query and returned cursor. `calculate` accepts the query from any
page and clears its continuation automatically, preserving the selection and
filters: a calculation covers the whole matching population. Expired data, disabled
fields and missing observations cannot be reconstructed by MCP.

An exported Canvas's `conditions` object (`version: 4`) or its execution
definition (`version: 2`) can be passed to `run_canvas`. Validation is the default;
execution requires `validate_only: false`. An entire saved-object wrapper is
not an execution definition. A Canvas definition is limited to 16,384 bytes of
UTF-8 JSON and its notes to 8,192 bytes; `describe_catalogue` with
`section: canvas` returns both (`max_definition_bytes`, `max_notes_bytes`). A
larger one is refused with `query_budget_exceeded:request`.

A `query_budget_exceeded` tool error names the limit it reached after a colon:
`rows` (more than 50,000 rows in one fixed list), `memory` (the stored list or
the read's working data is over the server's limit), `time` (the read ran out
of time), `request` (the request, its conditions or values are too large or
too complex) or `response` (the answer is too large: request fewer rows or
columns). Narrow the query and call again.

## Revoke or replace a key

On **Agents**, **Revoke** and then **Revoke now** ends a key's access at once,
including a read in progress. Replace a key by creating a new one, updating the
agent program's settings or key file and revoking the old one. Changing the owner
password or access mode revokes every key. Restarting the application does not
reset their expiry or revoke them. On the host:

```bash
sudo python3 /path/to/installation/manage.py agent-key-list
sudo python3 /path/to/installation/manage.py agent-key-revoke --key-id KEY_ID
```

Listing shows IDs, names and expiry, never secrets.

Only credential hashes are stored beside the installation's owner-access state,
separately from the YouTube database. They are not part of user-work exports.

If an agent credential store is damaged, it fails closed and is never reset
automatically. Keys are in `research-agents.sqlite3` and allowed programs in
`research-oauth.sqlite3`, beside the owner-auth SQLite file in the installation's
private auth volume. To recover, stop the application and any key-management
commands, and remove only these agent files and their SQLite sidecars if present
(`-journal`, `-wal`, `-shm`). Keep the owner-auth file and the database volume.
This revokes every agent key and connection. Start the application,
create replacement keys on **Agents**, and update each agent program's settings
or key file. Never restore the old agent store afterward: it would restore its keys.

For a Docker installation, finish any update or key-management command first.
Replace `/path/to/installation` below with the directory containing `manage.py`.
This stops only the backend, removes the eight agent-store files from its
`application-state` volume, and starts the backend again. It uses the same
Compose project, files and optional runtime override as `manage.py`.

```bash
sudo bash <<'BASH'
set -euo pipefail
cd /path/to/installation
project=$(python3 -c 'import json; print(json.load(open("installed.json"))["project"])')
compose=(docker compose --project-name "$project" --env-file "$PWD/installation.env"
  -f "$PWD/database/compose.yaml" -f "$PWD/application.compose.yaml")
if [[ -f runtime.compose.json ]]; then
  compose+=(-f "$PWD/runtime.compose.json")
fi
"${compose[@]}" stop --timeout 30 backend
"${compose[@]}" run --rm --no-deps -T backend rm -f -- \
  /var/lib/leti-cook/access/research-agents.sqlite3 \
  /var/lib/leti-cook/access/research-agents.sqlite3-journal \
  /var/lib/leti-cook/access/research-agents.sqlite3-wal \
  /var/lib/leti-cook/access/research-agents.sqlite3-shm \
  /var/lib/leti-cook/access/research-oauth.sqlite3 \
  /var/lib/leti-cook/access/research-oauth.sqlite3-journal \
  /var/lib/leti-cook/access/research-oauth.sqlite3-wal \
  /var/lib/leti-cook/access/research-oauth.sqlite3-shm </dev/null
"${compose[@]}" start backend
BASH
```

The block stops on failure. If removal fails, the backend remains stopped;
resolve the reported error before retrying. After success, sign the programs in
again or create replacement keys as described above.

## Access and data handling

Requests carrying an agent key or a signed-in program's token can use only
`/mcp` and the research endpoints its tools call. `/mcp` always requires one,
also in trusted-local mode; without it the answer names where to sign in. An
invalid or revoked key never falls back to a browser session or local owner
access. Each tool call re-enters the application with the same key and passes the
same checks as a direct request. Long reads recheck authorization before
returning their result. A buffered batch also rechecks access before sending its
combined response: if access was revoked, all its buffered data is discarded.
Agent programs send no `Origin` header, which `/mcp`
accepts only together with a key; a foreign `Origin` or host is refused.

The endpoint and stdio bridge support batches for MCP 2025-03-26, up to twenty
messages. The entire batch has a 35-second processing budget and a 6 MiB JSON
response limit. If either budget is exceeded, buffered results are discarded and
requests receive errors with their original IDs. Reduce the number of calls,
page size or selected columns before retrying. Newer MCP versions use individual
messages instead.

**Trusted-local mode is not an agent sandbox.** In that mode, any other program
that can reach the app can omit the agent key and obtain the existing local
owner access. Use password mode if you need separate HTTP identities, and do
not give an untrusted agent owner credentials or server-administrator access.

MCP stores no query results and opens no listener of its own. Your chosen agent may
send returned titles, descriptions, statistics and user-defined source names to
its model provider or retain them in chat history. Choose the client/provider
accordingly and respect [data rules](DATA.md) and YouTube's terms. Do not use
agent transcripts as a permanent archive of expiring YouTube data. Returned
text is untrusted content, not instructions to the agent.

If a call reports `field_disabled`, the owner must enable collection through
the normal interface if desired. `catalogue_preparing` means wait and retry;
`cursor_expired` means start the query again. Authorization failure requires a
valid key; MCP never repairs credentials or changes application settings.
`base_url_must_match_installation_origin` means correct the scheme, host and port
to match the configured origin exactly, including `127.0.0.1` versus `localhost`.
Do not solve it by enabling redirects.

The MCP instructions also tell the agent not to infer categories, tags, sentiment,
scores or rankings from YouTube API data without verified applicable YouTube
permission under [YouTube's additional-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy).
This is guidance for the external agent; the application's operation
permissions remain enforced on the server. It cannot control arbitrary calculations
an external client makes after receiving data.
