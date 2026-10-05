"""Windows compatibility for repository-owned temporary PostgreSQL clusters."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import testing.postgresql


class _WindowsPostgresql(testing.postgresql.Postgresql):
    """Use PostgreSQL's Windows restricted-token launcher for the server."""

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir=str(base_dir))
        # Database only removes base_dir when it created the path itself. This
        # invocation created the explicit path and transfers its ownership here.
        self._use_tmpdir = True

    @property
    def _pg_ctl(self) -> Path:
        pg_ctl = Path(self.postgres).with_name("pg_ctl.exe")
        if not pg_ctl.is_file():
            raise RuntimeError(f"PostgreSQL pg_ctl.exe is unavailable beside {self.postgres}")
        return pg_ctl

    def start(self) -> None:
        """Start via pg_ctl so PostgreSQL supplies its Windows restricted token."""
        if self.child_process:
            return

        self.prestart()
        log_path = Path(self.base_dir) / f"{self.name}.log"
        timeout = str(int(self.settings.get("boot_timeout", self.DEFAULT_BOOT_TIMEOUT)))
        server_options = subprocess.list2cmdline(self.get_server_commandline()[1:])
        result = subprocess.run(
            [
                str(self._pg_ctl),
                "start",
                "-D",
                self.get_data_directory(),
                "-l",
                str(log_path),
                "-w",
                "-t",
                timeout,
                "-o",
                server_options,
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("pg_ctl failed to start PostgreSQL; see its output above")

        # pg_ctl -w has completed the server readiness wait. Keep a non-null
        # marker for testing.common.database's ownership/cleanup lifecycle.
        self.child_process = _PgCtlProcess(self._pg_ctl, Path(self.get_data_directory()))
        try:
            self.poststart()
        except BaseException:
            self.stop()
            raise

    def terminate(self, *args: object) -> None:
        """Stop the exact server through the same PostgreSQL control utility."""
        if self.child_process is None:
            return

        result = subprocess.run(
            [
                str(self._pg_ctl),
                "stop",
                "-D",
                self.get_data_directory(),
                "-m",
                "fast",
                "-w",
                "-t",
                str(int(self.DEFAULT_KILL_TIMEOUT)),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("pg_ctl failed to stop PostgreSQL; see its output above")
        self.child_process = None


class _PgCtlProcess:
    """Minimal running-server marker required by testing.common.database."""

    def __init__(self, pg_ctl: Path, data_dir: Path) -> None:
        self.pg_ctl = pg_ctl
        self.data_dir = data_dir
        self.pid = self._read_pid()

    def _read_pid(self) -> int | None:
        try:
            return int((self.data_dir / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])
        except (IndexError, OSError, ValueError):
            return None

    def poll(self) -> int | None:
        result = subprocess.run(
            [str(self.pg_ctl), "status", "-D", str(self.data_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        return None if result.returncode == 0 else result.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("pg_ctl", timeout)
            time.sleep(0.1)
        return 0


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
