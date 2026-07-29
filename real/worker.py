#!/usr/bin/env python3
"""Remote process worker for a distributed LEGOSim coordinator.

One coordinator connection controls one upstream simulator process.  The worker
does not interpret LEGOSim commands: it forwards stdout/stderr bytes and writes
coordinator responses to the simulator's stdin.  This preserves the existing
InterChiplet command protocol while moving process placement out of the
coordinator container.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shlex
import socket
import time
import sys
from pathlib import Path
from typing import Any


# Concurrent phase-one connections can request the same immutable input asset.
# Serialize only that check-and-copy section so one worker does not observe an
# incomplete file created by another worker.
SHARED_ASSET_LOCK = asyncio.Lock()


def set_gpgpu_test_limits(
    workdir: Path, maximum_completed_cta: int | None, maximum_instructions: int | None
) -> None:
    """Apply functional-test limits to a copied upstream GPGPU-Sim config.

    The original configuration uses zero to mean unlimited. This helper is
    deliberately called only after the upstream ``pre_copy`` step, so it
    changes a per-process temporary copy and never mutates the native source
    tree or a shared image layer.
    """
    configuration = workdir / "gpgpusim.config"
    if not configuration.is_file():
        raise RuntimeError("GPGPU-Sim CTA limit requested but gpgpusim.config is missing")
    content = configuration.read_text(encoding="utf-8")
    updated = content
    limits = {
        "-gpgpu_max_completed_cta": maximum_completed_cta,
        "-gpgpu_max_insn": maximum_instructions,
    }
    for option, maximum in limits.items():
        if maximum is None:
            continue
        pattern = rf"(?m)^(\\s*{re.escape(option)}\\s+)\\d+(\\s*(?:#.*)?)$"
        updated, replacements = re.subn(
            pattern,
            lambda match: f"{match.group(1)}{maximum}{match.group(2)}",
            updated,
        )
        if replacements == 0:
            # The tested upstream SM7 configuration omits optional limits;
            # append the override to the private per-process copy.
            updated = updated.rstrip() + f"\n{option} {maximum}\n"
        elif replacements != 1:
            raise RuntimeError(f"GPGPU-Sim option {option} was found more than once")
    configuration.write_text(updated, encoding="utf-8")


async def send(writer: asyncio.StreamWriter, message: dict[str, Any], lock: asyncio.Lock) -> None:
    async with lock:
        writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()


async def forward_stream(
    stream: asyncio.StreamReader, stream_name: str, writer: asyncio.StreamWriter, lock: asyncio.Lock
) -> None:
    while chunk := await stream.read(4096):
        await send(writer, {"op": "output", "stream": stream_name,
                            "data": base64.b64encode(chunk).decode("ascii")}, lock)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, run_root: Path) -> None:
    lock = asyncio.Lock()
    process: asyncio.subprocess.Process | None = None
    try:
        peer = writer.get_extra_info("peername")
        print(f"worker: accepted connection peer={peer}", file=sys.stderr, flush=True)
        raw_request = await reader.readline()
        print(f"worker: received first frame bytes={len(raw_request)} peer={peer}", file=sys.stderr, flush=True)
        request = json.loads(raw_request.decode("utf-8"))
        if request.get("op") != "spawn":
            raise ValueError("first message must be spawn")
        process_id = str(request["process_id"])
        command = str(request["command"])
        arguments = [str(value) for value in request.get("args", [])]
        print(
            f"worker: spawn request process_id={process_id} command={command!r} args={arguments!r}",
            file=sys.stderr,
            flush=True,
        )
        environment = {**os.environ, **{str(key): str(value) for key, value in request.get("env", {}).items()}}
        # A coordinator-side proxy intentionally does not know which worker
        # hosts its child. Preserve this worker-local endpoint instead of
        # allowing an absent proxy field to disable the SimBricks backend.
        local_gateway = os.environ.get("LEGOSIM_PIPE_GATEWAY", "")
        if local_gateway:
            environment["LEGOSIM_PIPE_GATEWAY"] = local_gateway
            # Older GPGPU-Sim libcudart builds embed the initial distributed
            # PipeComm overlay, which selects LEGOSIM_FIFO_BROKER. Point its
            # compatibility name at the same BaseIf-backed gateway so CUDA
            # processes and freshly relinked CPU processes use one transport.
            environment["LEGOSIM_FIFO_BROKER"] = local_gateway
        print(
            f"worker: PipeComm gateway={environment.get('LEGOSIM_PIPE_GATEWAY', '<disabled>')}",
            file=sys.stderr,
            flush=True,
        )
        for endpoint_variable in ("LEGOSIM_FIFO_BROKER", "LEGOSIM_PIPE_GATEWAY"):
            endpoint = environment.get(endpoint_variable, "")
            if not endpoint:
                continue
            broker_host, broker_port = endpoint.rsplit(":", 1)
            # Resolve in Python before loading a Pin/SIFT plugin. Pin's linker
            # namespace cannot resolve getaddrinfo symbols from our C++ overlay.
            deadline = time.monotonic() + 60
            while True:
                try:
                    broker_ip = socket.gethostbyname(broker_host)
                    break
                except socket.gaierror:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"broker {broker_host} did not resolve within 60 seconds")
                    await asyncio.sleep(1)
            environment[endpoint_variable] = f"{broker_ip}:{broker_port}"
        pre_copy = str(request.get("pre_copy", ""))
        gpgpu_max_completed_cta = request.get("gpgpu_max_completed_cta")
        gpgpu_max_instructions = request.get("gpgpu_max_instructions")
        if gpgpu_max_completed_cta is not None:
            if not isinstance(gpgpu_max_completed_cta, int) or gpgpu_max_completed_cta < 1:
                raise ValueError("gpgpu_max_completed_cta must be a positive integer")
        if gpgpu_max_instructions is not None:
            if not isinstance(gpgpu_max_instructions, int) or gpgpu_max_instructions < 1:
                raise ValueError("gpgpu_max_instructions must be a positive integer")
        shared_assets = request.get("shared_assets", [])
        if not isinstance(shared_assets, list) or not all(isinstance(asset, str) for asset in shared_assets):
            raise ValueError("shared_assets must be a list of shell path strings")
        if "/" in process_id or process_id in {"", ".", ".."}:
            raise ValueError("process_id must be a single safe path component")
        # LEGOSim can restart the same YAML process in later convergence rounds.
        # Preserve every round's files instead of rejecting a valid re-launch.
        workdir = run_root / process_id
        suffix = 1
        while workdir.exists():
            workdir = run_root / f"{process_id}-{suffix}"
            suffix += 1
        workdir.mkdir(parents=True)
        # A few upstream applications intentionally address input files as
        # `../file` from their per-process workdir.  Put declared immutable
        # assets in the worker run root, once per spawn, without changing the
        # application's source-relative lookup convention.
        for asset in shared_assets:
            destination = run_root / Path(asset).name
            async with SHARED_ASSET_LOCK:
                if destination.exists():
                    continue
                copy_result = await asyncio.create_subprocess_exec(
                    "/bin/sh", "-c",
                    f"cp -- {shlex.quote(asset)} {shlex.quote(str(run_root))}/",
                    cwd=run_root, env=environment,
                )
                if await copy_result.wait() != 0:
                    raise RuntimeError(f"shared worker asset copy failed: {asset}")
        if pre_copy:
            copy_result = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", f"cp {pre_copy} .", cwd=workdir, env=environment
            )
            if await copy_result.wait() != 0:
                raise RuntimeError("upstream pre_copy command failed")
        if gpgpu_max_completed_cta is not None or gpgpu_max_instructions is not None:
            set_gpgpu_test_limits(workdir, gpgpu_max_completed_cta, gpgpu_max_instructions)
        # Return the resolved CUDA runtime dependencies through the existing
        # proxy connection. Docker service logs can disappear when Swarm tears
        # down a failed task, whereas the coordinator log is preserved per run.
        diagnostic_environment = {
            name: environment.get(name, "")
            for name in ("LD_LIBRARY_PATH", "CUDA_INSTALL_PATH", "LEGOSIM_PIPE_GATEWAY", "LEGOSIM_FIFO_BROKER")
        }
        ldd_result = await asyncio.create_subprocess_exec(
            "ldd", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=environment
        )
        ldd_output, _ = await ldd_result.communicate()
        diagnostic = {
            "op": "diagnostic",
            "text": json.dumps({
                "command": command,
                "gpgpu_max_completed_cta": gpgpu_max_completed_cta,
                "gpgpu_max_instructions": gpgpu_max_instructions,
                "environment": diagnostic_environment,
                "ldd_exit": ldd_result.returncode,
                "ldd": ldd_output.decode("utf-8", errors="replace"),
            }, separators=(",", ":")),
        }
        # Preserve the same evidence in the worker service log. InterChiplet
        # may discard wrapper stderr when it only reports a child exit status.
        print(f"worker diagnostic [{process_id}]: {diagnostic['text']}", file=sys.stderr, flush=True)
        await send(writer, diagnostic, lock)
        launch_diagnostic = workdir / "launch-diagnostic.json"
        launch_diagnostic.write_text(diagnostic["text"] + "\n", encoding="utf-8")
        # Route the same record through the child stderr stream. That stream
        # is already forwarded by the worker and is the only channel that
        # upstream InterChiplet reliably preserves in coordinator logs.
        process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", 'cat "$1" >&2; shift; exec "$@"',
            "legosim-worker-launch", str(launch_diagnostic), command, *arguments,
            cwd=workdir, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        print(f"worker: spawned {process_id} pid={process.pid} cwd={workdir}", file=sys.stderr, flush=True)
        await send(writer, {"op": "started", "pid": process.pid}, lock)
        output_tasks = [
            asyncio.create_task(forward_stream(process.stdout, "stdout", writer, lock)),
            asyncio.create_task(forward_stream(process.stderr, "stderr", writer, lock)),
        ]
        wait_task = asyncio.create_task(process.wait())
        read_task = asyncio.create_task(reader.readline())
        while not wait_task.done():
            done, _ = await asyncio.wait((wait_task, read_task), return_when=asyncio.FIRST_COMPLETED)
            if wait_task in done:
                break
            raw = read_task.result()
            read_task = asyncio.create_task(reader.readline())
            if not raw:
                if not wait_task.done():
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                break
            message = json.loads(raw.decode("utf-8"))
            if message.get("op") == "stdin":
                assert process.stdin is not None
                process.stdin.write(base64.b64decode(message["data"], validate=True))
                await process.stdin.drain()
            elif message.get("op") == "terminate":
                if not wait_task.done():
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
            else:
                raise ValueError(f"unsupported worker operation: {message.get('op')}")
        read_task.cancel()
        return_code = await wait_task
        await asyncio.gather(*output_tasks)
        print(f"worker: process {process_id} exited rc={return_code}", file=sys.stderr, flush=True)
        await send(writer, {"op": "exit", "returncode": return_code}, lock)
    except Exception as error:
        print(f"worker: request failed: {error}", file=sys.stderr, flush=True)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await send(writer, {"op": "error", "error": str(error)}, lock)
    finally:
        writer.close()
        await writer.wait_closed()


async def main_async(port: int, run_root: Path) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_server(lambda reader, writer: handle(reader, writer, run_root), "0.0.0.0", port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9300)
    parser.add_argument("--run-root", type=Path, default=Path("/run/legosim"))
    arguments = parser.parse_args()
    asyncio.run(main_async(arguments.port, arguments.run_root))


if __name__ == "__main__":
    main()
