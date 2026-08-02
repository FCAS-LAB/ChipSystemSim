#!/usr/bin/env bash
# Run one generated MLP-DP configuration in a single-host DinD Swarm and
# preserve the coordinator/transport evidence needed for later analysis.
#
# The outer Docker Engine owns the DinD containers.  The nested manager is
# always <prefix>-0; every other nested daemon is a Swarm worker.
set -euo pipefail

nodes=0
prefix=""
config_dir=""
stack=""
output_dir=""
timeout_seconds=900

usage() {
  cat <<'EOF'
Usage:
  run_dind_mlp_dp.sh --nodes N --prefix PREFIX --config-dir DIR \
      --stack NAME --output-dir DIR [--timeout-seconds N]

The generated configuration directory must contain stack.yml, workload.yml,
topology.json and routing.json.  The matching DinD Swarm must already have
been created by provision_dind_swarm.sh.
EOF
}

while (($#)); do
  case "$1" in
    --nodes) nodes="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --config-dir) config_dir="$2"; shift 2 ;;
    --stack) stack="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --timeout-seconds) timeout_seconds="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$nodes" =~ ^(1|2|4|8)$ ]] || { echo "--nodes must be 1, 2, 4, or 8" >&2; exit 2; }
[[ -n "$prefix" && -n "$config_dir" && -n "$stack" && -n "$output_dir" ]] || {
  usage >&2; exit 2;
}
for file in stack.yml workload.yml topology.json routing.json; do
  [[ -f "$config_dir/$file" ]] || { echo "missing $config_dir/$file" >&2; exit 2; }
done

manager="${prefix}-0"
docker inspect "$manager" >/dev/null
for slot in $(seq 0 $((nodes - 1))); do
  docker inspect "${prefix}-${slot}" >/dev/null
done

run_id="${stack}-files"
inner_dir="/work/${run_id}"
stage_dir=$(mktemp -d)
cleanup_stage() { rm -rf "$stage_dir"; }
trap cleanup_stage EXIT
mkdir -p "$output_dir"

# Docker configs are resolved by the nested manager, not by the outer host.
# Copy the generated files into that manager and rewrite only their config
# source paths.  Start the coordinator at zero replicas so all transports are
# ready before the timer begins.
cp "$config_dir"/{workload.yml,topology.json,routing.json} "$stage_dir/"
awk -v inner="$inner_dir" '
  /^  coordinator:$/ { in_coordinator = 1 }
  in_coordinator && /^    deploy:$/ {
    print
    print "      replicas: 0"
    next
  }
  /^  [[:alnum:]][[:alnum:]_-]*:$/ && $0 != "  coordinator:" { in_coordinator = 0 }
  /file: .*workload\.yml/ { sub(/file: .*/, "file: " inner "/workload.yml") }
  /file: .*topology\.json/ { sub(/file: .*/, "file: " inner "/topology.json") }
  /file: .*routing\.json/ { sub(/file: .*/, "file: " inner "/routing.json") }
  { print }
' "$config_dir/stack.yml" > "$stage_dir/stack.yml"

docker exec "$manager" mkdir -p "$inner_dir"
tar -C "$stage_dir" -cf - . | docker exec -i "$manager" tar -C "$inner_dir" -xf -
docker exec "$manager" docker stack rm "$stack" >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
  [[ -z "$(docker exec "$manager" docker service ls -q --filter "name=${stack}_")" ]] && break
  sleep 1
done
docker exec "$manager" docker stack deploy --detach=true -c "$inner_dir/stack.yml" "$stack" >/dev/null

for slot in $(seq 0 $((nodes - 1))); do
  service="${stack}_transport-${slot}"
  for _ in $(seq 1 180); do
    replicas=$(docker exec "$manager" docker service ls --filter "name=${service}" --format '{{.Replicas}}' || true)
    [[ "$replicas" == "1/1" ]] && break
    sleep 1
  done
  [[ "${replicas:-}" == "1/1" ]] || { echo "transport not ready: $service" >&2; exit 1; }
done

docker exec "$manager" docker service scale --detach=true "${stack}_coordinator=1" >/dev/null
coordinator="${stack}_coordinator"
coordinator_id=""
for _ in $(seq 1 "$timeout_seconds"); do
  coordinator_id=$(docker exec "$manager" docker ps -aq --filter "name=${coordinator}" | head -n 1 || true)
  if [[ -n "$coordinator_id" ]]; then
    status=$(docker exec "$manager" docker inspect "$coordinator_id" --format '{{.State.Status}}')
    [[ "$status" == "exited" ]] && break
  fi
  sleep 1
done
[[ -n "$coordinator_id" ]] || { echo "coordinator was never created" >&2; exit 1; }

docker exec "$manager" docker inspect "$coordinator_id" \
  --format $'id={{.Id}}\nstart={{.State.StartedAt}}\nfinish={{.State.FinishedAt}}\nexit={{.State.ExitCode}}' \
  > "$output_dir/coordinator_timing.txt"
exit_code=$(sed -n 's/^exit=//p' "$output_dir/coordinator_timing.txt")
[[ "$exit_code" == "0" ]] || { echo "coordinator did not finish successfully" >&2; exit 1; }
docker exec "$manager" docker logs "$coordinator_id" > "$output_dir/coordinator.log" 2>&1
grep -q 'End of Simulation' "$output_dir/coordinator.log" || {
  echo "missing natural-completion marker" >&2; exit 1;
}

: > "$output_dir/transport.log"
for slot in $(seq 0 $((nodes - 1))); do
  outer="${prefix}-${slot}"
  for container in $(docker exec "$outer" docker ps -aq --filter "name=${stack}_transport"); do
    docker exec "$outer" docker logs "$container" >> "$output_dir/transport.log" 2>&1 || true
  done
done

python3 - "$output_dir/transport.log" "$output_dir/metrics.txt" <<'PY'
import json
import sys

source, output = sys.argv[1:]
keys = ("records", "bytes", "elapsed_ns", "sync_wait_ns", "writes", "reads")
all_values = {key: 0 for key in keys}
cross_values = {key: 0 for key in keys}
# A sum of rank-local waits can exceed the coordinator wall-clock duration.
# Keep the wall-clock union of cross-node reads separately: it measures how
# long at least one process was blocked on a remote PipeComm producer. This is
# the only synchronization duration that may safely be divided by the single
# coordinator timing interval.
cross_sync_intervals = []
with open(source, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        if not line.startswith("pipe-metric:"):
            continue
        record = json.loads(line.split(":", 1)[1])
        targets = [all_values]
        if record.get("cross_node"):
            targets.append(cross_values)
        for values in targets:
            values["records"] += 1
            values["bytes"] += int(record.get("bytes", 0))
            values["elapsed_ns"] += int(record.get("elapsed_ns", 0))
            values["sync_wait_ns"] += int(record.get("synchronization_wait_ns", 0))
            values["writes"] += record.get("operation") == "W"
            values["reads"] += record.get("operation") == "R"
        if record.get("cross_node") and record.get("operation") == "R":
            started = record.get("started_unix_ns")
            finished = record.get("finished_unix_ns")
            if isinstance(started, int) and isinstance(finished, int) and finished >= started:
                cross_sync_intervals.append((started, finished))


def interval_union_nanoseconds(intervals):
    """Return the wall-clock union of timestamped blocking intervals."""
    if not intervals:
        return 0
    total = 0
    current_start, current_finish = sorted(intervals)[0]
    for started, finished in sorted(intervals)[1:]:
        if started <= current_finish:
            current_finish = max(current_finish, finished)
            continue
        total += current_finish - current_start
        current_start, current_finish = started, finished
    return total + current_finish - current_start


cross_sync_wall_union_ns = interval_union_nanoseconds(cross_sync_intervals)
with open(output, "w", encoding="utf-8") as handle:
    for name, values in (("all", all_values), ("cross", cross_values)):
        for key in keys:
            handle.write(f"{name}_{key}={values[key]}\n")
    handle.write(f"cross_sync_wall_union_ns={cross_sync_wall_union_ns}\n")
    handle.write(f"cross_sync_wall_interval_count={len(cross_sync_intervals)}\n")
PY

printf 'completed stack=%s output=%s\n' "$stack" "$output_dir"
