import os
import tempfile
import unittest
from unittest.mock import patch

import sys
from types import ModuleType
from unittest.mock import MagicMock

fastmcp = ModuleType("fastmcp")
fastmcp.FastMCP = lambda name: MagicMock()
sys.modules.setdefault("fastmcp", fastmcp)
import mcp_test_runner


class ModuleTargetValidationTests(unittest.TestCase):
    def test_empty_path_returns_existing_error_shape(self):
        self.assertEqual(
            mcp_test_runner._validate_module_target(""),
            {"status": "error", "message": "Module path must be provided."},
        )

    def test_missing_path_returns_existing_error_shape(self):
        with patch.object(mcp_test_runner.os.path, "exists", return_value=False):
            self.assertEqual(
                mcp_test_runner._validate_module_target("missing.py"),
                {"status": "error", "message": "Module file not found at missing.py"},
            )

    def test_existing_path_has_no_error(self):
        with patch.object(mcp_test_runner.os.path, "exists", return_value=True):
            self.assertIsNone(mcp_test_runner._validate_module_target("present.py"))


if __name__ == "__main__":
    unittest.main()
