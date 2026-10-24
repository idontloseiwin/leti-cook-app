"""Owned, bounded database transport for the self-hosted maintenance gate."""
from __future__ import annotations

from typing import Any
from dataclasses import replace

from db.base.client import create_database_gateway, load_database_config
from db.base.postgres import DatabaseError, DatabaseUnavailable
from db.hot.hot_retention import HotRetentionRepository, RetentionError
from hot_storage import utc_instant
from update_copy_protocol import window_facts_version, UpdateCopyError
from db.readiness.capabilities import get_hot_runtime_contract
from db.videos.collection_policy import require_cadence_snapshot


class MaintenanceRepository(HotRetentionRepository):
    """One private PostgreSQL connection; maintenance cannot starve behind API work."""

    def __init__(self, database):
        super().__init__(database)
        self._closed = False

    def _call(self, function: str, params: dict) -> Any:
        if self._closed:
            raise RetentionError("maintenance_repository_closed")
        return super()._call(function, params)

    def contract(self) -> dict:
        try:
            if self._closed:
                raise RetentionError("maintenance_repository_closed")
            value = get_hot_runtime_contract(self.database)
        except RetentionError:
            raise
        except DatabaseUnavailable:
            raise RetentionError("runtime_contract_unavailable") from None
        except DatabaseError:
            raise RetentionError("runtime_contract_schema_invalid") from None
        except Exception:
            raise RetentionError("runtime_contract_schema_invalid") from None
        if not isinstance(value, dict) or set(value) != {"version", "as_of", "schema_ready"}:
            raise RetentionError("invalid_runtime_contract")
        version, stamp, ready = value.get("version"), value.get("as_of"), value.get("schema_ready")
        if type(version) is not int or version != 1 or type(ready) is not bool:
            raise RetentionError("invalid_runtime_contract")
        if not isinstance(stamp, str) or len(stamp) > 48:
            raise RetentionError("invalid_runtime_contract")
        try:
            as_of = utc_instant(stamp).isoformat()
        except (ValueError, OverflowError):
            raise RetentionError("invalid_runtime_contract") from None
        if not ready:
            raise RetentionError("runtime_contract_schema_invalid")
        # Do not pass unexpected database fields into diagnostics or the gate.
        return {"version": version, "as_of": as_of, "schema_ready": ready}

    def update_copy_window(self) -> dict:
        """Observe source clocks only; host code owns copy creation and expiry."""
        if self._closed:
            raise RetentionError("maintenance_repository_closed")
        try:
            value = self.database.read_update_copy_window()
        except DatabaseUnavailable:
            raise RetentionError("update_copy_window_unavailable") from None
        except Exception:
            raise RetentionError("update_copy_window_failed") from None
        try:
            version = window_facts_version(value)
        except UpdateCopyError:
            raise RetentionError("invalid_update_copy_window") from None
        try:
            if type(value["as_of"]) is not str or len(value["as_of"]) > 48:
                raise ValueError
            at = utc_instant(value["as_of"])
            oldest = value["oldest_source_at"]
            if oldest is not None:
                if type(oldest) is not str or len(oldest) > 48:
                    raise ValueError
                oldest = utc_instant(oldest)
                if oldest > at and (not value["invalid_source_clock"]
                        or (version == 2 and value["retained_profile_only"])):
                    raise ValueError
            elif value["expired"]:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise RetentionError("invalid_update_copy_window") from None
        result = {"as_of": at.isoformat(), "oldest_source_at": oldest.isoformat() if oldest else None,
                  "expired": value["expired"], "invalid_source_clock": value["invalid_source_clock"]}
        if version == 2:
            result.update(version=2, retained_profile_only=value["retained_profile_only"])
        return result

    def startup_expiry(self, *, cutoff, strict_entry=False) -> dict:
        """Read one caller-fixed boundary; never advance a frozen catch-up cutoff."""
        if self._closed:
            raise RetentionError("maintenance_repository_closed")
        try:
            expected_cutoff = utc_instant(cutoff)
            if type(strict_entry) is not bool:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise RetentionError("invalid_startup_expiry") from None
        try:
            value = self.database.read_startup_expiry(cutoff=expected_cutoff, strict_entry=strict_entry)
        except DatabaseUnavailable:
            raise RetentionError("startup_expiry_unavailable") from None
        except Exception:
            raise RetentionError("startup_expiry_failed") from None
        if (not isinstance(value, dict) or set(value) != {"as_of", "cutoff", "expired"}
                or type(value["expired"]) is not bool):
            raise RetentionError("invalid_startup_expiry")
        try:
            if any(type(value[key]) is not str or len(value[key]) > 48 for key in ("as_of", "cutoff")):
                raise ValueError
            at, observed_cutoff = (utc_instant(value[key]) for key in ("as_of", "cutoff"))
            if observed_cutoff != expected_cutoff:
                raise ValueError
        except (ValueError, OverflowError):
            raise RetentionError("invalid_startup_expiry") from None
        return {"as_of": at.isoformat(), "expired": value["expired"]}

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.database.close()

    def __enter__(self):
        if self._closed:
            raise RetentionError("maintenance_repository_closed")
        return self

    def __exit__(self, *exc):
        self.close()


def maintenance_repository(config=None, *, cadence_snapshot) -> MaintenanceRepository:
    """Open a dedicated bounded pool, using the same exact-function admission."""
    config = load_database_config() if config is None else config
    database = create_database_gateway(replace(
        config, min_size=0, max_size=1, max_waiting=2,
        acquire_timeout=2.0, connect_timeout=2, statement_timeout_ceiling_ms=8_000,
    ), cadence_snapshot=cadence_snapshot)
    try:
        require_cadence_snapshot(database)
        database.open()
        return MaintenanceRepository(database)
    except Exception:
        database.close()
        raise RetentionError("maintenance_client_unavailable") from None
