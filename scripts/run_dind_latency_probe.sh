#!/usr/bin/env bash
# Measure raw TCP and native PipeComm delivery through two ready DinD workers.
#
# This script deploys only the transport services from a generated MLP-DP
# stack.  It never starts the LEGOSim coordinator or any CPU/GPU workload, so
# its output is a communication microbenchmark rather than an MLP measurement.
set -euo pipefail

nodes=0
prefix=""
config_dir=""
stack=""
output_dir=""
image=""
source_slot=0
destination_slot=1
pipe_name="buffer0_0_1_0"
payload_bytes=64
count=20

usage() {
  cat <<'EOF'
Usage:
  run_dind_latency_probe.sh --nodes N --prefix PREFIX --config-dir DIR \
      --stack NAME --output-dir DIR [options]

Required arguments:
  --nodes N             Number of already-provisioned DinD Swarm nodes (2, 4, or 8).
  --prefix PREFIX       Existing DinD container prefix.
  --config-dir DIR      Generated directory containing stack.yml and its JSON configs.
  --stack NAME          Temporary nested Docker stack name for the transport services.
  --output-dir DIR      Directory for JSONL records and summaries.
  --image IMAGE         Image already loaded in every nested Docker daemon.

Options:
  --source-slot N       Router slot that writes (default: 0).
  --destination-slot N  Router slot that reads (default: 1).
  --pipe NAME           Directed PipeComm buffer name (default: buffer0_0_1_0).
  --bytes N             Payload size in bytes (default: 64).
  --count N             Independent samples for each probe (default: 20).

The selected pipe must map from --source-slot to --destination-slot in the
given routing.json.  The script removes only its temporary nested stack when
it exits; the already-provisioned DinD nodes remain available for a workload.
EOF
}

while (($#)); do
  case "$1" in
    --nodes) nodes="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --config-dir) config_dir="$2"; shift 2 ;;
    --stack) stack="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --source-slot) source_slot="$2"; shift 2 ;;
    --destination-slot) destination_slot="$2"; shift 2 ;;
    --pipe) pipe_name="$2"; shift 2 ;;
    --bytes) payload_bytes="$2"; shift 2 ;;
    --count) count="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$nodes" =~ ^(2|4|8)$ ]] || { echo "--nodes must be 2, 4, or 8" >&2; exit 2; }
[[ "$source_slot" =~ ^[0-9]+$ && "$destination_slot" =~ ^[0-9]+$ ]] || {
  echo "worker slots must be non-negative integers" >&2; exit 2;
}
(( source_slot < nodes && destination_slot < nodes && source_slot != destination_slot )) || {
  echo "source and destination slots must be distinct values below --nodes" >&2; exit 2;
}
[[ "$payload_bytes" =~ ^[0-9]+$ ]] && (( payload_bytes >= 1 && payload_bytes <= 65000 )) || {
  echo "--bytes must be in 1..65000" >&2; exit 2;
}
[[ "$count" =~ ^[0-9]+$ ]] && (( count >= 1 )) || {
  echo "--count must be positive" >&2; exit 2;
}
[[ "$pipe_name" =~ ^buffer-?[0-9]+_-?[0-9]+_-?[0-9]+_-?[0-9]+$ ]] || {
  echo "--pipe must be a normalized bufferX_Y_X_Y PipeComm name" >&2; exit 2;
}
[[ -n "$prefix" && -n "$config_dir" && -n "$stack" && -n "$output_dir" && -n "$image" ]] || {
  usage >&2; exit 2;
}
for file in stack.yml workload.yml topology.json routing.json; do
  [[ -f "$config_dir/$file" ]] || { echo "missing $config_dir/$file" >&2; exit 2; }
done

manager="${prefix}-0"
source_outer="${prefix}-${source_slot}"
destination_outer="${prefix}-${destination_slot}"
for container in "$manager" "$source_outer" "$destination_outer"; do
  docker inspect "$container" >/dev/null
done

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
for script in pipecomm_latency_probe.py tcp_latency_probe.py \
              summarize_pipecomm_latency_probe.py summarize_tcp_latency_probe.py; do
  [[ -f "$script_dir/$script" ]] || { echo "missing probe helper: $script" >&2; exit 2; }
done

mkdir -p "$output_dir"
stage_dir=$(mktemp -d)
inner_dir="/work/${stack}-latency-files"
deployed=false

cleanup() {
  rm -rf "$stage_dir"
  if $deployed; then
    # Preserve router/gateway diagnostics before the temporary Swarm stack is
    # removed.  These logs are essential if a real PipeComm request is closed
    # by a gateway after transport readiness has already been established.
    : > "$output_dir/transport.log"
    for slot in "$source_slot" "$destination_slot"; do
      outer="${prefix}-${slot}"
      while IFS= read -r container; do
        [[ -n "$container" ]] || continue
        docker exec "$outer" docker logs "$container" >> "$output_dir/transport.log" 2>&1 || true
      done < <(docker exec "$outer" docker ps -aq \
        --filter "label=com.docker.swarm.service.name=${stack}_transport-${slot}" 2>/dev/null || true)
    done
    docker exec "$manager" docker stack rm "$stack" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Use the original stack and its exact placement/topology, but leave the
# coordinator at zero replicas.  This avoids creating an MLP process graph.
cp "$config_dir"/{workload.yml,topology.json,routing.json} "$stage_dir/"
awk -v inner="$inner_dir" -v selected_image="$image" '
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
  /^    image: / { print "    image: " selected_image; next }
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
deployed=true

for slot in $(seq 0 $((nodes - 1))); do
  service="${stack}_transport-${slot}"
  for _ in $(seq 1 240); do
    replicas=$(docker exec "$manager" docker service ls --filter "name=${service}" --format '{{.Replicas}}' || true)
    [[ "$replicas" == "1/1" ]] && break
    sleep 1
  done
  [[ "${replicas:-}" == "1/1" ]] || { echo "transport not ready: $service" >&2; exit 1; }
done

inner_container() {
  local outer="$1"
  local slot="$2"
  # Docker Swarm task names carry a generated suffix.  The service label is
  # stable across Docker versions, unlike a name substring through two Docker
  # client layers.  A just-ready service can need a moment before its task is
  # visible in the selected nested daemon.
  local container=""
  for _ in $(seq 1 30); do
    container=$(docker exec "$outer" docker ps -q \
      --filter "label=com.docker.swarm.service.name=${stack}_transport-${slot}" | head -n 1)
    [[ -n "$container" ]] && break
    sleep 1
  done
  printf '%s\n' "$container"
}

source_inner=$(inner_container "$source_outer" "$source_slot")
destination_inner=$(inner_container "$destination_outer" "$destination_slot")
[[ -n "$source_inner" && -n "$destination_inner" ]] || {
  echo "could not identify nested transport containers: source=${source_inner:-none} destination=${destination_inner:-none}" >&2
  docker exec "$manager" docker service ps "${stack}_transport-${source_slot}" --no-trunc >&2 || true
  docker exec "$manager" docker service ps "${stack}_transport-${destination_slot}" --no-trunc >&2 || true
  exit 1;
}

wait_for_router() {
  local outer="$1"
  local inner="$2"
  local description="$3"
  for _ in $(seq 1 180); do
    # A successful TCP connect is the supervisor's definitive readiness
    # boundary: only after every BaseIf link is established does it launch the
    # Python router on port 9400.  The empty connection is rejected harmlessly
    # by the router and does not enqueue a PipeComm operation.
    if docker exec "$outer" docker exec "$inner" python3 -c \
        'import socket; client = socket.create_connection(("127.0.0.1", 9400), 1); client.close()' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "router did not become ready within 180 seconds: $description" >&2
  return 1
}

wait_for_router "$source_outer" "$source_inner" "source slot $source_slot"
wait_for_router "$destination_outer" "$destination_inner" "destination slot $destination_slot"

copy_to_inner() {
  local outer="$1"
  local inner="$2"
  local file="$3"
  local name
  name=$(basename "$file")
  # `docker cp HOST:CONTAINER` is unreliable for the privileged DinD root
  # filesystem on the lab host: it can return success without materializing a
  # file below /tmp.  Stream through the running outer container instead, then
  # let its nested Docker client copy the verified local file into the task.
  docker exec -i "$outer" tee "/tmp/${name}" < "$file" >/dev/null
  docker exec "$outer" test -s "/tmp/${name}"
  docker exec "$outer" docker cp "/tmp/${name}" "${inner}:/tmp/${name}"
}

for helper in pipecomm_latency_probe.py tcp_latency_probe.py; do
  copy_to_inner "$source_outer" "$source_inner" "$script_dir/$helper"
  copy_to_inner "$destination_outer" "$destination_inner" "$script_dir/$helper"
done

# First measure the service-to-service network lower bound on a persistent
# TCP connection.  It has no PipeComm, BaseIf, router or queue operations.
# Keep the nested `docker exec` attached to this host-side script and place it
# in the background here.  Detached `docker exec` loses the probe's stdout in
# this Docker-in-Docker setup, which made readiness impossible to verify.
docker exec "$destination_outer" docker exec "$destination_inner" \
  python3 /tmp/tcp_latency_probe.py server --port 9411 --bytes "$payload_bytes" --count "$count" \
  > "$output_dir/tcp_server.log" 2>&1 &
tcp_server_pid=$!
for _ in $(seq 1 60); do
  if grep -qx ready "$output_dir/tcp_server.log" 2>/dev/null; then
    break
  fi
  sleep 1
done
grep -qx ready "$output_dir/tcp_server.log"
docker exec "$source_outer" docker exec "$source_inner" \
  python3 /tmp/tcp_latency_probe.py client --host "transport-${destination_slot}" \
  --port 9411 --bytes "$payload_bytes" --count "$count" > "$output_dir/tcp.jsonl"
wait "$tcp_server_pid"
python3 "$script_dir/summarize_tcp_latency_probe.py" \
  --input "$output_dir/tcp.jsonl" --output "$output_dir/tcp_summary.json"

# Post the receiver read before the source sends, so the following timing is
# a delivery measurement and not a workload/producer readiness measurement.
docker exec "$destination_outer" docker exec "$destination_inner" \
  python3 /tmp/pipecomm_latency_probe.py reader --pipe "$pipe_name" --bytes "$payload_bytes" --count "$count" \
  > "$output_dir/pipe_reader.jsonl" 2>&1 &
pipe_reader_pid=$!
for _ in $(seq 1 60); do
  if grep -q '"event": "reader_issued"' "$output_dir/pipe_reader.jsonl" 2>/dev/null; then
    break
  fi
  sleep 1
done
grep -q '"event": "reader_issued"' "$output_dir/pipe_reader.jsonl"
docker exec "$source_outer" docker exec "$source_inner" \
  python3 /tmp/pipecomm_latency_probe.py writer --pipe "$pipe_name" --bytes "$payload_bytes" \
  --count "$count" --interval-ms 10 > "$output_dir/pipe_writer.jsonl"
wait "$pipe_reader_pid"
[[ "$(grep -c '"event": "reader_completed"' "$output_dir/pipe_reader.jsonl")" == "$count" ]] || {
  echo "PipeComm reader did not complete all samples" >&2; exit 1;
}
python3 "$script_dir/summarize_pipecomm_latency_probe.py" \
  --reader "$output_dir/pipe_reader.jsonl" --writer "$output_dir/pipe_writer.jsonl" \
  --output "$output_dir/pipecomm_summary.json"

printf 'completed probe output=%s source_slot=%s destination_slot=%s pipe=%s bytes=%s samples=%s\n' \
  "$output_dir" "$source_slot" "$destination_slot" "$pipe_name" "$payload_bytes" "$count"
