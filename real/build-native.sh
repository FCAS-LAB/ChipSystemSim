#!/usr/bin/env bash
# Build the unmodified upstream LEGOSim components required by the native runs.
# This script deliberately fails on a patch/build error: a partial build must
# never be mistaken for a usable simulation image.
set -euo pipefail

export SIMULATOR_ROOT=/opt/legosim
# Ubuntu's nvidia-cuda-toolkit installs nvcc and headers under /usr. The
# upstream GPGPU-Sim setup script dereferences this variable, so define it
# before sourcing while preserving an explicitly supplied CUDA installation.
export CUDA_INSTALL_PATH="${CUDA_INSTALL_PATH:-/usr}"
cd "$SIMULATOR_ROOT"
initialise_snapshot_repository() {
  local directory="$1"
  git -C "$directory" init -q
  if ! git -C "$directory" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$directory" add -A
    git -C "$directory" -c user.name=offline-builder \
      -c user.email=offline-builder@localhost commit -qm "offline source snapshot"
  fi
}
# GPGPU-Sim's environment setup queries its repository revision. Offline
# source archives intentionally exclude .git, so initialise the disposable
# repository before sourcing that script.
initialise_snapshot_repository "$SIMULATOR_ROOT/gpgpu-sim"
# The upstream setup script reads several optional variables without defaults.
# Keep strict mode for this build, but source that legacy script with nounset
# temporarily disabled.
set +u
source "$SIMULATOR_ROOT/gpgpu-sim/setup_environment"
set -u

# The upstream helper patches five optional submodules. This MLP-DP image
# deliberately ships only Sniper, GPGPU-Sim, and PopNet; GEM5 and Scale-Sim
# are not part of the workload. Offline source snapshots omit nested .git
# metadata, so initialise disposable repositories solely for git apply.
for component_patch in \
    "snipersim snipersim.diff" \
    "gpgpu-sim gpgpu-sim.diff" \
    "popnet_chiplet popnet.diff"; do
  read -r component patch_name <<<"$component_patch"
  initialise_snapshot_repository "$SIMULATOR_ROOT/$component"
  git -C "$SIMULATOR_ROOT/$component" apply \
    "$SIMULATOR_ROOT/interchiplet/patch/$patch_name"
done

# The upstream GPU patch keeps payloads in a local named FIFO.  Install the
# same optional PipeComm backend used by the CPU overlay, then redirect only
# its payload read/write operations.  Synchronisation remains the untouched
# LEGOSim coordinator protocol.
cp /opt/chipsystemsim-distributed/remote_pipe_comm.h \
  "$SIMULATOR_ROOT/gpgpu-sim/libcuda/remote_pipe_comm.h"
python3 /opt/chipsystemsim-distributed/patch_gpgpusim_remote_pipe.py \
  "$SIMULATOR_ROOT/gpgpu-sim/libcuda/cuda_runtime_api.cc"

# PipeComm is shared by the Sniper recorder and several LEGOSim helpers.
# Redirect its payload methods after applying the upstream component patches
# so CPU operations use the same optional BaseIf gateway as the GPU runtime.
python3 /opt/chipsystemsim-distributed/patch_pipe_comm_remote.py \
  "$SIMULATOR_ROOT/interchiplet/includes/pipe_comm.h" \
  /opt/chipsystemsim-distributed/remote_pipe_comm.h \
  "$SIMULATOR_ROOT/interchiplet/includes/remote_pipe_comm.h"

# Sniper's Makefile downloads PinPlay when this shared library is absent.
# The target hosts used for the DinD study intentionally have no reliable
# outbound access to snipersim.org, so the reproducible source-preparation
# step places this exact upstream archive beside Sniper before `docker build`.
# Extracting it here satisfies the original Makefile dependency without
# modifying Sniper itself. Reject a missing archive rather than silently
# falling back to a network download.
readonly PINPLAY_ARCHIVE="${SIMULATOR_ROOT}/snipersim/pinplay-dcfg-3.11-pin-3.11-97998-g7ecce2dac-gcc-linux.tar.bz2"
readonly PINPLAY_LIBRARY="${SIMULATOR_ROOT}/snipersim/pin_kit/intel64/lib-ext/libpin3dwarf.so"
if [[ ! -f "$PINPLAY_LIBRARY" ]]; then
  [[ -f "$PINPLAY_ARCHIVE" ]] || {
    echo "missing required offline PinPlay archive: $PINPLAY_ARCHIVE" >&2
    exit 1
  }
  mkdir -p "${SIMULATOR_ROOT}/snipersim/pin_kit"
  tar -x -f "$PINPLAY_ARCHIVE" --auto-compress --strip-components 1 \
    -C "${SIMULATOR_ROOT}/snipersim/pin_kit"
fi
[[ -f "$PINPLAY_LIBRARY" ]] || {
  echo "offline PinPlay archive did not provide $PINPLAY_LIBRARY" >&2
  exit 1
}
rm -f "$PINPLAY_ARCHIVE"

# Keep Sniper's original dependency graph intact while satisfying the two
# remaining runtime archives from the offline source snapshot. Passing
# NO_MCPAT_DOWNLOAD=1 is not valid here: upstream still names `mcpat` as a
# prerequisite, but removes its rule under that flag.
readonly SNIPER_PYTHON_ARCHIVE="${SIMULATOR_ROOT}/snipersim/sniper-python27-intel64.tgz"
readonly SNIPER_PYTHON_LIBRARY="${SIMULATOR_ROOT}/snipersim/python_kit/intel64/lib/python2.7/lib-dynload/_sqlite3.so"
readonly MCPAT_ARCHIVE="${SIMULATOR_ROOT}/snipersim/mcpat-1.0.tgz"
readonly MCPAT_BINARY="${SIMULATOR_ROOT}/snipersim/mcpat/mcpat-1.0"
if [[ ! -f "$SNIPER_PYTHON_LIBRARY" ]]; then
  [[ -f "$SNIPER_PYTHON_ARCHIVE" ]] || {
    echo "missing required offline Sniper Python archive: $SNIPER_PYTHON_ARCHIVE" >&2
    exit 1
  }
  mkdir -p "${SIMULATOR_ROOT}/snipersim/python_kit/intel64"
  tar -xzf "$SNIPER_PYTHON_ARCHIVE" --strip-components 1 \
    -C "${SIMULATOR_ROOT}/snipersim/python_kit/intel64"
fi
if [[ ! -f "$MCPAT_BINARY" ]]; then
  [[ -f "$MCPAT_ARCHIVE" ]] || {
    echo "missing required offline McPAT archive: $MCPAT_ARCHIVE" >&2
    exit 1
  }
  mkdir -p "${SIMULATOR_ROOT}/snipersim/mcpat"
  tar -xzf "$MCPAT_ARCHIVE" -C "${SIMULATOR_ROOT}/snipersim/mcpat"
fi
[[ -f "$SNIPER_PYTHON_LIBRARY" && -f "$MCPAT_BINARY" ]] || {
  echo "offline Sniper runtime archives were incomplete" >&2
  exit 1
}
rm -f "$SNIPER_PYTHON_ARCHIVE" "$MCPAT_ARCHIVE"

cmake -S interchiplet -B interchiplet/build
cmake --build interchiplet/build --parallel
cmake -S popnet_chiplet -B popnet_chiplet/build
cmake --build popnet_chiplet/build --parallel
# All Sniper prerequisites above are injected from the offline snapshot.
make -C snipersim -j"$(nproc)"
make -C gpgpu-sim -j"$(nproc)"

# CUDA 11.5 cannot compile these CUDA sources through Ubuntu 22.04's GCC 11
# C++ standard library.  Keep the system compiler for non-CUDA components and
# inject GCC 10 into each upstream Makefile's NVCC variable explicitly. Those
# Makefiles assign NVCC themselves, so CUDAHOSTCXX alone is not sufficient.
if [[ -x /usr/bin/g++-10 ]]; then
  NVCC_WITH_HOST_COMPILER="nvcc -ccbin /usr/bin/g++-10"
else
  # The offline Ubuntu 18.04 build base provides GCC 7, which CUDA 11.3
  # supports directly. Keep the Ubuntu 22.04 workaround above when available.
  NVCC_WITH_HOST_COMPILER="nvcc -ccbin /usr/bin/g++"
fi

# The upstream MLP Makefile creates its json dependency inside a recipe but
# does not add its single-header directory to the compiler search path. The
# offline source-preparation step supplies this exact header-only dependency;
# validate it here and expose it without modifying upstream benchmark files.
readonly MLP_JSON_INCLUDE="${SIMULATOR_ROOT}/artifact/MLP/json/single_include"
[[ -f "$MLP_JSON_INCLUDE/nlohmann/json.hpp" ]] || {
  echo "missing required offline nlohmann/json header: $MLP_JSON_INCLUDE" >&2
  exit 1
}
CPLUS_INCLUDE_PATH="$MLP_JSON_INCLUDE${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}" \
  make -C artifact/MLP NVCC="$NVCC_WITH_HOST_COMPILER"

for benchmark in benchmark/resnet artifact/bfs_cuda; do
  make -C "$benchmark" NVCC="$NVCC_WITH_HOST_COMPILER" -j"$(nproc)"
done

make -C benchmark/dlrm_npu -j"$(nproc)"
