"""Downloaded release assets must retain exact accepted source, host and Apple evidence."""
import copy
import json
import os
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from test_hosted_acceptance import HostedFixture, SHA, TEAM
import release_assets as assets


class ReleaseAssetsTests(HostedFixture, unittest.TestCase):
    def setUp(self):
        self.fixture()
        self.mocks()
        self.report = self.accept()
        self.evidence = self.archive.with_suffix(".acceptance.json")
        self.tag = self.config["version"]

    def verify(self, signing=True):
        return assets.verify_assets(self.root, "miyako/" + self.name,
                                    self.config["version"], SHA, signing)

    def save_report(self, report):
        self.evidence.write_text(json.dumps(report))

    def change_proof(self, mutate):
        proof_path = self.root / "dist" / (self.name + "-signing.json")
        proof = json.loads(proof_path.read_text())
        mutate(proof)
        proof_path.write_text(json.dumps(proof))
        report = copy.deepcopy(self.report)
        report["assets"][2] = assets.asset_record(self.root, proof_path)
        report["steps"]["signing_proof"]["details"] = proof
        self.save_report(report)

    def test_downloaded_final_assets_and_evidence_verify(self):
        paths = self.verify()
        self.assertEqual([path.name for path in paths], [
            self.name + ".zip", self.name + ".dmg",
            self.name + "-signing.json", self.name + ".acceptance.json"])
        self.assertFalse(self.report["release_ready"])

    def test_rejects_asset_hash_size_symlink_and_extra_assets(self):
        original = self.archive.read_bytes()
        for contents in (original + b"corruption", b""):
            self.archive.write_bytes(contents)
            with self.assertRaises(ValueError):
                self.verify()
        self.archive.write_bytes(original)
        extra = self.write("dist/unverified.zip", "unexpected", raw=True)
        with self.assertRaisesRegex(ValueError, "unvalidated"):
            self.verify()
        extra.unlink()
        target = self.write("archive-copy", original.hex(), raw=True)
        self.archive.unlink()
        self.archive.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.verify()

    def test_rejects_different_source_sha_configuration_tool_and_native_inputs(self):
        for field, value in (("commit", "f" * 40), ("scope", "local"),
                             ("finished", False), ("result", "failed"), ("release_ready", True),
                             ("configuration", {**self.config, "namespace": "Wrong"}),
                             ("tool4d", {"version": "", "binary_sha256": "f" * 64})):
            report = copy.deepcopy(self.report)
            report[field] = value
            self.save_report(report)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
        self.save_report(self.report)
        self.write("cli/source.cpp", "changed source", raw=True)
        with self.assertRaisesRegex(ValueError, "native build"):
            self.verify()

    def test_rejects_missing_or_cross_built_native_execution_for_every_target(self):
        for target in assets.TARGETS:
            for field, value in (("execution", "not_run"), ("commit", "f" * 40),
                                 ("binary_sha256", ""), ("source_sha256", "e" * 64)):
                report = copy.deepcopy(self.report)
                report["steps"]["cli_test_" + target]["details"][field] = value
                self.save_report(report)
                with self.subTest(target=target, field=field), self.assertRaisesRegex(ValueError, "native build"):
                    self.verify()

    def test_host_registry_rechecked_for_both_modes_all_registered_cases_and_diagnostics(self):
        for name in ("source_host", "unsigned_compiled_host", "compiled_host"):
            for field, value in (("compiled", name == "source_host"), ("finished", False),
                                 ("cases", []), ("launch", None), ("serialization", None)):
                report = copy.deepcopy(self.report)
                report["steps"][name]["details"][field] = value
                self.save_report(report)
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    self.verify()

    def test_clean_compiler_diagnostics_and_both_targets_required(self):
        for field, value in (("errors", [{"message": "warning is not clean"}]),
                             ("errors", None), ("targets", ["x86_64_generic"]),
                             ("project", "/wrong.4DProject"), ("output_sha256", "")):
            report = copy.deepcopy(self.report)
            report["steps"]["compile"]["details"][field] = value
            self.save_report(report)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
        report = copy.deepcopy(self.report)
        report["unsigned_compile"]["output_sha256"] = "f" * 64
        self.save_report(report)
        with self.assertRaisesRegex(ValueError, "pre-sign"):
            self.verify()

    def test_policy_upgrade_cannot_be_downgraded(self):
        self.write("release-policy.json", {"schema": 1, "apple_notarized_versions": [self.config["version"]]})
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.verify(signing=False)
        self.verify()

    def test_notary_status_staple_origin_provenance_and_signed_file_guards(self):
        mutations = [
            lambda p: p.update(sourceSHA="e" * 40),
            lambda p: p.update(unsignedCompileInputSHA256="e" * 64),
            lambda p: p.update(unsignedZIPHashSHA256="e" * 64),
            lambda p: p["notarization"].update(status="Invalid"),
            lambda p: p["notarization"].update(submissionUUID="not a UUID"),
            lambda p: p["notarization"].update(ticketContents=[]),
            lambda p: p["dmg"].update(stapled=False),
            lambda p: p["dmg"].update(validated=False),
            lambda p: p["dmg"].update(sha256="e" * 64),
            lambda p: p["zip"].update(origin="unsigned staging"),
            lambda p: p["zip"].update(stapled=True),
            lambda p: p["zip"].update(sha256="e" * 64),
            lambda p: p["signedFiles"].pop(),
            lambda p: p["signedFiles"].append(copy.deepcopy(p["signedFiles"][0])),
            lambda p: p["signedFiles"][0].update(path="Resources/bin/macOS/../" + self.cli),
            lambda p: p["signedFiles"][0].update(sha256="e" * 64),
            lambda p: p["signedFiles"][0]["signatures"]["arm64"].update(team="ZZZZ999999"),
            lambda p: p["signedFiles"][0]["signatures"]["arm64"].update(hardenedRuntime=False),
            lambda p: p["signedFiles"][0]["signatures"]["arm64"].update(timestamp="none"),
            lambda p: p["signedFiles"][0]["signatures"].pop("x86_64"),
            lambda p: p["assessments"].update(codeNotarization="not_run"),
        ]
        original = (self.root / "dist" / (self.name + "-signing.json")).read_text()
        for index, mutation in enumerate(mutations):
            (self.root / "dist" / (self.name + "-signing.json")).write_text(original)
            self.change_proof(mutation)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.verify()

    def test_signature_team_and_exact_payload_bytes_must_match_accepted_proof(self):
        for field, value in (("team", "ZZZZ999999"), ("signedFiles", [])):
            report = copy.deepcopy(self.report)
            report["steps"]["apple_signatures"]["details"][field] = value
            self.save_report(report)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()

    def test_signed_archive_symlinks_and_path_aliases_rejected(self):
        original = self.archive.read_bytes()
        for name in (self.name + ".4dbase/Libraries/../alias.dylib",
                     self.name + ".4dbase/Libraries/link.dylib"):
            self.archive.write_bytes(original)
            with zipfile.ZipFile(self.archive, "a") as packed:
                entry = zipfile.ZipInfo(name)
                entry.external_attr = 0o120777 << 16
                packed.writestr(entry, b"elsewhere")
            report = copy.deepcopy(self.report)
            report["assets"][0] = assets.asset_record(self.root, self.archive)
            report["artifact"] = report["assets"][0]
            self.save_report(report)
            with self.assertRaises(ValueError):
                assets.signed_archive_files(self.root, self.config)

    def test_native_import_inspection_is_required(self):
        for key in ("macOS", "Windows"):
            report = copy.deepcopy(self.report)
            report["steps"]["dependencies"]["details"][key]["status"] = "not_run"
            self.save_report(report)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify()

    def test_candidate_cannot_publish_even_when_assets_verify(self):
        report = copy.deepcopy(self.report)
        report["candidate"] = True
        self.save_report(report)
        with patch.dict(os.environ, {"CANDIDATE_MODE": "true"}):
            with self.assertRaisesRegex(ValueError, "candidate"):
                assets.publish(self.root, "miyako/" + self.name, self.tag, SHA, True)
        report["candidate"] = True
        self.change_proof(lambda p: p.update(candidate=True))
        updated = json.loads(self.evidence.read_text())
        updated["candidate"] = True
        self.save_report(updated)
        self.verify()
        with self.assertRaisesRegex(ValueError, "candidate"):
            assets.publish(self.root, "miyako/" + self.name, self.tag, SHA, True)

    def test_explicit_publication_checks_existing_local_and_remote_tag(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, SHA + "\trefs/tags/" + self.tag + "\n", "")
        with patch.object(assets, "verify_tag") as tag, patch.object(assets.subprocess, "run", side_effect=run):
            assets.publish(self.root, "miyako/" + self.name, self.tag, SHA, True)
        tag.assert_called_once_with(self.root, self.tag, SHA)
        self.assertEqual(calls[0][:3], ["git", "ls-remote", "--tags"])
        self.assertEqual(calls[-1][:4], ["gh", "release", "create", self.tag])
        self.assertIn("--verify-tag", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--target") + 1], SHA)
        self.assertIn("--latest", calls[-1])
        self.assertNotIn("--prerelease", calls[-1])
        self.assertFalse(any("tag" in command[:2] for command in calls))

    def test_legacy_v_tag_verifies_existing_release_but_never_publishes(self):
        legacy = "v" + self.config["version"]
        self.change_proof(lambda p: p.update(releaseTag=legacy))
        assets.verify_assets(self.root, "miyako/" + self.name, legacy, SHA, True)
        with patch.object(assets, "verify_tag") as tag, patch.object(assets.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "no v prefix"):
                assets.publish(self.root, "miyako/" + self.name, legacy, SHA, True)
        tag.assert_not_called()
        run.assert_not_called()

    def test_remote_tag_movement_blocks_publication(self):
        with patch.object(assets, "verify_tag"), patch.object(
                assets.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "f" * 40 + "\trefs/tags/" + self.tag + "\n", "")) as run:
            with self.assertRaisesRegex(ValueError, "moved"):
                assets.publish(self.root, "miyako/" + self.name, self.tag, SHA, True)
        self.assertEqual(run.call_count, 1)
