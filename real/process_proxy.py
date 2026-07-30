#!/usr/bin/env python3
"""Run as an InterChiplet child and relay one simulator to a remote worker."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
def write_json(connection: socket.socket, message: dict[str, object], lock: threading.Lock) -> None:
    """Send exactly one protocol frame without relying on a duplex makefile.

    Python's unbuffered ``socket.makefile('rwb')`` did not reliably flush the
    first frame under Docker Desktop.  Keep socket writes on ``sendall`` and
    use a read-only file object only for line framing.
    """
    with lock:
        connection.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True, help="worker HOST:PORT")
    parser.add_argument("--process-id", required=True)
    parser.add_argument("--pre-copy", default="")
    parser.add_argument("--shared-asset", action="append", default=[],
                        help="file or glob copied into the worker run root before spawning")
    parser.add_argument("--stage-file", action="append", default=[], metavar="SOURCE:DESTINATION",
                        help="copy one coordinator-local input file into the remote child work directory")
    parser.add_argument("--gpgpu-max-completed-cta", type=int,
                        help="cap completed CTAs in the copied GPGPU-Sim configuration")
    parser.add_argument("--gpgpu-max-instructions", type=int,
                        help="cap simulated GPU instructions in the copied GPGPU-Sim configuration")
    parser.add_argument("command")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    staged_files: list[dict[str, str]] = []
    for specification in arguments.stage_file:
        source_text, separator, destination_text = specification.rpartition(":")
        if not separator or not source_text or not destination_text:
            raise ValueError("--stage-file must have SOURCE:DESTINATION form")
        destination = Path(destination_text)
        # The remote worker owns a fresh per-process directory.  Restrict
        # staging to one file in that directory; this prevents YAML input from
        # overwriting worker programs or another process's data.
        if destination.name != destination_text or destination_text in {".", ".."}:
            raise ValueError("--stage-file destination must be a plain filename")
        source = Path(source_text)
        if not source.is_file():
            raise FileNotFoundError(f"staged input does not exist: {source}")
        staged_files.append({
            "destination": destination_text,
            "data": base64.b64encode(source.read_bytes()).decode("ascii"),
        })
    host, port_text = arguments.worker.rsplit(":", 1)
    port = int(port_text)
    print(f"proxy: connecting process_id={arguments.process_id} worker={host}:{port}", file=sys.stderr, flush=True)

    # Swarm does not guarantee service start order.  A coordinator may launch
    # a phase-one process before its assigned worker has passed DNS discovery
    # or bound its port, so retry a bounded number of times instead of making
    # the experiment nondeterministically fail at startup.
    connection: socket.socket | None = None
    # Multi-VM cold starts can initialize several BaseIf peers concurrently.
    # Keep this comfortably above the worker supervisor's 90-second handshake
    # allowance so a healthy worker is not discarded during startup pressure.
    worker_ready_timeout_seconds = 180
    deadline = time.monotonic() + worker_ready_timeout_seconds
    last_error: OSError | None = None
    while connection is None and time.monotonic() < deadline:
        try:
            connection = socket.create_connection((host, port), timeout=5)
        except OSError as error:
            last_error = error
            time.sleep(1)
    if connection is None:
        raise RuntimeError(
            f"worker {arguments.worker} did not become ready within "
            f"{worker_ready_timeout_seconds} seconds"
        ) from last_error

    with connection:
        # socket.create_connection applies its timeout to the connected socket.
        # InterChiplet can legitimately remain silent for longer than five
        # seconds while a simulator executes, so use a timeout only for setup
        # and restore blocking I/O for the process lifetime.
        connection.settimeout(None)
        stream = connection.makefile("rb")
        write_lock = threading.Lock()
        print(f"proxy: connected process_id={arguments.process_id}", file=sys.stderr, flush=True)
        child_environment: dict[str, str] = {"SIMULATOR_ROOT": "/opt/legosim"}
        for endpoint_variable in ("LEGOSIM_FIFO_BROKER", "LEGOSIM_PIPE_GATEWAY"):
            endpoint = os.environ.get(endpoint_variable)
            if endpoint:
                child_environment[endpoint_variable] = endpoint
        write_json(connection, {
            "op": "spawn",
            "process_id": arguments.process_id,
            "command": arguments.command,
            "args": arguments.args,
            "pre_copy": arguments.pre_copy,
            "shared_assets": arguments.shared_asset,
            "staged_files": staged_files,
            "gpgpu_max_completed_cta": arguments.gpgpu_max_completed_cta,
            "gpgpu_max_instructions": arguments.gpgpu_max_instructions,
            "env": child_environment,
        }, write_lock)
        print(f"proxy: sent spawn process_id={arguments.process_id}", file=sys.stderr, flush=True)
        # The worker may emit a launch diagnostic (ldd/environment) before it
        # acknowledges spawn. Drain those frames instead of treating a valid
        # diagnostic as a rejected process.
        while True:
            started = json.loads(stream.readline())
            if started.get("op") == "diagnostic":
                print(f"proxy diagnostic [{arguments.process_id}]: {started['text']}",
                      file=sys.stderr, flush=True)
                continue
            if started.get("op") != "started":
                raise RuntimeError(started.get("error", "remote worker rejected process"))
            break
        print(f"proxy: started process_id={arguments.process_id} pid={started.get('pid')}", file=sys.stderr, flush=True)

        stop_stdin = threading.Event()

        def forward_stdin() -> None:
            try:
                while not stop_stdin.is_set():
                    # Do not use BufferedReader.read1 in a daemon thread:
                    # interpreter shutdown may abort while that buffered lock
                    # is held. Raw os.read has no Python buffered-state lock.
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    write_json(connection, {"op": "stdin", "data": base64.b64encode(data).decode("ascii")}, write_lock)
                # InterChiplet phase-one processes communicate through named
                # pipes, not stdin. Its child stdin reaches EOF immediately;
                # that only ends this optional forwarding loop and must not
                # terminate the real simulator running on the worker.
            # The remote worker can exit while this daemon thread is blocked
            # in stdin.  Its socket close is an expected shutdown condition.
            except (BrokenPipeError, ConnectionError, OSError):
                return

        stdin_thread = threading.Thread(target=forward_stdin, daemon=True)
        stdin_thread.start()
        while True:
            response = json.loads(stream.readline())
            operation = response["op"]
            if operation == "output":
                output = sys.stdout.buffer if response["stream"] == "stdout" else sys.stderr.buffer
                output.write(base64.b64decode(response["data"], validate=True))
                output.flush()
            elif operation == "diagnostic":
                print(f"proxy diagnostic [{arguments.process_id}]: {response['text']}",
                      file=sys.stderr, flush=True)
            elif operation == "exit":
                stop_stdin.set()
                return_code = int(response["returncode"])
                print(f"proxy: remote exit process_id={arguments.process_id} rc={return_code}", file=sys.stderr, flush=True)
                return return_code
            else:
                raise RuntimeError(response.get("error", f"unexpected worker response: {operation}"))


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except Exception as error:
        # InterChiplet only exposes the wrapper's exit status unless the proxy
        # emits its startup failure. Keep the remote rejection text and stack
        # trace in the coordinator log for deterministic diagnosis.
        print(f"proxy: fatal startup error: {error}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
