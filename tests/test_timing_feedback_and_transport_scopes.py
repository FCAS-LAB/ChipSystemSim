"""Regression coverage for timing feedback and PipeComm scope accounting."""

import json
import tempfile
import unittest
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.collect_timing_metrics import collect_pipe_metrics
from scripts.validate_ns3_timing_feedback import (
    load_counts_by_round,
    phase_two_directories,
    validate_round,
)


class TransportScopeMetricsTest(unittest.TestCase):
    """Do not collapse logical-worker and physical-host communication scopes."""

    def test_records_are_counted_in_the_correct_scope(self) -> None:
        records = (
            {
                "operation": "R",
                "bytes": 8,
                "source_slot": 0,
                "peer_slot": 0,
                "transport_scope": "same_logical_worker",
                "started_unix_ns": 0,
                "finished_unix_ns": 10,
                "elapsed_ns": 10,
                "synchronization_wait_ns": 10,
            },
            {
                "operation": "R",
                "bytes": 16,
                "source_slot": 0,
                "peer_slot": 1,
                "transport_scope": "cross_legosim_same_physical_host",
                "started_unix_ns": 5,
                "finished_unix_ns": 20,
                "elapsed_ns": 15,
                "synchronization_wait_ns": 15,
            },
            {
                "operation": "R",
                "bytes": 32,
                "source_slot": 1,
                "peer_slot": 2,
                "transport_scope": "cross_physical_host",
                "started_unix_ns": 30,
                "finished_unix_ns": 40,
                "elapsed_ns": 10,
                "synchronization_wait_ns": 10,
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transport.log"
            path.write_text(
                "".join("pipe-metric: " + json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            values = collect_pipe_metrics(path)

        self.assertEqual(values["same_logical_worker_records"], 1)
        self.assertEqual(values["cross_legosim_records"], 2)
        self.assertEqual(values["cross_physical_host_records"], 1)
        self.assertEqual(values["cross_legosim_bytes"], 48)
        self.assertEqual(values["cross_physical_host_bytes"], 32)
        self.assertEqual(values["all_sync_wall_union_ns"], 30)
        self.assertEqual(values["cross_legosim_sync_wall_union_ns"], 25)
        self.assertEqual(values["cross_physical_host_sync_wall_union_ns"], 10)


class TimingFeedbackValidationTest(unittest.TestCase):
    """Require a complete ns-3 artifact before accepting timing feedback."""

    def test_phase_two_records_match_and_next_round_loads_delay_info(self) -> None:
        metrics_header = (
            "trace_id,src_node,dst_node,model_dst_node,descriptor,special,flits,"
            "payload_bytes,src_cycle,dst_cycle,forward_tx_finish_cycle,"
            "forward_arrival_cycle,source_sync_advance_cycles,"
            "destination_network_delay_cycles,destination_sync_block_cycles,"
            "ack_tx_finish_cycle,ack_arrival_cycle\n"
        )
        metrics_rows = (
            "0,0,1,1,0,0,1,32,0,0,1,2,1,2,2,,\n"
            "1,1,2,2,0,0,1,32,2,0,3,4,1,2,4,,\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phase_two = root / "proc_r1_p2_t0"
            phase_two.mkdir()
            (phase_two / "phase2_input_bench.txt").write_text(
                "0 0 0 1 1 0\n2 0 1 2 1 0\n", encoding="utf-8"
            )
            (phase_two / "phase2_delayInfo.txt").write_text(
                "0 0 1 0 2 1 2\n2 1 2 0 2 1 2\n", encoding="utf-8"
            )
            (phase_two / "phase2_metrics.csv").write_text(
                metrics_header + metrics_rows, encoding="utf-8"
            )
            (phase_two / "phase2_summary.json").write_text(
                json.dumps({"phase2_backend": "ns-3"}), encoding="utf-8"
            )
            coordinator_log = root / "coordinator.log"
            coordinator_log.write_text(
                "**** Round 1 Phase 1 ****\nLoad 0 delay records.\n"
                "**** Round 2 Phase 1 ****\nLoad 2 delay records.\n",
                encoding="utf-8",
            )

            found = phase_two_directories(root)
            self.assertEqual([(round_number, thread_number) for round_number, thread_number, _ in found], [(1, 0)])
            values = validate_round(1, found[0][2])
            loads = load_counts_by_round(coordinator_log)

        self.assertEqual(values, {"bench_records": 2, "delay_records": 2, "metrics_records": 2})
        self.assertEqual(loads, {1: 0, 2: 2})


if __name__ == "__main__":
    unittest.main()
