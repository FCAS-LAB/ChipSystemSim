#!/usr/bin/env python3
"""One Docker node for a fixed 4-CPU plus 8-GPU LEGOSim workload adapter.

CPU0 is the controller. Each of the four CPU simlets drives two assigned GPU
simlets, so every 1/2/4/8-node run has the same 12 simulated components.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

PORT = 7000


def load_config(path: str) -> dict[str, Any]:
    serialized = os.environ.get("SIM_CONFIG")
    if serialized:
        return json.loads(serialized)
    with open(path, encoding="utf-8") as config_file:
        return json.load(config_file)


async def request(host: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Make one request/response RPC to a Docker node."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, PORT)
            break
        except OSError as error:
            last_error = error
            await asyncio.sleep(0.1)
    else:
        raise TimeoutError(f"Timed out connecting to {host}:{PORT}: {last_error}")
    writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    await writer.drain()
    response = json.loads((await asyncio.wait_for(reader.readline(), timeout)).decode())
    writer.close()
    await writer.wait_closed()
    if not response.get("ok", False):
        raise RuntimeError(response.get("error", "remote request failed"))
    return response


class Node:
    def __init__(self, node_id: int, config: dict[str, Any]) -> None:
        self.node_id = node_id
        self.config = config
        self.node_name = f"node{node_id}"
        self.roles = config["placement"][str(node_id)]
        self.stop_event = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None

    def host_for_role(self, role: str) -> str:
        for node_id, roles in self.config["placement"].items():
            if role in roles:
                return f"node{node_id}"
        raise KeyError(f"No Docker node hosts role {role}")

    def proxy_for(self, sequence: int) -> str | None:
        proxies = self.config.get("proxies", [])
        if not proxies:
            return None
        return f"node{proxies[sequence % len(proxies)]}"

    async def routed_request(self, role: str, payload: dict[str, Any], sequence: int) -> dict[str, Any]:
        """Send to a component directly or through an active TCP proxy."""
        target = self.host_for_role(role)
        proxy = self.proxy_for(sequence)
        if proxy is None:
            return await request(target, payload)
        return await request(proxy, {"op": "forward", "target": target, "payload": payload})

    async def network_delay(
        self, byte_count: int, source_role: str, destination_role: str, sequence: int
    ) -> tuple[int, int]:
        """Return modeled link delay and measured cross-node coordination time.

        Intra-container transfers are local and deliberately bypass ns-3. For a
        cross-container transfer, the returned wall time includes the timing
        RPC, plus the ns-3-simulated propagation/serialization delay.
        """
        if self.host_for_role(source_role) == self.host_for_role(destination_role):
            return 0, 0
        wait_started = time.perf_counter_ns()
        reply = await self.routed_request(
            "ns3", {"op": "network_delay", "bytes": byte_count}, sequence
        )
        delay_ns = int(reply["delay_ns"])
        await asyncio.sleep(delay_ns / 1_000_000_000)
        return delay_ns, time.perf_counter_ns() - wait_started

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_request = await reader.readline()
            message = json.loads(raw_request.decode())
            response = await self.dispatch(message)
            response = {"ok": True, **response}
        except Exception as error:  # Report errors to controller instead of hanging a run.
            response = {"ok": False, "error": str(error)}
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        operation = message["op"]
        if operation == "forward":
            target = str(message["target"])
            return await request(target, dict(message["payload"]))
        if operation == "network_delay":
            return self.ns3_delay(int(message["bytes"]))
        if operation == "gpu_compute":
            return await self.gpu_compute(message)
        if operation == "cpu_workload":
            return await self.cpu_workload(message)
        if operation == "shutdown":
            self.stop_event.set()
            return {"stopping": self.node_name}
        raise ValueError(f"Unsupported operation: {operation}")

    def ns3_delay(self, byte_count: int) -> dict[str, int]:
        """Run the ns-3 point-to-point model and return simulated time."""
        command = [
            "ns3-delay",
            f"--bytes={byte_count}",
            f"--bandwidth-mbps={self.config['network']['bandwidth_mbps']}",
            f"--propagation-us={self.config['network']['propagation_us']}",
        ]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return {"delay_ns": int(completed.stdout.strip())}

    async def gpu_compute(self, message: dict[str, Any]) -> dict[str, Any]:
        """Represent one GPGPU-Sim matmul process after its inputs arrive.

        The adapter intentionally uses a fixed host-time quantum. This measures
        distributed orchestration cost without claiming to replace GPGPU-Sim's
        timing model; the README documents that limitation.
        """
        if message["role"] not in self.roles:
            raise ValueError(f"{self.node_name} does not host {message['role']}")
        await asyncio.sleep(self.config["workload"]["gpu_compute_ms"] / 1000.0)
        return {"result_bytes": self.config["workload"]["result_bytes"], "role": message["role"]}

    async def cpu_workload(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run one CPU simlet and its two fixed GPU workers."""
        cpu_role = str(message["role"])
        if cpu_role not in self.roles:
            raise ValueError(f"{self.node_name} does not host {cpu_role}")
        cpu_index = int(cpu_role.removeprefix("cpu"))
        gpu_roles = (f"gpu{cpu_index * 2}", f"gpu{cpu_index * 2 + 1}")
        profile = self.config["workload"]

        async def run_gpu(gpu_role: str, sequence: int) -> tuple[dict[str, Any], int, int]:
            synchronization_ns = 0
            cross_machine_ns = 0
            result: dict[str, Any] = {}
            for round_index in range(profile["rounds"]):
                await asyncio.sleep(profile["cpu_compute_ms"] / 1000.0)
                for transfer in range(profile["input_messages_per_round"]):
                    wait_started = time.perf_counter_ns()
                    _, transfer_ns = await self.network_delay(
                        profile["payload_bytes"], cpu_role, gpu_role,
                        sequence * 1000 + round_index * 10 + transfer,
                    )
                    synchronization_ns += time.perf_counter_ns() - wait_started
                    cross_machine_ns += transfer_ns
                wait_started = time.perf_counter_ns()
                result = await self.routed_request(
                    gpu_role,
                    {"op": "gpu_compute", "role": gpu_role},
                    sequence * 1000 + round_index * 10 + 8,
                )
                synchronization_ns += time.perf_counter_ns() - wait_started
                wait_started = time.perf_counter_ns()
                _, transfer_ns = await self.network_delay(
                    profile["result_bytes"], gpu_role, cpu_role,
                    sequence * 1000 + round_index * 10 + 9,
                )
                synchronization_ns += time.perf_counter_ns() - wait_started
                cross_machine_ns += transfer_ns
            return result, synchronization_ns, cross_machine_ns

        completed_gpus = await asyncio.gather(
            run_gpu(gpu_roles[0], cpu_index * 2), run_gpu(gpu_roles[1], cpu_index * 2 + 1)
        )
        return {
            "role": cpu_role,
            "synchronization_time_ns": max(item[1] for item in completed_gpus),
            "cross_machine_time_ns": max(item[2] for item in completed_gpus),
            "gpu_results": [item[0] for item in completed_gpus],
        }

    async def run_controller(self) -> None:
        """Execute the fixed 4-CPU plus 8-GPU workload and collect one sample."""
        await asyncio.sleep(0.4)  # Let peer listeners become ready after Compose starts.
        started = time.perf_counter_ns()
        completed_cpus = await asyncio.gather(
            *(self.routed_request(role, {"op": "cpu_workload", "role": role}, index)
              for index, role in enumerate(("cpu0", "cpu1", "cpu2", "cpu3")))
        )
        finished = time.perf_counter_ns()
        synchronization_ns = max(item["synchronization_time_ns"] for item in completed_cpus)
        cross_machine_ns = max(item["cross_machine_time_ns"] for item in completed_cpus)
        measurement = {
            "benchmark": self.config["benchmark"],
            "container_count": self.config["container_count"],
            "total_time_ns": finished - started,
            "synchronization_time_ns": synchronization_ns,
            "cross_machine_time_ns": cross_machine_ns,
            "synchronization_fraction": synchronization_ns / (finished - started),
            "cpu_results": completed_cpus,
        }
        output = Path(self.config["output_dir"]) / f"measurement-{self.config['run_id']}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(measurement, indent=2), encoding="utf-8")
        # This line is the host-side collection interface; it avoids relying
        # on host-to-container bind mounts for measurement collection.
        print("MEASUREMENT_JSON=" + json.dumps(measurement, separators=(",", ":")), flush=True)

        for node_id in self.config["placement"]:
            if int(node_id) != self.node_id:
                try:
                    await request(f"node{node_id}", {"op": "shutdown"}, timeout=3)
                except OSError:
                    pass
        self.stop_event.set()

    async def run(self) -> None:
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", PORT)
        if "cpu0" in self.roles:
            asyncio.create_task(self.run_controller())
        await self.stop_event.wait()
        self.server.close()
        await self.server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-id", type=int, required=True)
    parser.add_argument("--config", default="/run/config.json")
    arguments = parser.parse_args()
    asyncio.run(Node(arguments.node_id, load_config(arguments.config)).run())


if __name__ == "__main__":
    main()
