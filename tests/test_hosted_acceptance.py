"""Hosted orchestration tests use the real destination configuration and host registry."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import accept_hosted as hosted
import build_provenance
import release_assets as assets
from test_host_runner import valid_status

SHA = "a" * 40
TEAM = "ABCD123456"


class HostedFixture:
    """Only subprocess/runtime boundaries are mocked, never the host report validator."""

    def fixture(self):
        self.root = ROOT / (".hosted-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.config = json.loads((ROOT / "template.json").read_text())
        self.name, self.cli = self.config["repository"], self.config["cli"]
        self.write("template.json", self.config)
        shutil.copy2(ROOT / "package.json", self.root / "package.json")
        shutil.copy2(ROOT / "LICENSE", self.root / "LICENSE")
        source = ROOT / "component" / self.name
        project = "component/" + self.name + "/Project/"
        for suffix in (self.name + ".4DProject", "Sources/settings.4DSettings",
                       "Sources/catalog.4DCatalog", "Sources/Classes/" + self.config["feature_class"] + ".4dm"):
            path = self.root / project / suffix
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / "Project" / suffix, path)
        self.write("tests/host/contract.json", json.loads((ROOT / "tests/host/contract.json").read_text()))
        self.write("cli/source.cpp", "native source\n", raw=True)
        self.write("scripts/sign_release.py", "mocked signer subprocess\n", raw=True)
        self.tool = self.write("tools/tool4d", "mocked executable identity\n", raw=True)
        self.archive = self.root / "dist" / (self.name + ".zip")
        self.host_calls = []
        self.commands = []
        self.signature_paths = ["Resources/bin/macOS/" + self.cli, "Libraries/lib4d-arm64.dylib"]
        self.marker = {
            "success": True, "errors": [],
            "project": str(self.root / project / (self.name + ".4DProject")),
            "targets": ["x86_64_generic", "arm64_macOS_lib"],
            "input_sha256": "b" * 64, "output_sha256": "c" * 64,
        }
        source_hash = build_provenance.source_digest(self.root)
        self.manifests = {}
        for target in hosted.TARGETS:
            binary = self.write("build/" + target + "/" + self.cli + (
                ".exe" if target == "windows-x64" else ""), target, raw=True)
            manifest = {
                "schema": 1, "target": target, "source_sha256": source_hash, "commit": SHA,
                "binary_sha256": build_provenance.sha256(binary),
                "execution": "passed", "toolchain": "clang fixture",
            }
            self.write("build/" + target + "/build-manifest.json", manifest)
            self.manifests[target] = manifest

    def write(self, name, value, raw=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if raw else json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def make_archive(self, signed=False, extra=None):
        self.archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(self.archive, "w") as packed:
            prefix = self.name + ".4dbase/"
            packed.writestr(prefix, b"")
            packed.writestr(prefix + self.name + ".4DZ", b"compiled project")
            for index, path in enumerate(self.signature_paths):
                packed.writestr(prefix + path, b"\xcf\xfa\xed\xfe" + (
                    b"signed" if signed else b"unsigned") + bytes([index]))
            if extra:
                packed.writestr(prefix + extra, b"not expected")
        return self.archive

    def make_proof(self):
        self.make_archive(signed=True)
        dmg = self.write("dist/" + self.name + ".dmg", "stapled accepted DMG", raw=True)
        files = assets.signed_archive_files(self.root, self.config)
        signed = []
        tickets = []
        for index, (path, hash_) in enumerate(files.items()):
            signatures = {}
            for architecture in (["x86_64", "arm64"] if index == 0 else ["arm64"]):
                cdhash = ("d" if index == 0 else "e") * 40
                signatures[architecture] = {
                    "cdhash": cdhash, "team": TEAM,
                    "authority": "Developer ID Application: Example (" + TEAM + ")",
                    "timestamp": "Nov 4, 2026 at 12:00:00", "hardenedRuntime": True,
                }
                tickets.append({
                    "path": self.name + ".dmg/" + self.name + ".4dbase/" + path,
                    "arch": architecture, "digestAlgorithm": "SHA-256", "cdhash": cdhash,
                })
            signed.append({"path": path, "sha256": hash_, "verified": True, "signatures": signatures})
        tickets.append({"path": self.name + ".dmg", "arch": None,
                        "digestAlgorithm": "SHA-256", "cdhash": "f" * 40})
        unsigned = json.loads((self.root / "build/hosted-acceptance.json").read_text())["unsigned_artifact"]
        proof = {
            "schema": 1, "version": self.config["version"], "sourceSHA": SHA,
            "releaseTag": "v" + self.config["version"],
            "candidate": os.environ.get("CANDIDATE_MODE", "").lower() == "true",
            "unsignedCompileInputSHA256": self.marker["input_sha256"],
            "unsignedZIPHashSHA256": unsigned["sha256"],
            "signedFiles": signed,
            "notarization": {"status": "Accepted", "submissionUUID": "01234567-89ab-cdef-0123-456789abcdef",
                             "ticketContents": tickets},
            "assessments": {"codeNotarization": "passed", "dmgOpen": "passed"},
            "dmg": {"stapled": True, "validated": True, "sha256": build_provenance.sha256(dmg)},
            "zip": {"sha256": build_provenance.sha256(self.archive), "stapled": False,
                    "notarizedViaDMG": True, "origin": "final-mounted-accepted-DMG"},
            "hostValidation": "required-after-signing",
        }
        self.write("dist/" + self.name + "-signing.json", proof)
        return proof

    def fake_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command == [str(self.tool), "--version"]:
            return subprocess.CompletedProcess(command, 0, "tool4d 20.7 build 101905\n", "")
        if "sign" in command:
            self.make_proof()
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if command[0] == "/usr/bin/lipo" or "--verify" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "/usr/bin/codesign" and "--display" in command:
            cli = command[-1].endswith(self.cli)
            output = (
                "TeamIdentifier=" + TEAM + "\n"
                "Authority=Developer ID Application: Example (" + TEAM + ")\n"
                "Timestamp=Nov 4, 2026 at 12:00:00\n"
                "CDHash=" + ("d" if cli else "e") * 40 + "\n"
                "CodeDirectory v=20500 flags=0x10000(runtime) hashes=123+0\n")
            return subprocess.CompletedProcess(command, 0, "", output)
        raise AssertionError("unexpected subprocess: " + repr(command))

    def fake_host(self, root, tool, component=None):
        self.host_calls.append(component)
        return valid_status(component is not None)

    def mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {
            "CANDIDATE_MODE": "false", "RELEASE_SHA": SHA,
            "RELEASE_TAG": "v" + self.config["version"], "NOTARYTOOL_TEAM_ID": TEAM,
        }))
        stack.enter_context(patch.object(hosted, "commit", return_value=SHA))
        stack.enter_context(patch.object(build_provenance, "commit", return_value=SHA))
        stack.enter_context(patch.object(assets, "commit", return_value=SHA))
        stack.enter_context(patch.object(hosted, "require_tool4d_host"))
        stack.enter_context(patch.object(hosted, "assemble", return_value={"assembled": True}))
        stack.enter_context(patch.object(hosted, "inspect", side_effect=lambda path, target:
                                        {"target": target, "dependencies": [], "status": "passed"}))
        stack.enter_context(patch.object(hosted, "validate_compile"))
        stack.enter_context(patch.object(hosted, "compile_component", side_effect=lambda *args:
                                        self.compile()))
        stack.enter_context(patch.object(hosted, "package", side_effect=lambda *args, **kwargs:
                                        self.make_archive()))
        stack.enter_context(patch.object(hosted, "test_4d", side_effect=self.fake_host))
        stack.enter_context(patch.object(hosted.subprocess, "run", side_effect=self.fake_run))
        return stack

    def compile(self):
        self.write("build/compile-status.json", self.marker)
        return copy.deepcopy(self.marker)

    def accept(self, signing=True):
        return hosted.accept(self.root, "miyako/" + self.name, self.tool,
                             "v" + self.config["version"], signing)


class HostedAcceptanceTests(HostedFixture, unittest.TestCase):
    def setUp(self):
        self.fixture()
        self.mocks()

    def test_signed_flow_validates_unsigned_and_exact_final_host_with_real_registry(self):
        report = self.accept()
        self.assertEqual((report["result"], report["finished"], report["release_ready"]),
                         ("partial", True, False))
        self.assertEqual(len(self.host_calls), 3)
        self.assertIsNone(self.host_calls[0])
        self.assertEqual(self.host_calls[-1], self.root / "build/release-test" / (self.name + ".4dbase"))
        self.assertEqual(len(report["steps"]["compiled_host"]["details"]["cases"]), 8)
        self.assertTrue(report["steps"]["compiled_host"]["details"]["compiled"])
        self.assertNotEqual(report["unsigned_artifact"]["sha256"], report["artifact"]["sha256"])
        self.assertEqual(report["unsigned_compile"], self.marker)
        self.assertEqual(json.loads((self.root / "build/compile-status.json").read_text()), self.marker)
        self.assertEqual(report["steps"]["apple_signatures"]["details"]["team"], TEAM)
        for name in ("native_intel_4d", "windows_4d", "release_download"):
            self.assertEqual(report["steps"][name]["status"], "not_run")
        self.assertEqual(report, json.loads(self.archive.with_suffix(".acceptance.json").read_text()))
        signing = [(command, options) for command, options in self.commands if "sign" in command]
        self.assertEqual(len(signing), 1)
        self.assertEqual(signing[0][0][2:], ["sign", "--root", str(self.root), "--require-apple-signing"])
        self.assertTrue(signing[0][1]["capture_output"])
        self.assertFalse(any(command[0] in ("gh", "git") for command, _ in self.commands))

    def test_signed_candidate_runs_same_pipeline_without_any_tag_or_publication(self):
        with patch.dict(os.environ, {"CANDIDATE_MODE": "true"}):
            report = self.accept()
        self.assertTrue(report["candidate"])
        self.assertTrue(report["steps"]["signing_proof"]["details"]["candidate"])
        self.assertFalse(report["release_ready"])

    def test_policy_enforces_signing_even_without_cli_upgrade_flag(self):
        self.write("release-policy.json", {"schema": 1, "apple_notarized_versions": [self.config["version"]]})
        self.assertTrue(self.accept(signing=False)["require_apple_signing"])

    def test_unsigned_flow_still_runs_unpacked_unsigned_and_final_compiled_hosts(self):
        report = self.accept(signing=False)
        self.assertEqual(len(self.host_calls), 3)
        self.assertEqual(report["artifact"], report["unsigned_artifact"])
        for name in ("signing", "signing_proof", "apple_signatures"):
            self.assertEqual(report["steps"][name]["status"], "not_run")

    def test_every_native_target_requires_current_manifest_and_native_execution(self):
        for target in hosted.TARGETS:
            manifest = self.root / "build" / target / "build-manifest.json"
            original = manifest.read_text()
            for field, value in (("execution", "not_run"), ("commit", "f" * 40),
                                 ("source_sha256", "f" * 64), ("binary_sha256", "f" * 64)):
                altered = json.loads(original)
                altered[field] = value
                manifest.write_text(json.dumps(altered))
                with self.subTest(target=target, field=field), self.assertRaises(ValueError):
                    self.accept()
                report = json.loads((self.root / "build/hosted-acceptance.json").read_text())
                self.assertFalse(report["finished"])
                self.assertEqual(report["steps"]["cli_test_" + target]["status"], "failed")
            manifest.write_text(original)

    def test_successful_compiler_with_diagnostics_is_rejected_before_package(self):
        self.marker["errors"] = [{"message": "failed symbol"}]
        with self.assertRaises(ValueError):
            self.accept()
        self.assertFalse(self.archive.exists())

    def test_final_compiled_mode_never_falls_back_to_interpreted(self):
        count = 0

        def incorrect(root, tool, component=None):
            nonlocal count
            count += 1
            return valid_status(compiled=component is not None and count != 3)
        with patch.object(hosted, "test_4d", side_effect=incorrect), self.assertRaises(ValueError):
            self.accept()
        report = json.loads((self.root / "build/hosted-acceptance.json").read_text())
        self.assertEqual(report["failed_step"], "compiled_host")
        self.assertEqual(report["steps"]["compiled_host"]["status"], "failed")
        self.assertFalse(self.archive.with_suffix(".acceptance.json").exists())

    def test_eight_cases_and_raw_failure_assertions_are_not_bypassed(self):
        for field in ("cases", "launch", "serialization"):
            status = valid_status(True)
            if field == "cases":
                status["cases"].pop()
            else:
                status.pop(field, None)
            with self.subTest(field=field), patch.object(
                    hosted, "test_4d", side_effect=[valid_status(False), valid_status(True), status]):
                with self.assertRaises(ValueError):
                    self.accept()

    def test_signing_failure_report_and_cli_do_not_disclose_child_output(self):
        secret = "PRIVATE-NOTARY-PASSWORD"
        original = self.fake_run

        def failure(command, **kwargs):
            if "sign" in command:
                raise subprocess.CalledProcessError(1, ["sign", secret], output=secret, stderr=secret)
            return original(command, **kwargs)
        with patch.object(hosted.subprocess, "run", side_effect=failure):
            with self.assertRaises(ValueError) as error:
                self.accept()
        report = (self.root / "build/hosted-acceptance.json").read_text()
        self.assertNotIn(secret, report + str(error.exception))
        self.assertEqual(json.loads(report)["failed_step"], "signing")
        self.assertFalse(json.loads(report)["finished"])

    def test_final_signature_must_match_expected_team_and_receipts(self):
        original = self.fake_run

        def wrong_team(command, **kwargs):
            result = original(command, **kwargs)
            if "--display" in command:
                result.stderr = result.stderr.replace(TEAM, "ZZZZ999999")
            return result
        with patch.object(hosted.subprocess, "run", side_effect=wrong_team), self.assertRaises(ValueError):
            self.accept()
        report = json.loads((self.root / "build/hosted-acceptance.json").read_text())
        self.assertEqual(report["failed_step"], "apple_signatures")

    def test_unexpected_runtime_abort_preserves_fresh_raw_host_diagnostics(self):
        raw = {"success": False, "finished": False, "phase": "feature call",
               "errors": [{"errCode": 901, "componentSignature": "xbox"}]}

        def abort(*args):
            self.write("build/host-status.json", raw)
            raise ValueError("runtime aborted")
        with patch.object(hosted, "test_4d", side_effect=abort), self.assertRaises(ValueError):
            self.accept()
        report = json.loads((self.root / "build/hosted-acceptance.json").read_text())
        self.assertEqual(report["failure_host_status"], raw)
        self.assertEqual(report["steps"]["source_host"]["status"], "failed")

    def test_signer_cannot_rewrite_pre_sign_compile_marker(self):
        original = self.fake_run

        def rewrite(command, **kwargs):
            result = original(command, **kwargs)
            if "sign" in command:
                self.write("build/compile-status.json", {**self.marker, "output_sha256": "f" * 64})
            return result
        with patch.object(hosted.subprocess, "run", side_effect=rewrite), self.assertRaises(ValueError):
            self.accept()
        report = json.loads((self.root / "build/hosted-acceptance.json").read_text())
        self.assertEqual(report["failed_step"], "signing")

    def test_final_asset_mutation_during_host_execution_rejected(self):
        count = 0

        def mutation(root, tool, component=None):
            nonlocal count
            count += 1
            if count == 3:
                self.archive.write_bytes(self.archive.read_bytes() + b"changed")
            return valid_status(component is not None)
        with patch.object(hosted, "test_4d", side_effect=mutation), self.assertRaises(ValueError):
            self.accept()

    def test_report_output_symlink_rejected_without_overwriting_target(self):
        sentinel = self.write("sentinel", "unchanged", raw=True)
        (self.root / "build/hosted-acceptance.json").symlink_to(sentinel)
        with self.assertRaises(ValueError):
            self.accept()
        self.assertEqual(sentinel.read_text(), "unchanged")
