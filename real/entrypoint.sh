#!/usr/bin/env bash
set -euo pipefail
export SIMULATOR_ROOT=/opt/legosim
# The image builds GPGPU-Sim against the distribution CUDA headers under /usr.
# Keep the runtime default identical; /usr/local/cuda is not installed in the
# minimal simulation image and makes every GPU worker terminate at startup.
export CUDA_INSTALL_PATH="${CUDA_INSTALL_PATH:-/usr}"
set +u
source "$SIMULATOR_ROOT/gpgpu-sim/setup_environment"
set -u
if [[ ! -x "$SIMULATOR_ROOT/interchiplet/bin/interchiplet" ]]; then
  echo "LEGOSim is not built. Run legosim-build-native during image build or explicitly before this entrypoint." >&2
  exit 64
fi

# A timing run must retain the Phase-1 trace and the Phase-2 feedback that
# drove the next LEGOSim round.  They are deliberately separate from
# PipeComm's functional payload path.  The generated Swarm stack mounts a
# dedicated named volume at /legosim-artifacts only for the coordinator.
artifact_base="${LEGOSIM_ARTIFACT_DIR:-}"
artifact_run_id="${LEGOSIM_ARTIFACT_RUN_ID:-}"
artifact_root=""
if [[ -n "$artifact_base" || -n "$artifact_run_id" ]]; then
  [[ "$artifact_base" == "/legosim-artifacts" ]] || {
    echo "LEGOSIM_ARTIFACT_DIR must be /legosim-artifacts" >&2
    exit 64
  }
  [[ "$artifact_run_id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
    echo "LEGOSIM_ARTIFACT_RUN_ID must be a safe single path component" >&2
    exit 64
  }
  artifact_root="$artifact_base/$artifact_run_id"
  # This is a validated per-run directory on the dedicated artifact volume;
  # clearing it avoids mixing a reused Swarm stack's old timing trace with the
  # current coordinator execution.
  rm -rf -- "$artifact_root"
  mkdir -p "$artifact_root"
fi

snapshot_timing_artifacts() {
  local status="$1"
  [[ -n "$artifact_root" ]] || return 0

  {
    printf 'exit_code=%s\n' "$status"
    printf 'working_directory=%s\n' "$PWD"
    printf 'command='
    printf '%q ' "$SIMULATOR_ROOT/interchiplet/bin/interchiplet" "$@"
    printf '\n'
  } > "$artifact_root/manifest.txt"

  # The two protocol files live in the coordinator work directory.  Their
  # names are hard-coded by upstream InterChiplet, independent of optional
  # YAML file-name fields.
  for file in bench.txt delayInfo.txt bridge-trace.log ns3_phase2_metrics.csv ns3_phase2_summary.json; do
    [[ -f "$file" ]] && cp -- "$file" "$artifact_root/$file"
  done
  # Each phase-two process has its own log (for example popnet.log or
  # ns3_phase2.log) in proc_r*_p2_t*. Preserve only process directories,
  # never the whole simulator tree.
  shopt -s nullglob
  local process_dir
  for process_dir in proc_r*_p[12]_t*; do
    [[ -d "$process_dir" ]] && cp -a -- "$process_dir" "$artifact_root/"
  done
}

if [[ -n "$artifact_root" ]]; then
  trap 'status=$?; snapshot_timing_artifacts "$status"; exit "$status"' EXIT
fi

"$SIMULATOR_ROOT/interchiplet/bin/interchiplet" "$@"
