"""Windows compatibility for repository-owned temporary PostgreSQL clusters."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import testing.postgresql


class _WindowsPostgresql(testing.postgresql.Postgresql):
    """Use upstream cluster setup while letting upstream clean our owned base."""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir=str(base_dir))
        # Database only removes base_dir when it created the path itself. This
        # invocation created the explicit path and transfers its ownership here.
        self._use_tmpdir = True


def _create_inherited_acl_base_dir() -> Path:
    """Create a unique base with normal Windows ACL inheritance, not mode 0o700."""
    temp_root = Path(tempfile.gettempdir())
    for _ in range(10):
        base_dir = temp_root / f"flussra-postgresql-{uuid4().hex}"
        try:
            # os.mkdir's default mode inherits the parent ACL on Windows.
            os.mkdir(base_dir)
        except FileExistsError:
            continue
        return base_dir
    raise RuntimeError(f"Could not allocate a unique PostgreSQL base under {temp_root}")


def create_test_postgresql() -> testing.postgresql.Postgresql:
    """Create an isolated PostgreSQL cluster with safe Windows ACL inheritance."""
    if os.name != "nt":
        return testing.postgresql.Postgresql()

    base_dir = _create_inherited_acl_base_dir()
    try:
        # Keep testing.postgresql's normal data/tmp directory and initdb lifecycle.
        return _WindowsPostgresql(base_dir)
    except BaseException:
        # This exact path was exclusively allocated above; no caller path is removed.
        if base_dir.exists():
            try:
                shutil.rmtree(base_dir)
            except OSError as cleanup_error:
                raise RuntimeError(
                    f"Could not remove failed invocation-owned PostgreSQL base {base_dir}"
                ) from cleanup_error
        raise
