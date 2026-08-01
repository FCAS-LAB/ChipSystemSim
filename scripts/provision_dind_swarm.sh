#!/usr/bin/env bash
# Create one bounded, single-host Docker-in-Docker Swarm experiment.
#
# The outer Docker Engine enforces the *total* CPU and memory budget by
# dividing it evenly across the requested nested nodes.  netem is configured
# only on each DinD node's external interface, so traffic within one logical
# node remains local while overlay traffic to another logical node is delayed.
set -euo pipefail

nodes=0
image=""
total_cpus=8
total_memory="32g"
delay="1ms"
rate="10gbit"
dind_image="docker:27-dind"
prefix="chipsystemsim-dind"
network="chipsystemsim-dind-net"
cleanup=false

usage() {
  cat <<'EOF'
Usage: provision_dind_swarm.sh --nodes N --image IMAGE [options]

Options:
  --total-cpus N       Fixed aggregate CPU quota (default: 8).
  --total-memory SIZE  Fixed aggregate memory quota (default: 32g).
  --delay DURATION     One-way netem delay per DinD external interface (default: 1ms).
  --rate RATE          Netem link rate (default: 10gbit).
  --dind-image IMAGE   DinD image, useful with an internal registry/mirror.
  --prefix NAME        Container-name prefix (default: chipsystemsim-dind).
  --cleanup            Remove a previously created DinD experiment for --prefix.
EOF
}

while (($#)); do
  case "$1" in
    --nodes) nodes="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --total-cpus) total_cpus="$2"; shift 2 ;;
    --total-memory) total_memory="$2"; shift 2 ;;
    --delay) delay="$2"; shift 2 ;;
    --rate) rate="$2"; shift 2 ;;
    --dind-image) dind_image="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --cleanup) cleanup=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$nodes" =~ ^(1|2|4|8)$ ]] || { echo "--nodes must be 1, 2, 4, or 8" >&2; exit 2; }
if ! $cleanup; then
  [[ -n "$image" ]] || { echo "--image is required" >&2; exit 2; }
  docker image inspect "$image" >/dev/null
fi

remove_experiment() {
  docker ps -aq --filter "name=^/${prefix}-" | xargs -r docker rm -f >/dev/null
  docker network rm "$network" >/dev/null 2>&1 || true
}

if $cleanup; then
  remove_experiment
  exit 0
fi

remove_experiment
docker network create --driver bridge --subnet 172.31.240.0/24 "$network" >/dev/null

# Docker accepts fractional CPU quotas. All requested points divide 8 exactly.
per_node_cpus=$(awk -v total="$total_cpus" -v count="$nodes" 'BEGIN { printf "%.3f", total / count }')
memory_bytes=$(python3 - "$total_memory" <<'PY'
import re
import sys

value = sys.argv[1].strip().lower()
match = re.fullmatch(r"(\d+)([kmgt]i?b?|)", value)
if not match:
    raise SystemExit(f"unsupported memory size: {value}")
number, suffix = match.groups()
power = {"": 0, "k": 1, "kb": 1, "kib": 1, "m": 2, "mb": 2, "mib": 2,
         "g": 3, "gb": 3, "gib": 3, "t": 4, "tb": 4, "tib": 4}[suffix]
print(int(number) * (1024 ** power))
PY
)
per_node_memory=$((memory_bytes / nodes))

wait_docker() {
  local container="$1"
  for _ in $(seq 1 90); do
    if docker exec "$container" docker info >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "nested Docker daemon did not become ready: $container" >&2
  return 1
}

for slot in $(seq 0 $((nodes - 1))); do
  container="${prefix}-${slot}"
  address="172.31.240.$((10 + slot))"
  docker run -d --privileged --name "$container" --hostname "$container" \
    --network "$network" --ip "$address" --cpus "$per_node_cpus" --memory "$per_node_memory" \
    "$dind_image" --tls=false >/dev/null
  wait_docker "$container"
  # tc is absent from the minimal DinD image. It is installed inside the
  # disposable node, not on the host, and applies only to outbound overlay traffic.
  docker exec "$container" sh -ec "apk add --no-cache iproute2-tc >/dev/null; tc qdisc replace dev eth0 root netem delay $delay rate $rate"
done

manager="${prefix}-0"
docker exec "$manager" docker swarm init --advertise-addr 172.31.240.10 >/dev/null
token=$(docker exec "$manager" docker swarm join-token -q worker)
for slot in $(seq 1 $((nodes - 1))); do
  docker exec "${prefix}-${slot}" docker swarm join --token "$token" 172.31.240.10:2377 >/dev/null
done

for _ in $(seq 1 60); do
  ready=$(docker exec "$manager" docker node ls --format '{{.Status}}' | grep -cx Ready || true)
  [[ "$ready" -eq "$nodes" ]] && break
  sleep 1
done
[[ "${ready:-0}" -eq "$nodes" ]] || { docker exec "$manager" docker node ls >&2; exit 1; }
for slot in $(seq 0 $((nodes - 1))); do
  docker exec "$manager" docker node update --label-add "chipsystemsim.node.${slot}=true" "${prefix}-${slot}" >/dev/null
done

# Every nested Docker Engine has an independent image store. Stream the exact
# host image to each one so no registry, tag ambiguity, or external pull is involved.
for slot in $(seq 0 $((nodes - 1))); do
  docker save "$image" | docker exec -i "${prefix}-${slot}" docker load >/dev/null
done

echo "manager_container=$manager"
echo "nodes=$nodes per_node_cpus=$per_node_cpus per_node_memory_bytes=$per_node_memory"
echo "netem_one_way_delay=$delay netem_rate=$rate"
docker exec "$manager" docker node ls
