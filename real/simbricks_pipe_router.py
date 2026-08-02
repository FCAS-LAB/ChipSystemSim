#!/usr/bin/env python3
"""Route native LEGOSim PipeComm requests on one Swarm worker.

The router is intentionally local to one worker slot.  It keeps pipes whose
source and destination chiplets share that slot in memory, and forwards every
cross-slot request to the matching single-peer C++ BaseIf gateway.  The
gateway, not this router, owns the queue at the remote endpoint.
"""
import argparse
import asyncio
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict


PIPE_NAME = re.compile(r"^buffer(-?\d+)_(-?\d+)_(-?\d+)_(-?\d+)$")
MAX_REQUEST_BYTES = 65_000


def monotonic_time_ns():
    """Return a monotonic timestamp on both Python 3.6 and newer runtimes."""
    monotonic_ns = getattr(time, "monotonic_ns", None)
    if monotonic_ns is not None:
        return monotonic_ns()
    return int(time.monotonic() * 1_000_000_000)


def unix_time_ns():
    """Return a wall-clock timestamp without requiring Python 3.7's time_ns."""
    time_ns = getattr(time, "time_ns", None)
    if time_ns is not None:
        return time_ns()
    return int(time.time() * 1_000_000_000)


class Router:
    def __init__(self, slot, routing):
        self.slot = slot
        self.coordinates = {key: int(value) for key, value in routing["coordinate_to_worker_slot"].items()}
        # Logical LEGOSim partitions can be co-located for the one-machine
        # control.  Metrics must distinguish that from a partition deployed
        # on another physical Swarm node.
        physical = routing.get("worker_physical_slots", {})
        self.physical_slots = {
            int(worker_slot): int(physical_slot)
            for worker_slot, physical_slot in physical.items()
        }
        base_port = int(routing.get("gateway_base_port", 9500))
        self.gateway_ports = {slot: base_port + slot for slot in set(self.coordinates.values()) if slot != self.slot}
        self.local_pipes = defaultdict(deque)
        self.local_ready = asyncio.Condition()

    def peer_for(self, operation, pipe_name):
        match = PIPE_NAME.fullmatch(pipe_name)
        if match is None:
            raise ValueError(f"unsupported PipeComm name: {pipe_name}")
        source = f"{match.group(1)},{match.group(2)}"
        destination = f"{match.group(3)},{match.group(4)}"
        coordinate = destination if operation == "W" else source
        try:
            return self.coordinates[coordinate]
        except KeyError as error:
            raise ValueError(f"no worker placement for chiplet {coordinate}") from error

    async def handle(self, reader, writer):
        try:
            line = await reader.readline()
            fields = line.decode("ascii").rstrip("\n").split(" ")
            if len(fields) != 3 or fields[0] not in {"W", "R"}:
                raise ValueError("invalid PipeComm request header")
            operation, pipe_name, byte_text = fields
            # InterChiplet returns its FIFO path relative to the coordinator
            # (for example ``.././buffer0_0_0_1``).  The path is only a
            # transport identifier here, never a filesystem target; normalize
            # it to the supported buffer name before validating or forwarding.
            pipe_name = pipe_name.rsplit("/", 1)[-1]
            byte_count = int(byte_text)
            if byte_count < 0 or byte_count > MAX_REQUEST_BYTES:
                raise ValueError("invalid PipeComm byte count")
            payload = await reader.readexactly(byte_count) if operation == "W" else b""
            peer = self.peer_for(operation, pipe_name)
            print(
                f"pipe-router: op={operation} pipe={pipe_name} bytes={byte_count} "
                f"slot={self.slot} peer={peer}",
                flush=True,
            )
            operation_started_ns = monotonic_time_ns()
            operation_started_unix_ns = unix_time_ns()
            if peer == self.slot:
                await self.handle_local(operation, pipe_name, payload, byte_count, writer)
            else:
                normalized_header = f"{operation} {pipe_name} {byte_count}\n".encode("ascii")
                await self.forward(peer, normalized_header, payload, reader, writer, byte_count)
            operation_finished_ns = monotonic_time_ns()
            operation_finished_unix_ns = unix_time_ns()
            elapsed_ns = operation_finished_ns - operation_started_ns
            # Emit one machine-readable record per completed native PipeComm
            # operation.  A cross-slot write measures the time for this router
            # to submit its payload through the local BaseIf gateway. A read's
            # elapsed time is the caller's PipeComm synchronization wait,
            # whether its producer is local or remote.
            print("pipe-metric: " + json.dumps({
                "operation": operation,
                "bytes": byte_count,
                "source_slot": self.slot,
                "peer_slot": peer,
                "cross_node": self.physical_slots.get(peer, peer)
                != self.physical_slots.get(self.slot, self.slot),
                # These timestamps make the blocking interval auditable within
                # a router process. They are monotonic-clock values, so they
                # must not be compared directly across different VMs.
                "started_monotonic_ns": operation_started_ns,
                "finished_monotonic_ns": operation_finished_ns,
                # VMware guest clocks are synchronized before the scalability
                # matrix. These wall-clock stamps let the collector form one
                # communication-epoch makespan across router processes.
                "started_unix_ns": operation_started_unix_ns,
                "finished_unix_ns": operation_finished_unix_ns,
                "elapsed_ns": elapsed_ns,
                "synchronization_wait_ns": elapsed_ns if operation == "R" else 0,
            }, separators=(",", ":")), flush=True)
            print(
                f"pipe-router: completed op={operation} pipe={pipe_name} bytes={byte_count}",
                flush=True,
            )
        except (ValueError, UnicodeDecodeError, asyncio.IncompleteReadError) as error:
            print(f"pipe-router: rejected request: {error}", flush=True)
            writer.write(f"ERR {error}\n".encode("utf-8"))
            await writer.drain()
        except OSError as error:
            print(f"pipe-router: gateway unavailable: {error}", flush=True)
            writer.write(f"ERR gateway unavailable: {error}\n".encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()

    async def handle_local(self, operation, pipe_name, payload, byte_count, writer):
        async with self.local_ready:
            if operation == "W":
                self.local_pipes[pipe_name].append(payload)
                self.local_ready.notify_all()
                writer.write(b"OK\n")
            else:
                await self.local_ready.wait_for(
                    lambda: bool(self.local_pipes[pipe_name])
                    and len(self.local_pipes[pipe_name][0]) == byte_count
                )
                data = self.local_pipes[pipe_name].popleft()
                writer.write(f"OK {byte_count}\n".encode("ascii") + data)
        await writer.drain()

    async def forward(self, peer, header, payload, client_reader, client_writer, byte_count):
        del client_reader  # The request is fully read before entering this method.
        try:
            port = self.gateway_ports[peer]
        except KeyError as error:
            raise ValueError(f"no BaseIf gateway for worker slot {peer}") from error
        gateway_reader, gateway_writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            gateway_writer.write(header + payload)
            await gateway_writer.drain()
            response = await gateway_reader.readline()
            if response == b"OK\n":
                client_writer.write(response)
            elif response == f"OK {byte_count}\n".encode("ascii"):
                data = await gateway_reader.readexactly(byte_count)
                client_writer.write(response + data)
            else:
                client_writer.write(response or b"ERR gateway closed connection\n")
            await client_writer.drain()
        finally:
            gateway_writer.close()


async def main_async(arguments):
    routing = json.loads(arguments.routing.read_text(encoding="utf-8"))
    router = Router(arguments.slot, routing)
    server = await asyncio.start_server(router.handle, "0.0.0.0", arguments.port)
    try:
        # asyncio.Server.serve_forever() was added after Python 3.6. Keep the
        # listener alive with an unresolved Future, which is supported by the
        # Ubuntu 18.04 runtime shipped in the native image.
        await asyncio.Future()
    finally:
        server.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, type=int)
    parser.add_argument("--routing", required=True, type=Path)
    parser.add_argument("--port", default=9400, type=int)
    arguments = parser.parse_args()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main_async(arguments))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
