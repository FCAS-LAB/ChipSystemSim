#!/usr/bin/env python3
"""Run a bounded native LEGOSim functional-startup matrix on a Swarm manager.

This is deliberately not a benchmark-completion runner.  It keeps the original
LEGOSim process graph but uses a previously generated instruction-limited YAML.
For each placement size it records (1) worker readiness, (2) startup of all
seven original MLP phase-one proxies, and (3) the first InterChiplet command.
The resulting timings are deployment/protocol-startup overhead only.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import paramiko
import yaml


DEFAULT_IMAGE = "192.168.244.1:5001/chipsystemsim:native-base-functional-ready"
PHASE_ONE_PROXY = re.compile(r"proxy: started process_id=phase1-\d+")
PHASE_ONE_COUNTS = {
    "mlp": 7, "dlrm": 2, "resnet": 7, "bfs": 7,
    "fft": 9, "pagerank": 9, "pde": 9, "moe": 8,
}


def remote_command(client: paramiko.SSHClient, command: str) -> tuple[int, str]:
    """Run one command and return its exit code plus combined text output."""
    _, stdout, stderr = client.exec_command(command, timeout=90)
    # Docker service logs may exceed Paramiko's channel window. Combine both
    # streams and drain them before waiting for process exit; waiting first can
    # deadlock when the remote command is blocked on its full output buffer.
    stdout.channel.set_combine_stderr(True)
    output = stdout.read().decode(errors="replace")
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, output


def wait_for_removal(client: paramiko.SSHClient, stack: str) -> None:
    """Remove an older stack of the same name before attempting deployment."""
    remote_command(client, f"sudo docker stack rm {stack}")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        _, stacks = remote_command(client, "sudo docker stack ls --format '{{.Name}}'")
        network_code, _ = remote_command(client, f"sudo docker network inspect {stack}_chipsystemsim")
        if (stack not in {line.strip() for line in stacks.splitlines()} and network_code != 0):
            return
        time.sleep(1)
    raise RuntimeError(f"previous stack {stack} did not disappear within 45 seconds")


def rewrite_stack(source: Path, remote_directory: str, image: str) -> str:
    """Point the generated Compose file at manager-local config files and image."""
    stack = yaml.safe_load(source.read_text(encoding="utf-8"))
    for service in stack["services"].values():
        service["image"] = image
    stack["configs"]["workload"]["file"] = f"{remote_directory}/workload.yml"
    stack["configs"]["topology"]["file"] = f"{remote_directory}/topology.json"
    stack["configs"]["routing"]["file"] = f"{remote_directory}/routing.json"
    return yaml.safe_dump(stack, sort_keys=False)


def service_states(client: paramiko.SSHClient, stack: str, node_count: int) -> dict[str, bool]:
    """Return whether each current task has reached Docker's Running state."""
    states: dict[str, bool] = {}
    for service in ["coordinator", *[f"transport-{slot}" for slot in range(node_count)]]:
        _, text = remote_command(
            client,
            "sudo docker service ps --no-trunc "
            f"--format '{{{{.DesiredState}}}}|{{{{.CurrentState}}}}|{{{{.Error}}}}' {stack}_{service}",
        )
        states[service] = any(
            line.startswith("Running|Running") for line in text.splitlines()
        )
    return states


def service_log(client: paramiko.SSHClient, stack: str, service: str, tail: int | None = None) -> str:
    """Obtain logs without treating a stopped task as a collection failure."""
    tail_option = f" --tail {tail}" if tail is not None else ""
    _, text = remote_command(client, f"sudo docker service logs{tail_option} {stack}_{service}")
    return text


def run_point(client: paramiko.SSHClient, workload: str, source_directory: Path, output_directory: Path,
              node_count: int, timeout_seconds: int, observation_seconds: int,
              image: str) -> dict[str, object]:
    """Deploy one placement and collect bounded functional-startup evidence."""
    stack = f"chipsystemsim_{workload}_functional_{node_count}"
    remote_directory = f"/home/legosim/functional-matrix/{stack}"
    output_directory.mkdir(parents=True, exist_ok=False)
    remote_command(client, f"mkdir -p {remote_directory}")
    sftp = client.open_sftp()
    try:
        for name in ("workload.yml", "topology.json", "routing.json"):
            sftp.put(str(source_directory / name), f"{remote_directory}/{name}")
        stack_text = rewrite_stack(source_directory / "stack.yml", remote_directory, image)
        with sftp.file(f"{remote_directory}/stack.yml", "w") as remote_stack:
            remote_stack.write(stack_text)
    finally:
        sftp.close()
    (output_directory / "stack.yml").write_text(stack_text, encoding="utf-8")

    wait_for_removal(client, stack)
    started = time.monotonic()
    code, deployment = remote_command(
        client, f"sudo docker stack deploy --resolve-image never -c {remote_directory}/stack.yml {stack}"
    )
    result: dict[str, object] = {
        "nodes": node_count,
        "workload": workload,
        "stack": stack,
        "image": image,
        "mode": "instruction-limited-functional-startup",
        "timeout_seconds": timeout_seconds,
        "observation_seconds": observation_seconds,
        "deploy_exit_code": code,
        "ready_seconds": None,
        "all_phase1_started_seconds": None,
        "first_interchiplet_command_seconds": None,
        "phase1_proxy_count": 0,
        "status": "deployment-error" if code else "timeout",
    }
    (output_directory / "deployment.log").write_text(deployment, encoding="utf-8")
    if code:
        return result

    deadline = started + timeout_seconds
    coordinator_log = ""
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - started
        states = service_states(client, stack, node_count)
        if result["ready_seconds"] is None and all(states.values()):
            result["ready_seconds"] = round(elapsed, 3)
        coordinator_log = service_log(client, stack, "coordinator", tail=2_000)
        proxy_count = len(set(PHASE_ONE_PROXY.findall(coordinator_log)))
        result["phase1_proxy_count"] = proxy_count
        if result["all_phase1_started_seconds"] is None and proxy_count >= PHASE_ONE_COUNTS[workload]:
            result["all_phase1_started_seconds"] = round(elapsed, 3)
        if result["first_interchiplet_command_seconds"] is None and "[INTERCMD]" in coordinator_log:
            result["first_interchiplet_command_seconds"] = round(elapsed, 3)
        if result["all_phase1_started_seconds"] is not None and result["first_interchiplet_command_seconds"] is not None:
            result["status"] = "functional-ok"
            break
        time.sleep(1)

    # Keep the native processes alive for a fixed, bounded observation period.
    # A coordinator can issue an InterChiplet command before the matching
    # PipeComm read finishes, so tearing down immediately would undercount
    # cross-node transfer and synchronization events.
    if result["status"] == "functional-ok" and observation_seconds:
        observation_deadline = time.monotonic() + observation_seconds
        while time.monotonic() < observation_deadline:
            time.sleep(1)

    # This is the bounded workload's wall-clock duration. Log download and
    # Swarm teardown are collection overhead and must not alter runtime or
    # speedup calculations.
    result["measurement_elapsed_seconds"] = round(time.monotonic() - started, 3)

    for service in ["coordinator", *[f"transport-{slot}" for slot in range(node_count)]]:
        (output_directory / f"{service}.log").write_text(
            service_log(client, stack, service, tail=1_500), encoding="utf-8"
        )
    result["collection_elapsed_seconds"] = round(time.monotonic() - started, 3)
    (output_directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    wait_for_removal(client, stack)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", default="192.168.244.135")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--workloads", nargs="+", choices=sorted(PHASE_ONE_COUNTS), default=["mlp"])
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--observation-seconds", type=int, default=0,
                        help="additional bounded post-startup interval used to collect PipeComm metrics")
    arguments = parser.parse_args()
    password = arguments.password_file.read_text(encoding="utf-8").strip()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(arguments.manager, username="legosim", password=password,
                   timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        results = []
        for workload in arguments.workloads:
            for node_count in arguments.nodes:
                source = arguments.source_root / f"{workload}-nodes{node_count}"
                destination = arguments.output_root / f"{workload}-nodes{node_count}"
                results.append(run_point(client, workload, source, destination, node_count,
                                         arguments.timeout_seconds, arguments.observation_seconds,
                                         arguments.image))
                print(json.dumps(results[-1], ensure_ascii=False))
        arguments.output_root.mkdir(parents=True, exist_ok=True)
        (arguments.output_root / "summary.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
