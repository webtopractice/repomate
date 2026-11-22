class ToolError(Exception):
    """A tool failed in a way we can explain to the user (never a crash)."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable
