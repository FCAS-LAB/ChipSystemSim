#!/usr/bin/env python3
"""Patch LEGOSim's child-output loop to drain stdout and stderr independently."""
from __future__ import annotations

from pathlib import Path


target = Path("/opt/legosim/interchiplet/srcs/interchiplet.cpp")
source = target.read_text(encoding="utf-8")
old = """                int res = read({fd}, pipe_buf, PIPE_BUF_SIZE);
                if (res <= 0) break;
                pipe_buf[res] = '\\0';"""

for descriptor in ("stdout_fd", "stderr_fd"):
    expected = old.format(fd=descriptor)
    replacement = """                int res = read({fd}, pipe_buf, PIPE_BUF_SIZE);
                if (res < 0) {{
                    perror(\"read\");
                    break;
                }}
                if (res == 0) {{
                    // One stream may close before the other. Do not abandon
                    // buffered stderr/stdout from a failing child process.
                    close({fd});
                    fd_list[{index}].fd = -1;
                    fd_list[{index}].events = 0;
                    continue;
                }}
                pipe_buf[res] = '\\0';""".format(fd=descriptor, index=0 if descriptor == "stdout_fd" else 1)
    if expected not in source:
        raise RuntimeError(f"expected child pipe read block not found for {descriptor}")
    source = source.replace(expected, replacement, 1)

target.write_text(source, encoding="utf-8")
