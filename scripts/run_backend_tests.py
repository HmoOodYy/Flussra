"""Run focused, everyday, complete or duration-balanced backend pytest selections."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _is_backend_test_target(argument: str, backend_dir: Path, tests_root: Path) -> bool:
    if argument.startswith("-"):
        return False
    target = argument.split("::", 1)[0]
    if not target:
        return False
    try:
        path = Path(target)
        if not path.is_absolute():
            path = backend_dir / path
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved == tests_root or tests_root in resolved.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("focused", "quick", "full"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=4)
    parser.add_argument("--plan", help="Write selected test IDs to a JSON artifact")
    parser.add_argument("--junitxml", help="Write pytest's JUnit timing/result artifact")
    options, extra = parser.parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]
    backend_dir = (Path(__file__).resolve().parents[1] / "backend").resolve()
    tests_root = (backend_dir / "tests").resolve()
    has_test_target = any(
        _is_backend_test_target(argument, backend_dir, tests_root) for argument in extra
    )
    options.suite = options.suite or ("focused" if has_test_target else "quick")
    if options.workers < 1 or options.shard_count < 1:
        parser.error("workers and shard-count must be positive")
    if options.shard_index is not None and not 0 <= options.shard_index < options.shard_count:
        parser.error("shard-index must be in [0, shard-count)")
    if options.suite == "focused" and not has_test_target:
        parser.error("focused runs require an explicit tests/ path or node ID")
    args = [sys.executable, "-m", "pytest", "-ra", "--tb=short", "--durations=20",
            "--backend-suite", "quick" if options.suite == "quick" else "full"]
    if options.workers > 1:
        args.extend(["-n", str(options.workers), "--dist=loadfile"])
    if options.shard_index is not None:
        args.extend(["--backend-shard-index", str(options.shard_index),
                     "--backend-shard-count", str(options.shard_count)])
    if options.plan:
        args.extend(["--backend-plan", str(Path(options.plan).resolve())])
    if options.junitxml:
        args.append(f"--junitxml={Path(options.junitxml).resolve()}")
    args.extend(extra)
    return subprocess.run(args, cwd=backend_dir, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
