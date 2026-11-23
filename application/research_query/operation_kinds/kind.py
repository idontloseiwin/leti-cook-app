"""The contract every whole-population operation kind fills; shared helpers.

A leaf module: it imports nothing from the research_query package, so the
registry can be loaded by operations.py, validation.py and the SQL owner alike.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Callable


@dataclass(frozen=True)
class OperationDefinition:
    id: str
    source: str
    input_grain: str
    output_grain: str
    policy: str
    outputs: tuple[str, ...]
    semantics: str
    version: int = 1
    population: str = 'authorized_current_video_scope'
    history_budget: str = 'indexed_real_points; query_5s; service_8s'
    expiry: str = 'earliest_input_expiry'


@dataclass(frozen=True)
class Binding:
    """Server-resolved inputs of one execution, e.g. a checked frozen reference.

    parameters: extra SQL parameters the kind's compile reads from plan.binding.
    valid_before: the answer may not outlive this instant (None: no extra limit).
    finish: called after the read and its scope recheck, before answering; it
    raises QueryRejected when the bound input lapsed meanwhile.
    """
    parameters: dict
    valid_before: object = None
    finish: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class OperationKind:
    """One whole-population operation. See the package docstring for each part."""
    definition: OperationDefinition
    # (parameters, query source id, scope kind) -> field ids read; ValueError if not accepted.
    # The SQL of a kind lives in db/reads/research_operations/<module>.py (compile_sql there).
    dependencies: Callable[[object, str, str], tuple[str, ...]]
    # (database result dict) -> result: shape/bound checks and presentation rounding; ValueError if wrong.
    check: Callable[[dict], dict]
    # (plan) -> the response provenance object.
    provenance: Callable[[object], dict]
    # Research workshop block type offering this operation; None: no block.
    block: str | None = None
    # (service, plan, cancelled, deadline) -> Binding or None, run before the read
    # slot is taken (a frozen reference takes its own); None: nothing to bind.
    bind: Callable[[object, object, Callable[[], bool], float], Binding | None] | None = None
    # (checked result) -> earliest expiry (datetime or ISO text) of an input that
    # check itself read outside SQL, such as a reference label; None: SQL only.
    lifetime: Callable[[dict], object] | None = None
    # (checked result, formats turned off in config.ini) -> the result without
    # what it would show of them; None: the result names none.
    turned_off: Callable[[dict, frozenset], dict] | None = None

    @property
    def id(self):
        return self.definition.id


def population_provenance(origin):
    """Provenance of a result over all matching rows; origin names the input source."""
    def provenance(plan):
        return dict(origin=origin, method=plan.operation, population='all_authorized_matching_rows_before_pagination',
                    missing_values='excluded_per_metric', definition_epoch=None)
    return provenance


def round_summary(value):
    """Bounded scalar presentation; matches the half-even rounding rule.

    Sorting, populations, totals and quantiles are already computed in SQL.
    Decimal strings keep values above JS's exact integer range intact.
    """
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        number = Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_EVEN)
        return format(number, 'f').rstrip('0').rstrip('.') if number % 1 else str(int(number))
