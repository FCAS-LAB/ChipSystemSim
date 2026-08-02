#!/usr/bin/env bash
set -euo pipefail
export SIMULATOR_ROOT=/opt/legosim
export CUDA_INSTALL_PATH="${CUDA_INSTALL_PATH:-/usr/local/cuda}"
set +u
source "$SIMULATOR_ROOT/gpgpu-sim/setup_environment"
set -u
if [[ ! -x "$SIMULATOR_ROOT/interchiplet/bin/interchiplet" ]]; then
  echo "LEGOSim is not built. Run legosim-build-native during image build or explicitly before this entrypoint." >&2
  exit 64
fi
exec "$SIMULATOR_ROOT/interchiplet/bin/interchiplet" "$@"
