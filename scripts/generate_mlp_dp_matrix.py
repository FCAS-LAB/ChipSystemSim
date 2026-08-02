#!/usr/bin/env python3
"""Generate deterministic fixed-global-batch MLP-DP scaling configurations."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MAX_RANKS = 8
GPU_WORKERS_PER_RANK = 2


def phase_one_rank(process_index: int, ranks: int) -> int:
    """Map the fixed YAML order to its owning data-parallel rank."""
    if process_index < ranks:
        return process_index
    return (process_index - ranks) // GPU_WORKERS_PER_RANK


def call(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8], choices=[1, 2, 4, 8])
    parser.add_argument("--iterations", type=int, default=40,
                        help="fixed MLP-DP iterations at every node-count point")
    parser.add_argument("--global-samples", type=int, default=65536,
                        help="fixed global sample count at every node-count point")
    parser.add_argument("--ranks-per-node", type=int, default=1,
                        help="CPU data ranks colocated on each node; every rank owns two GPU workers")
    arguments = parser.parse_args()
    if arguments.iterations < 1 or arguments.global_samples < 1 or arguments.ranks_per_node < 1:
        raise ValueError("iterations, global samples, and ranks per node must be positive")

    source_yaml = ROOT / "real" / "mlp_dp.yml"
    source = yaml.safe_load(source_yaml.read_text(encoding="utf-8"))
    if len(source["phase1"]) != MAX_RANKS * (1 + GPU_WORKERS_PER_RANK):
        raise RuntimeError("MLP-DP YAML must contain exactly 8 CPUs and 16 GPU workers")

    for nodes in arguments.nodes:
        ranks = nodes * arguments.ranks_per_node
        if ranks > MAX_RANKS:
            raise ValueError(f"{nodes} nodes x {arguments.ranks_per_node} ranks exceeds {MAX_RANKS}")
        if arguments.global_samples % (ranks * GPU_WORKERS_PER_RANK) != 0:
            raise ValueError("global samples must divide evenly across all GPU workers")
        output = arguments.output_root / f"mlp-dp-nodes{nodes}"
        output.mkdir(parents=True, exist_ok=False)
        # The upstream template is ordered as eight CPU entries followed by
        # two GPU entries per rank. Retain only the active rank prefix.
        derived = dict(source)
        derived["phase1"] = (
            source["phase1"][:ranks]
            + source["phase1"][MAX_RANKS:MAX_RANKS + ranks * GPU_WORKERS_PER_RANK]
        )
        source_path = output / "source_workload.yml"
        source_path.write_text(yaml.safe_dump(derived, sort_keys=False), encoding="utf-8")
        processes: list[dict[str, object]] = []
        for index, process in enumerate(derived["phase1"]):
            rank = phase_one_rank(index, ranks)
            coordinates = [int(process["args"][-2]), int(process["args"][-1])]
            processes.append({
                "phase": "phase1",
                "process_index": index,
                "node_slot": rank // arguments.ranks_per_node,
                "node_label": f"chipsystemsim.node.{rank // arguments.ranks_per_node}",
                "coordinates": coordinates,
                "data_rank": rank,
            })
        # PopNet remains with the manager-side coordinator; it has no chiplet
        # coordinate and does not participate in the data-parallel placement.
        processes.append({"phase": "phase2", "process_index": 0, "node_slot": 0,
                          "node_label": "chipsystemsim.node.0"})
        placement = {
            "workload": "mlp-dp",
            "yaml": str(source_path),
            "swarm_nodes": nodes,
            "resource_contract": {
                "cpu_ranks": ranks,
                "gpu_workers": ranks * GPU_WORKERS_PER_RANK,
                "global_samples": arguments.global_samples,
                "ranks_per_node": arguments.ranks_per_node,
            },
            "processes": processes,
        }
        placement_path = output / "placement.json"
        placement_path.write_text(json.dumps(placement, indent=2) + "\n", encoding="utf-8")
        topology_path = output / "topology.json"
        routing_path = output / "routing.json"
        workload_path = output / "workload.yml"
        stack_path = output / "stack.yml"
        call(str(ROOT / "real" / "generate_simbricks_topology.py"), "--nodes", str(nodes),
             "--output", str(topology_path))
        call(str(ROOT / "real" / "generate_simbricks_routing.py"), "--placement", str(placement_path),
             "--topology", str(topology_path), "--output", str(routing_path))
        call(str(ROOT / "real" / "generate_distributed_yaml.py"), "--source-yaml", str(source_path),
             "--placement", str(placement_path), "--output", str(workload_path), "--stream-output",
             "--benchmark-root", "/opt/legosim/artifact/MLP_DP", "--sniper-cores", "1",
             "--sniper-maxthreads", "1")
        # The generated stack owns the exact run configuration, so the
        # iteration count cannot accidentally differ by node count.
        stack_command = [str(ROOT / "real" / "generate_swarm_stack.py"), "--placement", str(placement_path),
                         "--topology", str(topology_path), "--routing", str(routing_path),
                         "--workload-yaml", str(workload_path), "--image", arguments.image,
                         "--output", str(stack_path), "--stack-name", f"chipsystemsim_mlp_dp_{nodes}",
                         "--topology-width", "6", "--flit-size", "4", "--mlp-dp-iterations",
                         str(arguments.iterations), "--mlp-dp-ranks", str(ranks),
                         "--mlp-dp-samples", str(arguments.global_samples)]
        call(*stack_command)


if __name__ == "__main__":
    main()
