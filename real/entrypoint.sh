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
exec "$SIMULATOR_ROOT/interchiplet/bin/interchiplet" "$@"
