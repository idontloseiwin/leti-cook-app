"""Bounded current-page read owner, using the process-owned PostgreSQL pool."""
import time

from research_query.validation import QueryRejected, ValidatedQuery
from ..base.postgres import Operation, DatabaseServerError, DatabaseUnavailable, DatabaseError
from ..base.read_context import http_read_scope
from .research_sql import compile_query


_SCOPE_REVISION = """WITH scope AS MATERIALIZED (
 SELECT channel_id,user_work_id FROM readable.channel_user_work
 WHERE NOT is_hidden AND CASE WHEN %(kind)s::text='channel' THEN channel_id=%(id)s::text
 ELSE folder_id=%(id)s::uuid END)
 SELECT CASE WHEN %(kind)s::text='channel' THEN EXISTS(SELECT 1 FROM scope)
 ELSE EXISTS(SELECT 1 FROM public.folders WHERE id=%(id)s::uuid) END AS source_exists,
 encode(sha256(convert_to(coalesce(string_agg(channel_id||':'||user_work_id::text,',' ORDER BY channel_id COLLATE "C"),''),'UTF8')),'hex')
 AS scope_revision FROM scope"""


class ResearchQueryRepository:
    def __init__(self, database):
        self.database = database

    def _read(self, statement, parameters, *, deadline, cancelled, name):
        def check():
            if cancelled() or time.monotonic() >= deadline:
                raise QueryRejected("query_budget_exceeded")
        check()
        remaining = max(1, min(5000, int((deadline-time.monotonic())*1000)))
        # On the installed PostgreSQL image JIT is on by default; compiling this
        # many-branch statement took about 2.5 s of a 2.7 s folder page, while
        # execution itself took 0.2 s. Disable it only for these reads.
        operation = Operation(name, read_only=True, repeatable_read=True,
                              statement_timeout_ms=remaining, total_timeout_ms=remaining,
                              lock_timeout_ms=min(250, remaining), disable_jit=True)
        try:
            # Read POSTs use the same restricted role as HTTP GETs. This does
            # not grant access or bypass the database's existing HOT admission.
            with http_read_scope(), self.database.transaction(operation) as tx:
                check()  # Includes pool wait; no work after cancellation.
                row = tx.fetch_one(statement, parameters)
                check()
            return row
        except QueryRejected:
            raise
        except DatabaseUnavailable as error:
            code = "query_busy" if error.reason in {"pool_timeout", "pool_queue_full"} else "backend_unavailable"
            raise QueryRejected(code) from None
        except DatabaseServerError as error:
            raise QueryRejected("query_budget_exceeded" if error.code in {"57014", "55P03"} else "backend_unavailable") from None
        except DatabaseError:
            raise QueryRejected("backend_unavailable") from None

    def read_page(self, plan, continuation, *, deadline, cancelled):
        if not isinstance(plan, ValidatedQuery):
            raise QueryRejected("invalid_query")
        statement, parameters = compile_query(plan, continuation)
        row = self._read(statement, parameters, deadline=deadline, cancelled=cancelled, name="research_query_page")
        if not isinstance(row, dict) or type(row.get("source_exists")) is not bool:
            raise QueryRejected("backend_unavailable")
        if not row["source_exists"]:
            raise QueryRejected("source_unavailable")
        if continuation and continuation["s"] != row["scope_revision"]:
            raise QueryRejected("cursor_context_changed")
        return row

    def recheck_scope(self, plan, revision, *, deadline, cancelled):
        scope = plan.request.scope
        row = self._read(_SCOPE_REVISION, {"kind": scope.kind, "id": scope.id},
                         deadline=deadline, cancelled=cancelled, name="research_query_scope")
        if not isinstance(row, dict):
            raise QueryRejected("backend_unavailable")
        if not row.get("source_exists") or row.get("scope_revision") != revision:
            raise QueryRejected("cursor_context_changed")
