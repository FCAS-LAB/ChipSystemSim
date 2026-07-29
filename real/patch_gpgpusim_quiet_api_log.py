#!/usr/bin/env python3
"""Disable GPGPU-Sim's unconditional CUDA API trace flood.

The simulator prints every CUDA API call even in a production benchmark run.
For the BFS workload this fills an InterChiplet child log to roughly 64 MiB
and then causes libc output-path failure. The function is diagnostic-only;
making it a no-op does not affect simulated instructions, timing, or memory.
"""
from __future__ import annotations

from pathlib import Path


SOURCE = Path("/opt/legosim/gpgpu-sim/libcuda/cuda_runtime_api.cc")
OLD = '''void announce_call(const char *func) {
    printf("\\n\\nGPGPU-Sim PTX: CUDA API function \\"%s\\" has been called.\\n", func);
    fflush(stdout);
}
'''
NEW = '''void announce_call(const char *func) {
    // API call tracing is diagnostic-only. Keeping stdout quiet prevents a
    // simulator child from exhausting InterChiplet's per-process log path.
    (void)func;
}
'''


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    if NEW in source:
        return
    if OLD not in source:
        raise RuntimeError(f"unexpected GPGPU-Sim announce_call definition: {SOURCE}")
    SOURCE.write_text(source.replace(OLD, NEW), encoding="utf-8")


if __name__ == "__main__":
    main()
