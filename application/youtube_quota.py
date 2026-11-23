"""Observe-only persistence for YouTube API request attempts.

This module records what happened. It does not decide whether a request may run
and does not enforce application quota limits or reserves.
"""

from collections.abc import Mapping
import re
import threading
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db.quota.youtube_quota import YouTubeQuotaRepository
from telemetry import emit_event
from operational_copy_policy import quota_label


YOUTUBE_QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
TERMINAL_ATTEMPT_STATUSES = {
    "success",
    "http_error",
    "transport_error",
}
ERROR_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")


def utc_now():
    return datetime.now(timezone.utc)


def sanitized_error_type(error):
    name = (
        error.__class__.__name__
        if isinstance(error, BaseException)
        else str(error or "").strip()
    )
    if not ERROR_TYPE_PATTERN.fullmatch(name):
        return "InvalidErrorType"
    return quota_label("error_type", name)


def quota_bucket_units(rows):
    """Build a stable bucket -> units read model from ledger rows."""
    result = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        bucket = str(row.get("quota_bucket") or "").strip()
        if not bucket:
            continue
        try:
            units = max(0, int(row.get("quota_units") or 0))
        except (TypeError, ValueError):
            units = 0
        result[bucket] = result.get(bucket, 0) + units
    return dict(sorted(result.items()))


def load_quota_read_model(repository, quota_day):
    """Read the ledger only; failed or bounded reads cannot supply an exact balance."""
    warnings = []
    try:
        summary = repository.summarize_day(quota_day)
        truncated = summary["truncated"]
        bucket_units = quota_bucket_units([
            {"quota_bucket": bucket, "quota_units": units}
            for bucket, units in summary["bucket_units"].items()
        ])
        if truncated:
            warnings.append({
                "code": "quota_ledger_truncated",
                "message": "The local quota ledger read reached its safety bound.",
            })
        return {
            "units_used": int(bucket_units.get("general") or 0),
            "bucket_units": bucket_units,
            "ledger_attempts": summary["attempts"],
            "ledger_truncated": bool(truncated),
            "read_model_source": "youtube_api_request_attempts",
            "read_status": "partial" if truncated else "ok",
            "warnings": warnings,
        }
    except Exception as error:
        warnings.append({
            "code": "quota_ledger_read_failed",
            "message": (
                "Could not read the local quota ledger: "
                + error.__class__.__name__
            ),
        })

    return {
        "units_used": None,
        "bucket_units": {},
        "ledger_attempts": None,
        "ledger_truncated": False,
        "read_model_source": "youtube_api_request_attempts",
        "read_status": "unavailable",
        "warnings": warnings,
    }


class YouTubeQuotaAttemptRecorder:
    """Write request-attempt lifecycle rows without blocking collection."""

    def __init__(self, database, project_id="primary", *, journal_path=None,
                 journal_namespace="default", background=True):
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("YouTube quota project_id is required")
        self.repository = YouTubeQuotaRepository(
            database,
            project_id=project_id,
        )
        self.project_id = project_id
        self.last_error = None
        self.buffer = None
        # Only active HTTP calls retain their start; completion frees this entry.
        self._active_starts = {}
        self._active_lock = threading.Lock()
        if journal_path is not None:
            from youtube_quota_buffer import QuotaEventBuffer
            self.buffer = QuotaEventBuffer(
                self.repository, journal_path, namespace=journal_namespace,
                background=background,
            )

    def start(self):
        if self.buffer is not None:
            self.buffer.start()

    def close(self):
        if self.buffer is not None:
            self.buffer.close()

    def buffer_status(self):
        if self.buffer is not None:
            return self.buffer.status()
        return {"mode": "direct", "status": "ok", "pending_events": 0,
                "oldest_pending_at": None, "last_error": None}

    def _record_write_failure(self, operation, phase, error):
        error_type = sanitized_error_type(error)
        safe_phase = str(phase or "")
        if not ERROR_TYPE_PATTERN.fullmatch(safe_phase):
            safe_phase = "invalid"
        self.last_error = {
            "operation": operation,
            "phase": safe_phase,
            "error_type": error_type,
            "observed_at": utc_now().isoformat(),
        }
        try:
            emit_event(
                "db_write", "youtube_client", "db_tables", "youtube_quota_ledger",
                phase=safe_phase, direction="write", status="error", detail=error_type,
            )
        except Exception:
            pass

    def start_attempt(
        self,
        *,
        phase,
        endpoint,
        quota_bucket,
        quota_units,
        request_attempt_id=None,
        retry_of_request_attempt_id=None,
    ):
        """Durably record the start and return its UUID; failure cannot block HTTP."""
        try:
            attempt_id = str(request_attempt_id or uuid.uuid4())
            now = utc_now()
            row = {
                "request_attempt_id": attempt_id,
                "project_id": self.project_id,
                "quota_bucket": quota_label("quota_bucket", str(quota_bucket)),
                "quota_day": now.astimezone(YOUTUBE_QUOTA_TIMEZONE).date().isoformat(),
                "phase": quota_label("phase", str(phase)),
                "endpoint": quota_label("endpoint", str(endpoint)),
                "quota_units": int(quota_units),
                "status": "started",
                "started_at": now.isoformat(),
            }
            if retry_of_request_attempt_id is not None:
                row["retry_of_request_attempt_id"] = str(
                    retry_of_request_attempt_id
                )

            if self.buffer is not None:
                event = {"kind": "start", "request_attempt_id": attempt_id,
                         "project_id": self.project_id, "values": row}
                if not self.buffer.record(event):
                    raise RuntimeError("quota_event_unrecorded")
                with self._active_lock:
                    self._active_starts[attempt_id] = event
            else:
                self.repository.start_attempt(row)
            self.last_error = None
            return attempt_id
        except Exception as error:
            self._record_write_failure("start", phase, error)
            return None

    def finish_attempt(
        self,
        request_attempt_id,
        *,
        status,
        phase,
        duration_ms,
        http_status=None,
        error_type=None,
    ):
        """Finish a started row, returning False when recording cannot complete."""
        if request_attempt_id is None:
            return False

        try:
            if status not in TERMINAL_ATTEMPT_STATUSES:
                raise ValueError("Invalid YouTube request attempt terminal status")

            values = {
                "status": status,
                "finished_at": utc_now().isoformat(),
                "duration_ms": max(0, int(duration_ms)),
                "http_status": int(http_status) if http_status is not None else None,
                "error_type": (
                    sanitized_error_type(error_type)
                    if error_type is not None
                    else None
                ),
            }
            if self.buffer is not None:
                event = {"kind": "finish", "request_attempt_id": str(request_attempt_id),
                         "project_id": self.project_id, "values": values}
                with self._active_lock:
                    start_event = self._active_starts.pop(str(request_attempt_id), None)
                fallback = [start_event, event] if start_event is not None else [event]
                if not self.buffer.record(event, fallback_events=fallback):
                    raise RuntimeError("quota_event_unrecorded")
            else:
                self.repository.finish_attempt(request_attempt_id, values)
            self.last_error = None
            return True
        except Exception as error:
            self._record_write_failure("finish", phase, error)
            return False
