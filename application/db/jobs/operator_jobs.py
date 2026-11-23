"""Database boundary for durable manual operator jobs.

The job is a control-plane envelope. Per-video ownership remains in the
existing video collection repository and its leased queue RPCs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..base.repository import DatabaseRepository
from ..readiness import capabilities
from channel_collection_scope import require_control_epoch


def _mapping(value: Any, *, operation: str, allow_none: bool = False) -> dict[str, Any] | None:
    if value is None and allow_none:
        return None
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping):
        raise TypeError(f"{operation} returned a non-object response")
    return dict(value)


class OperatorJobRepository(DatabaseRepository):
    """Typed owner of the durable operator-job RPC contract."""

    def _rpc(self, name: str, params: Mapping[str, Any] | None = None, *, abort_on_denied=True) -> Any:
        from ..hot.hot_collection_control import dispatch_owned_rpc
        def execute():
            from . import operator_job_lifecycle
            values = dict(params or {})
            if name == "retry_channel_initial_inventory_job_v5":
                from . import operator_job_retry
                return operator_job_retry.retry_channel_initial_inventory_job_v5(self.database,
                    job_id=values["p_job_id"], retry_request_id=values["p_retry_request_id"],
                    max_items=values["p_max_items"], max_pages=values["p_max_pages"],
                    max_quota_units=values["p_max_quota_units"], confirmed=values["p_confirmed"],
                    expected_epoch=values["p_expected_epoch"])
            if name == "verify_initial_hydration_job":
                from . import operator_job_verification
                return operator_job_verification.verify_initial_hydration_job(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"])
            if name in ("get_operator_job_status", "get_channel_initial_inventory_job"):
                from . import operator_job_read_queries
                action = (operator_job_read_queries.get_operator_job_status if name == "get_operator_job_status"
                          else operator_job_read_queries.get_channel_initial_inventory_job)
                return action(self.database, values["p_channel_id"])
            if name == "list_active_operator_jobs":
                from . import operator_job_read_queries
                return operator_job_read_queries.list_active_operator_jobs(self.database, values["p_limit"])
            if name == "estimate_channel_initial_inventory_job":
                from . import operator_job_enqueuing
                return operator_job_enqueuing.estimate_channel_initial_inventory_job(self.database, values["p_channel_id"])
            if name == "enqueue_channel_current_stats_backfill":
                from . import operator_job_enqueuing
                return operator_job_enqueuing.enqueue_channel_current_stats_backfill(self.database,
                    channel_id=values["p_channel_id"], max_items=values["p_max_items"],
                    max_quota_units=values["p_max_quota_units"], max_target_attempts=values["p_max_target_attempts"],
                    expected_epoch=values["p_expected_epoch"])
            if name == "enqueue_channel_initial_inventory_job":
                from . import operator_job_enqueuing
                return operator_job_enqueuing.enqueue_channel_initial_inventory_job(self.database,
                    channel_id=values["p_channel_id"], expected_items=values["p_expected_items"],
                    expected_pages=values["p_expected_pages"], expected_quota_units=values["p_expected_quota_units"],
                    max_items=values["p_max_items"], max_pages=values["p_max_pages"],
                    max_quota_units=values["p_max_quota_units"], max_attempts=values["p_max_attempts"],
                    confirmed=values["p_confirmed"], expected_epoch=values["p_expected_epoch"])
            if name == "operator_candidate_page":
                return operator_job_lifecycle.operator_candidate_page(self.database, **values)
            if name == "claim_operator_job_id":
                return operator_job_lifecycle.claim_operator_job_id(self.database, **values)
            if name == "claim_operator_job":
                return operator_job_lifecycle.claim_operator_job(self.database,
                    worker_id=values["p_worker_id"], lease_seconds=values["p_lease_seconds"])
            if name == "claim_channel_initial_hydration_job":
                return operator_job_lifecycle.claim_channel_initial_hydration_job(self.database,
                    channel_id=values["p_channel_id"], worker_id=values["p_worker_id"],
                    lease_seconds=values["p_lease_seconds"])
            if name == "release_operator_job":
                return operator_job_lifecycle.release_operator_job(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"], error_code=values["p_error_code"])
            if name in ("prepare_operator_job_chunk", "prepare_initial_hydration_job_chunk"):
                from . import operator_job_chunks
                action = (operator_job_chunks.prepare_operator_job_chunk if name == "prepare_operator_job_chunk"
                          else operator_job_chunks.prepare_initial_hydration_job_chunk)
                return action(self.database, job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    limit=values["p_limit"], lease_seconds=values["p_lease_seconds"])
            if name == "prepare_initial_inventory_job_chunk":
                from . import operator_job_inventory_preparation
                return operator_job_inventory_preparation.prepare_initial_inventory_job_chunk(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    page_limit=values["p_page_limit"], lease_seconds=values["p_lease_seconds"])
            if name == "bind_initial_inventory_scan":
                from . import operator_job_inventory_binding
                return operator_job_inventory_binding.bind_initial_inventory_scan(self.database,
                    job_id=values["p_job_id"], job_lease_token=values["p_job_lease_token"],
                    playlist_lease_token=values["p_playlist_lease_token"], scan_id=values["p_scan_id"],
                    transition_id=values["p_transition_id"])
            if name in ("settle_operator_job_chunk", "settle_initial_hydration_job_chunk"):
                from . import operator_job_chunk_settlement
                action = (operator_job_chunk_settlement.settle_operator_job_chunk if name == "settle_operator_job_chunk"
                          else operator_job_chunk_settlement.settle_initial_hydration_job_chunk)
                return action(self.database, job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    observed_quota_units=values["p_observed_quota_units"], error_code=values["p_error_code"],
                    summary=values["p_summary"])
            if name == "settle_initial_inventory_job_chunk":
                from . import operator_job_inventory_page_settlement
                return operator_job_inventory_page_settlement.settle_initial_inventory_job_chunk(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    reserved_units=values["p_reserved_units"], pages_processed=values["p_pages_processed"],
                    items_processed=values["p_items_processed"], inserted=values["p_inserted"],
                    updated=values["p_updated"], next_page_token=values["p_next_page_token"], summary=values["p_summary"])
            if name == "fail_initial_inventory_job_chunk":
                from . import operator_job_inventory_failure
                return operator_job_inventory_failure.fail_initial_inventory_job_chunk(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    error_code=values["p_error_code"], playlist_not_found=values["p_playlist_not_found"])
            if name == "settle_initial_inventory_stats":
                from . import operator_job_completion
                return operator_job_completion.settle_initial_inventory_stats(self.database,
                    job_id=values["p_job_id"], lease_token=values["p_lease_token"],
                    reserved_units=values["p_reserved_units"], observed_quota_units=values["p_observed_quota_units"],
                    status=values["p_status"], summary=values["p_summary"])
            if name == "mark_completed_initial_hydration_failed":
                from . import operator_job_completion
                return operator_job_completion.mark_completed_initial_hydration_failed(self.database,
                    job_id=values["p_job_id"], channel_id=values["p_channel_id"],
                    error_count=values["p_error_count"], expected_epoch=values["p_expected_epoch"])
            raise ValueError("database_operation_not_registered")
        response = dispatch_owned_rpc(self.database, name, params, execute,
            abort_on_denied=abort_on_denied)
        return response

    def schema_status(self) -> dict[str, Any]:
        result = _mapping(
            capabilities.assert_operator_jobs_ready(self.database),
            operation="operator jobs schema assertion",
        )
        if (type(result.get("schema_version")) is not int or result["schema_version"] < 9
                or result.get("initial_inventory_scan_bridge") is not True
                or type(result.get("initial_chunk_pages")) is not int
                or result["initial_chunk_pages"] != 1
                or type(result.get("full_initial_scope_version")) is not int
                or result["full_initial_scope_version"] != 1
                or result.get("initial_total_limits_enforced") is not False):
            raise RuntimeError("operator_jobs_inventory_bridge_schema_required")
        return result

    def get_channel_job(self, channel_id: str) -> dict[str, Any] | None:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel_id is required")
        return _mapping(
            self._rpc("get_operator_job_status", {"p_channel_id": channel_id}),
            operation="operator job status",
            allow_none=True,
        )

    def list_active(self, *, limit: int = 20) -> list[dict[str, Any]]:
        data = self._rpc("list_active_operator_jobs", {"p_limit": int(limit)}) or []
        if not isinstance(data, list):
            raise TypeError("active operator jobs query returned non-list data")
        rows: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, Mapping):
                raise TypeError("active operator jobs query returned a non-object row")
            rows.append(dict(row))
        return rows

    def enqueue_current_stats(
        self,
        channel_id: str,
        *,
        expected_epoch: int,
        max_items: int | None,
        max_quota_units: int,
        max_target_attempts: int = 3,
    ) -> dict[str, Any]:
        data = _mapping(
            self._rpc(
                "enqueue_channel_current_stats_backfill",
                {
                    "p_expected_epoch": require_control_epoch(expected_epoch),
                    "p_channel_id": str(channel_id or "").strip(),
                    "p_max_items": None if max_items is None else int(max_items),
                    "p_max_quota_units": int(max_quota_units),
                    "p_max_target_attempts": int(max_target_attempts),
                },
            ),
            operation="operator job enqueue",
        )
        job = _mapping(data.get("job"), operation="operator job enqueue payload")
        return {"created": bool(data.get("created")), "job": job}

    def estimate_initial_inventory(self, channel_id: str) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "estimate_channel_initial_inventory_job",
                {"p_channel_id": str(channel_id or "").strip()},
            ),
            operation="initial inventory job estimate",
        )

    def get_initial_inventory_job(self, channel_id: str) -> dict[str, Any] | None:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel_id is required")
        return _mapping(
            self._rpc(
                "get_channel_initial_inventory_job",
                {"p_channel_id": channel_id},
            ),
            operation="initial inventory job status",
            allow_none=True,
        )

    def enqueue_initial_inventory(
        self,
        channel_id: str,
        *,
        expected_epoch: int,
        expected_items: int | None,
        expected_pages: int | None,
        expected_quota_units: int | None,
        max_items: int | None,
        max_pages: int | None,
        max_quota_units: int | None,
        max_attempts: int = 3,
        confirmed: bool,
    ) -> dict[str, Any]:
        data = _mapping(
            self._rpc(
                "enqueue_channel_initial_inventory_job",
                {
                    "p_expected_epoch": require_control_epoch(expected_epoch),
                    "p_channel_id": str(channel_id or "").strip(),
                    "p_expected_items": None if expected_items is None else int(expected_items),
                    "p_expected_pages": None if expected_pages is None else int(expected_pages),
                    "p_expected_quota_units": None if expected_quota_units is None else int(expected_quota_units),
                    "p_max_items": None if max_items is None else int(max_items),
                    "p_max_pages": None if max_pages is None else int(max_pages),
                    "p_max_quota_units": None if max_quota_units is None else int(max_quota_units),
                    "p_max_attempts": int(max_attempts),
                    "p_confirmed": bool(confirmed),
                },
            ),
            operation="initial inventory job enqueue",
        )
        job = _mapping(data.get("job"), operation="initial inventory enqueue payload")
        return {"created": bool(data.get("created")), "job": job}

    def candidate_page(self, *, limit=16, after=None, ceiling=None):
        return self._rpc("operator_candidate_page",
            dict(limit=limit, after=after, ceiling=ceiling), abort_on_denied=False)

    def claim_id(self, job_id, expected_channel, expected_epoch, worker_id, *, lease_seconds=1200):
        return _mapping(self._rpc("claim_operator_job_id", dict(job_id=job_id,
            expected_channel=expected_channel, expected_epoch=expected_epoch,
            worker_id=worker_id, lease_seconds=lease_seconds), abort_on_denied=False),
            operation="exact operator job claim", allow_none=True)

    def claim(self, worker_id: str, *, lease_seconds: int = 1200) -> dict[str, Any] | None:
        return _mapping(
            self._rpc(
                "claim_operator_job",
                {
                    "p_worker_id": str(worker_id or "").strip(),
                    "p_lease_seconds": int(lease_seconds),
                },
            ),
            operation="operator job claim",
            allow_none=True,
        )

    def claim_initial_hydration(
        self,
        channel_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 1200,
    ) -> dict[str, Any] | None:
        return _mapping(
            self._rpc(
                "claim_channel_initial_hydration_job",
                {
                    "p_channel_id": str(channel_id or "").strip(),
                    "p_worker_id": str(worker_id or "").strip(),
                    "p_lease_seconds": int(lease_seconds),
                },
            ),
            operation="channel initial hydration job claim",
            allow_none=True,
        )

    def prepare_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        limit: int = 50,
        lease_seconds: int = 1200,
    ) -> dict[str, Any]:
        data = _mapping(
            self._rpc(
                "prepare_operator_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_limit": int(limit),
                    "p_lease_seconds": int(lease_seconds),
                },
            ),
            operation="operator job chunk preparation",
        )
        job = _mapping(data.get("job"), operation="prepared operator job")
        video_ids = data.get("video_ids") or []
        if not isinstance(video_ids, list) or not all(isinstance(value, str) for value in video_ids):
            raise TypeError("prepared operator job returned invalid video IDs")
        return {
            "job": job,
            "video_ids": list(dict.fromkeys(video_ids)),
            "reserved_units": int(data.get("reserved_units") or 0),
            "deferred": bool(data.get("deferred")),
        }

    def prepare_initial_inventory_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        page_limit: int,
        lease_seconds: int = 1200,
    ) -> dict[str, Any]:
        data = _mapping(
            self._rpc(
                "prepare_initial_inventory_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_page_limit": int(page_limit),
                    "p_lease_seconds": int(lease_seconds),
                }, abort_on_denied=False,
            ),
            operation="initial inventory chunk preparation",
        )
        job = _mapping(data.get("job"), operation="prepared initial inventory job")
        return {
            "job": job,
            "action": str(data.get("action") or "blocked"),
            "page_limit": int(data.get("page_limit") or 0),
            "resume_page_token": data.get("resume_page_token"),
            "reserved_units": int(data.get("reserved_units") or 0),
            "video_type": str(data.get("video_type") or "long"),
            "transition_id": data.get("transition_id"),
            "scan_id": data.get("scan_id"),
            "evidence": _mapping(data.get("evidence"), operation="initial inventory evidence", allow_none=True),
        }

    def bind_initial_inventory_scan(
        self, job_id: str, job_lease_token: str, *, playlist_lease_token: str,
        scan_id: str, transition_id: str,
    ) -> dict[str, Any]:
        # Exact replay proves the binding but does not authorize another page.
        return _mapping(self._rpc("bind_initial_inventory_scan", {
            "p_job_id": str(job_id), "p_job_lease_token": str(job_lease_token),
            "p_playlist_lease_token": str(playlist_lease_token),
            "p_scan_id": str(scan_id), "p_transition_id": str(transition_id),
        }, abort_on_denied=False), operation="initial inventory scan binding")

    def prepare_initial_hydration_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        limit: int = 50,
        lease_seconds: int = 1200,
    ) -> dict[str, Any]:
        data = _mapping(
            self._rpc(
                "prepare_initial_hydration_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_limit": int(limit),
                    "p_lease_seconds": int(lease_seconds),
                },
            ),
            operation="initial hydration chunk preparation",
        )
        job = _mapping(data.get("job"), operation="prepared initial hydration job")
        video_ids = data.get("video_ids") or []
        if not isinstance(video_ids, list) or not all(isinstance(value, str) for value in video_ids):
            raise TypeError("prepared initial hydration job returned invalid video IDs")
        return {
            "job": job,
            "video_ids": list(dict.fromkeys(video_ids)),
            "reserved_units": int(data.get("reserved_units") or 0),
            "deferred": bool(data.get("deferred")),
        }

    def settle_initial_hydration_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        observed_quota_units: int,
        error_code: str | None,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "settle_initial_hydration_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_observed_quota_units": max(int(observed_quota_units), 0),
                    "p_error_code": str(error_code)[:120] if error_code else None,
                    "p_summary": dict(summary),
                },
            ),
            operation="initial hydration chunk settlement",
        )

    def verify_initial_hydration(self, job_id: str, lease_token: str) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "verify_initial_hydration_job",
                {"p_job_id": str(job_id), "p_lease_token": str(lease_token)},
            ),
            operation="initial hydration verification",
        )

    def settle_initial_inventory_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        reserved_units: int,
        pages_processed: int,
        items_processed: int,
        inserted: int,
        updated: int,
        next_page_token: str | None,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "settle_initial_inventory_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_reserved_units": max(int(reserved_units), 0),
                    "p_pages_processed": max(int(pages_processed), 0),
                    "p_items_processed": max(int(items_processed), 0),
                    "p_inserted": max(int(inserted), 0),
                    "p_updated": max(int(updated), 0),
                    "p_next_page_token": str(next_page_token) if next_page_token else None,
                    "p_summary": dict(summary),
                }, abort_on_denied=False,
            ),
            operation="initial inventory chunk settlement",
        )

    def settle_initial_stats(
        self,
        job_id: str,
        lease_token: str,
        *,
        reserved_units: int,
        observed_quota_units: int,
        status: str,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "settle_initial_inventory_stats",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_reserved_units": max(int(reserved_units), 0),
                    "p_observed_quota_units": max(int(observed_quota_units), 0),
                    "p_status": str(status or "error")[:64],
                    "p_summary": dict(summary),
                },
            ),
            operation="initial inventory stats settlement",
        )

    def fail_initial_inventory(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        playlist_not_found: bool = False,
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "fail_initial_inventory_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_error_code": str(error_code or "initial_inventory_error")[:120],
                    "p_playlist_not_found": bool(playlist_not_found),
                }, abort_on_denied=False,
            ),
            operation="initial inventory job failure",
        )

    def retry_initial_inventory(
        self,
        job_id: str,
        *,
        expected_epoch: int,
        retry_request_id: str,
        max_items: int | None,
        max_pages: int | None,
        max_quota_units: int | None,
        confirmed: bool,
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "retry_channel_initial_inventory_job_v5",
                {
                    "p_expected_epoch": require_control_epoch(expected_epoch),
                    "p_job_id": str(job_id),
                    "p_retry_request_id": str(retry_request_id),
                    "p_max_items": None if max_items is None else int(max_items),
                    "p_max_pages": None if max_pages is None else int(max_pages),
                    "p_max_quota_units": None if max_quota_units is None else int(max_quota_units),
                    "p_confirmed": bool(confirmed),
                },
            ),
            operation="initial inventory job retry",
        )

    def mark_completed_initial_hydration_failed(
        self, job_id: str, channel_id: str, *, error_count: int, expected_epoch: int,
    ) -> None:
        self._rpc("mark_completed_initial_hydration_failed", {
            "p_job_id": str(job_id), "p_channel_id": str(channel_id),
            "p_error_count": int(error_count), "p_expected_epoch": require_control_epoch(expected_epoch),
        })

    def settle_chunk(
        self,
        job_id: str,
        lease_token: str,
        *,
        observed_quota_units: int,
        error_code: str | None,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "settle_operator_job_chunk",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_observed_quota_units": max(int(observed_quota_units), 0),
                    "p_error_code": str(error_code)[:120] if error_code else None,
                    "p_summary": dict(summary),
                },
            ),
            operation="operator job chunk settlement",
        )

    def release(self, job_id: str, lease_token: str, *, error_code: str) -> dict[str, Any]:
        return _mapping(
            self._rpc(
                "release_operator_job",
                {
                    "p_job_id": str(job_id),
                    "p_lease_token": str(lease_token),
                    "p_error_code": str(error_code or "operator_worker_error")[:120],
                },
            ),
            operation="operator job release",
        )


    def _inventory_chain(self, name, *args, **kwargs):
        """Explicit future coordinator seam; unknown writes never auto-abort siblings."""
        from . import operator_job_inventory_chains
        from ..hot.hot_collection_control import dispatch_owned_rpc
        action = getattr(operator_job_inventory_chains, name)
        return _mapping(dispatch_owned_rpc(self.database, name, {}, lambda: action(
            self.database, *args, **kwargs), abort_on_denied=False), operation=name)

    def initialize_inventory_chains(self, job_id, lease_token):
        return self._inventory_chain("initialize_inventory_chains", job_id, lease_token)

    def prepare_inventory_chain(self, job_id, lease_token, video_type):
        return self._inventory_chain("prepare_inventory_chain", job_id, lease_token, video_type)

    def bind_inventory_chain(self, job_id, lease_token, video_type, *,
                             transition_id, scan_id, playlist_lease_token):
        return self._inventory_chain("bind_inventory_chain", job_id, lease_token, video_type,
            transition_id, scan_id, playlist_lease_token)

    def acknowledge_inventory_chain(self, job_id, lease_token, video_type, *, transition_id, resolve_unknown=False):
        return self._inventory_chain("acknowledge_inventory_chain", job_id, lease_token, video_type, transition_id, resolve_unknown=resolve_unknown)

    def cancel_prepared_inventory_chain(self, job_id, lease_token, video_type, *, transition_id):
        return self._inventory_chain("cancel_prepared_inventory_chain", job_id, lease_token, video_type, transition_id)

    def cancel_unsent_inventory_chain(self, job_id, lease_token, video_type, *,
                                      transition_id, scan_id, playlist_lease_token, fetch_unsent):
        return self._inventory_chain("cancel_unsent_inventory_chain", job_id, lease_token, video_type,
            transition_id, scan_id, playlist_lease_token, fetch_unsent=fetch_unsent)

    def finish_inventory_chains(self, job_id, lease_token):
        return self._inventory_chain("finish_inventory_chains", job_id, lease_token)

    def claim_inventory_chain_source(self, job_id, lease_token, video_type, *,
                                     transition_id, worker_id, run_id, lease_seconds=900):
        return self._inventory_chain("claim_inventory_chain_source", job_id, lease_token, video_type,
            transition_id, worker_id, run_id, lease_seconds)
