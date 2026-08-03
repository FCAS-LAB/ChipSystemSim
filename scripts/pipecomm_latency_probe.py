#!/usr/bin/env python3
"""Measure one native PipeComm operation through a local SimBricks router.

This probe speaks the same small TCP protocol as ``remote_pipe_comm.h``.  It
is intended to run *inside* an already-ready ``transport-N`` container, so the
result covers the real router -> gateway -> SimBricks BaseIf -> net_sockets
path, rather than a host-side proxy.  The reader must be started before the
writer; a coordinator collects the JSON records and computes the one-way
delivery interval from their monotonic timestamps.
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


def read_line(connection):
    """Read one newline-terminated router response without buffering payload."""
    response = bytearray()
    while len(response) < 512:
        byte = connection.recv(1)
        if not byte:
            raise RuntimeError("router closed the connection before its response")
        response += byte
        if byte == b"\n":
            return bytes(response)
    raise RuntimeError("router response header exceeds 512 bytes")


def read_exactly(connection, byte_count):
    """Receive exactly one PipeComm payload."""
    payload = bytearray()
    while len(payload) < byte_count:
        part = connection.recv(byte_count - len(payload))
        if not part:
            raise RuntimeError("router closed the connection during payload receive")
        payload += part
    return bytes(payload)


def emit(record):
    """Write one machine-readable result and flush it for shell orchestration."""
    print(json.dumps(record, sort_keys=True), flush=True)


def connect(port):
    """Open the same per-operation local TCP connection as remote_pipe_comm.h."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=30)
    connection.settimeout(30)
    return connection


def run_reader(arguments):
    """Post blocking reads and report when each remote payload becomes visible."""
    expected = bytes([arguments.fill]) * arguments.bytes
    for sequence in range(arguments.count):
        with connect(arguments.port) as connection:
            request_started_ns = now_ns()
            header = f"R {arguments.pipe} {arguments.bytes}\n".encode("ascii")
            connection.sendall(header)
            request_issued_ns = now_ns()
            # This marker lets the external harness wait until the read is
            # pending before it submits the matching write on the peer node.
            emit({
                "event": "reader_issued",
                "sequence": sequence,
                "request_started_ns": request_started_ns,
                "request_issued_ns": request_issued_ns,
            })
            response = read_line(connection)
            expected_header = f"OK {arguments.bytes}\n".encode("ascii")
            if response != expected_header:
                raise RuntimeError(f"unexpected read response: {response!r}")
            payload = read_exactly(connection, arguments.bytes)
            completed_ns = now_ns()
            if payload != expected:
                raise RuntimeError("received payload differs from the probe pattern")
            emit({
                "event": "reader_completed",
                "sequence": sequence,
                "request_started_ns": request_started_ns,
                "request_issued_ns": request_issued_ns,
                "completed_ns": completed_ns,
                "router_read_wait_ns": completed_ns - request_issued_ns,
            })


def run_writer(arguments):
    """Submit writes and measure local router acknowledgement latency."""
    payload = bytes([arguments.fill]) * arguments.bytes
    for sequence in range(arguments.count):
        with connect(arguments.port) as connection:
            request_started_ns = now_ns()
            header = f"W {arguments.pipe} {arguments.bytes}\n".encode("ascii")
            connection.sendall(header + payload)
            request_issued_ns = now_ns()
            response = read_line(connection)
            completed_ns = now_ns()
            if response != b"OK\n":
                raise RuntimeError(f"unexpected write response: {response!r}")
            emit({
                "event": "writer_completed",
                "sequence": sequence,
                "request_started_ns": request_started_ns,
                "request_issued_ns": request_issued_ns,
                "completed_ns": completed_ns,
                "router_write_service_ns": completed_ns - request_issued_ns,
            })
            if arguments.interval_ms:
                time.sleep(arguments.interval_ms / 1000.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reader", "writer"))
    parser.add_argument("--pipe", required=True, help="directed PipeComm buffer name")
    parser.add_argument("--bytes", type=int, default=64, help="payload size, at most 65000")
    parser.add_argument("--count", type=int, default=1, help="number of independent operations")
    parser.add_argument("--port", type=int, default=9400, help="local router port")
    parser.add_argument("--fill", type=int, default=90, help="one-byte payload pattern (0..255)")
    parser.add_argument("--interval-ms", type=float, default=0.0,
                        help="delay after every writer acknowledgement")
    arguments = parser.parse_args()
    if not 1 <= arguments.bytes <= 65000:
        parser.error("--bytes must be in 1..65000")
    if arguments.count < 1:
        parser.error("--count must be positive")
    if not 0 <= arguments.fill <= 255:
        parser.error("--fill must be in 0..255")
    if arguments.interval_ms < 0:
        parser.error("--interval-ms must not be negative")
    try:
        if arguments.mode == "reader":
            run_reader(arguments)
        else:
            run_writer(arguments)
    except (OSError, RuntimeError) as error:
        print(f"pipecomm-latency-probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
