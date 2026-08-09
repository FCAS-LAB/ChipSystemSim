#!/usr/bin/env python3
"""Generate native LEGOSim block-GEMM DinD/Swarm configurations.

The upstream Matmul benchmark has a fixed four-GPU process graph.  This
generator instead keeps one 480x64 by 64x64 GEMM fixed while assigning a
contiguous row block to each native GPGPU-Sim process.  By default, two GPU
ranks are placed on each logical Docker/Swarm worker.  ``--gpu-ranks`` fixes
the simulated component count across all machine-count points, which is the
fair counterpart of the paper's multi-machine experiment: only placement and
available host CPU resources change.  The Sniper controller dispatches every
block before it receives any result; therefore the GPU compute phases can
overlap and the experiment measures a meaningful strong-scaling window.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GLOBAL_ROWS = 480
INNER = 64
COLUMNS = 64
MAX_RANKS = 35


def call(*arguments: str) -> None:
    """Run one common generator and preserve its diagnostics on failure."""
    subprocess.run([sys.executable, *arguments], check=True)


def gpu_coordinate(rank: int) -> tuple[int, int]:
    """Map ranks onto all 6x6 chiplets except controller coordinate (5,5)."""
    return rank % 6, rank // 6


def source_workload(rank_count: int) -> dict[str, object]:
    """Return the unwrapped process graph consumed by the generic wrapper."""
    phase1: list[dict[str, object]] = [
        {
            "cmd": "$SIMULATOR_ROOT/snipersim/run-sniper",
            "args": ["--", "/opt/legosim/artifact/matmul_dp/bin/matmul_dp_c",
                     "5", "5", str(rank_count)],
            "log": "sniper.matmul_dp.log",
            "is_to_stdout": True,
            "clock_rate": 1,
        }
    ]
    for rank in range(rank_count):
        coordinate_x, coordinate_y = gpu_coordinate(rank)
        phase1.append(
            {
                "cmd": "/opt/legosim/artifact/matmul_dp/bin/matmul_dp_cu",
                "args": [str(coordinate_x), str(coordinate_y), str(rank), str(rank_count)],
                "log": f"gpgpusim.matmul_dp.{rank}.log",
                "is_to_stdout": True,
                "clock_rate": 1,
                "pre_copy": "$SIMULATOR_ROOT/gpgpu-sim/configs/tested-cfgs/SM7_TITANV/*",
            }
        )

    # The generic distributed-YAML generator reads -A/-F/-G from this
    # upstream-compatible PopNet placeholder and replaces the phase with its
    # coordinator-local ns-3 adapter.  The mesh file is present in artifact/
    # matmul, keeping the timing topology explicit and reproducible.
    return {
        "phase1": phase1,
        "phase2": [
            {
                "cmd": "$SIMULATOR_ROOT/popnet_chiplet/build/popnet",
                "args": [
                    "-A", "36", "-c", "1", "-V", "3", "-B", "12", "-O", "12",
                    "-F", "4", "-L", "1000", "-T", "1000000000", "-r", "1",
                    "-I", "../bench.txt", "-R", "4",
                    "-G", "../topology/mesh_6_6_flit_4.gv", "-D", "../delayInfo.txt", "-P",
                ],
                "log": "popnet.matmul_dp.log",
                "is_to_stdout": True,
                "clock_rate": 1,
            }
        ],
        "bench_file": "./bench.txt",
        "delayinfo_file": "./delayInfo.txt",
    }


def placement(node_count: int, rank_count: int) -> dict[str, object]:
    """Place the controller and an equal contiguous rank group per worker."""
    ranks_per_worker = rank_count // node_count
    processes: list[dict[str, object]] = [
        {
            "phase": "phase1",
            "process_index": 0,
            "node_slot": 0,
            "coordinates": [5, 5],
            "role": "sniper-controller",
        }
    ]
    for rank in range(rank_count):
        coordinate_x, coordinate_y = gpu_coordinate(rank)
        processes.append(
            {
                "phase": "phase1",
                "process_index": rank + 1,
                "node_slot": rank // ranks_per_worker,
                "coordinates": [coordinate_x, coordinate_y],
                "role": "gpgpu-block-rank",
                "rank": rank,
            }
        )
    # Phase two remains coordinator-local for prompt delayInfo feedback, but
    # the placement entry is retained to satisfy the common metadata schema.
    processes.append(
        {"phase": "phase2", "process_index": 0, "node_slot": 0,
         "role": "coordinator-local-ns3"}
    )
    return {
        "version": 1,
        "workload": "matmul_dp_block_gemm",
        "swarm_nodes": node_count,
        "global_matrix": {"rows": GLOBAL_ROWS, "inner": INNER, "columns": COLUMNS},
        "gpu_ranks": rank_count,
        "processes": processes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8],
                        choices=[1, 2, 4, 8])
    parser.add_argument("--gpu-ranks", type=int,
                        help=("fix the total GPU simlet count at every node point; "
                              "it must divide 240 and be evenly placeable on every node count"))
    parser.add_argument("--stream-output", action="store_true")
    parser.add_argument("--ns3-cycle-ns", type=int, default=1)
    parser.add_argument("--ns3-link-rate", default="128Gbps")
    parser.add_argument("--ns3-link-delay-ns", type=int, default=1)
    parser.add_argument("--ns3-queue-packets", type=int, default=100000)
    parser.add_argument(
        "--ns3-localize-cross-worker-network", action="store_true",
        help=("generate the local-network counterfactual: keep process placement and "
              "PipeComm functional transport, but replace only cross-worker ns-3 delays "
              "with zero-cycle local timing"),
    )
    arguments = parser.parse_args()
    if arguments.ns3_cycle_ns < 1 or arguments.ns3_link_delay_ns < 1:
        raise ValueError("ns-3 timing parameters must be positive")
    if arguments.ns3_queue_packets < 1:
        raise ValueError("ns-3 queue depth must be positive")

    for node_count in arguments.nodes:
        rank_count = arguments.gpu_ranks or node_count * 2
        if (rank_count < 1 or rank_count > MAX_RANKS or GLOBAL_ROWS % rank_count != 0 or
                rank_count % node_count != 0):
            raise ValueError("node count produces an unsupported block partition")
        output = arguments.output_root / f"matmul-dp-nodes{node_count}"
        output.mkdir(parents=True, exist_ok=False)
        source = output / "source.yml"
        placement_path = output / "placement.json"
        topology = output / "topology.json"
        routing = output / "routing.json"
        workload = output / "workload.yml"
        stack = output / "stack.yml"

        source.write_text(yaml.safe_dump(source_workload(rank_count), sort_keys=False), encoding="utf-8")
        placement_path.write_text(json.dumps(placement(node_count, rank_count), indent=2) + "\n",
                                  encoding="utf-8")
        call(str(ROOT / "real" / "generate_simbricks_topology.py"),
             "--nodes", str(node_count), "--output", str(topology))
        call(str(ROOT / "real" / "generate_simbricks_routing.py"),
             "--placement", str(placement_path), "--topology", str(topology),
             "--output", str(routing))
        distributed_command = [
            str(ROOT / "real" / "generate_distributed_yaml.py"),
            "--source-yaml", str(source), "--placement", str(placement_path),
            "--output", str(workload), "--benchmark-root", "/opt/legosim/artifact/matmul",
            "--sniper-cores", "1", "--sniper-maxthreads", "1",
            "--phase2-backend", "ns3",
            "--ns3-cycle-ns", str(arguments.ns3_cycle_ns),
            "--ns3-link-rate", arguments.ns3_link_rate,
            "--ns3-link-delay-ns", str(arguments.ns3_link_delay_ns),
            "--ns3-queue-packets", str(arguments.ns3_queue_packets),
        ]
        if arguments.ns3_localize_cross_worker_network:
            distributed_command.append("--ns3-localize-cross-worker-network")
        if arguments.stream_output:
            distributed_command.append("--stream-output")
        call(*distributed_command)
        call(
            str(ROOT / "real" / "generate_swarm_stack.py"),
            "--placement", str(placement_path), "--topology", str(topology),
            "--routing", str(routing), "--workload-yaml", str(workload),
            "--image", arguments.image, "--output", str(stack),
            "--stack-name", f"chipsystemsim_matmul_dp_{node_count}",
            "--topology-width", "6", "--flit-size", "4",
        )


if __name__ == "__main__":
    main()
