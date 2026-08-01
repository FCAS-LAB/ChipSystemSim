#!/usr/bin/env python3
"""Generate deterministic 1/2/4/8-node configurations for the 8-rank MLP-DP job."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RANKS = 8
GPU_WORKERS_PER_RANK = 2


def phase_one_rank(process_index: int) -> int:
    """Map the fixed YAML order to its owning data-parallel rank."""
    if process_index < RANKS:
        return process_index
    return (process_index - RANKS) // GPU_WORKERS_PER_RANK


def call(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8], choices=[1, 2, 4, 8])
    arguments = parser.parse_args()

    source_yaml = ROOT / "real" / "mlp_dp.yml"
    source = yaml.safe_load(source_yaml.read_text(encoding="utf-8"))
    if len(source["phase1"]) != RANKS * (1 + GPU_WORKERS_PER_RANK):
        raise RuntimeError("MLP-DP YAML must contain exactly 8 CPUs and 16 GPU workers")

    for nodes in arguments.nodes:
        output = arguments.output_root / f"mlp-dp-nodes{nodes}"
        output.mkdir(parents=True, exist_ok=False)
        processes: list[dict[str, object]] = []
        for index, process in enumerate(source["phase1"]):
            rank = phase_one_rank(index)
            coordinates = [int(process["args"][-2]), int(process["args"][-1])]
            processes.append({
                "phase": "phase1",
                "process_index": index,
                "node_slot": rank % nodes,
                "node_label": f"chipsystemsim.node.{rank % nodes}",
                "coordinates": coordinates,
                "data_rank": rank,
            })
        # PopNet remains with the manager-side coordinator; it has no chiplet
        # coordinate and does not participate in the data-parallel placement.
        processes.append({"phase": "phase2", "process_index": 0, "node_slot": 0,
                          "node_label": "chipsystemsim.node.0"})
        placement = {
            "workload": "mlp-dp",
            "yaml": str(source_yaml),
            "swarm_nodes": nodes,
            "resource_contract": {"cpu_ranks": RANKS, "gpu_workers": RANKS * GPU_WORKERS_PER_RANK},
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
        call(str(ROOT / "real" / "generate_distributed_yaml.py"), "--source-yaml", str(source_yaml),
             "--placement", str(placement_path), "--output", str(workload_path), "--stream-output",
             "--benchmark-root", "/opt/legosim/artifact/MLP_DP", "--sniper-cores", "1",
             "--sniper-maxthreads", "1")
        call(str(ROOT / "real" / "generate_swarm_stack.py"), "--placement", str(placement_path),
             "--topology", str(topology_path), "--routing", str(routing_path),
             "--workload-yaml", str(workload_path), "--image", arguments.image, "--output", str(stack_path),
             "--stack-name", f"chipsystemsim_mlp_dp_{nodes}", "--topology-width", "6", "--flit-size", "4")


if __name__ == "__main__":
    main()
