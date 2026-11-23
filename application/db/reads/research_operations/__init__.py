"""SQL of the registered research operations: one module per operation kind.

research_query.operation_kinds holds what an operation is (definition,
dependencies, result checks, provenance, binding); the module of the same name
here holds the database text it appends to the shared population statement
(compile_sql). COMPILERS maps each registered operation id to its compile_sql;
an operation without SQL here, or SQL without a registered operation, stops the
import. Shared parts: common (final SELECT texts), population_stats (views
definition rule and exact quantiles), overview (Overview period CTEs),
inspection (the inspected video of the Inspector operations).
"""
from types import MappingProxyType

from research_query.operation_kinds import OPERATION_KINDS
from research_query.operation_kinds import (calendar as calendar_kind, channel_history as channel_history_kind,
                                            content_breakdown as content_breakdown_kind,
                                            counter_change as counter_change_kind, facets as facets_kind,
                                            fingerprint as fingerprint_kind, format_totals as format_totals_kind,
                                            launch_comparison as launch_comparison_kind,
                                            lifetime_baseline as lifetime_baseline_kind,
                                            observed_growth_summary as observed_growth_summary_kind,
                                            observed_history as observed_history_kind,
                                            publication_rhythm as publication_rhythm_kind,
                                            publication_years as publication_years_kind,
                                            static_details as static_details_kind, summary as summary_kind,
                                            view_pace_median as view_pace_median_kind)
from . import (calendar, channel_history, content_breakdown, counter_change, facets, fingerprint, format_totals,
               launch_comparison, lifetime_baseline, observed_growth_summary, observed_history, publication_rhythm,
               publication_years, static_details, summary, view_pace_median)

_PAIRS = (
    (summary_kind, summary),
    (calendar_kind, calendar),
    (channel_history_kind, channel_history),
    (observed_growth_summary_kind, observed_growth_summary),
    (static_details_kind, static_details),
    (observed_history_kind, observed_history),
    (launch_comparison_kind, launch_comparison),
    (lifetime_baseline_kind, lifetime_baseline),
    (fingerprint_kind, fingerprint),
    (publication_rhythm_kind, publication_rhythm),
    (publication_years_kind, publication_years),
    (content_breakdown_kind, content_breakdown),
    (counter_change_kind, counter_change),
    (format_totals_kind, format_totals),
    (facets_kind, facets),
    (view_pace_median_kind, view_pace_median),
)


def _compilers(pairs):
    compilers = {}
    for kind_module, sql_module in pairs:
        identity = kind_module.KIND.id
        if identity in compilers or not callable(getattr(sql_module, 'compile_sql', None)):
            raise RuntimeError('invalid_operation_sql_registry')
        compilers[identity] = sql_module.compile_sql
    if set(compilers) != set(OPERATION_KINDS):
        raise RuntimeError('invalid_operation_sql_registry')
    return MappingProxyType(compilers)


COMPILERS = _compilers(_PAIRS)
