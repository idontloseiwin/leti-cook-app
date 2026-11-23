"""Read-only source helper for explicitly reviewed same-schema transitions."""
from pathlib import Path
import re

from ops.release_verification import VerifiedRelease
from ops.update_candidate import _capture_tree, _file, _safe_path
from ops.update_engine import UpdateError


def validate_reader_source(source, release, plan):
    # This is not a generic cross-schema plugin. Old Python dependencies and
    # native contracts are accepted only for this reviewed same-schema bridge.
    if (type(release) is not VerifiedRelease or plan is None
            or (plan.installed_schema.schema_version, plan.schema.schema_version)
            not in {(31, 31), (47, 47)}
            or release.manifest.get('schema_version') != plan.schema.schema_version):
        raise UpdateError('update_reader_schema_unsupported')
    source = Path(source)
    if not source.is_absolute() or re.fullmatch(r'[A-Za-z0-9_./:\\-]+', str(source)) is None:
        raise UpdateError('update_reader_path_invalid')
    files, _ = _capture_tree(source)
    expected = {name.removeprefix('application/'): value for name, value in release.files.items()
                if name.startswith('application/')}
    if (not expected or 'ops/update_copy_window.py' not in expected or files != expected):
        raise UpdateError('update_reader_source_changed')
    installed = _safe_path(plan.installed / 'application')
    # Running the signed reader in the old image is permitted only with the
    # same reviewed Python/runtime dependency inputs. This cannot be a generic
    # way to execute a future application's code in an arbitrary old image.
    for name in ('requirements.lock', 'Dockerfile'):
        if name not in expected or _file(installed / name) != expected[name]:
            raise UpdateError('update_reader_runtime_incompatible')
    return source
