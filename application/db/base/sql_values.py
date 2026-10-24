"""Value/identifier encoding for explicit repository SQL, without a query DSL."""
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
import math
import re
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


def identifier(name):
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError('database_identifier_invalid')
    return sql.Identifier(name)


def projection(columns):
    """Quote a repository-owned comma-separated projection; no expressions."""
    if not isinstance(columns, str) or not columns:
        raise ValueError('database_projection_invalid')
    if columns == '*':
        return sql.SQL('*')
    names = [part.strip() for part in columns.split(',')]
    if len(names) != len(set(names)) or len(names) > 256:
        raise ValueError('database_projection_invalid')
    return sql.SQL(', ').join(identifier(name) for name in names)


def _value(name, value, json_columns):
    return Jsonb(value) if name in json_columns and value is not None else value


def assignments(values, *, json_columns=()):
    """Return SET terms and bound values. The repository owns table and WHERE."""
    if not isinstance(values, Mapping) or not values or len(values) > 256:
        raise ValueError('database_assignments_invalid')
    terms = [sql.SQL('{} = %s').format(identifier(name)) for name in values]
    parameters = tuple(_value(name, value, json_columns) for name, value in values.items())
    return sql.SQL(', ').join(terms), parameters


def insert_values(rows, *, json_columns=()):
    """Return columns, VALUES terms, and parameters. Missing values use DEFAULT."""
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 1000:
        raise ValueError('database_insert_batch_invalid')
    if any(not isinstance(row, Mapping) or not row for row in rows):
        raise ValueError('database_insert_row_invalid')
    names = list(dict.fromkeys(name for row in rows for name in row))
    if len(names) > 256:
        raise ValueError('database_insert_row_invalid')
    columns = sql.SQL(', ').join(identifier(name) for name in names)
    parameters, batches = [], []
    for row in rows:
        terms = []
        for name in names:
            if name in row:
                terms.append(sql.SQL('%s'))
                parameters.append(_value(name, row[name], json_columns))
            else:
                terms.append(sql.SQL('DEFAULT'))
        batches.append(sql.SQL('({})').format(sql.SQL(', ').join(terms)))
    return columns, sql.SQL(', ').join(batches), tuple(parameters)


def application_value(value):
    """Keep the JSON-shaped repository contract formerly decoded from HTTP."""
    if value is None or type(value) in (str, bool, int):
        return value
    value_type = type(value)
    # Driver rows and decoded JSON use exact built-in containers. Keep their
    # key validation and copy behavior, with subclass protocols below unchanged.
    if value_type is dict:
        if any(not isinstance(key, str) for key in value):
            raise ValueError('database_result_key_invalid')
        return {key: application_value(item) for key, item in value.items()}
    if value_type is list:
        return [application_value(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('database_result_key_invalid')
        return {key: application_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [application_value(item) for item in value]
    raise ValueError('database_result_value_invalid')
