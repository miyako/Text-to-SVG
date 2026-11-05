"""Publish computes the next bare SemVer from tags and rewrites every version literal."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bump_version  # noqa: E402
from ci_config import load_config  # noqa: E402


class NextVersionTests(unittest.TestCase):
    def test_bumps_latest_stable_tag_including_legacy_v_prefix(self):
        tags = ["v0.1.0", "v0.1.1", "0.1.2-rc.1", "v0.2.0-beta", "latest", "v01.0.0"]
        self.assertEqual(bump_version.next_version(tags, "patch", "0.0.1"), "0.1.2")
        self.assertEqual(bump_version.next_version(tags, "minor", "0.0.1"), "0.2.0")
        self.assertEqual(bump_version.next_version(tags, "major", "0.0.1"), "1.0.0")

    def test_numeric_not_lexical_ordering_and_mixed_prefixes(self):
        tags = ["v0.9.0", "0.10.0", "v0.2.5"]
        self.assertEqual(bump_version.next_version(tags, "patch", "0.0.1"), "0.10.1")

    def test_without_tags_bumps_configured_version(self):
        self.assertEqual(bump_version.next_version([], "patch", "0.1.1"), "0.1.2")
        self.assertEqual(bump_version.next_version(["0.3.0-rc.1"], "minor", "0.1.1"), "0.2.0")

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            bump_version.next_version([], "build", "0.1.1")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        config = load_config(ROOT, initialized=True)
        project = Path("component") / config["repository"] / "Project"
        for name in ("template.json", "package.json", "cli/CMakeLists.txt"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.root / name)
        shutil.copytree(ROOT / project / "Sources", self.root / project / "Sources")
        shutil.copy2(ROOT / project / (config["repository"] + ".4DProject"),
                     self.root / project / (config["repository"] + ".4DProject"))
        shutil.copytree(ROOT / "tests/host", self.root / "tests/host")
        self.old = config["version"]

    def test_rewrites_all_version_literals_and_keeps_config_consistent(self):
        files = bump_version.apply(self.root, "9.8.7")
        self.assertIn("template.json", files)
        self.assertIn("cli/CMakeLists.txt", files)
        config = load_config(self.root, initialized=True)
        self.assertEqual(config["version"], "9.8.7")
        package = json.loads((self.root / "package.json").read_text())
        self.assertEqual(package["version"], "9.8.7")
        self.assertNotEqual(package["tool"]["version"], "9.8.7")
        contract = json.loads((self.root / "tests/host/contract.json").read_text())
        self.assertIn("9.8.7", json.dumps(contract))
        for name in files:
            self.assertNotIn('"' + self.old + '"', (self.root / name).read_text(), name)
        self.assertIn("VERSION 9.8.7 ", (self.root / "cli/CMakeLists.txt").read_text())

    def test_same_version_is_a_noop_for_retries(self):
        self.assertEqual(bump_version.apply(self.root, self.old), [])

    def test_unexpected_literal_count_fails_without_partial_writes(self):
        smoke = self.root / "tests/host/Project/Sources/Methods/Smoke.4dm"
        smoke.write_text(smoke.read_text().replace(self.old, "0.0.0"))
        before = (self.root / "template.json").read_text()
        with self.assertRaisesRegex(ValueError, "Smoke.4dm"):
            bump_version.apply(self.root, "9.8.7")
        self.assertEqual((self.root / "template.json").read_text(), before)

    def test_rejects_non_stable_target(self):
        for version in ("v1.0.0", "1.0", "1.0.0-rc.1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                bump_version.apply(self.root, version)


if __name__ == "__main__":
    unittest.main()
