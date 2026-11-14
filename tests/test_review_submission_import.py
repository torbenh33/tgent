import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import mcp_tgit_review as review


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class SubmissionImportTests(unittest.TestCase):
    def test_import_creates_namespaced_ref_and_leaves_main_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "shared"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.invalid")
            (repo / "file.txt").write_text("base\n")
            git(repo, "add", "file.txt")
            git(repo, "commit", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "file.txt").write_text("submitted\n")
            git(repo, "commit", "-am", "submitted change")
            submitted = git(repo, "rev-parse", "HEAD")
            mbox = subprocess.check_output(["git", "-C", str(repo), "format-patch", "--stdout", "--cover-letter", f"{base}..{submitted}"])
            messages = review._parse_mbox(mbox)
            messages[0]["Message-ID"] = "<cover@example.invalid>"
            messages[1]["Message-ID"] = "<patch@example.invalid>"
            mbox = review._serialize_mbox(messages)
            git(repo, "reset", "--hard", base)
            main_before = git(repo, "rev-parse", "main")
            digest = hashlib.sha256(mbox).hexdigest()
            paths = review.ReviewPaths(root / "state", root / "state/series", root / "mail")
            result = review.import_patch_series(mbox, base=base, submitted_head=submitted, repo_path=str(repo), series_id="remote-agent-1", artifact_sha256=digest, paths=paths)
            self.assertEqual(result["status"], "ok", result)
            ref = f"refs/agent-submissions/remote-agent-1/revision-{digest}"
            imported = git(repo, "rev-parse", ref)
            self.assertEqual(git(repo, "cat-file", "-t", imported), "commit")
            self.assertEqual(git(repo, "rev-list", "--count", f"{base}..{ref}"), "1")
            self.assertNotEqual(imported, base)
            self.assertEqual(git(repo, "rev-parse", "main"), main_before)
            self.assertEqual(git(repo, "branch", "--show-current"), "main")
            self.assertEqual(sorted(p.name for p in root.iterdir() if p.is_dir()), ["mail", "shared", "state"])


if __name__ == "__main__":
    unittest.main()
