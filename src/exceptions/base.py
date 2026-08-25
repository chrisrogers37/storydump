"""Base exception classes for Storydump."""


class StorydumpError(Exception):
    """
    Base exception for all Storydump errors.

    All custom exceptions in the application should inherit from this class
    to enable consistent error handling and catching.
    """

    pass


class RefusalError(StorydumpError):
    """A typed refusal carrying a closed ``reason`` vocabulary.

    The shape three tiers had each written out for themselves: a machine-
    routable reason, an optional human detail, and a message built from both.
    What varies between them is only the vocabulary and the prefix, so those
    are what a subclass declares — the constructor is not a fourth place to
    re-derive the same three lines.

    ``reason`` is the contract: callers route on it and must never parse the
    message. Every reason a subclass can carry belongs in that subclass's
    docstring, because a vocabulary that is closed in prose and open in
    practice is not closed.
    """

    #: What the message says was refused. Subclasses set it.
    _prefix = "refused"

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"{self._prefix}: {reason}" + (f" — {detail}" if detail else "")
        )
