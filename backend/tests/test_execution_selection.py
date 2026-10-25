"""Selection preserves whole-file ownership and accounts for every collected case."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.execution import is_quick, plan_files


def test_balanced_shards_are_deterministic_disjoint_and_cover_every_file():
    weights = {"a": 10, "b": 8, "c": 6, "d": 4, "e": 2, "f": 2}
    first = plan_files(weights, weights, 4)
    assert first == plan_files(reversed(weights), weights, 4)
    assigned = [name for group in first for name in group]
    assert len(assigned) == len(set(assigned)) == len(weights)
    assert set(assigned) == set(weights)
    assert max(sum(weights[name] for name in group) for group in first) == 10


def test_new_files_are_included_without_timing_history():
    assert sorted(name for group in plan_files(["old", "new", "old"], {"old": 8}, 2)
                  for name in group) == ["new", "old"]


@pytest.mark.parametrize("count,weights", [(0, {}), (2, {"a": -1}), (2, {"a": float("nan")})])
def test_invalid_shard_configuration_is_refused(count, weights):
    with pytest.raises(ValueError):
        plan_files(["a"], weights, count)


def test_quick_selection_includes_pure_checks_and_named_database_regressions():
    assert is_quick("test_calculation_snapshot_hash.py", ["request"])
    assert is_quick("test_p4b_day_grid_contract.py", ["pg_instance", "schema_databases"])
    assert not is_quick("test_development_database_lifecycle.py", ["server", "rebuilt"])
    assert not is_quick("test_durable_driver_schema.py", ["schema_databases"])


def _runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_backend_tests.py"
    spec = importlib.util.spec_from_file_location("backend_test_command", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_focused_command_requires_an_explicit_target():
    with pytest.raises(SystemExit) as refused:
        _runner().main(["--suite", "focused"])
    assert refused.value.code == 2


def test_complete_command_forwards_ownership_and_child_failure(monkeypatch, tmp_path):
    runner = _runner()
    captured = {}

    def execute(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner.subprocess, "run", execute)
    assert runner.main(["--suite", "full", "--workers", "2", "--shard-index", "1",
                        "--plan", str(tmp_path / "plan.json"), "-x"]) == 1
    command = captured["command"]
    assert command[command.index("--backend-suite") + 1] == "full"
    assert command[command.index("--backend-shard-index") + 1] == "1"
    assert command[command.index("--dist=loadfile") - 1] == "2"
    assert command[-1] == "-x"
    assert captured["cwd"].name == "backend"


def test_explicit_paths_select_the_full_target_instead_of_the_quick_subset(monkeypatch):
    runner = _runner()
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **_: (
        calls.append(command) or SimpleNamespace(returncode=0)))
    assert runner.main(["tests/test_durable_driver_schema.py", "-x"]) == 0
    assert calls[0][calls[0].index("--backend-suite") + 1] == "full"
    assert "tests/test_durable_driver_schema.py" in calls[0]
