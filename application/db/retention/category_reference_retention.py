"""One bounded source-clock cleanup page for the singleton category reference."""

from __future__ import annotations

from ..base.postgres import DatabaseServerError


ERRORS = frozenset({
    "hot_category_reference_target_invalid", "hot_category_reference_limit_invalid",
    "hot_category_reference_cursor_invalid",
})

_CLOCK = """WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
SELECT to_jsonb(at)#>>'{}' AS at,
    to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM clock"""

_PAGE = """SELECT to_jsonb(retrieved_at)#>>'{}' AS source_at,
    isfinite(retrieved_at) AND retrieved_at<=%(cutoff)s::timestamptz AS expired,
    NOT isfinite(retrieved_at) OR retrieved_at>%(at)s::timestamptz AS invalid_clock
FROM public.youtube_category_reference
WHERE retrieved_at<=%(cutoff)s::timestamptz
   OR retrieved_at>%(at)s::timestamptz OR NOT isfinite(retrieved_at)
ORDER BY retrieved_at LIMIT 1"""

_LOCK = """SELECT to_jsonb(retrieved_at)#>>'{}' AS source_at
FROM public.youtube_category_reference WHERE singleton FOR UPDATE NOWAIT"""

_DELETE = """DELETE FROM public.youtube_category_reference
WHERE singleton AND (retrieved_at<=%(cutoff)s::timestamptz
    OR NOT isfinite(retrieved_at) OR retrieved_at>clock_timestamp())"""


def cleanup_hot_category_reference(transaction, *, target, after_id=None, limit=100, references=None):
    tx = transaction
    with tx.borrow():
        args = tx.fetch_one(
            "SELECT %s::text AS target,%s::text AS after_id,%s::integer AS limit",
            (target, after_id, limit),
        )
        if args["target"] != "category_reference":
            raise DatabaseServerError("hot_category_reference_target_invalid", code="P0001")
        if args["limit"] is None or not 1 <= args["limit"] <= 500:
            raise DatabaseServerError("hot_category_reference_limit_invalid", code="P0001")
        # The entire target is at most one row; it never issues a next cursor.
        if args["after_id"] is not None:
            raise DatabaseServerError("hot_category_reference_cursor_invalid", code="P0001")
        stamp = tx.fetch_one(_CLOCK)
        rows = tx.fetch_all(_PAGE, stamp, max_rows=1)
        deleted = deferred = invalid = 0
        oldest = None
        if rows:
            candidate = rows[0]
            try:
                with tx.savepoint():
                    locked = tx.fetch_one(_LOCK)
                    if locked is not None:
                        # A concurrent refresh can replace the candidate before
                        # this lock. The predicate is checked on the locked row.
                        deleted = tx.execute(_DELETE, stamp)
            except DatabaseServerError as error:
                if error.code != "55P03":
                    raise
                deferred = 1
                invalid = int(candidate["invalid_clock"] is True)
                if candidate["expired"] is True:
                    oldest = candidate["source_at"]
        return dict(
            target=target, as_of=stamp["at"], cutoff=stamp["cutoff"],
            scanned=len(rows), deleted=deleted, scrubbed=0, deferred=deferred,
            complete=True, next_cursor=None,
            oldest_remaining_expired_source_at=oldest,
            invalid_clock_remaining=invalid, unknown_expiry_remaining=0,
            priority_complete=True,
            priority_references=[dict(target=target,key=['singleton'])] if deferred else [],
        )
