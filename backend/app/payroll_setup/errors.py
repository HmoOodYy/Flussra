"""Stable errors for internal Payroll Setup policy services."""


class PolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
