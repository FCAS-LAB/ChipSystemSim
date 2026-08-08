#!/usr/bin/env python3
"""Route GPGPU-Sim PipeComm payloads through the optional remote backend.

LEGOSim's GPGPU-Sim patch originally writes payload bytes straight to a
named FIFO.  That works when both chiplets share a filesystem, but bypasses
the SimBricks BaseIf gateway once GPU processes are placed in Docker workers.
This small, idempotent source transformation preserves the original FIFO
path when no gateway is configured and selects the remote backend otherwise.
"""

from __future__ import print_function

import sys


def replace_once(source, old, new, description):
    """Replace one known upstream fragment, accepting an already-patched file."""
    if new in source:
        return source
    if old not in source:
        raise RuntimeError("unexpected GPGPU-Sim {} fragment".format(description))
    return source.replace(old, new, 1)


def patch_source(source):
    include_anchor = '#include "pipe_comm.h"\n'
    include = include_anchor + '#include "remote_pipe_comm.h"\n'
    if '#include "remote_pipe_comm.h"' not in source:
        if include_anchor not in source:
            raise RuntimeError("cannot locate GPGPU-Sim PipeComm include")
        source = source.replace(include_anchor, include, 1)

    source = replace_once(
        source,
        '    global_pipe_comm.write_data(fileName.c_str(), interdata, __nbyte);',
        '''    int pipe_result = 0;
    if (InterChiplet::remotePipeEnabled()) {
        pipe_result = InterChiplet::remotePipeWrite(fileName.c_str(), interdata, __nbyte);
    } else {
        pipe_result = global_pipe_comm.write_data(fileName.c_str(), interdata, __nbyte);
    }
    delete[] interdata;
    if (pipe_result != __nbyte) {
        std::cerr << "GPGPU-Sim PipeComm write failed for " << fileName << std::endl;
        return cudaErrorUnknown;
    }''',
        "sendMessage write")
    source = replace_once(
        source,
        '    global_pipe_comm.read_data(fileName.c_str(), interdata, __nbyte);',
        '''    int pipe_result = 0;
    if (InterChiplet::remotePipeEnabled()) {
        pipe_result = InterChiplet::remotePipeRead(fileName.c_str(), interdata, __nbyte);
    } else {
        pipe_result = global_pipe_comm.read_data(fileName.c_str(), interdata, __nbyte);
    }
    if (pipe_result != __nbyte) {
        delete[] interdata;
        std::cerr << "GPGPU-Sim PipeComm read failed for " << fileName << std::endl;
        return cudaErrorUnknown;
    }''',
        "receiveMessage read")
    # ``receiveMessage`` allocates an array. The original LEGOSim patch used
    # scalar ``delete`` at the end of the function, which is undefined
    # behaviour and can terminate a GPU simlet after its first successful
    # PipeComm receive. Keep this transformation independent of the remote
    # transport replacement so it also repairs already-overlaid sources.
    source = replace_once(
        source,
        '    delete interdata;\n\n    return cudaSuccess;',
        '    delete[] interdata;\n\n    return cudaSuccess;',
        "receiveMessage array cleanup")
    # CUDA's triple-chevron launch stub calls this ABI entry point and checks
    # its unsigned result. The LEGOSim snapshot invoked the internal helper
    # but fell off a non-void function. On optimized builds that undefined
    # return value can corrupt the launch path immediately after a successful
    # PipeComm receive. Return the helper's CUDA status explicitly.
    source = replace_once(
        source,
        '    cudaConfigureCallInternal(gridDim, blockDim, sharedMem, stream);\n}\n\ncudaError_t CUDARTAPI __cudaPopCallConfiguration',
        '    return cudaConfigureCallInternal(gridDim, blockDim, sharedMem, stream);\n}\n\ncudaError_t CUDARTAPI __cudaPopCallConfiguration',
        "__cudaPushCallConfiguration return")
    return source


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_gpgpusim_remote_pipe.py CUDA_RUNTIME_API_CC")
    source_path = sys.argv[1]
    with open(source_path, "r") as source_file:
        source = source_file.read()
    patched = patch_source(source)
    with open(source_path, "w") as source_file:
        source_file.write(patched)


if __name__ == "__main__":
    main()
