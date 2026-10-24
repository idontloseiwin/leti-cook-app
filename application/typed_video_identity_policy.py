"""Finite typed identity admission over native relational Boolean facts.

Membership defines type. Manual or validated automatic evidence can resolve
same-channel conflicts, never foreign-channel evidence. Clocks stay with callers.
"""

TYPED_IDENTITY_COMMON = (
    ("typed_type", True), ("channel_runtime", True),
    ("playlist_enabled", True), ("inventory_ready", True),
    ("scan_complete", True), ("foreign_present", False),
)
TYPED_IDENTITY_BRANCHES = (
    (("manual_valid", True),),
    (("automatic_valid", True),),
    (("exact_present", True), ("conflicting_present_type", False)),
)
TYPED_MANUAL_SOURCE = (("manual_valid", True),)
TYPED_IDENTITY_FACT_NAMES = frozenset(name for name, _ in TYPED_IDENTITY_COMMON) | frozenset(
    name for terms in TYPED_IDENTITY_BRANCHES for name, _ in terms)


def typed_identity_from_native_facts(facts):
    """Return exact EXISTS admission; SQL NULL only satisfies neither Boolean."""
    if not isinstance(facts, dict) or set(facts) != TYPED_IDENTITY_FACT_NAMES:
        raise ValueError("typed_identity_facts_invalid")
    if any(value is not None and type(value) is not bool for value in facts.values()):
        raise ValueError("typed_identity_facts_invalid")
    common = all(facts[name] is required for name, required in TYPED_IDENTITY_COMMON)
    branch = any(all(facts[name] is required for name, required in terms)
                 for terms in TYPED_IDENTITY_BRANCHES)
    return common and branch
