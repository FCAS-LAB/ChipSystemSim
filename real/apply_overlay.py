#!/usr/bin/env python3
"""Install the distributed PipeComm overlay into a pristine LEGOSim checkout."""
import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legosim-root", type=Path, default=Path("/opt/legosim"))
    parser.add_argument("--overlay-root", type=Path, default=Path("/opt/chipsystemsim-distributed"))
    arguments = parser.parse_args()
    include_dir = arguments.legosim_root / "interchiplet" / "includes"
    target = include_dir / "pipe_comm.h"
    overlay = arguments.overlay_root / "remote_pipe_comm.h"
    (include_dir / "remote_pipe_comm.h").write_text(overlay.read_text(encoding="utf-8"), encoding="utf-8")

    # The public MNSIM submodule lacks the private -Id1/-Id2 extension used
    # by the three upstream mnsim.cpp tasks. Install an auditable source-
    # calibrated task instead of retaining author-machine paths or silently
    # reading a missing result file.
    mnsim_task = arguments.overlay_root / "mnsim_compat.cpp"
    for relative_path in (
        "artifact/MLP/mnsim.cpp",
        "artifact/bfs_cuda/mnsim.cpp",
        "benchmark/resnet/mnsim.cpp",
    ):
        (arguments.legosim_root / relative_path).write_text(
            mnsim_task.read_text(encoding="utf-8"), encoding="utf-8"
        )

    # The checked-in MLP source still points at an author's renamed benchmark
    # directory. The actual input data is installed beside this binary.
    mlp_source_path = arguments.legosim_root / "artifact/MLP/mlp.cpp"
    mlp_source = mlp_source_path.read_text(encoding="utf-8")
    upstream_root = 'return std::string(env_p) + "/benchmark/MLP_NVL/";'
    image_root = 'return std::string(env_p) + "/artifact/MLP/";'
    if image_root not in mlp_source and upstream_root in mlp_source:
        mlp_source_path.write_text(mlp_source.replace(upstream_root, image_root), encoding="utf-8")

    # GPGPU-Sim's legacy generator assumes the Bison token enum has no
    # trailing comments. Ubuntu 22.04's Bison 3.8 emits comments such as
    # ``/* \"end of file\" */``; without stripping them, the generated C++
    # contains adjacent string literals and fails to compile. Keep the patch
    # narrow and reject an unexpected upstream Makefile instead of silently
    # editing a different revision.
    gpgpu_makefile = arguments.legosim_root / "gpgpu-sim" / "src" / "cuda-sim" / "Makefile"
    gpgpu_source = gpgpu_makefile.read_text(encoding="utf-8")
    legacy_pipeline = r"| sed 's/[=,]//g' | sed 's/\([_A-Z1-9]\+\)[ ]\+\([0-9]\+\)/\1 \1/'"
    fixed_pipeline = r"| sed 's/[=,]//g' | sed 's@/\*.*\*/@@' | sed 's/\([_A-Z1-9]\+\)[ ]\+\([0-9]\+\)/\1 \1/'"
    if fixed_pipeline not in gpgpu_source:
        if legacy_pipeline not in gpgpu_source:
            raise RuntimeError("upstream GPGPU-Sim token-generator layout did not match the expected revision")
        gpgpu_makefile.write_text(
            gpgpu_source.replace(legacy_pipeline, fixed_pipeline), encoding="utf-8"
        )

    source = target.read_text(encoding="utf-8")
    if "remotePipeEnabled" not in source:
        source = source.replace('#include "sync_protocol.h"', '#include "sync_protocol.h"\n#include "remote_pipe_comm.h"')
        source = source.replace(
            '    int read_data(const char *file_name, void *buf, int nbyte) {',
            '    int read_data(const char *file_name, void *buf, int nbyte) {\n'
            '        if (remotePipeEnabled()) return remotePipeRead(file_name, buf, nbyte);',
        )
        source = source.replace(
            '    int write_data(const char *file_name, void *buf, int nbyte) {',
            '    int write_data(const char *file_name, void *buf, int nbyte) {\n'
            '        if (remotePipeEnabled()) return remotePipeWrite(file_name, buf, nbyte);',
        )
        if "remotePipeEnabled" not in source:
            raise RuntimeError("upstream PipeComm layout did not match the expected revision")
        target.write_text(source, encoding="utf-8")

    # Two native workloads invoke MNSIM through an absolute path from the
    # original author's machine and install Python packages at every run. The
    # image supplies those packages and its checked-out MNSIM tree, so retain
    # the same MNSIM_Chiplet.py calculation while making invocation offline and
    # reproducible. Restrict the replacement to the exact pinned-source line.
    mnsim_command = (
        'system("cd /home/qc/test_simulator/Chiplet_Heterogeneous_newVersion/'
        'MNSIMChiplet; pip install torch torchvision ; python3 '
        'MNSIM_Chiplet.py -ID1 0 -ID2 2");'
    )
    reproducible_command = (
        'system("cd /opt/legosim/MNSIMChiplet; '
        'PYTHONPATH=/opt/legosim/MNSIMChiplet/MNSIM python3 '
        'MNSIM_Chiplet.py -ID1 0 -ID2 2");'
    )
    for relative_path in ("artifact/MLP/mnsim.cpp", "artifact/bfs_cuda/mnsim.cpp"):
        launcher = arguments.legosim_root / relative_path
        launcher_source = launcher.read_text(encoding="utf-8")
        if reproducible_command not in launcher_source and mnsim_command in launcher_source:
            launcher.write_text(
                launcher_source.replace(mnsim_command, reproducible_command), encoding="utf-8"
            )


if __name__ == "__main__":
    main()
