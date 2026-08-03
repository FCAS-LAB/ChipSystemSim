#!/usr/bin/env bash
# Warm up and measure the 1/2/4-node MLP-DP matrix with fixed CPU pinning.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out="${1:?usage: $0 OUTPUT_ROOT IMAGE_ARCHIVE}"
archive="${2:?usage: $0 OUTPUT_ROOT IMAGE_ARCHIVE}"
image="chipsystemsim:mlp-dp-tree-v5"
prefix="chipsystemsim-pinned-124"
[[ ! -e "$out" && -r "$archive" ]] || exit 2
mkdir -p "$out/config" "$out/dind-storage"
cleanup() { "$root/scripts/provision_dind_swarm.sh" --nodes 4 --prefix "$prefix" --dind-data-root "$out/dind-storage" --cleanup || true; }
trap cleanup EXIT

python3 "$root/scripts/generate_mlp_dp_matrix.py" --output-root "$out/config" --image "$image" \
  --nodes 1 2 4 --iterations 3 --global-samples 32768 --ranks-per-node 1
"$root/scripts/provision_dind_swarm.sh" --nodes 4 --dind-image chipsystemsim:dind-netem --image "$image" \
  --image-archive "$archive" --total-cpus 64 --total-memory 128g --delay 1ms --rate none \
  --image-load-jobs 4 --dind-data-root "$out/dind-storage" --prefix "$prefix"

configure() {
  local n="$1" slot cpus memory cpuset
  cpus=$((64 / n)); memory=$((128 * 1024 * 1024 * 1024 / n))
  for slot in 0 1 2 3; do
    if ((slot < n)); then
      cpuset="$((slot * cpus))-$(((slot + 1) * cpus - 1))"
      docker update --cpus "$cpus" --memory "$memory" --memory-swap "$memory" --cpuset-cpus "$cpuset" "${prefix}-${slot}" >/dev/null
    else
      docker update --cpus 1 --memory 4g --memory-swap 4g --cpuset-cpus "$((64 + slot))" "${prefix}-${slot}" >/dev/null
    fi
  done
}
run_point() {
  local n="$1" name="$2" dest="$3"
  "$root/scripts/run_dind_mlp_dp.sh" --nodes "$n" --prefix "$prefix" \
    --config-dir "$out/config/mlp-dp-nodes${n}" --stack "mlp_pinned_${name}" \
    --output-dir "$dest" --timeout-seconds 1600
}

# One unreported warm-up per node count fills page cache and starts each toolchain.
for n in 1 2 4; do configure "$n"; run_point "$n" "warm${n}" "$out/warmup/nodes${n}"; done

for n in 1 2 4; do
  configure "$n"
  for r in 1 2 3; do run_point "$n" "n${n}_r${r}" "$out/measurements/nodes${n}/run${r}"; done
done

python3 - "$out" <<'PY'
import csv, statistics, sys
from pathlib import Path
root = Path(sys.argv[1]); fields = ['nodes','repetitions','median_total_simulation_seconds','mean_total_simulation_seconds','stdev_total_simulation_seconds','mean_cross_node_sync_wall_seconds','mean_cross_node_sync_overhead_percent']
with (root/'summary_statistics.csv').open('w', newline='', encoding='utf-8') as f:
 w=csv.DictWriter(f, fieldnames=fields); w.writeheader()
 for n in (1,2,4):
  rows=[]
  for r in (1,2,3):
   d=root/'measurements'/f'nodes{n}'/f'run{r}'
   timing=dict(x.strip().split('=',1) for x in (d/'coordinator_timing.txt').read_text().splitlines())
   metrics=dict(x.strip().split('=',1) for x in (d/'metrics.txt').read_text().splitlines())
   # Python's ISO parser accepts at most microsecond precision; Docker emits
   # nanoseconds, so retain the first six fractional digits consistently.
   parse = lambda value: __import__('datetime').datetime.fromisoformat(value[:26] + '+00:00')
   seconds=(parse(timing['finish'])-parse(timing['start'])).total_seconds()
   sync=float(metrics['cross_sync_wall_union_ns'])/1e9
   rows.append((seconds,sync))
  totals=[x[0] for x in rows]
  w.writerow({'nodes':n,'repetitions':3,'median_total_simulation_seconds':statistics.median(totals),'mean_total_simulation_seconds':statistics.fmean(totals),'stdev_total_simulation_seconds':statistics.stdev(totals),'mean_cross_node_sync_wall_seconds':statistics.fmean(x[1] for x in rows),'mean_cross_node_sync_overhead_percent':statistics.fmean(x[1]/x[0]*100 for x in rows)})
PY
echo "PINNED_REPETITIONS_SUCCESS=$out"
