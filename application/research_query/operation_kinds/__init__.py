"""Registered whole-population research operations: one module per operation.

An operation reads every authorized matching row of a validated query, before
pagination, and returns one bounded JSON result. The field-level observation
operations in operations.py are not kinds: they are columns inside queries.

Adding an operation: a new module next to summary.py that defines KIND, plus its
import and one entry in _MODULES below; its SQL goes into a module of the same
name in db/reads/research_operations/ with compile_sql and one entry in that
package's COMPILERS (and both packaging/files.json entries). Validation, SQL
compilation, execution, capabilities, the policy gate and the workshop graph
read these registries; none of them needs a per-operation branch.
KIND = OperationKind(definition, dependencies, check, provenance, block, lifetime):

- definition: OperationDefinition. The id is '<video|channel>.<name>.v<N>'; its
  prefix decides which source's capabilities list it. New meaning = new id.
- dependencies(params, source_id, scope_kind) -> field ids the result reads.
  Accept only exact keys, types and bounds and the supported source/scope;
  raise ValueError otherwise (invalid_query). The fields pass the same field
  registry and access context as select fields (grants, disabled fields).
  Here and in check raise only ValueError, TypeError or KeyError (they map to
  a public code); test types before any attribute access.
- db/reads/research_operations/<module>.compile_sql(plan, sql, params) -> (sql,
  params): append this kind's CTEs (text starting with ',') and a final SELECT to
  the population statement; keep scope_info.*, coverage.*, evaluated_at and one
  jsonb result column (FINAL_SELECT does this). It may add operation_valid_before.
- check(result) -> result: verify shape and bounds, apply presentation rounding
  (round_summary); raise ValueError on anything else (backend_unavailable).
- provenance(plan) -> dict, normally population_provenance(origin).
- block: research workshop block type, or None for no block.
- lifetime(checked result) -> the earliest expiry of an input that check read
  outside SQL (a reference label), or None; it shortens valid_before. Optional.

Policy: a definition grants nothing. An operation runs only if its id is in
AccessContext.allowed_operations. web/research.py builds that set per request
from definition.policy and its POLICY_FLAGS (every listed installation flag
on); an unknown policy never runs, a new policy needs a reviewed entry there.

Budgets (aggregate_service): one 8 s deadline for validation, admission wait,
SQL (statement itself at most 5 s) and output; at most 512 KiB of canonical
JSON. Nothing is stored.

SQL (the PostgreSQL gateway refuses violations): fixed text in the db module, no
';' anywhere (comments included), values only as %(name)s parameters,
identifiers only from a closed mapping of validated values, read-only views and
the population CTE, never user SQL or user-chosen identifiers.
"""
from types import MappingProxyType

from .kind import OperationKind
from . import summary
from . import calendar
from . import channel_history
from . import observed_growth_summary
from . import static_details
from . import observed_history
from . import launch_comparison
from . import lifetime_baseline
from . import fingerprint
from . import publication_rhythm
from . import publication_years
from . import content_breakdown
from . import counter_change
from . import format_totals
from . import facets
from . import view_pace_median

_MODULES = (
    summary,
    calendar,
    channel_history,
    observed_growth_summary,
    static_details,
    observed_history,
    launch_comparison,
    lifetime_baseline,
    fingerprint,
    publication_rhythm,
    publication_years,
    content_breakdown,
    counter_change,
    format_totals,
    facets,
    view_pace_median,
)


def _registry(modules):
    kinds = {}
    for module in modules:
        kind = module.KIND
        if not isinstance(kind, OperationKind) or kind.id in kinds:
            raise RuntimeError('invalid_operation_registry')
        kinds[kind.id] = kind
    blocks = [kind.block for kind in kinds.values() if kind.block is not None]
    if len(blocks) != len(set(blocks)):
        raise RuntimeError('invalid_operation_registry')
    return MappingProxyType(kinds)


OPERATION_KINDS = _registry(_MODULES)
