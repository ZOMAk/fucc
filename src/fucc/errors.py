"""Stable CLI failures. Messages must never contain private settings or credentials."""


class FuccError(Exception):
    def __init__(self, message: str, code: str = "STATE_INVALID", exit_code: int = 24):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code

