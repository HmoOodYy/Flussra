"""Windows compatibility for repository-owned temporary PostgreSQL clusters."""

from __future__ import annotations

import os

import testing.postgresql


class _WindowsPostgresql(testing.postgresql.Postgresql):
    """Let Windows initdb create the data directory inside its owned temp base."""

    # testing.postgresql.setup() pre-creates both directories. On Windows runners,
    # initdb may report ERROR_ALREADY_EXISTS when it creates the data path itself.
    # Keep the library's temp directory, but leave data absent for initdb to own.
    subdirectories = [
        subdirectory
        for subdirectory in testing.postgresql.Postgresql.subdirectories
        if subdirectory != "data"
    ]


def create_test_postgresql() -> testing.postgresql.Postgresql:
    """Create an isolated cluster, avoiding pre-created data dirs on Windows."""
    if os.name == "nt":
        return _WindowsPostgresql()
    return testing.postgresql.Postgresql()
