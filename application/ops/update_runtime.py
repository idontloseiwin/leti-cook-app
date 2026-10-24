"""Read and verify update status inside the installed backend; no browser login."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status',))
    parser.add_argument('--operation')
    args = parser.parse_args(argv)
    try:
        from hot_runtime import load_hot_admission_config
        from update_runtime_status import UpdateRuntimeClient
        from update_runtime_protocol import UpdateRuntimeError
        if os.environ.get('LIC_DEPLOYMENT_PROFILE') != 'self-hosted':
            raise UpdateRuntimeError('update_runtime_not_configured')
        result = UpdateRuntimeClient(os.environ.get('LIC_HOT_RUNTIME_URL'),
            admission=load_hot_admission_config()).status(expected_operation=args.operation)
        print(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
        return 0
    except Exception:
        # Do not render arbitrary exception text, URLs, environment or proofs.
        print('{"code":"update_status_unavailable"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
