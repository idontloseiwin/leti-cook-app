"""Shared parts of the Library Overview operations; not an operation itself.

Population: the query's authorized population, that is the public
videos of the one selected type admitted by the shared rules (membership,
availability, publication, stream completion). A stale value is unknown; the
video stays and is counted. Fingerprint, rhythm, years/calendar and content
breakdown all read this one population, so a bucket and a Videos page agree.

Period: a rolling window of publication time from the statement's own
evaluation instant: 7d/28d/3m/1y are 7/28/90/365 days of 24 hours (exact hours,
never calendar months), both ends inclusive; max keeps every video, including
one whose publication time is unknown. A rolling period cannot place a video
whose publication time is unknown: it is left out and counted
(population.publication_unknown_excluded).

Local hours, weekdays, dates and years use the query's own viewer time zone,
time.timezone (part of the query fingerprint). A result lives as long as
the data clocks of its inputs (kind.DATA_FINAL_SELECT), and no longer
than the first instant time alone would change it: the oldest video leaving a
rolling period (PERIOD_UNTIL), a catalogue-age anniversary, the next local year.

Parameters are a closed object. "population" is optional and today accepts only
{"kind": "query"}, the current query population (operations refuse a frozen
query). A frozen browse session can be added later as {"kind": "frozen",
"ref": ...} without changing any other parameter: it replaces the relation
`population` that PERIOD_CTES and the year CTEs read.
"""
from datetime import datetime, timezone

PERIODS = {'7d': ('7D', 7), '28d': ('28D', 28), '3m': ('3M', 90), '1y': ('1Y', 365), 'max': ('MAX', None)}
# Python str.strip() removes exactly these code points; SQL btrim gets the same set.
STRIP_CHARS = ''.join(chr(n) for n in range(0x110000) if chr(n).isspace())


def accept(params, required, optional=()):
    """Exact keys only; ValueError for anything else (invalid_query)."""
    if type(params) is not dict or not set(required) <= set(params) or set(params) - {*required, *optional, 'population'}:
        raise ValueError()
    if 'population' in params and params['population'] != {'kind': 'query'}:
        raise ValueError()
    if 'period' in params and (type(params['period']) is not str or params['period'] not in PERIODS):
        raise ValueError()


def time_zone_parameter(plan, params):
    """The viewer's zone of the query (time.timezone), validated by the contract."""
    params['time_zone'] = plan.query.request.time.timezone
    return params


def period_parameters(plan, params):
    key = plan.parameters['period']
    label, days = PERIODS[key]
    params.update(period_key=key, period_label=label, period_days=days,
                  period_hours=None if days is None else days * 24)
    return params


def utc_iso(value):
    """A timestamp read from JSON, as the shared backend writes instants."""
    if type(value) is not str:
        raise TypeError()
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError()
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError()
    return value


def check_period(period):
    if set(period) != {'key', 'label', 'kind', 'days', 'start_at', 'end_at'} or period['key'] not in PERIODS:
        raise ValueError()
    label, days = PERIODS[period['key']]
    if (period['label'], period['days'], period['kind']) != (label, days, 'max' if days is None else 'rolling'):
        raise ValueError()
    period['end_at'] = utc_iso(period['end_at'])
    period['start_at'] = None if days is None else utc_iso(period['start_at'])
    if days is None and period['start_at'] is not None:
        raise ValueError()
    return period


def check_population(population, extra=()):
    if set(population) != {'admitted', 'matched', 'publication_unknown_excluded', *extra}:
        raise ValueError()
    for key in ('admitted', 'matched', 'publication_unknown_excluded'):
        count(population[key])
    if population['matched'] + population['publication_unknown_excluded'] > population['admitted']:
        raise ValueError()
    return population


def share(part, whole):
    """Percentage of the population rounded to two decimal places."""
    if count(whole) == 0 or count(part) > whole:
        raise ValueError()
    value = round(part * 100 / whole, 2)
    return str(int(value)) if value.is_integer() else repr(value)
