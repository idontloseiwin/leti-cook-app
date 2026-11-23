"""Database settings the backend of one NEW installation uses; install.py writes them.

These are backend service credentials, not the owner login or a YouTube API key.
No connection, Docker operation, database mutation or deployment is performed.
"""
from __future__ import annotations

import re
import secrets
import uuid
from urllib.parse import quote


def runtime_settings(authenticator_password: str, *, database_id=None, internal_secret=None) -> dict:
    """Private direct transport settings for the existing non-owner login."""
    if not isinstance(authenticator_password, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,}', authenticator_password) is None:
        raise ValueError('authenticator_password_invalid')
    if (database_id is None) != (internal_secret is None):
        raise ValueError('database_admission_pair_required')
    if database_id is None:
        database_id, internal_secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
    try:
        identity = uuid.UUID(database_id)
        if identity.int == 0 or str(identity) != database_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('database_identity_invalid') from None
    if type(internal_secret) is not str or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', internal_secret) is None:
        raise ValueError('database_internal_secret_invalid')
    password = quote(authenticator_password, safe='')
    return {
        'LIC_DATABASE_TRANSPORT': 'postgres',
        'LIC_DATABASE_DSN': f'postgresql://authenticator:{password}@db:5432/leti-cook',
        'LIC_DATABASE_ID': database_id,
        'LIC_HOT_RUNTIME_SECRET': internal_secret,
    }
