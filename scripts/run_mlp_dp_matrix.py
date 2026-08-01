#!/usr/bin/env python3
"""Run and validate deterministic MLP data-parallel jobs on a Swarm manager."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import paramiko

from run_remote_functional_matrix import remote_command, rewrite_stack, service_log, wait_for_removal
from mlp_dp_reference import reference_model


RESULT = re.compile(
    r"MLP_DP_RESULT rank=(?P<rank>\d+) iterations=(?P<iterations>\d+) "
    r"parameters=(?P<parameters>\d+) values=(?P<values>[0-9eE+.,-]+)"
)
RANKS = 8
PIPE_METRIC_PREFIX = "pipe-metric: "


def models_from_logs(log_text: str) -> dict[int, list[float]]:
    """Parse exactly one final model from each CPU rank's streamed output."""
    models: dict[int, list[float]] = {}
    for match in RESULT.finditer(log_text):
        rank = int(match["rank"])
        values = [float(item) for item in match["values"].split(",") if item]
        if int(match["iterations"]) != 100 or int(match["parameters"]) != len(values):
            raise RuntimeError(f"malformed MLP-DP result for rank {rank}")
        previous = models.setdefault(rank, values)
        if previous != values:
            raise RuntimeError(f"rank {rank} emitted conflicting final models")
    if set(models) != set(range(RANKS)):
        raise RuntimeError(f"expected ranks 0..{RANKS - 1}, found {sorted(models)}")
    return models


def max_error(reference: list[float], candidate: list[float]) -> tuple[float, float]:
    """Return maximum absolute and scale-normalised elementwise errors."""
    if len(reference) != len(candidate):
        raise RuntimeError("model parameter count differs from reference")
    absolute = max(abs(left - right) for left, right in zip(reference, candidate))
    relative = max(
        abs(left - right) / max(abs(left), 1e-30)
        for left, right in zip(reference, candidate)
    )
    return absolute, relative


def assert_equivalent(reference: list[float], candidate: list[float], absolute_tolerance: float,
                      relative_tolerance: float, description: str) -> dict[str, float]:
    absolute, relative = max_error(reference, candidate)
    if absolute > absolute_tolerance and relative > relative_tolerance:
        raise RuntimeError(
            f"{description} differs from baseline: abs={absolute:.3e}, rel={relative:.3e}"
        )
    return {"max_absolute_error": absolute, "max_relative_error": relative}


def pipe_metric_summary(logs: list[str], wall_seconds: float) -> dict[str, float | int]:
    """Summarise complete router records without treating concurrent waits as a critical path."""
    metrics: list[dict[str, object]] = []
    for log in logs:
        for line in log.splitlines():
            if PIPE_METRIC_PREFIX not in line:
                continue
            metrics.append(json.loads(line.split(PIPE_METRIC_PREFIX, 1)[1]))

    cross_node = [metric for metric in metrics if metric["cross_node"]]
    elapsed_seconds = sum(int(metric["elapsed_ns"]) for metric in cross_node) / 1_000_000_000
    synchronization_seconds = (
        sum(int(metric["synchronization_wait_ns"]) for metric in cross_node) / 1_000_000_000
    )
    return {
        "all_pipe_operation_count": len(metrics),
        "cross_node_operation_count": len(cross_node),
        "cross_node_router_bytes": sum(int(metric["bytes"]) for metric in cross_node),
        "cross_node_router_elapsed_seconds": round(elapsed_seconds, 6),
        "cross_node_synchronization_wait_seconds": round(synchronization_seconds, 6),
        # These are aggregate process-seconds divided by job wall time. Router
        # operations from independent ranks can overlap, so they are not a
        # critical-path attribution of the end-to-end duration.
        "cross_node_router_elapsed_wall_percent": round(100 * elapsed_seconds / wall_seconds, 6),
        "cross_node_synchronization_wait_wall_percent": round(
            100 * synchronization_seconds / wall_seconds, 6
        ),
    }


def upload_configuration(client: paramiko.SSHClient, source: Path, remote_directory: str,
                         image: str) -> str:
    """Upload the generated config and rewrite its image/config file locations."""
    sftp = client.open_sftp()
    try:
        for name in ("workload.yml", "topology.json", "routing.json"):
            sftp.put(str(source / name), f"{remote_directory}/{name}")
    finally:
        sftp.close()
    stack = rewrite_stack(source / "stack.yml", remote_directory, image)
    sftp = client.open_sftp()
    try:
        with sftp.file(f"{remote_directory}/stack.yml", "w") as handle:
            handle.write(stack)
    finally:
        sftp.close()
    return stack


def run_once(client: paramiko.SSHClient, source: Path, output: Path, nodes: int, repetition: int,
             image: str, timeout_seconds: int) -> dict[str, object]:
    """Deploy one job and retain logs only after all eight rank models appear."""
    stack = f"chipsystemsim_mlp_dp_n{nodes}_r{repetition}"
    remote_directory = f"/home/legosim/mlp-dp/{stack}"
    output.mkdir(parents=True, exist_ok=False)
    remote_command(client, f"mkdir -p {remote_directory}")
    stack_text = upload_configuration(client, source, remote_directory, image)
    (output / "stack.yml").write_text(stack_text, encoding="utf-8")
    wait_for_removal(client, stack)
    started = time.monotonic()
    deploy_code, deploy_text = remote_command(
        client, f"sudo docker stack deploy --resolve-image never -c {remote_directory}/stack.yml {stack}"
    )
    (output / "deployment.log").write_text(deploy_text, encoding="utf-8")
    if deploy_code:
        raise RuntimeError(f"Swarm deployment failed for {stack}")
    deadline = started + timeout_seconds
    merged_logs = ""
    try:
        while time.monotonic() < deadline:
            logs = [service_log(client, stack, f"transport-{slot}", tail=4_000) for slot in range(nodes)]
            merged_logs = "\n".join(logs)
            try:
                models = models_from_logs(merged_logs)
                break
            except RuntimeError:
                time.sleep(2)
        else:
            raise TimeoutError(f"MLP-DP result markers did not appear within {timeout_seconds} seconds")
        # Use the short tail above only to detect completion. Persist the full
        # streams before stack teardown so every PipeComm timing record remains
        # available for cross-node communication accounting.
        complete_logs = [service_log(client, stack, f"transport-{slot}") for slot in range(nodes)]
        for slot, text in enumerate(complete_logs):
            (output / f"transport-{slot}.log").write_text(text, encoding="utf-8")
        coordinator = service_log(client, stack, "coordinator", tail=4_000)
        (output / "coordinator.log").write_text(coordinator, encoding="utf-8")
        within_run = [assert_equivalent(models[0], models[rank], 1e-12, 1e-12,
                                        f"rank {rank} in {stack}") for rank in range(1, RANKS)]
        wall_seconds = round(time.monotonic() - started, 3)
        result: dict[str, object] = {
            "nodes": nodes,
            "repetition": repetition,
            "status": "completed",
            "wall_seconds": wall_seconds,
            "model": models[0],
            "within_run_max_absolute_error": max(item["max_absolute_error"] for item in within_run),
            "within_run_max_relative_error": max(item["max_relative_error"] for item in within_run),
        }
        result["pipe_metrics"] = pipe_metric_summary(complete_logs, wall_seconds)
        (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        wait_for_removal(client, stack)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--nodes", type=int, nargs="+", default=[1, 2, 4, 8], choices=[1, 2, 4, 8])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=3_600)
    parser.add_argument("--absolute-tolerance", type=float, default=1e-6)
    parser.add_argument("--relative-tolerance", type=float, default=1e-5)
    arguments = parser.parse_args()
    if arguments.repetitions < 1:
        raise ValueError("--repetitions must be positive")
    password = arguments.password_file.read_text(encoding="utf-8").strip()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(arguments.manager, username="legosim", password=password,
                   timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        summaries: list[dict[str, object]] = []
        expected_model = reference_model()
        for repetition in range(1, arguments.repetitions + 1):
            reference: list[float] | None = None
            for nodes in arguments.nodes:
                source = arguments.source_root / f"mlp-dp-nodes{nodes}"
                output = arguments.output_root / f"nodes{nodes}" / f"rep-{repetition}"
                result = run_once(client, source, output, nodes, repetition, arguments.image,
                                  arguments.timeout_seconds)
                reference_comparison = assert_equivalent(
                    expected_model, list(result["model"]), arguments.absolute_tolerance,
                    arguments.relative_tolerance, f"{nodes}-node repetition {repetition} against reference"
                )
                if reference is None:
                    reference = list(result["model"])
                    comparison = {"max_absolute_error": 0.0, "max_relative_error": 0.0}
                else:
                    comparison = assert_equivalent(reference, list(result["model"]),
                                                   arguments.absolute_tolerance,
                                                   arguments.relative_tolerance,
                                                   f"{nodes}-node repetition {repetition}")
                result["baseline_comparison"] = comparison
                result["reference_comparison"] = reference_comparison
                (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                summaries.append({key: value for key, value in result.items() if key != "model"})
                print(json.dumps(summaries[-1], ensure_ascii=False))
        arguments.output_root.mkdir(parents=True, exist_ok=True)
        (arguments.output_root / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    finally:
        client.close()


if __name__ == "__main__":
    main()
