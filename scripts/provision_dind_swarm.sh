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
rate="none"
image_load_jobs=1
dind_image="docker:27-dind"
prefix="chipsystemsim-dind"
cleanup=false

usage() {
  cat <<'EOF'
Usage: provision_dind_swarm.sh --nodes N --image IMAGE [options]

Options:
  --total-cpus N       Fixed aggregate CPU quota (default: 8).
  --total-memory SIZE  Fixed aggregate memory quota (default: 32g).
  --delay DURATION     One-way netem delay per DinD external interface (default: 1ms).
  --rate RATE|none     Optional netem link-rate cap (default: none).
  --image-load-jobs N  Concurrent nested image imports (default: 1).
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
    --image-load-jobs) image_load_jobs="$2"; shift 2 ;;
    --dind-image) dind_image="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --cleanup) cleanup=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$nodes" =~ ^(1|2|4|8)$ ]] || { echo "--nodes must be 1, 2, 4, or 8" >&2; exit 2; }
[[ "$image_load_jobs" =~ ^[1-9][0-9]*$ ]] || { echo "--image-load-jobs must be a positive integer" >&2; exit 2; }
if ! $cleanup; then
  [[ -n "$image" ]] || { echo "--image is required" >&2; exit 2; }
  docker image inspect "$image" >/dev/null
fi

# Experiments with separate prefixes must not contend for one globally named
# bridge network. This also makes --cleanup precise: it removes only the
# containers and network belonging to the selected experiment.
network="${prefix}-net"

# Choose an unallocated private /24 for this outer bridge. A fixed subnet
# collided with preserved smoke-test networks, preventing otherwise isolated
# experiments from starting. The manager and workers derive their addresses
# from the selected subnet, so Swarm never relies on a host-visible NIC name.
subnet=""
address_prefix=""
for third_octet in $(seq 240 254); do
  candidate="172.31.${third_octet}.0/24"
  if ! docker network ls -q | xargs -r docker network inspect \
      --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' | grep -Fxq "$candidate"; then
    subnet="$candidate"
    address_prefix="172.31.${third_octet}"
    break
  fi
done
[[ -n "$subnet" ]] || { echo "no free DinD subnet in 172.31.240.0/20" >&2; exit 1; }

remove_experiment() {
  docker ps -aq --filter "name=^/${prefix}-" | xargs -r docker rm -f >/dev/null
  docker network rm "$network" >/dev/null 2>&1 || true
}

if $cleanup; then
  remove_experiment
  exit 0
fi

remove_experiment
docker network create --driver bridge --subnet "$subnet" "$network" >/dev/null

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

install_tc() {
  local container="$1"
  # The public Alpine mirror occasionally rejects one index request. DinD
  # nodes are disposable, so retry only this bootstrap dependency a bounded
  # number of times instead of weakening the experiment or continuing without
  # the requested netem impairment.
  for _ in $(seq 1 8); do
    if docker exec "$container" sh -ec \
        "apk add --no-cache iproute2-tc >/dev/null 2>&1 || apk add --no-cache iproute2 >/dev/null 2>&1"; then
      return 0
    fi
    sleep 3
  done
  echo "could not install tc in DinD node: $container" >&2
  return 1
}

for slot in $(seq 0 $((nodes - 1))); do
  container="${prefix}-${slot}"
  address="${address_prefix}.$((10 + slot))"
  docker run -d --privileged --name "$container" --hostname "$container" \
    --network "$network" --ip "$address" --cpus "$per_node_cpus" --memory "$per_node_memory" \
    "$dind_image" --tls=false >/dev/null
  wait_docker "$container"
  # tc is absent from the minimal DinD image. It is installed inside the
  # disposable node, not on the host, and applies only to outbound overlay traffic.
  install_tc "$container"
  if [[ "$rate" == "none" ]]; then
    docker exec "$container" tc qdisc replace dev eth0 root netem delay "$delay"
  else
    docker exec "$container" tc qdisc replace dev eth0 root netem delay "$delay" rate "$rate"
  fi
done

manager="${prefix}-0"
manager_address="${address_prefix}.10"
docker exec "$manager" docker swarm init --advertise-addr "$manager_address" >/dev/null
token=$(docker exec "$manager" docker swarm join-token -q worker)
for slot in $(seq 1 $((nodes - 1))); do
  docker exec "${prefix}-${slot}" docker swarm join --token "$token" "$manager_address:2377" >/dev/null
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

# Every nested Docker Engine has an independent image store. Create one local
# archive and fan it out to a bounded number of loaders. This avoids external
# pulls while allowing large 4/8-node experiments to use the available host
# I/O parallelism. The default remains sequential for reproducibility on small
# hosts.
image_archive=$(mktemp -t "${prefix}-image.XXXXXX.tar")
trap 'rm -f "$image_archive"' EXIT
docker save --output "$image_archive" "$image"

load_pids=()
for slot in $(seq 0 $((nodes - 1))); do
  docker exec -i "${prefix}-${slot}" docker load <"$image_archive" >/dev/null &
  load_pids+=("$!")
  if ((${#load_pids[@]} >= image_load_jobs)); then
    for pid in "${load_pids[@]}"; do wait "$pid"; done
    load_pids=()
  fi
done
for pid in "${load_pids[@]}"; do wait "$pid"; done

echo "manager_container=$manager"
echo "outer_subnet=$subnet"
echo "nodes=$nodes per_node_cpus=$per_node_cpus per_node_memory_bytes=$per_node_memory"
echo "netem_one_way_delay=$delay netem_rate=$rate"
docker exec "$manager" docker node ls
