import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_tgit_review as review


class BulkReviewReportTests(unittest.TestCase):
    def test_reports_all_replies_for_repo_without_mutating_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            series_root = root / "series"
            series_root.mkdir()
            for sid, owner in (("first", str(repo)), ("other-repo", str(root))):
                directory = series_root / sid
                directory.mkdir()
                (directory / "manifest.json").write_text(json.dumps({"series_id": sid, "repo_path": owner, "acknowledged_review_ids": []}))
            replies = [
                {"series_id": "first", "review_id": "r1", "patch_message_id": "p1", "acknowledged": False},
                {"series_id": "first", "review_id": "r2", "patch_message_id": "p2", "acknowledged": True},
                {"series_id": "other-repo", "review_id": "r3", "acknowledged": False},
            ]
            with patch.object(review, "SERIES_ROOT", series_root), patch.object(review, "_resolve_repo", return_value=(str(repo), None)), patch.object(review, "_reviews", return_value=replies):
                result = review.get_review_replies(str(repo))
                filtered = review.get_review_replies(str(repo), unacknowledged_only=True)
            self.assertEqual(result["review_count"], 2)
            self.assertEqual(result["unacknowledged_count"], 1)
            self.assertEqual([item["review_id"] for item in result["reviews"]], ["r1", "r2"])
            self.assertEqual([item["review_id"] for item in filtered["reviews"]], ["r1"])
            manifest = json.loads((series_root / "first" / "manifest.json").read_text())
            self.assertEqual(manifest["acknowledged_review_ids"], [])


if __name__ == "__main__":
    unittest.main()
