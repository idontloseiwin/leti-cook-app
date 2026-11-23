"""Fixed startup signal generated from the shared acquisition-clock registry.

This retains the pre-updater finite-expiry semantics, including grace entry and
inclusive category completion. Update-copy admission uses a separate stricter
observation over the same sources.
"""
from .source_clocks import STARTUP_EXPIRY_SOURCES

STARTUP_EXPIRY_SQL = """WITH clock AS MATERIALIZED(
 SELECT clock_timestamp() AS at,%s::timestamptz AS cutoff,%s::boolean AS strict_entry)
SELECT to_jsonb(clock.at)#>>'{}' AS as_of,to_jsonb(clock.cutoff)#>>'{}' AS cutoff,
(""" + '\n OR '.join('EXISTS(' + query + ')' for query in STARTUP_EXPIRY_SOURCES.values()) + ') AS expired FROM clock'
