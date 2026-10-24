"""Real hot-runtime admission with a synthetic blocked owner, for offline driver tests."""
from contextlib import contextmanager
import os
from unittest.mock import patch

import hot_runtime


class BlockedOwner:
    def require_ready(self):
        raise hot_runtime.HotRuntimeUnavailable()

    require_collection_ready = require_ready
    require_database_ready = require_ready


@contextmanager
def real_admission():
    """Synthetic configuration/owner, real admission function and ContextVar scope."""
    values = {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
              'LIC_DATABASE_ID': 'f9800000-0000-4000-8000-000000000001',
              'LIC_HOT_RUNTIME_SECRET': 'synthetic_hot_probe_secret_000000000001'}
    owner = BlockedOwner()
    with patch.dict(os.environ, values):
        hot_runtime.install_local_runtime(owner)
        try:
            yield
        finally:
            hot_runtime.remove_local_runtime(owner)
