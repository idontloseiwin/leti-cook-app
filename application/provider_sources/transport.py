"""The only module of the source layer that imports the shared YouTube transport.

Every request goes through ``youtube_client.youtube_api_get`` inside
``provider_runtime.request_scope("interactive")``: the shared admission gate,
HOT readiness (pauses, update holds), deadline, cancellation, the 8 MiB body
bound and the quota attempt journal apply exactly as for collection. Phase
``explicit_acquisition``; bucket, units and ledger label come from the plan.
The API family comes from the registry method; a chained request
(the Reporting media download) names its own checked endpoint and label.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

import requests

import youtube_client
from provider_admission import AdmissionCancelled, AdmissionUnavailable
from provider_boundary import PROVIDER_FORBIDDEN
from provider_runtime import request_scope

PHASE = "explicit_acquisition"
MAX_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Exchange:
    status: int
    content: bytes
    date_header: str | None
    retry_after: str | None
    attempt_id: str | None


class TransportStop(Exception):
    """A fixed stop code; ``attempted`` says whether the HTTP attempt (and its quota) happened."""

    def __init__(self, code, *, attempted, attempt_id=None):
        super().__init__(code)
        self.code, self.attempted, self.attempt_id = code, attempted, attempt_id


def _header(response, name):
    try:
        value = response.headers.get(name)
    except Exception:
        return None
    return value if isinstance(value, str) and len(value) <= 128 else None


def _stop(code, error):
    return TransportStop(code, attempted=getattr(error, "lic_http_attempted", False) is True,
                         attempt_id=getattr(error, "lic_request_attempt_id", None))


def send(plan, params, *, credential, cancelled, deadline, attempt_recorder, api_key=None, phase=PHASE,
         endpoint=None, label=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransportStop("deadline", attempted=False)
    from hot_runtime import HotRuntimeUnavailable
    from self_hosted_settings import SettingsError
    try:
        with request_scope("interactive", cancelled, deadline=deadline):
            response = youtube_client.youtube_api_get(
                endpoint or plan.endpoint, phase=phase, quota_bucket=plan.quota_bucket,
                attempt_recorder=attempt_recorder, api_family=plan.api,
                params=params, api_key=None if credential.transport_value is not None else api_key,
                timeout_seconds=min(MAX_TIMEOUT_SECONDS, remaining), quota_units=plan.quota_units,
                deadline=deadline, cancelled=cancelled, max_response_bytes=plan.max_response_bytes,
                credential=credential.transport_value, endpoint_label=label or plan.ledger_endpoint_label)
    except AdmissionCancelled as error:
        raise _stop("cancelled", error) from None
    except AdmissionUnavailable as error:
        raise _stop("provider_forbidden" if str(error) == PROVIDER_FORBIDDEN else "provider_busy", error) from None
    except youtube_client.YouTubeResponseTooLarge as error:
        raise _stop("response_too_large", error) from None
    except requests.Timeout as error:
        raise _stop("deadline" if str(error) == "provider_request_deadline" else "transient", error) from None
    except requests.RequestException as error:
        raise _stop("transient", error) from None
    except HotRuntimeUnavailable as error:
        raise _stop("runtime_not_ready", error) from None
    except SettingsError as error:
        code = {"provider_key_rejected": "credential_rejected",
                "provider_access_denied": "credential_access_denied"}.get(str(error), "setup_required")
        raise _stop(code, error) from None
    status = youtube_client.response_http_status(response)
    content = getattr(response, "content", b"")
    return Exchange(status=status if status is not None else 0, content=content if isinstance(content, bytes) else b"",
                    date_header=_header(response, "Date"), retry_after=_header(response, "Retry-After"),
                    attempt_id=getattr(response, "lic_request_attempt_id", None))
