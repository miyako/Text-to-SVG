import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
SECRETS = (
    "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
    "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_PASSWORD",
    "NOTARYTOOL_TEAM_ID",
)


class SigningWorkflowTests(unittest.TestCase):
    def test_published_verification_is_pinned_secret_free_and_read_only(self):
        workflow = (WORKFLOWS / "verify-prerelease.yml").read_text()
        self.assertIn("contents: read", workflow)
        self.assertIn("ref: a89cdde65e380263963de688a773a5bcc9b71561", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("github.ref == 'refs/heads/miyako-text-to-svg-implementation'", workflow)
        self.assertIn("contains(github.event.head_commit.message, '[verify-prerelease]')", workflow)
        self.assertIn('release["prerelease"] is not True', workflow)
        self.assertIn("verify_assets(root", workflow)
        self.assertIn("verify_signed_payload(root", workflow)
        self.assertIn("status = test_4d(root", workflow)
        self.assertIn('"public_compiled_host"', workflow)
        for forbidden in ("contents: write", "secrets.", "--publish", "continue-on-error"):
            self.assertNotIn(forbidden, workflow)
        ci = (WORKFLOWS / "ci.yml").read_text()
        self.assertIn("!contains(github.event.head_commit.message, '[verify-prerelease]')", ci)
        self.assertIn("inputs.source_sha != ''", ci)

    def test_candidate_is_explicit_trusted_push_without_publication(self):
        candidate = (WORKFLOWS / "signing-candidate.yml").read_text()
        for guard in ("github.repository == 'miyako/Text-to-SVG'",
                      "github.event_name == 'push'",
                      "github.ref == 'refs/heads/miyako-text-to-svg-implementation'",
                      "contains(github.event.head_commit.message, '[signed-candidate]')"):
            self.assertIn(guard, candidate)
        self.assertIn("contents: read", candidate)
        self.assertNotIn("contents: write", candidate)
        self.assertNotIn("--publish", candidate)
        self.assertNotIn("gh release", candidate)
        self.assertNotIn("workflow_dispatch:", candidate)
        self.assertIn("cancel-in-progress: false", candidate)
        self.assertIn('sha != os.environ["GITHUB_SHA"]', candidate)

    def test_callers_share_the_actual_signed_pipeline(self):
        for filename in ("signing-candidate.yml", "release.yml"):
            workflow = (WORKFLOWS / filename).read_text()
            self.assertIn("uses: ./.github/workflows/signed-build.yml", workflow)
            self.assertNotIn("secrets: inherit", workflow)
            for name in SECRETS:
                self.assertIn(name + ": ${{ secrets." + name + " }}", workflow)
        signed = (WORKFLOWS / "signed-build.yml").read_text()
        self.assertIn("uses: ./.github/workflows/ci.yml", signed)
        self.assertIn("release_call: true", signed)
        self.assertIn("source_sha: ${{ inputs.source_sha }}", signed)
        self.assertIn("ref: ${{ inputs.source_sha }}", signed)
        self.assertIn("persist-credentials: false", signed)
        self.assertIn("github.event_name != 'pull_request'", signed)
        self.assertIn("github.event_name != 'pull_request_target'", signed)
        self.assertNotIn("CODESIGN_APPLE_ID", signed)

    def test_cleanup_and_final_verification_are_mandatory(self):
        signed = (WORKFLOWS / "signed-build.yml").read_text()
        self.assertIn("sign_release.py cleanup --root .", signed)
        self.assertIn("if: always()\n        run: python scripts/sign_release.py cleanup", signed)
        self.assertNotIn("continue-on-error", signed)
        self.assertIn("build/signing-diagnostics/", signed)
        self.assertNotIn("RUNNER_TEMP/", signed)
        self.assertLess(signed.index("release_assets.py"), signed.index("name: compiled-release"))
        self.assertIn("--require-apple-signing", signed)

    def test_publication_requires_exact_signed_evidence_and_existing_tag(self):
        release = (WORKFLOWS / "release.yml").read_text()
        self.assertIn("needs: [gate, build]", release)
        self.assertIn("release_ref.py verify", release)
        self.assertIn("--require-apple-signing --publish", release)
        self.assertNotIn("Unsigned compiled", release)
        self.assertNotIn("git tag", release)
        self.assertIn('git fetch --force --no-tags origin "+refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"', release)
        policy = json.loads((ROOT / "release-policy.json").read_text())
        config = json.loads((ROOT / "template.json").read_text())
        self.assertEqual(policy["schema"], 1)
        self.assertIn(config["version"], policy["apple_notarized_versions"])
