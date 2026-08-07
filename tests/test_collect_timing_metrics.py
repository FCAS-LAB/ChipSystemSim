"""Regression tests for timing-metric aggregation boundaries."""

import tempfile
import unittest
from pathlib import Path
import sys

# Allow direct execution with ``python3 tests/test_collect_timing_metrics.py``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.collect_timing_metrics import collect_ns3_metrics


class CollectNs3MetricsTest(unittest.TestCase):
    """Keep normal READ/WRITE timing separate from control-operation timing."""

    def test_counts_total_and_normal_payloads_separately(self) -> None:
        header = (
            "trace_id,src_node,dst_node,model_dst_node,descriptor,special,flits,"
            "payload_bytes,src_cycle,dst_cycle,forward_tx_finish_cycle,"
            "forward_arrival_cycle,source_sync_advance_cycles,"
            "destination_network_delay_cycles,destination_sync_block_cycles,"
            "ack_tx_finish_cycle,ack_arrival_cycle\n"
        )
        normal = "0,0,1,1,0,0,2,64,0,0,1,2,1,2,2,,\n"
        special = "1,1,0,0,131072,1,1,32,0,0,1,2,1,2,2,3,4\n"
        with tempfile.TemporaryDirectory() as directory:
            metrics_path = Path(directory) / "ns3_phase2_metrics.csv"
            metrics_path.write_text(header + normal + special, encoding="utf-8")
            values = collect_ns3_metrics(metrics_path)

        self.assertEqual(values["ns3_records"], 2)
        self.assertEqual(values["ns3_normal_records"], 1)
        self.assertEqual(values["ns3_special_records"], 1)
        self.assertEqual(values["ns3_payload_bytes"], 96)
        self.assertEqual(values["ns3_normal_payload_bytes"], 64)
        self.assertEqual(values["ns3_normal_source_sync_advance_cycles"], 1)
        self.assertEqual(values["ns3_normal_destination_network_delay_cycles"], 2)
        self.assertEqual(values["ns3_normal_destination_sync_block_cycles"], 2)


if __name__ == "__main__":
    unittest.main()
