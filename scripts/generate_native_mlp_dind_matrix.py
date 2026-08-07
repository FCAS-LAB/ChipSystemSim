#!/usr/bin/env python3
"""Generate communication-aware DinD configurations for original LEGOSim MLP.

Unlike ``generate_mlp_dp_matrix.py``, this generator keeps the upstream MLP
process graph: four GPGPU-Sim processes, Sniper CPU, DSA, MNSIM and PopNet.
It invokes the common placement/topology/routing generators so the generated
files can be deployed by the generic DinD runner.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_MLP = ROOT.parent / "LEGOSIM_MICRO" / "artifact" / "MLP" / "mlp.yml"


def call(*arguments: str) -> None:
    """Run one generator and propagate a useful failure to the caller."""
    subprocess.run([sys.executable, *arguments], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8],
                        choices=[1, 2, 4, 8])
    parser.add_argument("--placement-policy", default="communication-aware",
                        choices=["contiguous", "communication-aware", "rank-aware"])
    parser.add_argument("--stream-output", action="store_true",
                        help="forward phase-one stdout/stderr to coordinator logs")
    parser.add_argument("--mnsim-fast-input-size", type=int,
                        help="smaller MNSIM feature map for labelled functional/communication validation")
    parser.add_argument("--ns3-cycle-ns", type=int, default=1)
    parser.add_argument("--ns3-link-rate", default="128Gbps")
    parser.add_argument("--ns3-link-delay-ns", type=int, default=1)
    parser.add_argument("--ns3-queue-packets", type=int, default=100000)
    arguments = parser.parse_args()

    if not UPSTREAM_MLP.is_file():
        raise FileNotFoundError(f"original MLP YAML not found: {UPSTREAM_MLP}")
    if arguments.ns3_cycle_ns < 1 or arguments.ns3_link_delay_ns < 1 or arguments.ns3_queue_packets < 1:
        raise ValueError("ns-3 timing parameters must be positive")

    for node_count in arguments.nodes:
        output = arguments.output_root / f"mlp-nodes{node_count}"
        output.mkdir(parents=True, exist_ok=False)
        placement = output / "placement.json"
        topology = output / "topology.json"
        routing = output / "routing.json"
        workload = output / "workload.yml"
        stack = output / "stack.yml"

        call(
            str(ROOT / "real" / "generate_placement.py"),
            "--source-yaml", str(UPSTREAM_MLP), "--workload-name", "mlp",
            "--nodes", str(node_count), "--placement-policy", arguments.placement_policy,
            "--output", str(placement),
        )
        call(str(ROOT / "real" / "generate_simbricks_topology.py"),
             "--nodes", str(node_count), "--output", str(topology))
        call(str(ROOT / "real" / "generate_simbricks_routing.py"),
             "--placement", str(placement), "--topology", str(topology),
             "--output", str(routing))
        yaml_command = [
            str(ROOT / "real" / "generate_distributed_yaml.py"),
            "--source-yaml", str(UPSTREAM_MLP), "--placement", str(placement),
            "--output", str(workload), "--benchmark-root", "/opt/legosim/artifact/MLP",
            "--sniper-cores", "1", "--sniper-maxthreads", "1",
            "--phase2-backend", "ns3",
            "--ns3-cycle-ns", str(arguments.ns3_cycle_ns),
            "--ns3-link-rate", arguments.ns3_link_rate,
            "--ns3-link-delay-ns", str(arguments.ns3_link_delay_ns),
            "--ns3-queue-packets", str(arguments.ns3_queue_packets),
        ]
        if arguments.stream_output:
            yaml_command.append("--stream-output")
        call(*yaml_command)
        stack_command = [
            str(ROOT / "real" / "generate_swarm_stack.py"),
            "--placement", str(placement), "--topology", str(topology),
            "--routing", str(routing), "--workload-yaml", str(workload),
            "--image", arguments.image, "--output", str(stack),
            "--stack-name", f"chipsystemsim_native_mlp_{node_count}",
            "--topology-width", "6", "--flit-size", "4",
        ]
        if arguments.mnsim_fast_input_size is not None:
            stack_command.extend(["--mnsim-fast-input-size", str(arguments.mnsim_fast_input_size)])
        call(*stack_command)


if __name__ == "__main__":
    main()
