#!/usr/bin/env bash
# Import one locally built image into every existing DinD node without
# recreating the Swarm or changing its already-configured netem qdiscs.
set -euo pipefail

nodes=0
prefix=""
image=""
jobs=4

while (($#)); do
  case "$1" in
    --nodes) nodes="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    *) echo "Usage: $0 --nodes N --prefix PREFIX --image IMAGE [--jobs N]" >&2; exit 2 ;;
  esac
done

[[ "$nodes" =~ ^[1-9][0-9]*$ && "$jobs" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ -n "$prefix" && -n "$image" ]] || exit 2
docker image inspect "$image" >/dev/null
for slot in $(seq 0 $((nodes - 1))); do docker inspect "${prefix}-${slot}" >/dev/null; done

archive=$(mktemp -t "${prefix}-image.XXXXXX.tar")
trap 'rm -f "$archive"' EXIT
docker save --output "$archive" "$image"

pids=()
for slot in $(seq 0 $((nodes - 1))); do
  docker exec -i "${prefix}-${slot}" docker load < "$archive" >/dev/null &
  pids+=("$!")
  if ((${#pids[@]} >= jobs)); then
    for pid in "${pids[@]}"; do wait "$pid"; done
    pids=()
  fi
done
for pid in "${pids[@]}"; do wait "$pid"; done
echo "imported image=$image into nodes=$nodes prefix=$prefix"
