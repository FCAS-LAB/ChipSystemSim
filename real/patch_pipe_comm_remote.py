#!/usr/bin/env python3
"""Install the optional remote payload backend into LEGOSim PipeComm."""

from __future__ import print_function

import shutil
import sys


def replace_once(source, old, new, description):
    if new in source:
        return source
    if old not in source:
        raise RuntimeError("unexpected PipeComm {} fragment".format(description))
    return source.replace(old, new, 1)


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: patch_pipe_comm_remote.py PIPE_COMM_H REMOTE_PIPE_COMM_H OUTPUT_HEADER")
    pipe_path, remote_path, output_path = sys.argv[1:]
    shutil.copyfile(remote_path, output_path)
    with open(pipe_path, "r") as source_file:
        source = source_file.read()
    include_anchor = '#include "sync_protocol.h"\n'
    include = include_anchor + '#include "remote_pipe_comm.h"\n'
    if '#include "remote_pipe_comm.h"' not in source:
        if include_anchor not in source:
            raise RuntimeError("cannot locate PipeComm include insertion point")
        source = source.replace(include_anchor, include, 1)
    source = replace_once(
        source,
        '    int read_data(const char *file_name, void *buf, int nbyte) {',
        '''    int read_data(const char *file_name, void *buf, int nbyte) {
        if (remotePipeEnabled()) return remotePipeRead(file_name, buf, nbyte);''',
        "read method")
    source = replace_once(
        source,
        '    int write_data(const char *file_name, void *buf, int nbyte) {',
        '''    int write_data(const char *file_name, void *buf, int nbyte) {
        if (remotePipeEnabled()) return remotePipeWrite(file_name, buf, nbyte);''',
        "write method")
    with open(pipe_path, "w") as source_file:
        source_file.write(source)


if __name__ == "__main__":
    main()
