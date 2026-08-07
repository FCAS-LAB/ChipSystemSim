"""Regression tests for MLP MNSIM source-layout handling."""

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from real import patch_mlp_mnsim_runtime as runtime_patch


class MnsimRuntimePatchTest(unittest.TestCase):
    """Preserved server images may already use the compatibility backend."""

    def test_prepatched_compatibility_source_is_accepted_unchanged(self) -> None:
        source_text = (
            "// Compatibility MNSIM task\n"
            "system(\"python3 /opt/chipsystemsim-distributed/mnsim_compat.py\");\n"
        )
        original_source = runtime_patch.SOURCE
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "mnsim.cpp"
                source.write_text(source_text, encoding="utf-8")
                runtime_patch.SOURCE = source
                runtime_patch.main()
                self.assertEqual(source.read_text(encoding="utf-8"), source_text)
        finally:
            runtime_patch.SOURCE = original_source


if __name__ == "__main__":
    unittest.main()
