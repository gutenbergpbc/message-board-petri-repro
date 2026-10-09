"""Discover trusted local definitions from scenarios/<id>/scenario.py."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from .scenario_types import Scenario

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"


def load_scenarios(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    """Load immediate scenario subdirectories; nested legacy archives are not active."""
    scenarios = {}
    for path in sorted(directory.glob("*/scenario.py")):
        spec = spec_from_file_location(f"message_board_petri.conditions.{path.parent.name}", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load scenario definition: {path}")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        scenario = getattr(module, "SCENARIO", None)
        if not isinstance(scenario, Scenario):
            raise TypeError(f"Expected a SCENARIO declaration: {path}")
        if scenario.id != path.parent.name:
            raise ValueError(f"Scenario ID must match directory name: {path}")
        scenarios[scenario.id] = scenario
    if not scenarios:
        raise ValueError(f"No scenario definitions found in {directory}")
    return scenarios


SCENARIOS = load_scenarios()
