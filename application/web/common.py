"""Pure formatting and input helpers shared by HTTP adapters."""
import re

def normalize_label_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def has_control_characters(value):
    # The database and the user-work backup both reject these in stored names.
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def round_metric(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
