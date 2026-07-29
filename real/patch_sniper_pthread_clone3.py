#!/usr/bin/env python3
"""Avoid phantom Sniper worker channels for Docker-rejected clone3 calls."""
from pathlib import Path


source_path = Path("/opt/legosim/snipersim/sift/recorder/syscall_modeling.cc")
source = source_path.read_text(encoding="utf-8")
old = '''         case SYS_clone3_sniper:
         {
            if (args[0] && CLONE_THREAD)
            {
               struct clone_args_sniper* clone3_args = (struct clone_args_sniper*)args[0];
               ADDRINT tidptr = clone3_args->parent_tid;
               PIN_GetLock(&new_threadid_lock, threadid);
               tidptrs.push_back(tidptr);
               PIN_ReleaseLock(&new_threadid_lock);
               /* New thread */
               thread_data[threadid].output->NewThread();
            }
            else
            {
               /* New process */
               // Nothing to do there, handled in fork() -> to check SYS_clone3 is new
            }
            break;
         }'''
new = '''         case SYS_clone3_sniper:
         {
            // Docker's default seccomp profile may reject clone3. Glibc then
            // retries with SYS_clone, whose handler below opens the matching
            // SIFT channel. Do not emit NewThread for the failed attempt.
            break;
         }'''
if old not in source:
    raise RuntimeError("unexpected Sniper clone3 handler")
source_path.write_text(source.replace(old, new, 1), encoding="utf-8")
