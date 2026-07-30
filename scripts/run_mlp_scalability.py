#!/usr/bin/env python3
"""Run a repeated MLP communication-epoch scalability experiment.

One sample ends when all expected native PipeComm operations in the fixed MLP
phase graph have completed. This is deliberately a fixed-work functional
experiment, not natural termination of instruction-limited GPU processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import paramiko
import yaml

from run_remote_functional_matrix import (
    PHASE_ONE_COUNTS,
    remote_command,
    rewrite_stack,
    service_log,
    service_states,
    wait_for_removal,
)


PIPE_METRIC = re.compile(r"pipe-metric: (\{[^\n]+\})")
PHASE_ONE_SPAWN = re.compile(r"worker: spawn request process_id=(phase1-\d+)")
PHASE_ONE_EXIT = re.compile(r"worker: process (phase1-\d+) exited rc=(\d+)")
PHASE_TWO_SPAWN = re.compile(r"worker: spawn request process_id=(phase2-\d+)")
WORKER_READY = re.compile(r"worker: ready port=9300")
EXPECTED_MLP_PIPECOMM_EVENTS = 18
COORDINATOR_COMPLETE = re.compile(r"^Complete\\|", re.MULTILINE)
COORDINATOR_FAILED = re.compile(r"^(?:Failed|Rejected)\\|", re.MULTILINE)


def transport_logs(client: paramiko.SSHClient, stack: str, node_count: int) -> str:
    """Read bounded logs without allowing an unavailable worker to hang polling."""
    commands = [
        # Worker diagnostics are intentionally verbose.  Keep enough history
        # to retain every phase-one launch marker as well as the first native
        # PipeComm records; a short tail made the old poller report zero work.
        f"timeout 15s sudo docker service logs --tail 500 {stack}_transport-{slot}"
        for slot in range(node_count)
    ]
    # A sequential query makes an eight-node prewarm check take up to
    # 8 * 15 seconds when Swarm is still converging.  Run the independent
    # service-log requests concurrently and tolerate a single slow worker;
    # the next polling iteration will collect it once ready.
    parallel = " ".join(f"({command} || true) &" for command in commands)
    _, output = remote_command(client, f"{parallel} wait")
    return output


def metric_count(log_text: str) -> int:
    """Count completed PipeComm records emitted by the native routers."""
    return len(PIPE_METRIC.findall(log_text))


def phase_one_spawn_count(log_text: str) -> int:
    """Count unique phase-one worker launch requests despite coordinator spam."""
    return len(set(PHASE_ONE_SPAWN.findall(log_text)))


def phase_one_exits(log_text: str) -> dict[str, int]:
    """Return the final observed exit code for every native phase-one process."""
    return {
        process_id: int(exit_code)
        for process_id, exit_code in PHASE_ONE_EXIT.findall(log_text)
    }


def rewrite_prewarm_stack(
    source: Path,
    remote_directory: str,
    image: str,
    train_iterations: int | None,
    parallel_gpu_tasks: bool,
) -> str:
    """Deploy transports first; coordinator timing begins only after prewarm."""
    stack = yaml.safe_load(rewrite_stack(source, remote_directory, image))
    stack["services"]["coordinator"].setdefault("deploy", {})["replicas"] = 0
    for name, service in stack["services"].items():
        if name.startswith("transport-"):
            environment = service.setdefault("environment", {})
            if train_iterations is not None:
                environment["LEGOSIM_MLP_TRAIN_ITERATIONS"] = str(train_iterations)
            if parallel_gpu_tasks:
                # The image retains serial behavior by default.  Set this only
                # for the explicit parallel-task experiment arm.
                environment["LEGOSIM_MLP_PARALLEL_GPU_TASKS"] = "1"
    return yaml.safe_dump(stack, sort_keys=False)


def remove_gpgpu_instruction_limits(workload: dict[str, object]) -> dict[str, object]:
    """Remove only the smoke-test instruction cap from a derived MLP YAML."""
    for process in workload.get("phase1", []):
        arguments = list(process.get("args", []))
        cleaned: list[object] = []
        index = 0
        while index < len(arguments):
            if arguments[index] == "--gpgpu-max-instructions":
                index += 2
                continue
            cleaned.append(arguments[index])
            index += 1
        process["args"] = cleaned
    return workload


def remove_sniper_fast_forward(workload: dict[str, object]) -> dict[str, object]:
    """Run the native CPU simulator instead of its smoke-test fast-forward mode."""
    for process in workload.get("phase1", []):
        arguments = list(process.get("args", []))
        process["args"] = [argument for argument in arguments if argument != "--fast-forward"]
    return workload


def enable_proxy_evidence(workload: dict[str, object]) -> dict[str, object]:
    """Preserve remote child startup and failure diagnostics in coordinator logs."""
    for phase in ("phase1", "phase2"):
        for process in workload.get(phase, []):
            process["is_to_stdout"] = True
    return workload


def coordinator_terminal_state(client: paramiko.SSHClient, stack: str) -> str | None:
    """Return the current coordinator task's terminal state, if any."""
    _, text = remote_command(
        client,
        "sudo docker service ps --no-trunc "
        "--format '{{.DesiredState}}|{{.CurrentState}}|{{.Error}}' "
        f"{stack}_coordinator",
    )
    # Swarm retains historical tasks.  Prewarm scales this service from zero
    # to one, so an old Complete task must never terminate the newly started
    # coordinator epoch.  Only the current desired Running task is relevant.
    for line in text.splitlines():
        desired, separator, remainder = line.partition("|")
        if desired != "Running" or not separator:
            continue
        if remainder.startswith("Complete|"):
            return "complete"
        if remainder.startswith(("Failed|", "Rejected|")):
            return "failed"
    return None


def capture_coordinator_process_logs(
    client: paramiko.SSHClient, stack: str, remote_directory: str
) -> None:
    """Archive upstream per-process logs before removing a finished task."""
    command = (
        "container=$(sudo docker ps -aq --filter "
        f"label=com.docker.swarm.service.name={stack}_coordinator | head -n 1); "
        "test -n \"$container\" || exit 0; "
        f"mkdir -p {remote_directory}/coordinator-process-logs; "
        "for path in proc_r1_p1_t0 proc_r1_p1_t1 proc_r1_p1_t2 proc_r1_p1_t3 "
        "proc_r1_p1_t4 proc_r1_p1_t5 proc_r1_p1_t6 proc_r1_p2_t0; do "
        "sudo docker cp \"$container\":/opt/legosim/$path "
        f"{remote_directory}/coordinator-process-logs/ 2>/dev/null || true; done; "
        "sudo docker cp \"$container\":/opt/legosim/bridge-trace.log "
        f"{remote_directory}/coordinator-process-logs/ 2>/dev/null || true"
    )
    remote_command(client, command)


def capture_runtime_snapshot(
    client: paramiko.SSHClient, stack: str, node_count: int, remote_directory: str
) -> None:
    """Preserve live process and socket state before Swarm removes a sample.

    Per-process stdout explains what the simulators last requested, while this
    snapshot distinguishes an intentional long GPGPU-Sim compute interval from
    a PipeComm socket/FIFO deadlock.  It must run before ``wait_for_removal``:
    Swarm otherwise destroys the only authoritative process state.
    """
    services = ["coordinator", *[f"transport-{slot}" for slot in range(node_count)]]
    quoted_services = " ".join(services)
    command = (
        f"mkdir -p {remote_directory}/runtime-snapshot; "
        f"for service in {quoted_services}; do "
        "container=$(sudo docker ps -q --filter "
        "label=com.docker.swarm.service.name=" + stack + "_$service | head -n 1); "
        "test -n \"$container\" || continue; "
        "{ "
        "echo \"service=$service container=$container\"; "
        "sudo docker exec \"$container\" sh -c "
        "'ps -eo pid,ppid,stat,wchan:32,etime,cmd; echo ---sockets---; ss -ntp' "
        "2>&1 || true; "
        "} > " + remote_directory + "/runtime-snapshot/$service.txt; "
        "done"
    )
    remote_command(client, command)


def deploy_sample(
    client: paramiko.SSHClient,
    source_directory: Path,
    output_directory: Path,
    node_count: int,
    repetition: int,
    run_id: str,
    timeout_seconds: int,
    image: str,
    expected_pipecomm_events: int,
    train_iterations: int | None,
    parallel_gpu_tasks: bool,
    wait_for_natural_completion: bool,
    unbounded_gpgpu: bool,
    unbounded_sniper: bool,
) -> dict[str, object]:
    """Run one fixed MLP communication epoch and preserve its evidence."""
    # Docker retains historical task logs for a service name after stack
    # removal. Include the output-root-derived ID so no sample can read a
    # previous experiment's PipeComm records.
    stack = f"chipsystemsim_mlp_scale_{run_id}_{node_count}_rep_{repetition}"
    remote_directory = f"/home/legosim/mlp-scalability/{stack}"
    output_directory.mkdir(parents=True, exist_ok=False)
    remote_command(client, f"mkdir -p {remote_directory}")
    sftp = client.open_sftp()
    try:
        for name in ("topology.json", "routing.json"):
            sftp.put(str(source_directory / name), f"{remote_directory}/{name}")
        workload = yaml.safe_load((source_directory / "workload.yml").read_text(encoding="utf-8"))
        workload = enable_proxy_evidence(workload)
        if unbounded_gpgpu:
            workload = remove_gpgpu_instruction_limits(workload)
        if unbounded_sniper:
            workload = remove_sniper_fast_forward(workload)
        with sftp.file(f"{remote_directory}/workload.yml", "w") as handle:
            handle.write(yaml.safe_dump(workload, sort_keys=False))
        stack_text = rewrite_prewarm_stack(
            source_directory / "stack.yml",
            remote_directory,
            image,
            train_iterations,
            parallel_gpu_tasks,
        )
        with sftp.file(f"{remote_directory}/stack.yml", "w") as handle:
            handle.write(stack_text)
    finally:
        sftp.close()
    (output_directory / "stack.yml").write_text(stack_text, encoding="utf-8")

    wait_for_removal(client, stack)
    prewarm_started = time.monotonic()
    deploy_code, deployment = remote_command(
        client, f"sudo docker stack deploy --resolve-image never -c {remote_directory}/stack.yml {stack}"
    )
    result: dict[str, object] = {
        "workload": "mlp",
        "nodes": node_count,
        "repetition": repetition,
        "stack": stack,
        "image": image,
        "mode": "natural-mlp-completion" if wait_for_natural_completion else "fixed-mlp-communication-epoch",
        "expected_pipecomm_events": expected_pipecomm_events,
        "timeout_seconds": timeout_seconds,
        "deploy_exit_code": deploy_code,
        "ready_seconds": None,
        "all_phase1_started_seconds": None,
        "first_interchiplet_command_seconds": None,
        "completed_pipecomm_events": 0,
        "phase_one_exit_count": 0,
        "epoch_completion_seconds": None,
        "communication_epoch_wall_seconds": None,
        "phase_graph_wall_seconds": None,
        "natural_completion_seconds": None,
        "status": "deployment-error" if deploy_code else "timeout",
    }
    (output_directory / "deployment.log").write_text(deployment, encoding="utf-8")
    if deploy_code:
        return result

    prewarm_deadline = prewarm_started + timeout_seconds
    while time.monotonic() < prewarm_deadline:
        try:
            workers_log = transport_logs(client, stack, node_count)
        except TimeoutError:
            # Swarm can temporarily block log forwarding while remote tasks
            # are being scheduled.  Treat it as an incomplete prewarm poll,
            # rather than abandoning the sample and leaving its stack alive.
            time.sleep(0.5)
            continue
        # A Docker task being ``Running`` only proves that its entrypoint has
        # started.  The coordinator must wait for the explicit worker marker:
        # especially with staggered eight-node BaseIf setup, a Running task can
        # still be several seconds away from accepting proxy connections.
        if len(WORKER_READY.findall(workers_log)) >= node_count:
            break
        time.sleep(1)
    else:
        result["status"] = "prewarm-timeout"
        result["prewarm_elapsed_seconds"] = round(time.monotonic() - prewarm_started, 3)
        (output_directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        wait_for_removal(client, stack)
        return result

    started = time.monotonic()
    # Docker's scale command can wait long enough for a short coordinator task
    # to execute before returning.  Start timing before issuing it so the
    # reported wall time covers task creation, execution, and completion.
    remote_command(client, f"sudo docker service scale {stack}_coordinator=1")
    result["prewarm_elapsed_seconds"] = round(started - prewarm_started, 3)
    deadline = started + timeout_seconds
    last_transport_log = ""
    coordinator_log = ""
    while time.monotonic() < deadline:
        if result["ready_seconds"] is None and all(service_states(client, stack, node_count).values()):
            result["ready_seconds"] = round(time.monotonic() - started, 3)
        coordinator_log = service_log(client, stack, "coordinator", tail=2_000)
        try:
            last_transport_log = transport_logs(client, stack, node_count)
        except TimeoutError:
            # A transiently unreachable Swarm worker must not abort the
            # sample. The bounded command is retried on the next poll.
            time.sleep(0.5)
            continue
        exits = phase_one_exits(last_transport_log)
        result["phase_one_exit_count"] = len(exits)
        failed_exits = {name: code for name, code in exits.items() if code != 0}
        if failed_exits:
            result["status"] = "phase-one-failed"
            result["phase_one_exit_codes"] = failed_exits
            break
        elapsed = time.monotonic() - started
        if wait_for_natural_completion:
            terminal_state = coordinator_terminal_state(client, stack)
            if terminal_state == "failed":
                result["status"] = "coordinator-failed"
                break
            # LEGOSim's coordinator owns the original single-epoch termination
            # protocol.  The independent worker supervisors intentionally stay
            # alive for diagnostics, so requiring all child exit messages here
            # would turn a completed epoch into an artificial timeout.
            if terminal_state == "complete":
                phase_one_count = phase_one_spawn_count(last_transport_log)
                phase_two_count = len(set(PHASE_TWO_SPAWN.findall(last_transport_log)))
                if phase_one_count >= PHASE_ONE_COUNTS["mlp"] and phase_two_count >= 1:
                    result["natural_completion_seconds"] = round(elapsed, 3)
                    result["status"] = "natural-completion"
                    break
                result["status"] = "incomplete-native-phase-graph"
                result["phase_one_spawn_count"] = phase_one_count
                result["phase_two_spawn_count"] = phase_two_count
                break
        if result["first_interchiplet_command_seconds"] is None and "[INTERCMD]" in coordinator_log:
            result["first_interchiplet_command_seconds"] = round(elapsed, 3)
        elapsed = time.monotonic() - started
        if (
            result["all_phase1_started_seconds"] is None
            and phase_one_spawn_count(last_transport_log) >= PHASE_ONE_COUNTS["mlp"]
        ):
            result["all_phase1_started_seconds"] = round(elapsed, 3)
        if not wait_for_natural_completion and result["all_phase1_started_seconds"] is not None:
            result["completed_pipecomm_events"] = metric_count(last_transport_log)
            if result["completed_pipecomm_events"] >= expected_pipecomm_events:
                completion = time.monotonic() - started
                result["epoch_completion_seconds"] = round(completion, 3)
                if result["first_interchiplet_command_seconds"] is not None:
                    result["communication_epoch_wall_seconds"] = round(
                        completion - float(result["first_interchiplet_command_seconds"]), 3
                    )
                result["phase_graph_wall_seconds"] = round(
                    completion - float(result["all_phase1_started_seconds"]), 3
                )
                result["status"] = "communication-epoch-complete"
                break
        time.sleep(0.5)

    # Capture raw evidence before the asynchronous Swarm removal starts.
    capture_runtime_snapshot(client, stack, node_count, remote_directory)
    capture_coordinator_process_logs(client, stack, remote_directory)
    archive_directory = output_directory / "coordinator-process-logs"
    archive_directory.mkdir(exist_ok=True)
    snapshot_directory = output_directory / "runtime-snapshot"
    snapshot_directory.mkdir(exist_ok=True)
    sftp = client.open_sftp()
    try:
        sftp.get(
            f"{remote_directory}/coordinator-process-logs/bridge-trace.log",
            str(archive_directory / "bridge-trace.log"),
        )
        # Copy only the upstream process directories.  They are the authoritative
        # location for simulator stdout/stderr when the coordinator has exited.
        for item in sftp.listdir_attr(f"{remote_directory}/coordinator-process-logs"):
            if item.filename.startswith("proc_r"):
                source = f"{remote_directory}/coordinator-process-logs/{item.filename}"
                target = archive_directory / item.filename
                target.mkdir(exist_ok=True)
                for entry in sftp.listdir_attr(source):
                    if not entry.filename.startswith("."):
                        sftp.get(f"{source}/{entry.filename}", str(target / entry.filename))
        for item in sftp.listdir_attr(f"{remote_directory}/runtime-snapshot"):
            if item.filename.endswith(".txt"):
                sftp.get(
                    f"{remote_directory}/runtime-snapshot/{item.filename}",
                    str(snapshot_directory / item.filename),
                )
    except (IOError, OSError):
        pass
    finally:
        sftp.close()
    for service in ["coordinator", *[f"transport-{slot}" for slot in range(node_count)]]:
        try:
            _, text = remote_command(
                client, f"timeout 15s sudo docker service logs --tail 500 {stack}_{service}"
            )
        except TimeoutError as error:
            # Preserve the measurement result even when Docker's log driver is
            # temporarily unable to reach a remote Swarm worker.
            text = f"log collection timed out: {error}\n"
        (output_directory / f"{service}.log").write_text(text, encoding="utf-8")
    final_transport_logs = "\n".join(
        (output_directory / f"transport-{slot}.log").read_text(encoding="utf-8")
        for slot in range(node_count)
    )
    result["completed_pipecomm_events"] = metric_count(final_transport_logs)
    result["phase_one_exit_count"] = len(phase_one_exits(final_transport_logs))
    result["collection_elapsed_seconds"] = round(time.monotonic() - started, 3)
    (output_directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    wait_for_removal(client, stack)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--source-suffix",
        default="",
        help=("optional suffix in generated workload directory names; for example "
              "'-insn1000' selects mlp-nodes1-insn1000 through mlp-nodes8-insn1000"),
    )
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--first-repetition", type=int, default=1,
                        help="first one-based repetition number; supports resuming a matrix")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--expected-pipecomm-events",
        type=int,
        default=EXPECTED_MLP_PIPECOMM_EVENTS,
        help=("number of native router completion records required for a sample; "
              "the default 18 is the complete configured MLP epoch"),
    )
    parser.add_argument("--train-iterations", type=int,
                        help="finite native CPU MLP training iterations, passed to worker services")
    parser.add_argument("--parallel-gpu-tasks", action="store_true",
                        help="enable the image's upstream-derived parallel GPU task scheduler")
    parser.add_argument("--wait-for-natural-completion", action="store_true",
                        help="measure coordinator normal exit instead of a PipeComm prefix")
    parser.add_argument("--unbounded-gpgpu", action="store_true",
                        help="remove the generated smoke-test GPU instruction cap")
    parser.add_argument("--unbounded-sniper", action="store_true",
                        help="remove Sniper's smoke-test --fast-forward option")
    parser.add_argument("--epochs-per-sample", type=int, default=1,
                        help="serially repeat the native one-epoch MLP process graph")
    parser.add_argument("--manager", default="192.168.244.135")
    arguments = parser.parse_args()
    if arguments.expected_pipecomm_events < 1:
        raise ValueError("--expected-pipecomm-events must be positive")
    if arguments.train_iterations is not None and arguments.train_iterations < 1:
        raise ValueError("--train-iterations must be positive")
    if arguments.epochs_per_sample < 1:
        raise ValueError("--epochs-per-sample must be positive")
    password = arguments.password_file.read_text(encoding="utf-8").strip()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(arguments.manager, username="legosim", password=password,
                   timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        samples = []
        run_id = hashlib.sha256(
            str(arguments.output_root.resolve()).encode("utf-8")
        ).hexdigest()[:10]
        for node_count in arguments.nodes:
            source = arguments.source_root / f"mlp-nodes{node_count}{arguments.source_suffix}"
            if not source.is_dir():
                raise FileNotFoundError(f"generated MLP source directory not found: {source}")
            for repetition in range(
                arguments.first_repetition,
                arguments.first_repetition + arguments.repetitions,
            ):
                output = arguments.output_root / f"nodes{node_count}" / f"rep-{repetition}"
                epoch_samples = []
                for epoch in range(1, arguments.epochs_per_sample + 1):
                    epoch_output = output / f"epoch-{epoch}"
                    epoch_sample = deploy_sample(
                        client, source, epoch_output, node_count, repetition,
                        f"{run_id}e{epoch}", arguments.timeout_seconds, arguments.image,
                        arguments.expected_pipecomm_events,
                        arguments.train_iterations,
                        arguments.parallel_gpu_tasks,
                        arguments.wait_for_natural_completion,
                        arguments.unbounded_gpgpu,
                        arguments.unbounded_sniper,
                    )
                    epoch_samples.append(epoch_sample)
                    success_status = (
                        "natural-completion"
                        if arguments.wait_for_natural_completion
                        else "communication-epoch-complete"
                    )
                    if epoch_sample["status"] != success_status:
                        raise RuntimeError(f"epoch {epoch} failed: {epoch_sample}")
                sample = {
                    "workload": "mlp",
                    "nodes": node_count,
                    "repetition": repetition,
                    "epochs_per_sample": arguments.epochs_per_sample,
                    "mode": "serial-native-mlp-epochs",
                    "status": "natural-completion" if arguments.wait_for_natural_completion
                    else "communication-epoch-complete",
                    "natural_completion_seconds": round(sum(
                        float(item["natural_completion_seconds"] or 0) for item in epoch_samples
                    ), 3),
                    "prewarm_elapsed_seconds": round(sum(
                        float(item.get("prewarm_elapsed_seconds") or 0) for item in epoch_samples
                    ), 3),
                    "completed_pipecomm_events": sum(
                        int(item.get("completed_pipecomm_events") or 0) for item in epoch_samples
                    ),
                    "epoch_results": epoch_samples,
                }
                output.mkdir(parents=True, exist_ok=True)
                (output / "result.json").write_text(
                    json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                samples.append(sample)
                print(json.dumps(sample, ensure_ascii=False))
        arguments.output_root.mkdir(parents=True, exist_ok=True)
        (arguments.output_root / "samples.json").write_text(
            json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
