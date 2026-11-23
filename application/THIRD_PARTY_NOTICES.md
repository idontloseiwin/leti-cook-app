# Third-party notices

Project code uses AGPL-3.0-or-later. The following dependencies keep their own licenses.

## Bundled frontend dependencies

| Package | Version | License |
| --- | --- | --- |
| classcat | 5.0.5 | MIT |
| d3-color | 3.1.0 | ISC |
| d3-dispatch | 3.0.1 | ISC |
| d3-drag | 3.0.0 | ISC |
| d3-ease | 3.0.1 | BSD-3-Clause |
| d3-interpolate | 3.0.1 | ISC |
| d3-selection | 3.0.0 | ISC |
| d3-timer | 3.0.1 | ISC |
| d3-transition | 3.0.1 | ISC |
| d3-zoom | 3.0.0 | ISC |
| @fontsource/inter | 5.3.0 | OFL-1.1 |
| lucide-react | 1.21.0 | ISC |
| react | 19.3.0 | MIT |
| react-dom | 19.3.0 | MIT |
| scheduler | 0.28.0 | MIT |
| use-sync-external-store | 1.7.0 | MIT |
| @xyflow/react | 12.12.0 | MIT |
| @xyflow/system | 0.0.83 | MIT |
| zustand | 4.5.7 | MIT |

Verbatim license and notice texts are in `licenses/`.

## Runtime dependencies

Python dependencies are installed by pip from the hash-locked
`requirements.lock` file in this directory.
Their packages and transitive dependencies retain their upstream license metadata.
PostgreSQL and Python container images are fetched from their
upstream registries rather than relicensed by this project. Inspect the pinned
image references in the Compose files and Dockerfile.

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
