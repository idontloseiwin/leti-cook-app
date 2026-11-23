"""Explicit default-off policy for offline database fixtures."""
from research_field_policy import defaults

def preference_row():
    return dict(revision=1,document=defaults(),pending_import=None,purge_fields=[])

def preference_read(operation,statement,parameters):
    assert operation=='research_preferences_read'
    assert parameters==('installation',)
    return preference_row()
