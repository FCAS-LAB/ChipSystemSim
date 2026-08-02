#!/usr/bin/env python3
"""Start one worker's local routers and all of its SimBricks peer links."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple


def start(command):
    return subprocess.Popen(command, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)


def wait_for_path(path, process):
    deadline = time.monotonic() + 60
    while not path.exists():
        if process.poll() is not None:
            raise RuntimeError(f"transport process exited before creating {path}: {process.returncode}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"transport process did not create {path} within 60 seconds")
        time.sleep(0.1)


def wait_for_ready(paths, timeout_seconds):
    """Do not accept PipeComm traffic until every local BaseIf handshake completed."""
    deadline = time.monotonic() + timeout_seconds
    while any(not path.exists() for path, _ in paths):
        for path, process in paths:
            if process.poll() is not None:
                raise RuntimeError(f"gateway exited before BaseIf became ready ({path}): {process.returncode}")
        if time.monotonic() >= deadline:
            missing = ", ".join(str(path) for path, _ in paths if not path.exists())
            raise RuntimeError(
                f"BaseIf handshakes did not become ready within {timeout_seconds} seconds: {missing}"
            )
        time.sleep(0.1)


def wait_for_stable(processes, timeout_seconds):
    """Reject a false-ready transport whose socket process has already died."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for description, process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"transport exited after BaseIf became ready ({description}): {process.returncode}"
                )
        time.sleep(0.1)


def resolve_transport_service(service_name):
    """Wait for Swarm service DNS instead of assuming task start order.

    On a nested DIND worker, the overlay-network DNS entry can appear several
    seconds after the local task starts. A one-shot lookup made connector
    transports crash and be restarted before their listener peer was visible.
    """
    deadline = time.monotonic() + 60
    last_error = None  # type: Optional[socket.gaierror]
    while time.monotonic() < deadline:
        try:
            return socket.gethostbyname(service_name)
        except socket.gaierror as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"could not resolve {service_name} within 60 seconds") from last_error


def start_connector_with_retry(command, socket_path, timeout_seconds):
    """Start a TCP connector after its remote listener becomes available.

    `net_sockets` exits when a listener has not bound its TCP port yet.  In a
    multi-worker Swarm deployment a DNS lookup can already succeed at that
    point, so service-DNS retry alone is insufficient.  A short bounded retry
    keeps this transport startup race outside the application process graph.
    """
    deadline = time.monotonic() + timeout_seconds
    last_return_code = None  # type: Optional[int]
    while time.monotonic() < deadline:
        if socket_path.exists():
            socket_path.unlink()
        connector = start(command)
        wait_for_path(socket_path, connector)
        # Give net_sockets enough time to report an immediate ECONNREFUSED.
        time.sleep(0.5)
        if connector.poll() is None:
            return connector
        last_return_code = connector.returncode
        if socket_path.exists():
            socket_path.unlink()
        time.sleep(1)
    raise RuntimeError(
        f"connector could not reach listener within {timeout_seconds} seconds: {last_return_code}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, type=int)
    parser.add_argument("--topology", required=True, type=Path)
    parser.add_argument("--routing", required=True, type=Path)
    parser.add_argument("--worker-port", default=9300, type=int)
    parser.add_argument("--router-port", default=9400, type=int)
    parser.add_argument("--gateway-base-port", default=9500, type=int)
    arguments = parser.parse_args()
    # An eight-node full mesh contains 28 BaseIf links. On cold Docker-in-
    # Docker Swarm starts, its control-plane setup can exceed the short
    # timeout suitable for one or two nodes, even with a healthy data path.
    ready_timeout_seconds = int(os.environ.get("LEGOSIM_BASEIF_READY_TIMEOUT_SECONDS", "90"))
    connector_timeout_seconds = int(
        os.environ.get("LEGOSIM_BASEIF_CONNECT_TIMEOUT_SECONDS", "60")
    )
    stability_timeout_seconds = int(
        os.environ.get("LEGOSIM_BASEIF_STABILITY_TIMEOUT_SECONDS", "5")
    )
    if ready_timeout_seconds < 1:
        raise ValueError("LEGOSIM_BASEIF_READY_TIMEOUT_SECONDS must be positive")
    if connector_timeout_seconds < 1:
        raise ValueError("LEGOSIM_BASEIF_CONNECT_TIMEOUT_SECONDS must be positive")
    if stability_timeout_seconds < 1:
        raise ValueError("LEGOSIM_BASEIF_STABILITY_TIMEOUT_SECONDS must be positive")
    topology = json.loads(arguments.topology.read_text(encoding="utf-8"))
    run_dir = Path("/run/simbricks")
    run_dir.mkdir(parents=True, exist_ok=True)
    children = []  # type: List[subprocess.Popen]
    ready_paths = []  # type: List[Tuple[Path, subprocess.Popen]]
    transport_processes = []  # type: List[Tuple[str, subprocess.Popen]]
    try:
        # Start every listener before any connector.  In particular, a worker
        # that owns both kinds of edge must not block its local listener on an
        # earlier connector edge in the channel list.
        local_channels = [
            channel for channel in topology["channels"]
            if arguments.slot in {int(channel["listener_slot"]), int(channel["connector_slot"])}
        ]
        for channel in local_channels:
            left = int(channel["listener_slot"])
            right = int(channel["connector_slot"])
            if arguments.slot != left:
                continue
            peer = right
            socket_path = run_dir / f"base-{peer}.sock"
            pool_path = run_dir / f"gateway-{peer}.pool"
            net_pool = run_dir / f"net-{peer}.pool"
            gateway_port = arguments.gateway_base_port + peer
            ready_path = run_dir / f"gateway-{peer}.ready"
            gateway = start([
                "/usr/local/bin/simbricks-pipe-gateway", "--ready-file", str(ready_path),
                str(socket_path), str(pool_path), str(gateway_port)
            ])
            children.append(gateway)
            ready_paths.append((ready_path, gateway))
            wait_for_path(socket_path, gateway)
            net = start([
                "/opt/simbricks/dist/sockets/net_sockets", "-l", "-C", str(socket_path), "-s", str(net_pool),
                "-S", "32", "0.0.0.0", str(channel["tcp_port"]),
                str(run_dir / f"listen-{peer}.info"), str(run_dir / f"listen-{peer}.ready"),
            ])
            children.append(net)
            transport_processes.append((f"listener-{arguments.slot}-{peer}", net))

        # Every transport has now created its listener endpoints.  Establish
        # connector edges in deterministic slot order instead of allowing all
        # 28 BaseIf handshakes of an eight-node mesh to race at once.  The
        # small stagger is applied only during startup; it does not alter the
        # simulated workload or PipeComm timing.
        time.sleep(arguments.slot * 2)
        for channel in local_channels:
            left = int(channel["listener_slot"])
            right = int(channel["connector_slot"])
            if arguments.slot != right:
                continue
            peer = left
            socket_path = run_dir / f"base-{peer}.sock"
            pool_path = run_dir / f"gateway-{peer}.pool"
            net_pool = run_dir / f"net-{peer}.pool"
            gateway_port = arguments.gateway_base_port + peer
            ready_path = run_dir / f"gateway-{peer}.ready"
            peer_ip = resolve_transport_service(f"transport-{left}")
            net = start_connector_with_retry([
                "/opt/simbricks/dist/sockets/net_sockets", "-L", str(socket_path), "-s", str(net_pool), "-S", "32",
                peer_ip, str(channel["tcp_port"]), str(run_dir / f"listen-{peer}.info"),
                str(run_dir / f"listen-{peer}.ready"),
            ], socket_path, connector_timeout_seconds)
            children.append(net)
            transport_processes.append((f"connector-{arguments.slot}-{peer}", net))
            gateway = start([
                "/usr/local/bin/simbricks-pipe-gateway", "--connect", "--ready-file", str(ready_path),
                str(socket_path), str(pool_path), str(gateway_port),
            ])
            children.append(gateway)
            ready_paths.append((ready_path, gateway))

        wait_for_ready(ready_paths, ready_timeout_seconds)
        wait_for_stable(transport_processes, stability_timeout_seconds)
        router = start([
            "python3", "/opt/chipsystemsim-distributed/simbricks_pipe_router.py", "--slot", str(arguments.slot),
            "--routing", str(arguments.routing), "--port", str(arguments.router_port),
        ])
        children.append(router)
        worker_environment = {**os.environ, "LEGOSIM_PIPE_GATEWAY": f"127.0.0.1:{arguments.router_port}"}
        worker = subprocess.Popen(
            ["python3", "/opt/chipsystemsim-distributed/worker.py", "--port", str(arguments.worker_port)],
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            env=worker_environment,
        )
        children.append(worker)
        while worker.poll() is None:
            for description, process in transport_processes:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"transport exited while worker was running ({description}): {process.returncode}"
                    )
            time.sleep(0.2)
        raise SystemExit(worker.returncode)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
