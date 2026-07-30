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
    # Match legosim-run's CUDA default before loading the upstream environment.
    environment_prefix = "export CUDA_INSTALL_PATH=${CUDA_INSTALL_PATH:-/usr}; source /opt/legosim/gpgpu-sim/setup_environment && exec "
    services: dict[str, object] = {
        "coordinator": {
            "image": arguments.image,
            # This command is passed to the image's legosim-run entry point.
            "command": [image_yaml_path, "-w", str(arguments.topology_width), "-f", str(arguments.flit_size)],
            "configs": [{"source": "workload", "target": image_yaml_path, "mode": 0o444}],
            "networks": {"chipsystemsim": {"aliases": ["coordinator"]}},
            "deploy": {"placement": {"constraints": ["node.role == manager"]}, "restart_policy": {"condition": "none"}},
        },
    }
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
