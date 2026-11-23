"""Bounded current-page read owner, using the process-owned PostgreSQL pool."""
import time

from research_query.validation import QueryRejected, ValidatedQuery, check_budget
from ..base.postgres import Operation, DatabaseServerError, DatabaseUnavailable, DatabaseError
from ..base.read_context import http_read_scope
from .research_sql import compile_query, compile_scope_recheck

# One frozen statement reads a whole scope;
# the build keeps its 8-second work budget around it, counted from getting
# its read slot (research_query.service.FROZEN_WAIT_SECONDS).
FROZEN_STATEMENT_MS = 7000


class ResearchQueryRepository:
    def __init__(self, database):
        self.database = database

    def _read(self, statement, parameters, *, deadline, cancelled, name, statement_ms=5000):
        def check():
            # Past the deadline names the time limit; a client that left is not called a time limit.
            check_budget(deadline, cancelled)
        check()
        remaining = max(1, min(statement_ms, int((deadline-time.monotonic())*1000)))
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
            # Statement and lock timeouts name the time limit; 53400 (a server setting such as
            # temp_file_limit) names the memory limit: the read's working data did not fit.
            if error.code in {"57014", "55P03"}:
                raise QueryRejected("query_budget_exceeded", limit="time") from None
            if error.code == "53400":
                raise QueryRejected("query_budget_exceeded", limit="memory") from None
            raise QueryRejected("backend_unavailable") from None
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

    def read_frozen(self, plan, *, deadline, cancelled):
        """Every matching row of a frozen request in final order, in one statement."""
        if not isinstance(plan, ValidatedQuery) or plan.request.consistency != "frozen":
            raise QueryRejected("invalid_query")
        statement, parameters = compile_query(plan)
        row = self._read(statement, parameters, deadline=deadline, cancelled=cancelled,
                         name="research_query_frozen", statement_ms=FROZEN_STATEMENT_MS)
        if not isinstance(row, dict) or type(row.get("source_exists")) is not bool:
            raise QueryRejected("backend_unavailable")
        if not row["source_exists"]:
            raise QueryRejected("source_unavailable")
        return row

    def scope_revision(self,plan,*,deadline,cancelled):
        statement,parameters=compile_scope_recheck(plan)
        row=self._read(statement,parameters,deadline=deadline,cancelled=cancelled,name="research_query_scope")
        if not isinstance(row,dict):raise QueryRejected("backend_unavailable")
        if not row.get("source_exists"):raise QueryRejected("source_unavailable")
        return row["scope_revision"]

    def read_operation(self, plan, *, deadline, cancelled):
        from .research_aggregate import compile_operation
        statement,parameters=compile_operation(plan)
        row=self._read(statement,parameters,deadline=deadline,cancelled=cancelled,name="research_query_operation")
        if not isinstance(row,dict) or type(row.get("source_exists")) is not bool:
            raise QueryRejected("backend_unavailable")
        if not row["source_exists"]:raise QueryRejected("source_unavailable")
        return row

    def recheck_scope(self, plan, revision, *, deadline, cancelled):
        statement, parameters = compile_scope_recheck(plan)
        row = self._read(statement, parameters,
                         deadline=deadline, cancelled=cancelled, name="research_query_scope")
        if not isinstance(row, dict):
            raise QueryRejected("backend_unavailable")
        # A page is one repeatable-read snapshot. Counter writes after that
        # snapshot must not starve page one while collection is active. This
        # final check fences scope ownership/visibility, not newer data.
        if not row.get("source_exists") or row.get("scope_revision") != revision:
            raise QueryRejected("cursor_context_changed")
