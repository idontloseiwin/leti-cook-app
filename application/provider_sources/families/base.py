"""The default family hook: no semantics beyond the registry."""


class Hook:
    """Override only what the registry cannot express."""

    def batch_accounting(self, payload, targets):
        """Return (failed IDs, summary reported) for one ID batch; raise ResponseInvalid on a broken page."""
        return frozenset(), False

    def finish_row(self, method, item, fields):
        return fields

    def row_identity(self, method, item):
        """Identity of a row of a method without one identity path: (kind, id), None or parse.REJECT_ROW."""
        return None

    def page_notes(self, method, payload):
        """Short fixed-form notes about one page's envelope (never row values, IDs or tokens)."""
        return ()

    def finish_result(self, method, rows):
        """Return (rows, notes) once all pages are read; notes are fixed codes with counts."""
        return rows, ()
