#!/usr/bin/env python3
"""Generate deterministic native-process placements for a Swarm experiment.

The generator never fabricates extra CPU/GPU simlets.  It reads the process
counts recorded from upstream YAML files and maps each original process to a
Swarm node label in contiguous groups.  Thus an eight-process workload uses
one process per node at eight nodes, adjacent pairs at four nodes, and two
adjacent four-process groups at two nodes.  The resulting JSON is input to the
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


def communication_aware_mlp_order(
    source_processes: list[tuple[str, int, dict[str, object]]],
) -> list[tuple[str, int, dict[str, object]]]:
    """Order the original MLP processes for static affinity partitioning.

    The original MLP has four GPU simulators, one Sniper CPU, one DSA, one
    MNSIM process and one phase-two PopNet process.  Its dominant PipeComm
    edges are CPU-to-GPU matrix transfers and their responses.  Placing the
    CPU before the GPU processes makes the contiguous, equal-capacity groups
    keep as many of those edges local as possible:

    * 2 nodes: CPU + three GPUs share one four-process group;
    * 4 nodes: CPU + one GPU share one two-process group;
    * 8 nodes: every process must occupy its own group.

    DSA, MNSIM and PopNet are placed after the GPU set because their direct
    CPU exchanges are lower-volume control/transpose/inference transfers.
    The order is static, deterministic and preserves every upstream process.
    """
    by_identity = {(phase, index): process for phase, index, process in source_processes}
    desired_identities = [
        ("phase1", 4),  # Sniper CPU at (5, 5), source of the GPU transfers.
        ("phase1", 0),
        ("phase1", 1),
        ("phase1", 2),
        ("phase1", 3),
        ("phase1", 5),  # DSA at (2, 0).
        ("phase1", 6),  # MNSIM at (0, 3).
        ("phase2", 0),  # Coordinator-owned PopNet.
    ]
    if set(by_identity) != set(desired_identities):
        raise ValueError(
            "communication-aware MLP placement requires the unmodified "
            "four-GPU, CPU, DSA, MNSIM and PopNet process graph"
        )
    return [(phase, index, by_identity[(phase, index)]) for phase, index in desired_identities]


def rank_aware_mlp_order(
    source_processes: list[tuple[str, int, dict[str, object]]], nodes: int
) -> list[tuple[str, int, dict[str, object]]]:
    """Apply the MLP-specific static affinity groups.

    For two nodes, rank-0 GPU workers remain with the single CPU process and
    MNSIM, which are on the MLP critical path.  Rank-1 GPU workers remain
    together on the other node with DSA and PopNet.  The order is chosen so
    the existing equal-capacity partitioner produces these two exact groups.
    A one-node run naturally keeps every original process local.
    """
    by_identity = {(phase, index): process for phase, index, process in source_processes}
    required = {
        ("phase1", 0), ("phase1", 1), ("phase1", 2), ("phase1", 3),
        ("phase1", 4), ("phase1", 5), ("phase1", 6), ("phase2", 0),
    }
    if set(by_identity) != required:
        raise ValueError("rank-aware MLP placement requires the unmodified native MLP process graph")
    if nodes == 1:
        identities = [
            ("phase1", 4), ("phase1", 0), ("phase1", 1), ("phase1", 6),
            ("phase1", 2), ("phase1", 3), ("phase1", 5), ("phase2", 0),
        ]
    elif nodes == 2:
        identities = [
            # Node 0: CPU, GPU rank 0, and its MNSIM critical-path peer.
            ("phase1", 4), ("phase1", 0), ("phase1", 1), ("phase1", 6),
            # Node 1: GPU rank 1 plus the remaining accelerator/post phase.
            ("phase1", 2), ("phase1", 3), ("phase1", 5), ("phase2", 0),
        ]
    else:
        raise ValueError("rank-aware MLP placement is defined only for 1 or 2 nodes")
    return [(phase, index, by_identity[(phase, index)]) for phase, index in identities]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workload", nargs="?", choices=("mlp", "dlrm", "resnet", "bfs"))
    parser.add_argument("--source-yaml", type=Path,
                        help="use an arbitrary complete LEGOSim YAML instead of the built-in manifest")
    parser.add_argument("--workload-name", help="name recorded for an arbitrary YAML")
    parser.add_argument(
        "--placement-policy",
        choices=("contiguous", "communication-aware", "rank-aware"),
        default="contiguous",
        help="static process order before equal-capacity worker partitioning",
    )
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
    source_processes: list[tuple[str, int, dict[str, object]]] = []
    for phase in ("phase1", "phase2"):
        phase_processes = source_config.get(phase, [])
        for process_index, process in enumerate(phase_processes):
            source_processes.append((phase, process_index, process))
    if len(source_processes) % arguments.nodes != 0:
        raise ValueError(
            f"{len(source_processes)} native processes cannot be evenly grouped across "
            f"{arguments.nodes} nodes"
        )
    processes_per_node = len(source_processes) // arguments.nodes
    if arguments.placement_policy == "communication-aware":
        if workload_name != "mlp":
            raise ValueError("communication-aware placement is currently defined only for workload 'mlp'")
        source_processes = communication_aware_mlp_order(source_processes)
    elif arguments.placement_policy == "rank-aware":
        if workload_name != "mlp":
            raise ValueError("rank-aware placement is currently defined only for workload 'mlp'")
        source_processes = rank_aware_mlp_order(source_processes, arguments.nodes)
    placement: list[dict[str, int | str]] = []
    for global_index, (phase, process_index, process) in enumerate(source_processes):
        node_slot = global_index // processes_per_node
        record: dict[str, int | str | list[int]] = {
            "phase": phase,
            "process_index": process_index,
            "node_slot": node_slot,
            "node_label": f"chipsystemsim.node.{node_slot}",
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
        "placement_policy": arguments.placement_policy,
        "processes": placement,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
