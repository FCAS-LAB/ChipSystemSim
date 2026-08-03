#!/usr/bin/env python3
"""Measure raw TCP round-trip latency between two ready transport services.

Running this probe in the same two DinD/Swarm service containers as the
PipeComm probe supplies a network-only lower bound.  It deliberately avoids
the router, BaseIf gateway and SimBricks queues.
"""

import argparse
import json
import socket
import sys
import time


def now_ns():
    """Return monotonic nanoseconds on both Python 3.6 and newer runtimes."""
    monotonic_ns = getattr(time, "monotonic_ns", None)
    if monotonic_ns is not None:
        return monotonic_ns()
    return int(time.monotonic() * 1_000_000_000)


def read_exactly(connection, byte_count):
    payload = bytearray()
    while len(payload) < byte_count:
        part = connection.recv(byte_count - len(payload))
        if not part:
            raise RuntimeError("peer closed the TCP connection early")
        payload += part
    return bytes(payload)


def run_server(arguments):
    """Echo exactly ``count`` probe payloads, then exit deterministically."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", arguments.port))
    listener.listen(1)
    listener.settimeout(30)
    print("ready", flush=True)
    try:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(30)
            for _ in range(arguments.count):
                connection.sendall(read_exactly(connection, arguments.bytes))
    finally:
        listener.close()


def run_client(arguments):
    """Send fixed-size echoes on one persistent TCP connection."""
    payload = bytes([arguments.fill]) * arguments.bytes
    connection = socket.create_connection((arguments.host, arguments.port), timeout=30)
    with connection:
        connection.settimeout(30)
        for sequence in range(arguments.count):
            started_ns = now_ns()
            connection.sendall(payload)
            echoed = read_exactly(connection, arguments.bytes)
            completed_ns = now_ns()
            if echoed != payload:
                raise RuntimeError("echoed TCP payload differs from the probe pattern")
            print(json.dumps({
                "sequence": sequence,
                "round_trip_ns": completed_ns - started_ns,
                "started_ns": started_ns,
                "completed_ns": completed_ns,
            }, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("server", "client"))
    parser.add_argument("--host", help="server name/IP (required for client mode)")
    parser.add_argument("--port", type=int, default=9411)
    parser.add_argument("--bytes", type=int, default=64)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--fill", type=int, default=90)
    arguments = parser.parse_args()
    if arguments.mode == "client" and not arguments.host:
        parser.error("--host is required for client mode")
    if not 1 <= arguments.bytes <= 65000:
        parser.error("--bytes must be in 1..65000")
    if arguments.count < 1:
        parser.error("--count must be positive")
    if not 0 <= arguments.fill <= 255:
        parser.error("--fill must be in 0..255")
    try:
        if arguments.mode == "server":
            run_server(arguments)
        else:
            run_client(arguments)
    except (OSError, RuntimeError) as error:
        print(f"tcp-latency-probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
