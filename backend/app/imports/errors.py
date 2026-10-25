"""Stable failures shared by Import file and request-boundary helpers."""


class ImportFailure(Exception):
    """Expected, client-actionable failure while inspecting or receiving an Import."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
