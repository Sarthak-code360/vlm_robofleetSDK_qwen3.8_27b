"""Autonomous factory: the central model turns requests into machine jobs and robot tasks.

    from robofleet import ModelClient
    from robofleet.factory import load_factory, serve

    factory = load_factory("examples/actuator_factory/factory.json", ModelClient.from_env())
    serve(factory, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

from pathlib import Path

from ..coordinator import load_json
from ..model import ModelClient
from .machines import DRIVERS, BambuMachine, Machine, MachineError, SimulatedPrinter, build_machine
from .robot_agent import CommandExecutor, FactoryClient, RobotAgent, SimulatedExecutor
from .runtime import Factory
from .server import make_server, serve

REQUIRED_KEYS = ("zones", "chargers", "rules", "machines")


def load_factory(path: str | Path, model: ModelClient | None = None, simulate_machines: bool = False,
                 log_path: str | Path | None = None, data_dir: str | Path | None = None) -> Factory:
    """Builds a :class:`Factory` from a factory file. Machine paths in it are relative to the file.

    ``data_dir`` keeps robots, machines and zones added from the dashboard, and uploaded documents,
    across restarts. Relative paths of dashboard-added machines resolve against the current folder.
    """
    path = Path(path)
    config = load_json(path)
    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    for spec in config["machines"]:
        if spec.get("zone") not in config["zones"]:
            raise ValueError(f"{path}: machine {spec.get('id')} has unknown zone {spec.get('zone')!r}")
    if config.get("default_output_zone") not in config["zones"]:
        raise ValueError(f"{path}: default_output_zone must be one of the zones")
    machines = {spec["id"]: build_machine(spec, path.parent, simulate_machines) for spec in config["machines"]}
    return Factory(config, machines, model, log_path, data_dir=data_dir, base_dir=Path.cwd(),
                   simulate_machines=simulate_machines)


__all__ = [
    "Factory", "load_factory", "serve", "make_server",
    "Machine", "SimulatedPrinter", "BambuMachine", "MachineError", "DRIVERS", "build_machine",
    "RobotAgent", "FactoryClient", "SimulatedExecutor", "CommandExecutor",
]
