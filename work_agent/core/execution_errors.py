"""Shared execution outcomes consumed by graph runners and gateways."""


class ExecutionBlocked(RuntimeError):
    """Execution requires reconciliation or a corrected/new request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ExecutionRetryable(RuntimeError):
    """The same task may resume without changing its operation keys."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)
