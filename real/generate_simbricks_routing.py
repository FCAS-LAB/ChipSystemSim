#!/usr/bin/env python3
"""Combine native process placement and point-to-point BaseIf topology."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--placement", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--worker-physical-slots",
        help=("comma-separated physical-host slot for each logical worker; "
              "omit for one physical host (the DinD default)"),
    )
    arguments = parser.parse_args()

    placement = json.loads(arguments.placement.read_text(encoding="utf-8"))
    topology = json.loads(arguments.topology.read_text(encoding="utf-8"))
    nodes = int(placement["swarm_nodes"])
    if len(topology["workers"]) != nodes:
        raise ValueError("placement and topology disagree on worker count")

    if arguments.worker_physical_slots:
        physical_slots = [int(value) for value in arguments.worker_physical_slots.split(",")]
        if len(physical_slots) != nodes or any(value < 0 for value in physical_slots):
            raise ValueError("--worker-physical-slots must give one non-negative value per logical worker")
    else:
        # DinD models several logical LEGOSim workers on one physical host.
        # Do not silently promote logical-worker traffic to physical-network
        # traffic in the resulting measurement.
        physical_slots = [0] * nodes

    coordinate_to_slot: dict[str, int] = {}
    for process in placement["processes"]:
        coordinates = process.get("coordinates")
        if coordinates is None:
            continue
        key = f"{coordinates[0]},{coordinates[1]}"
        slot = int(process["node_slot"])
        previous = coordinate_to_slot.setdefault(key, slot)
        if previous != slot:
            raise ValueError(f"chiplet coordinate {key} was placed on multiple worker slots")

    channel_by_pair: dict[str, str] = {}
    for channel in topology["channels"]:
        left = int(channel["listener_slot"])
        right = int(channel["connector_slot"])
        channel_by_pair[f"{left},{right}"] = str(channel["id"])

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(
            {
                "version": 1,
                "transport": topology["transport"],
                "workload": placement["workload"],
                "coordinate_to_worker_slot": coordinate_to_slot,
                "worker_physical_slots": {
                    str(worker_slot): physical_slots[worker_slot] for worker_slot in range(nodes)
                },
                "channel_by_worker_pair": channel_by_pair,
                "gateway_base_port": 9500,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
