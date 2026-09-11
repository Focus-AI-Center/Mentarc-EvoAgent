"""Regression checks for release snapshot disclosure detection."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_check import findings, history_findings


class ReleaseCheckTests(unittest.TestCase):
    def test_private_network_ranges(self):
        for octets in [(10, 1, 2, 3), (172, 16, 0, 1), (172, 31, 255, 1), (192, 168, 1, 1)]:
            with self.subTest(octets=octets):
                address = ".".join(map(str, octets))
                self.assertTrue(findings("config.py", address.encode()))

    def test_public_and_local_development_addresses(self):
        for address in ["https://example.com/api", "127.0.0.1", "0.0.0.0", "172.32.0.1"]:
            self.assertEqual(findings("config.py", address.encode()), [])

    def test_secret_files_and_values(self):
        for name in [".env", ".env.production", "deploy/.env.backup", "client.pem"]:
            self.assertTrue(findings(name, b""))
        self.assertTrue(findings("config.py", ("sk-" + "a" * 32).encode()))
        self.assertEqual(findings(".env.example", b"OPENAI_API_KEY=<your-api-key>"), [])

    def test_corporate_domains_but_preserve_formal_attribution(self):
        for domain in ["focus" + "china.com", "focus" + "aim.com", "ve" + "mic.com"]:
            self.assertTrue(findings("metadata.txt", domain.encode()))
        self.assertEqual(findings("NOTICE", b"Copyright 2026 Focus Technology Co., Ltd."), [])


class HistoryCheckTests(unittest.TestCase):
    def test_deleted_file_and_old_author_still_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

            git("init", "--quiet")
            git("config", "user.name", "Test Author")
            git("config", "user.email", "author@example.com")
            (root / "README.md").write_text("Public source")
            git("add", ".")
            git("commit", "--quiet", "-m", "Initial source")
            self.assertEqual(history_findings(root), (1, []))

            domain = "focus" + "china.com"
            (root / "old-config.txt").write_text("contact@" + domain)
            git("add", ".")
            git("-c", "user.email=author@" + domain, "commit", "--quiet", "-m", "Add config")
            git("rm", "old-config.txt")
            git("commit", "--quiet", "-m", "Remove config")
            count, errors = history_findings(root)
            self.assertEqual(count, 3)
            self.assertTrue(any("old-config.txt" in error for error in errors))
            self.assertTrue(any(error.startswith("commit/") for error in errors))


if __name__ == "__main__":
    unittest.main()
