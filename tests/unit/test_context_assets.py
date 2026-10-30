"""Context budget boundaries and executable skill directory guidance."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from utils.context_assets import (
    MEMORY_INDEX_MAX_BYTES,
    memory_index_context,
    memory_index_notice,
    skill_location_context,
)


class MemoryIndexContextTests(unittest.TestCase):
    def test_small_index_is_unchanged(self):
        body = "- [user] Unicode background\n- [feedback] Collaboration preferences"
        self.assertEqual(memory_index_context(body), body)
        self.assertEqual(memory_index_context(""), "")

    def test_line_limit_keeps_prefix_and_reports_hidden_entries(self):
        body = "\n".join(f"entry-{i}" for i in range(201))
        context = memory_index_context(body)
        prefix = context.split("\n\n[Index maintenance]")[0]
        self.assertEqual(len(prefix.splitlines()), 200)
        self.assertTrue(prefix.endswith("entry-199"))
        self.assertNotIn("entry-200", context)
        self.assertIn("excess content is not automatically loaded", context)
        self.assertTrue(body.endswith("entry-200"))

    def test_utf8_byte_limit_preserves_multibyte_text(self):
        body = "€" * (MEMORY_INDEX_MAX_BYTES // 3 + 1)
        prefix = memory_index_context(body).split("\n\n[Index maintenance]")[0]
        self.assertLessEqual(len(prefix.encode("utf-8")), MEMORY_INDEX_MAX_BYTES)
        self.assertNotIn("\ufffd", prefix)
        self.assertTrue(body.startswith(prefix))

    def test_exact_limits_are_loaded_with_maintenance_warning(self):
        for body in ("x" * MEMORY_INDEX_MAX_BYTES, "\n".join(["x"] * 200)):
            context = memory_index_context(body)
            self.assertEqual(context.split("\n\n[Index maintenance]")[0], body)
            self.assertIn("near", memory_index_notice(body))
            self.assertNotIn("exceeds", memory_index_notice(body))

    def test_near_limit_notice(self):
        self.assertEqual(memory_index_notice("\n".join(["x"] * 179)), "")
        self.assertIn("near", memory_index_notice("\n".join(["x"] * 180)))
        self.assertIn("exceeds", memory_index_notice("x" * (MEMORY_INDEX_MAX_BYTES + 1)))


class SkillLocationContextTests(unittest.TestCase):
    def test_nested_skill_uses_workspace_relative_directory(self):
        context = skill_location_context("parent/child")
        self.assertIn("`skills/parent/child/SKILL.md`", context)
        self.assertIn('cwd="skills/parent/child"', context)
        self.assertIn("relative paths from the skill directory", context)

    def test_directory_argument_is_quoted(self):
        name = 'Unicode skill/"child"'
        self.assertIn(f"cwd={json.dumps('skills/' + name, ensure_ascii=False)}", skill_location_context(name))

    def test_documented_cwd_resolves_script_and_relative_assets(self):
        with tempfile.TemporaryDirectory() as root:
            skill_dir = Path(root) / "skills" / "parent" / "child"
            (skill_dir / "scripts").mkdir(parents=True)
            (skill_dir / "references").mkdir()
            (skill_dir / "references" / "data.txt").write_text("asset contents", encoding="utf-8")
            (skill_dir / "scripts" / "run.py").write_text(
                'from pathlib import Path\nprint(Path("references/data.txt").read_text())\n',
                encoding="utf-8",
            )
            context = skill_location_context("parent/child")
            cwd = json.loads(context.split("cwd=", 1)[1].split(",", 1)[0])
            result = subprocess.run(
                [sys.executable, "scripts/run.py"],
                cwd=Path(root) / cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "asset contents")


if __name__ == "__main__":
    unittest.main()
