#!/usr/bin/env python3
"""Patch Sniper's recorder with a safe clone3 handler and bounded diagnostics."""
from pathlib import Path

syscalls = Path("/opt/legosim/snipersim/sift/recorder/syscall_modeling.cc")
source = syscalls.read_text(encoding="utf-8")
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
            // Docker's default seccomp profile can reject clone3, after which
            // glibc retries with SYS_clone. Emitting NewThread here would make
            // the trace frontend wait for a child trace that clone3 never
            // creates. The successful SYS_clone path below owns NewThread.
            fprintf(stderr, "recorder-pthread: defer clone3 tid=%u\\n", static_cast<unsigned>(threadid));
            break;
         }'''
if old not in source:
    raise RuntimeError("unexpected clone3 handler")
syscalls.write_text(source.replace(old, new, 1), encoding="utf-8")

threads = Path("/opt/legosim/snipersim/sift/recorder/threads.cc")
source = threads.read_text(encoding="utf-8")
old = '''static VOID threadStart(THREADID threadid, CONTEXT *ctxt, INT32 flags, VOID *v)
{
   sift_assert(threadid < max_num_threads);'''
new = '''static VOID threadStart(THREADID threadid, CONTEXT *ctxt, INT32 flags, VOID *v)
{
   fprintf(stderr, "recorder-pthread: ThreadStart tid=%u capacity=%u\\n",
           static_cast<unsigned>(threadid), max_num_threads);
   sift_assert(threadid < max_num_threads);'''
if old not in source:
    raise RuntimeError("unexpected threadStart handler")
threads.write_text(source.replace(old, new, 1), encoding="utf-8")
