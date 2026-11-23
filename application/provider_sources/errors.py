"""Fixed refusal codes of the source layer; never provider text, values or tokens."""


class PlanRejected(ValueError):
    """A request was refused before any network use. ``subject`` names a registry item, never a value."""

    def __init__(self, code, subject=None):
        super().__init__(code if subject is None else f"{code}:{subject}")
        self.code = code
        self.subject = subject


class ResponseInvalid(ValueError):
    """A provider page broke the registry contract; the page is rejected, its values are dropped."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
