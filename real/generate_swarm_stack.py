#!/usr/bin/env python3
"""Generate a Docker Swarm stack for one native distributed LEGOSim run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--placement", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--routing", type=Path, required=True)
    parser.add_argument("--workload-yaml", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stack-name", default="chipsystemsim")
    parser.add_argument("--workload-target", default="/opt/legosim/workload.yml",
                        help="absolute in-container path at which to mount the derived YAML")
    parser.add_argument("--topology-width", type=int, required=True)
    parser.add_argument("--flit-size", type=int, required=True)
    parser.add_argument("--transport-ptrace", action="store_true",
                        help="allow gdb to attach to a stalled worker process (diagnostic runs only)")
    parser.add_argument("--mlp-dp-iterations", type=int,
                        help="set LEGOSIM_MLP_DP_ITERATIONS for the MLP-DP worker processes")
    parser.add_argument("--mlp-dp-ranks", type=int,
                        help="set the active MLP-DP CPU rank count")
    parser.add_argument("--mlp-dp-samples", type=int,
                        help="set the fixed global MLP-DP sample count")
    parser.add_argument("--mnsim-fast-input-size", type=int,
                        help="use a smaller MNSIM input feature map for labelled functional/communication validation")
    parser.add_argument("--worker-ready-timeout", type=int, default=180,
                        help="seconds the coordinator may wait for transport worker port 9300")
    # Eight distributed BaseIf endpoints can need longer than the small-stack
    # default to complete their all-to-all socket introductions under DinD.
    parser.add_argument("--baseif-ready-timeout", type=int, default=300,
                        help="seconds a transport may wait for all BaseIf links")
    parser.add_argument("--baseif-connect-timeout", type=int, default=180,
                        help="seconds a connector may retry its remote BaseIf listener")
    arguments = parser.parse_args()
    placement = json.loads(arguments.placement.read_text(encoding="utf-8"))
    node_count = int(placement["swarm_nodes"])
    image_yaml_path = arguments.workload_target
    if not image_yaml_path.startswith("/"):
        raise ValueError("--workload-target must be an absolute container path")
    topology = json.loads(arguments.topology.read_text(encoding="utf-8"))
    routing = json.loads(arguments.routing.read_text(encoding="utf-8"))
    if len(topology["workers"]) != node_count or len(routing["coordinate_to_worker_slot"]) == 0:
        raise ValueError("placement, topology, and routing must describe the same non-empty experiment")
    if arguments.mlp_dp_iterations is not None and arguments.mlp_dp_iterations < 1:
        raise ValueError("--mlp-dp-iterations must be positive")
    if arguments.mlp_dp_ranks is not None and arguments.mlp_dp_ranks < 1:
        raise ValueError("--mlp-dp-ranks must be positive")
    if arguments.mlp_dp_samples is not None and arguments.mlp_dp_samples < 1:
        raise ValueError("--mlp-dp-samples must be positive")
    if arguments.mnsim_fast_input_size is not None and arguments.mnsim_fast_input_size < 3:
        raise ValueError("--mnsim-fast-input-size must be at least 3")
    if arguments.worker_ready_timeout < 1:
        raise ValueError("--worker-ready-timeout must be positive")
    if arguments.baseif_ready_timeout < 1 or arguments.baseif_connect_timeout < 1:
        raise ValueError("BaseIf timeout arguments must be positive")
    # Match legosim-run's CUDA default before loading the upstream environment.
    # The CUDA-enabled base image installs nvcc below /usr/local/cuda. Do not
    # fall back to /usr: that location works for one distro package layout but
    # fails inside the Ubuntu 18.04 runtime image used by DinD workers.
    # The container provides CUDA headers/libraries through the distribution
    # path.  Do not default to /usr/local/cuda: that path is absent in the
    # minimal runtime image and GPGPU-Sim rejects it before serving requests.
    environment_prefix = "export CUDA_INSTALL_PATH=${CUDA_INSTALL_PATH:-/usr}; source /opt/legosim/gpgpu-sim/setup_environment && exec "
    services: dict[str, object] = {
        "coordinator": {
            "image": arguments.image,
            # This command is passed to the image's legosim-run entry point.
            "command": [image_yaml_path, "-w", str(arguments.topology_width), "-f", str(arguments.flit_size)],
            "configs": [{"source": "workload", "target": image_yaml_path, "mode": 0o444}],
            "networks": {"chipsystemsim": {"aliases": ["coordinator"]}},
            "environment": {"LEGOSIM_WORKER_READY_TIMEOUT_SECONDS": str(arguments.worker_ready_timeout)},
            "deploy": {"placement": {"constraints": ["node.role == manager"]}, "restart_policy": {"condition": "none"}},
        },
    }
    if arguments.mnsim_fast_input_size is not None:
        # process_proxy runs in the coordinator, whereas its spawned simulator
        # runs on a transport worker.  Both sides need the label: the proxy
        # forwards it in the deliberately minimal child environment.
        services["coordinator"]["environment"]["LEGOSIM_MNSIM_FAST_INPUT_SIZE"] = str(arguments.mnsim_fast_input_size)
    for slot in range(node_count):
        transport_service = {
            "image": arguments.image,
            "entrypoint": [
                "/bin/bash", "-lc",
                environment_prefix + "python3 /opt/chipsystemsim-distributed/simbricks_worker_supervisor.py "
                f"--slot {slot} --topology /run/config/topology.json --routing /run/config/routing.json",
            ],
            "configs": [
                {"source": "topology", "target": "/run/config/topology.json", "mode": 0o444},
                {"source": "routing", "target": "/run/config/routing.json", "mode": 0o444},
            ],
            "networks": {"chipsystemsim": {"aliases": [f"worker-{slot}", f"transport-{slot}"]}},
            "deploy": {"placement": {"constraints": [f"node.labels.chipsystemsim.node.{slot} == true"]}},
        }
        if arguments.transport_ptrace:
            transport_service["cap_add"] = ["SYS_PTRACE"]
        transport_environment = {
            "LEGOSIM_BASEIF_READY_TIMEOUT_SECONDS": str(arguments.baseif_ready_timeout),
            "LEGOSIM_BASEIF_CONNECT_TIMEOUT_SECONDS": str(arguments.baseif_connect_timeout),
        }
        if arguments.mlp_dp_iterations is not None:
            transport_environment["LEGOSIM_MLP_DP_ITERATIONS"] = str(arguments.mlp_dp_iterations)
        if arguments.mlp_dp_ranks is not None:
            transport_environment["LEGOSIM_MLP_DP_RANKS"] = str(arguments.mlp_dp_ranks)
        if arguments.mlp_dp_samples is not None:
            transport_environment["LEGOSIM_MLP_DP_SAMPLES"] = str(arguments.mlp_dp_samples)
        if arguments.mnsim_fast_input_size is not None:
            transport_environment["LEGOSIM_MNSIM_FAST_INPUT_SIZE"] = str(arguments.mnsim_fast_input_size)
        transport_service["environment"] = transport_environment
        services[f"transport-{slot}"] = transport_service
    stack = {
        "services": services,
        "configs": {
            "workload": {"file": str(arguments.workload_yaml.resolve())},
            "topology": {"file": str(arguments.topology.resolve())},
            "routing": {"file": str(arguments.routing.resolve())},
        },
        "networks": {"chipsystemsim": {"driver": "overlay", "attachable": True}},
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(yaml.safe_dump(stack, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
