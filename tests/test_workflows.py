"""CI validates; Publish is the single signed release workflow."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
SECRETS = (
    "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
    "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_PASSWORD",
    "NOTARYTOOL_TEAM_ID",
)


class WorkflowTests(unittest.TestCase):
    def test_only_ci_and_publish_exist(self):
        self.assertEqual(sorted(path.name for path in WORKFLOWS.iterdir()), ["ci.yml", "publish.yml"])

    def test_ci_is_secret_free_and_never_publishes(self):
        ci = (WORKFLOWS / "ci.yml").read_text()
        self.assertIn("name: CI\n", ci)
        self.assertIn("pull_request:", ci)
        self.assertIn("workflow_call:", ci)
        self.assertIn("vars.FOURD_ENABLED == 'true' || inputs.run_4d", ci)
        for forbidden in ("secrets.", "sign_release.py", "--publish", "contents: write",
                          "source_sha", "release_call", "candidate", "prerelease"):
            self.assertNotIn(forbidden, ci)

    def test_publish_has_only_a_mode_choice(self):
        publish = (WORKFLOWS / "publish.yml").read_text()
        self.assertIn("name: Publish\n", publish)
        header = publish[:publish.index("permissions:")]
        self.assertIn("workflow_dispatch:", header)
        self.assertNotIn("push:", header)
        self.assertNotIn("tags:", header)
        self.assertEqual(header.count("type:"), 1)
        self.assertIn("type: choice", header)
        for option in ("- patch", "- minor", "- major"):
            self.assertIn(option, header)
        self.assertIn("default: patch", header)
        for forbidden in ("version_tag", "source_sha", "zip_sha256", "candidate", "prerelease"):
            self.assertNotIn(forbidden, publish)

    def test_publish_bumps_on_main_then_builds_signs_and_releases(self):
        publish = (WORKFLOWS / "publish.yml").read_text()
        order = ["bump_version.py --mode \"$MODE\" --write", "git push origin HEAD:main",
                 "uses: ./.github/workflows/ci.yml", "accept_hosted.py",
                 "--require-apple-signing", "name: compiled-release",
                 "--require-apple-signing --publish"]
        positions = [publish.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("github.ref != 'refs/heads/main'", publish)
        self.assertIn("ref: main", publish)
        self.assertIn("cancel-in-progress: false", publish)
        self.assertNotIn("secrets: inherit", publish)
        self.assertNotIn("CODESIGN_APPLE_ID", publish)
        for name in SECRETS:
            self.assertIn(name + ": ${{ secrets." + name + " }}", publish)
        self.assertIn("if: always()\n        run: python scripts/sign_release.py cleanup", publish)
        self.assertNotIn("continue-on-error", publish)
        self.assertNotIn("git tag", publish)

    def test_secrets_and_write_token_are_separated(self):
        publish = (WORKFLOWS / "publish.yml").read_text()
        build = publish[publish.index("\n  build:"):publish.index("\n  release:")]
        release = publish[publish.index("\n  release:"):]
        self.assertIn("secrets.", build)
        self.assertNotIn("contents: write", build)
        self.assertIn("persist-credentials: false", build)
        self.assertNotIn("secrets.", release)
        self.assertIn("contents: write", release)


if __name__ == "__main__":
    unittest.main()
