# Third-party notices

Project code uses AGPL-3.0-or-later. The following dependencies keep their own licenses.

## Bundled frontend dependencies

| Package | Version | License |
| --- | --- | --- |
| @fontsource/inter | 5.3.0 | OFL-1.1 |
| lucide-react | 1.21.0 | ISC |
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| scheduler | 0.28.0 | MIT |

Verbatim license and notice texts are in `licenses/`.

## Runtime dependencies

Python dependencies are installed by pip from the hash-locked
`application/requirements.lock` file.
Their packages and transitive dependencies retain their upstream license metadata.
PostgreSQL and Python container images are fetched from their
upstream registries rather than relicensed by this project. Inspect the pinned
base image references in `database/Dockerfile` and `application/Dockerfile`.
The database image preserves upstream notices and uses the base image's Debian
`setpriv` utility in place of `gosu`.

This inventory is not a claim that the project license replaces third-party terms.

### PostgreSQL Python driver

| Package | Version | Upstream license |
| --- | --- | --- |
| [psycopg](https://pypi.org/project/psycopg/3.3.6/) | 3.3.6 | LGPL-3.0-only |
| [psycopg-binary](https://pypi.org/project/psycopg-binary/3.3.6/) | 3.3.6 | LGPL-3.0-only |
| [psycopg-pool](https://pypi.org/project/psycopg-pool/3.3.3/) | 3.3.3 | LGPL-3.0-only |

These packages are Copyright (C) 2020 The Psycopg Team. Their upstream LGPL
text is included in the installed packages' `.dist-info/licenses/LICENSE.txt`;
the corresponding source is available from the linked release pages and the
[Psycopg repository](https://github.com/psycopg/psycopg).

The binary distribution bundles native client libraries, including `libpq`.
`libpq` uses the [PostgreSQL License](https://www.postgresql.org/about/licence/);
other bundled libraries retain their own terms. Preserve upstream license files
and notices when distributing built images. The application's AGPL license does
not replace these licenses.
