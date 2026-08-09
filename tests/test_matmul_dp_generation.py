"""Regression tests for the native block-GEMM matrix generator."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_matmul_dp_dind_matrix.py"


class MatmulDpGenerationTest(unittest.TestCase):
    def test_all_scale_points_keep_global_work_and_two_ranks_per_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "generated"
            subprocess.run(
                [sys.executable, str(GENERATOR), "--output-root", str(output),
                 "--image", "example:matmul", "--nodes", "1", "2", "4", "8"],
                check=True,
            )
            for node_count in (1, 2, 4, 8):
                directory = output / f"matmul-dp-nodes{node_count}"
                placement = json.loads((directory / "placement.json").read_text())
                routing = json.loads((directory / "routing.json").read_text())
                workload = yaml.safe_load((directory / "workload.yml").read_text())
                ranks = node_count * 2
                self.assertEqual(placement["global_matrix"],
                                 {"rows": 480, "inner": 64, "columns": 64})
                self.assertEqual(placement["gpu_ranks"], ranks)
                self.assertEqual(len(workload["phase1"]), ranks + 1)
                gpu_entries = [item for item in placement["processes"]
                               if item["phase"] == "phase1" and item["role"] == "gpgpu-block-rank"]
                self.assertEqual([item["node_slot"] for item in gpu_entries],
                                 [rank // 2 for rank in range(ranks)])
                self.assertEqual(set(routing["worker_physical_slots"].values()), {0})
                self.assertIn("ns3_phase2_runner.py", " ".join(workload["phase2"][0]["args"]))

    def test_fixed_simlet_count_changes_only_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "fixed-components"
            subprocess.run(
                [sys.executable, str(GENERATOR), "--output-root", str(output),
                 "--image", "example:matmul", "--nodes", "1", "2", "4", "8",
                 "--gpu-ranks", "16"],
                check=True,
            )
            for node_count in (1, 2, 4, 8):
                placement = json.loads(
                    (output / f"matmul-dp-nodes{node_count}" / "placement.json").read_text()
                )
                gpu_entries = [item for item in placement["processes"]
                               if item.get("role") == "gpgpu-block-rank"]
                self.assertEqual(placement["gpu_ranks"], 16)
                self.assertEqual(len(gpu_entries), 16)
                self.assertEqual(
                    [item["node_slot"] for item in gpu_entries],
                    [rank // (16 // node_count) for rank in range(16)],
                )

    def test_paper_scale_32_simlet_matrix_keeps_15_rows_per_rank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "paper-scale"
            subprocess.run(
                [sys.executable, str(GENERATOR), "--output-root", str(output),
                 "--image", "example:matmul", "--nodes", "1", "2", "4", "8",
                 "--gpu-ranks", "32"],
                check=True,
            )
            for node_count in (1, 2, 4, 8):
                placement = json.loads(
                    (output / f"matmul-dp-nodes{node_count}" / "placement.json").read_text()
                )
                entries = [item for item in placement["processes"]
                           if item.get("role") == "gpgpu-block-rank"]
                self.assertEqual(len(entries), 32)
                self.assertNotIn([5, 5], [item["coordinates"] for item in entries])
                self.assertEqual(480 // placement["gpu_ranks"], 15)

    def test_local_counterfactual_changes_only_ns3_timing_arguments(self) -> None:
        """The baseline must retain the same workload and worker placement."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "local-baseline"
            subprocess.run(
                [sys.executable, str(GENERATOR), "--output-root", str(output),
                 "--image", "example:matmul", "--nodes", "4", "--gpu-ranks", "32",
                 "--ns3-localize-cross-worker-network"],
                check=True,
            )
            directory = output / "matmul-dp-nodes4"
            placement = json.loads((directory / "placement.json").read_text())
            workload = yaml.safe_load((directory / "workload.yml").read_text())
            phase2_args = workload["phase2"][0]["args"]

        self.assertEqual(placement["gpu_ranks"], 32)
        self.assertEqual(len(placement["processes"]), 34)
        self.assertIn("--worker-routing", phase2_args)
        self.assertIn("/run/config/routing.json", phase2_args)
        self.assertIn("--localize-cross-worker-network", phase2_args)


if __name__ == "__main__":
    unittest.main()
