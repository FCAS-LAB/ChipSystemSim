#!/usr/bin/env python3
"""Add bounded PipeComm progress diagnostics to Sniper's recorder source."""
from pathlib import Path


source_path = Path("/opt/legosim/snipersim/sift/recorder/syscall_modeling.cc")
source = source_path.read_text(encoding="utf-8")
old_write = '''                  std::string fileName = InterChiplet::sendSync(srcX, srcY, dstX, dstY);
                  global_pipe_comm.write_data(fileName.c_str(), data, nbytes);
                  break;'''
new_write = '''                  std::string fileName = InterChiplet::sendSync(srcX, srcY, dstX, dstY);
                  fprintf(stderr, "recorder-pipe: write begin pipe=%s bytes=%d\\n", fileName.c_str(), nbytes);
                  int pipe_result = global_pipe_comm.write_data(fileName.c_str(), data, nbytes);
                  fprintf(stderr, "recorder-pipe: write end pipe=%s result=%d\\n", fileName.c_str(), pipe_result);
                  break;'''
old_read = '''                  std::string fileName = InterChiplet::receiveSync(srcX, srcY, dstX, dstY);
                  global_pipe_comm.read_data(fileName.c_str(), data, nbytes);
                  break;'''
new_read = '''                  std::string fileName = InterChiplet::receiveSync(srcX, srcY, dstX, dstY);
                  fprintf(stderr, "recorder-pipe: read begin pipe=%s bytes=%d\\n", fileName.c_str(), nbytes);
                  int pipe_result = global_pipe_comm.read_data(fileName.c_str(), data, nbytes);
                  fprintf(stderr, "recorder-pipe: read end pipe=%s result=%d\\n", fileName.c_str(), pipe_result);
                  break;'''

for old, new, name in ((old_write, new_write, "write"), (old_read, new_read, "read")):
    if new in source:
        continue
    if old not in source:
        raise RuntimeError(f"unexpected Sniper recorder {name} handler")
    source = source.replace(old, new, 1)

source_path.write_text(source, encoding="utf-8")
