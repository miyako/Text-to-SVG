"""Release refs use real local Git tags; no workflows or releases are invoked."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_config
import release_ref


class ReleaseRefTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / (".release-ref-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.git("init", "-q")
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "release-fixture@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "first")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "0.1.0")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def resolve(self, **args):
        defaults = {"event": "workflow_dispatch", "version_tag": "0.1.0"}
        defaults.update(args)
        return release_ref.resolve(self.root, **defaults)

    def test_manual_branch_resolves_existing_tag_not_selected_branch(self):
        self.git("commit", "-q", "--allow-empty", "-m", "branch advanced")
        metadata = self.resolve(ref_type="branch", ref_name="main",
                                event_sha=self.git("rev-parse", "HEAD"))
        self.assertEqual(metadata, {"tag": "0.1.0", "sha": self.sha})

    def test_push_tag_requires_event_commit(self):
        self.assertEqual(self.resolve(event="push", ref_type="tag", ref_name="0.1.0",
                                      event_sha=self.sha),
                         {"tag": "0.1.0", "sha": self.sha})

    def test_push_branch_and_unsupported_events_fail(self):
        for args in ({"event": "push", "ref_type": "branch"},
                     {"event": "pull_request"}, {"event": "workflow_call"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.resolve(**args)

    def test_annotated_tag_peels_to_commit(self):
        self.git("tag", "-a", "0.2.0", "-m", "annotated")
        self.assertNotEqual(self.git("rev-parse", "refs/tags/0.2.0"), self.sha)
        self.assertEqual(self.resolve(version_tag="0.2.0")["sha"], self.sha)

    def test_semver_prerelease_and_build_metadata(self):
        tag = "1.2.3-rc.1+build-4"
        self.git("tag", tag)
        self.assertEqual(self.resolve(version_tag=tag)["sha"], self.sha)

    def test_malformed_and_arbitrary_refs_rejected_before_git(self):
        for tag in ("", "main", "refs/tags/0.1.0", "01.2.3", "1.2",
                    "1.2.3-01", "1.2.3\nsha=evil", "1.2.3^{commit}",
                    "--help", "1.2.3;echo x", "١.2.3", "vv1.2.3", "V1.2.3", None):
            with self.subTest(tag=tag), patch.object(release_ref.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    self.resolve(version_tag=tag)
                with self.assertRaises(ValueError):
                    release_ref.verify(self.root, tag, self.sha)
                run.assert_not_called()

    def test_new_releases_reject_legacy_v_prefix(self):
        self.git("tag", "v0.1.0")
        for args in ({"version_tag": "v0.1.0"},
                     {"event": "push", "ref_type": "tag", "ref_name": "v0.1.0",
                      "event_sha": self.sha}):
            with self.subTest(args=args), patch.object(release_ref.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "no v prefix"):
                    self.resolve(**args)
                run.assert_not_called()

    def test_existing_legacy_v_tags_still_verify(self):
        self.git("tag", "v0.1.0")
        self.git("tag", "-a", "v0.1.1", "-m", "annotated legacy")
        self.assertEqual(release_ref.verify(self.root, "v0.1.0", self.sha), self.sha)
        self.assertEqual(release_ref.verify(self.root, "v0.1.1", self.sha), self.sha)
        self.git("commit", "-q", "--allow-empty", "-m", "later")
        self.git("tag", "-f", "v0.1.0")
        with self.assertRaisesRegex(ValueError, "moved"):
            release_ref.verify(self.root, "v0.1.0", self.sha)

    def test_missing_tag_fails(self):
        with self.assertRaisesRegex(ValueError, "already exist"):
            self.resolve(version_tag="9.9.9")

    def test_tag_pointing_at_blob_fails(self):
        (self.root / "blob").write_text("not a commit", encoding="utf-8")
        blob = self.git("hash-object", "-w", "blob")
        self.git("tag", "0.3.0", blob)
        with self.assertRaisesRegex(ValueError, "point to a commit"):
            self.resolve(version_tag="0.3.0")

    def test_verify_expected_commit_and_invalid_sha(self):
        self.assertEqual(release_ref.verify(self.root, "0.1.0", self.sha), self.sha)
        for sha in ("", self.sha[:7], "HEAD", "A" * 40, "f" * 41, None):
            with self.subTest(sha=sha), self.assertRaisesRegex(ValueError, "full normalized"):
                release_ref.verify(self.root, "0.1.0", sha)

    def test_moved_tag_fails_push_and_publish(self):
        self.git("commit", "-q", "--allow-empty", "-m", "second")
        self.git("tag", "-f", "0.1.0")
        with self.assertRaisesRegex(ValueError, "moved"):
            release_ref.verify(self.root, "0.1.0", self.sha)
        with self.assertRaisesRegex(ValueError, "moved"):
            self.resolve(event="push", ref_type="tag", ref_name="0.1.0",
                         event_sha=self.sha)

    def test_deleted_tag_fails_publish(self):
        self.git("tag", "-d", "0.1.0")
        with self.assertRaisesRegex(ValueError, "already exist"):
            release_ref.verify(self.root, "0.1.0", self.sha)

    def test_git_uses_literal_tag_ref_argument_and_rejects_bad_output(self):
        result = subprocess.CompletedProcess([], 0, stdout="not-a-sha\n", stderr="")
        with patch.object(release_ref.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(ValueError, "invalid commit SHA"):
                release_ref.tag_commit(self.root, "0.1.0")
            self.assertEqual(run.call_args.args[0],
                             ["git", "rev-parse", "--verify", "refs/tags/0.1.0^{commit}"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_cli_outputs_only_validated_single_line_metadata(self):
        output = self.root / "github-output"
        output.write_text("previous=step\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/release_ref.py"), "--root", str(self.root),
             "resolve", "--event", "workflow_dispatch", "--version-tag", "0.1.0"],
            env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True,
            text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = f"tag=0.1.0\nsha={self.sha}\n"
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        self.assertEqual(output.read_text(encoding="utf-8"), "previous=step\n" + expected)

    def test_cli_failure_leaves_metadata_unchanged(self):
        output = self.root / "github-output"
        output.write_text("previous=step\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/release_ref.py"), "--root", str(self.root),
             "resolve", "--event", "workflow_dispatch", "--version-tag", "9.9.9"],
            env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True,
            text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("already exist", result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous=step\n")

    def test_existing_configuration_validator_checks_version(self):
        config = ci_config.load_config(ROOT)
        self.assertEqual(ci_config.load_config(ROOT, tag=config["version"]), config)
        self.assertEqual(ci_config.load_config(ROOT, tag="v" + config["version"]), config)
        with self.assertRaisesRegex(ValueError, "template.json version"):
            ci_config.load_config(ROOT, tag="999999.0.0")
        with self.assertRaisesRegex(ValueError, "template.json version"):
            ci_config.load_config(ROOT, tag="V" + config["version"])

    def test_workflows_pin_every_post_gate_checkout_and_package_tag(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        signed = (ROOT / ".github/workflows/signed-build.yml").read_text(encoding="utf-8")
        self.assertEqual(ci.count("uses: actions/checkout@v4"), 3)
        self.assertEqual(ci.count("ref: ${{ inputs.source_sha || github.sha }}"), 3)
        self.assertEqual(ci.count("&& !inputs.release_call"), 5)
        self.assertIn("workflow_dispatch:", ci)
        self.assertIn("workflow_dispatch:", release)
        self.assertIn("fetch-depth: 0", release)
        self.assertIn("source_sha: ${{ needs.gate.outputs.source_sha }}", release)
        self.assertIn("release_call: true", signed)
        self.assertIn("uses: ./.github/workflows/signed-build.yml", release)
        self.assertEqual(release.count("ref: ${{ needs.gate.outputs.source_sha }}"), 1)
        self.assertEqual(release.count('validate --release --tag "$RELEASE_TAG"'), 1)
        self.assertIn('accept_hosted.py --root .', signed)
        self.assertIn('--require-apple-signing', signed)
        self.assertLess(release.index("release_ref.py verify"), release.index("release_assets.py"))
        self.assertIn('git fetch --force --no-tags origin "+refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"', release)
        self.assertIn("--require-apple-signing --publish", release)

    def test_workflows_trigger_and_generate_only_bare_semver_tags(self):
        workflows = ROOT / ".github/workflows"
        release = (workflows / "release.yml").read_text(encoding="utf-8")
        candidate = (workflows / "signing-candidate.yml").read_text(encoding="utf-8")
        self.assertNotIn("'v*'", release)
        self.assertIn("- '[0-9]+.[0-9]+.[0-9]+'", release)
        self.assertIn("- '[0-9]+.[0-9]+.[0-9]+-*'", release)
        self.assertIn('"version_tag=" + config["version"]', candidate)
        for path in [*workflows.glob("*.yml"), *(ROOT / "scripts").glob("*.py")]:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn('tag="v" +', text)
                self.assertNotIn('"version_tag=v"', text)


if __name__ == "__main__":
    unittest.main()
