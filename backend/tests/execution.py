"""Backend selection and duration-balanced, file-owned CI partitioning."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

QUICK_DATABASE_FILES = frozenset({
    "test_auth.py", "test_branch_access.py", "test_p4b_day_grid_contract.py",
    "test_p4b_entry_contract.py", "test_payroll_lifecycle_evidence.py",
    "test_payroll_finalization_evidence.py", "test_status_direct_calculation.py",
    "test_security_matrix.py", "test_period_entry_overview.py",
})
DATABASE_FIXTURES = frozenset({"pg_instance", "schema_databases", "apply_schema", "server"})


def plan_files(files: Iterable[str], weights: Mapping[str, float], count: int) -> list[list[str]]:
    """Assign whole files exactly once, longest first, with deterministic tie breaks."""
    if count < 1:
        raise ValueError("Shard count must be positive")
    estimates = {name: float(weights.get(name, 5.0)) for name in set(files)}
    if any(not math.isfinite(value) or value <= 0 for value in estimates.values()):
        raise ValueError("File duration estimates must be finite and positive")
    groups: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for name in sorted(estimates, key=lambda name: (-estimates[name], name)):
        index = min(range(count), key=lambda index: (totals[index], index))
        groups[index].append(name)
        totals[index] += estimates[name]
    return [sorted(group) for group in groups]


def is_quick(file_name: str, fixtures: Iterable[str]) -> bool:
    return (file_name in QUICK_DATABASE_FILES
            or not DATABASE_FIXTURES.intersection(fixtures))


def pytest_addoption(parser):
    group = parser.getgroup("backend execution")
    group.addoption("--backend-suite", choices=("full", "quick"), default="full")
    group.addoption("--backend-shard-index", type=int, default=None)
    group.addoption("--backend-shard-count", type=int, default=4)
    group.addoption("--backend-plan", default=None, help="Write selected node IDs as JSON")


def pytest_collection_modifyitems(config, items):
    suite = config.getoption("--backend-suite")
    index = config.getoption("--backend-shard-index")
    count = config.getoption("--backend-shard-count")
    if count < 1 or (index is not None and not 0 <= index < count):
        raise pytest.UsageError("Backend shard index must be in [0, shard count)")
    root = Path(__file__).resolve().parents[1]

    def key(item):
        return item.path.relative_to(root).as_posix()

    eligible = [item for item in items if suite == "full" or is_quick(item.path.name, item.fixturenames)]
    weights = json.loads((Path(__file__).with_name("runtime_weights.json")).read_text(encoding="utf-8"))
    groups = plan_files((key(item) for item in eligible), weights, count)
    owned = set(groups[index]) if index is not None else {key(item) for item in eligible}
    selected = [item for item in eligible if key(item) in owned]
    chosen = {id(item) for item in selected}
    deselected = [item for item in items if id(item) not in chosen]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
    destination = config.getoption("--backend-plan")
    if destination:
        path = Path(destination)
        worker = getattr(config, "workerinput", {}).get("workerid")
        if worker:
            path = path.with_name(f"{path.stem}-{worker}{path.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "suite": suite, "shard_index": index, "shard_count": count,
            "selected_count": len(selected), "deselected_count": len(deselected),
            "files": sorted(owned), "nodeids": [item.nodeid for item in selected],
        }, indent=2) + "\n", encoding="utf-8")
