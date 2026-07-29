#!/usr/bin/env python3
"""Generate deterministic native-process placements for a Swarm experiment.

The generator never fabricates extra CPU/GPU simlets.  It reads the process
counts recorded from upstream YAML files and maps each original process to a
Swarm node label in round-robin order.  The resulting JSON is input to the
distributed coordinator, not a claim that Docker Compose alone can distribute
InterChiplet's local FIFO protocol.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
UPSTREAM_ROOT = ROOT.parent.parent / "LEGOSIM_MICRO"


def process_coordinates(process: dict[str, object], phase: str, index: int) -> list[int] | None:
    """Return the original phase-one chiplet coordinate when the YAML has one.

    LEGOSim application processes conventionally pass their X and Y chiplet
    coordinates as the first two positional arguments. Phase two Popnet does
    not represent a chiplet endpoint and deliberately receives no coordinate.
    """
    if phase != "phase1":
        return None
    arguments = process.get("args", [])
    if not isinstance(arguments, list) or len(arguments) < 2:
        raise ValueError(f"phase-one process {index} has no coordinate arguments")
    for first, second in zip(arguments, arguments[1:]):
        try:
            return [int(first), int(second)]
        except (TypeError, ValueError):
            continue
    raise ValueError(
        f"phase-one process {index} has no consecutive numeric chiplet coordinate pair"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workload", nargs="?", choices=("mlp", "dlrm", "resnet", "bfs"))
    parser.add_argument("--source-yaml", type=Path,
                        help="use an arbitrary complete LEGOSim YAML instead of the built-in manifest")
    parser.add_argument("--workload-name", help="name recorded for an arbitrary YAML")
    parser.add_argument("--nodes", type=int, choices=(1, 2, 4, 8), required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    if bool(arguments.workload) == bool(arguments.source_yaml):
        raise ValueError("provide exactly one of workload or --source-yaml")
    if arguments.source_yaml:
        source_yaml = arguments.source_yaml.resolve()
        workload_name = arguments.workload_name or source_yaml.stem
        source = {"repository": "local single-stage source", "revision": "workspace"}
    else:
        manifest = json.loads((ROOT / "workloads.json").read_text(encoding="utf-8"))
        workload = manifest["workloads"][arguments.workload]
        source_yaml = UPSTREAM_ROOT / str(workload["yaml"])
        workload_name = arguments.workload
        source = manifest["source"]
    source_config = yaml.safe_load(source_yaml.read_text(encoding="utf-8"))
    placement: list[dict[str, int | str]] = []
    for phase in ("phase1", "phase2"):
        phase_processes = source_config.get(phase, [])
        for process_index, process in enumerate(phase_processes):
            record: dict[str, int | str | list[int]] = {
                "phase": phase,
                "process_index": process_index,
                "node_slot": len(placement) % arguments.nodes,
                "node_label": f"legosim.node.{len(placement) % arguments.nodes}",
            }
            coordinates = process_coordinates(process, phase, process_index)
            if coordinates is not None:
                record["coordinates"] = coordinates
            placement.append(record)

    output = {
        "workload": workload_name,
        "yaml": str(source_yaml),
        "source": source,
        "swarm_nodes": arguments.nodes,
        "processes": placement,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
