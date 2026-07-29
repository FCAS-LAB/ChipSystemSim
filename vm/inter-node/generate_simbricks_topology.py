#!/usr/bin/env python3
"""Generate a deterministic point-to-point SimBricks transport topology.

Every worker slot gets a stable simulated management address and every unordered
worker pair gets exactly one `net_sockets` channel.  The channel is used in both
directions by its two BaseIf endpoints; no manager relay is introduced.
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, choices=(1, 2, 4, 8), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--subnet", default="10.203.0", help="first three IPv4 octets")
    parser.add_argument("--base-port", type=int, default=9700)
    arguments = parser.parse_args()

    octets = arguments.subnet.split(".")
    if len(octets) != 3 or any(not item.isdigit() or not 0 <= int(item) <= 255 for item in octets):
        raise ValueError("--subnet must contain exactly three IPv4 octets")
    if not 1 <= arguments.base_port <= 65535:
        raise ValueError("--base-port must be a valid TCP port")

    workers = [
        {
            "slot": slot,
            "hostname": f"worker-{slot}",
            "simulated_ip": f"{arguments.subnet}.{slot + 10}",
        }
        for slot in range(arguments.nodes)
    ]
    channels = []
    for index, (left, right) in enumerate(combinations(range(arguments.nodes), 2)):
        port = arguments.base_port + index
        if port > 65535:
            raise ValueError("not enough TCP ports for requested topology")
        channels.append(
            {
                "id": f"node{left}-node{right}",
                "listener_slot": left,
                "connector_slot": right,
                "listener_ip": workers[left]["simulated_ip"],
                "tcp_port": port,
                "link_latency_ps": 500_000,
                "sync_interval_ps": 500_000,
            }
        )

    topology = {
        "version": 1,
        "transport": "simbricks-baseif-net_sockets",
        "workers": workers,
        "channels": channels,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(topology, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
