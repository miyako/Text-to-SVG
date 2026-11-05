"""Secret-free signing tests. Synthetic code is never executed or really signed."""
import base64
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sign_release as signing
from compile_component import input_digest, output_digest


SHA = "a" * 40
TEAM = "ABCD123456"
IDENTITY = f"Developer ID Application: Example ({TEAM})"
FINGERPRINT = "B" * 40
SUBMISSION = "12345678-1234-1234-1234-123456789abc"


def universal():
    result = bytearray(112)
    struct.pack_into(">II", result, 0, 0xCAFEBABE, 2)
    for index, cpu in enumerate((0x01000007, 0x0100000C)):
        offset = 48 + index * 32
        struct.pack_into(">IIIII", result, 8 + index * 20, cpu, 0, offset, 32, 0)
        struct.pack_into("<II", result, offset, 0xFEEDFACF, cpu)
    return bytes(result)


def pe():
    result = bytearray(128)
    result[:2] = b"MZ"
    struct.pack_into("<I", result, 0x3C, 64)
    result[64:68] = b"PE\0\0"
    struct.pack_into("<H", result, 68, 0x8664)
    struct.pack_into("<H", result, 88, 0x20B)
    return bytes(result)


def thin():
    return struct.pack("<II", 0xFEEDFACF, 0x0100000C) + bytes(24)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / (".signing-tests-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.config = {
            "schema": 1, "initialized": True, "repository": "Text-to-SVG",
            "namespace": "TextToSVG", "cli": "text-to-svg", "language": "cpp",
            "feature": "Text outlines", "feature_class": "TextOutline", "version": "0.1.0",
        }
        self.write("template.json", json.dumps(self.config))
        self.write("package.json", json.dumps({"name": "Text-to-SVG", "version": "0.1.0"}))
        self.write("LICENSE", "MIT synthetic fixture\n")
        self.component = self.root / "component/Text-to-SVG"
        self.write("component/Text-to-SVG/Project/Text-to-SVG.4DProject", "{}")
        for name, value in {
            "settings.4DSettings": '<preferences><com.4d><general component_classStore_name="TextToSVG"/></com.4d></preferences>',
            "catalog.4DCatalog": '<base name="Text-to-SVG"/>',
            "Classes/TextOutline.4dm": "// source must not ship",
        }.items():
            self.write("component/Text-to-SVG/Project/Sources/" + name, value)
        for name in ("classes.json", "map.json", "MX64 123"):
            self.write("component/Text-to-SVG/Project/DerivedData/CompiledCode/" + name, "{}")
        self.write("component/Text-to-SVG/Libraries/lib4d-arm64.dylib", thin(), 0o755)
        self.write("component/Text-to-SVG/Libraries/nested/helper", thin(), 0o755)
        self.write("component/Text-to-SVG/Libraries/metadata.json", "{}")
        self.write("component/Text-to-SVG/Resources/bin/macOS/text-to-svg", universal(), 0o755)
        self.write("component/Text-to-SVG/Resources/bin/Windows/text-to-svg.exe", pe(), 0o755)
        self.write("component/Text-to-SVG/Resources/licenses/dependencies.txt", "MIT notices")
        self.write("build/compile-status.json", json.dumps({
            "success": True, "project": str(self.component / "Project/Text-to-SVG.4DProject"),
            "targets": ["x86_64_generic", "arm64_macOS_lib"],
            "input_sha256": input_digest(self.root), "output_sha256": output_digest(self.component),
        }))
        self.archive = signing.package_component.package(
            self.root, repository="miyako/Text-to-SVG", tag="0.1.0")
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.env = {
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REPOSITORY": "miyako/Text-to-SVG", "GITHUB_REF": signing.PUBLISH_REF,
            "GITHUB_SHA": SHA, "RELEASE_SHA": SHA, "RELEASE_TAG": "0.1.0",
            "RUNNER_TEMP": str(self.runner),
            "APPLE_DEVELOPER_ID_CERTIFICATE": base64.b64encode(b"fake PKCS12").decode(),
            "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD": "fake-import-secret",
            "KEYCHAIN_PASSWORD": "fake-keychain-secret", "NOTARYTOOL_APPLE_ID": "fake@example.invalid",
            "NOTARYTOOL_PASSWORD": "fake-notary-secret", "NOTARYTOOL_TEAM_ID": TEAM,
        }
        self.calls = []
        self.fail_label = None
        self.submit_status = "Accepted"
        self.submit_exit = 0
        self.signature_team = TEAM
        self.timestamp = "Nov 4, 2026"
        self.entitlements = ""
        self.mutate_mount = False
        self.after_assessment = lambda: None
        self.ticket_transform = lambda log: log
        self.identity_output = f'  1) {FINGERPRINT} "{IDENTITY}"\n  1 valid identities found\n'

    def write(self, name, content, mode=0o644):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(mode)
        return path

    def fake_run(self, args, **kwargs):
        args = [str(a) for a in args]
        self.calls.append((args, kwargs))
        tool = args[0]
        out = err = ""
        status = 0
        if self.fail_label and self.fail_label in " ".join(args):
            return subprocess.CompletedProcess(args, 1, "", "injected failure")
        if tool == signing.GIT:
            out = SHA + "\n"
        else:
            private = self.runner / "Text-to-SVG-release-signing"
            self.assertEqual(kwargs["env"]["HOME"], str(private / "home"))
            self.assertEqual(kwargs["env"]["CFFIXED_USER_HOME"], str(private / "home"))
            for name in signing.SECRET_NAMES:
                self.assertNotIn(name, kwargs["env"])
            if tool == signing.SECURITY:
                if args[1] == "create-keychain":
                    Path(args[-1]).touch()
                elif args[1] == "delete-keychain":
                    Path(args[-1]).unlink(missing_ok=True)
                elif args[1] == "find-identity":
                    out = self.identity_output
                elif args[1] == "list-keychains":
                    self.assertEqual(args[2:5], ["-d", "user", "-s"])
                    self.assertEqual(args[-1], "/Library/Keychains/System.keychain")
                self.assertNotIn("default-keychain", args)
            elif tool == signing.CODESIGN:
                if "--sign" in args:
                    self.assertEqual(args[args.index("--sign") + 1], FINGERPRINT)
                    self.assertIn("--timestamp", args)
                    self.assertNotIn("--deep", args)
                    self.assertNotIn("--entitlements", args)
                    path = Path(args[-1])
                    path.write_bytes(path.read_bytes() + b"signed")
                elif "--verbose=4" in args:
                    out = (f"Authority={IDENTITY}\nTeamIdentifier={self.signature_team}\n"
                           f"Timestamp={self.timestamp}\nHash type=sha256 size=32\n"
                           f"CDHash={'c' * 40}\nCodeDirectory v=20500 flags=0x10000(runtime)\n")
                elif "--entitlements" in args:
                    out = self.entitlements
            elif tool == signing.HDIUTIL:
                if args[1] == "create":
                    self.payload = Path(args[args.index("-srcfolder") + 1])
                    Path(args[-1]).write_bytes(b"synthetic DMG")
                elif args[1] == "attach":
                    mount = Path(args[args.index("-mountpoint") + 1])
                    shutil.copytree(self.payload / "Text-to-SVG.4dbase", mount / "Text-to-SVG.4dbase")
                    if self.mutate_mount:
                        (mount / "Text-to-SVG.4dbase/Text-to-SVG.4DZ").write_bytes(b"changed")
                    out = plistlib.dumps({"system-entities": [{"mount-point": str(mount)}]}).decode()
            elif tool == signing.XCRUN:
                if args[1:3] == ["notarytool", "submit"]:
                    self.dmg = Path(args[3])
                    out = json.dumps({"id": SUBMISSION, "status": self.submit_status})
                    status = self.submit_exit
                elif args[1:3] == ["notarytool", "log"]:
                    tickets = [{"path": "Text-to-SVG.dmg", "arch": None,
                                "digestAlgorithm": "SHA-256", "cdhash": "c" * 40}]
                    for relative, arches in [
                        ("Libraries/lib4d-arm64.dylib", ["arm64"]),
                        ("Libraries/nested/helper", ["arm64"]),
                        ("Resources/bin/macOS/text-to-svg", ["arm64", "x86_64"]),
                    ]:
                        tickets.extend({"path": f"Text-to-SVG.dmg/Text-to-SVG.4dbase/{relative}",
                                        "arch": arch, "digestAlgorithm": "SHA-256", "cdhash": "c" * 40}
                                       for arch in arches)
                    log = {"logFormatVersion": 1, "jobId": SUBMISSION, "status": self.submit_status,
                           "statusCode": 0, "archiveFilename": self.dmg.name,
                           "sha256": signing.sha256(self.dmg), "issues": None, "ticketContents": tickets}
                    out = json.dumps(self.ticket_transform(log))
                elif args[1:3] == ["stapler", "staple"]:
                    path = Path(args[-1])
                    path.write_bytes(path.read_bytes() + b"stapled")
            elif tool == signing.SPCTL:
                if "open" in args:
                    self.after_assessment()
            else:
                self.fail("unexpected tool " + tool)
        return subprocess.CompletedProcess(args, status, out, err)

    def run_sign(self):
        with patch.object(signing.platform, "system", return_value="Darwin"), \
                patch.object(signing.subprocess, "run", side_effect=self.fake_run):
            return signing.sign_release(self.root, self.env, require_apple_signing=True)


class LifecycleTests(Fixture):
    def test_signed_final_zip_from_dmg_and_unchanged_compilation(self):
        original_zip = signing.sha256(self.archive)
        marker = (self.root / "build/compile-status.json").read_bytes()
        original = signing.snapshot(self.component)
        proof = self.run_sign()
        self.assertNotIn("candidate", proof)
        self.assertEqual(proof["releaseTag"], "0.1.0")
        self.assertEqual(proof["sourceSHA"], SHA)
        self.assertEqual(proof["unsignedZIPHashSHA256"], original_zip)
        self.assertEqual(proof["unsignedCompileInputSHA256"], input_digest(self.root))
        self.assertEqual(proof["zip"]["origin"], "final-mounted-accepted-DMG")
        self.assertFalse(proof["zip"]["stapled"])
        self.assertEqual(proof["zip"]["sha256"], signing.sha256(self.archive))
        self.assertEqual(proof["dmg"]["sha256"], signing.sha256(self.root / "dist/Text-to-SVG.dmg"))
        self.assertEqual(proof["notarization"]["submissionUUID"], SUBMISSION)
        self.assertEqual(len(proof["notarization"]["ticketContents"]), 5)
        self.assertEqual(marker, (self.root / "build/compile-status.json").read_bytes())
        self.assertEqual(original, signing.snapshot(self.component))
        self.assertFalse(signing.private_directory(self.env).exists())
        with zipfile.ZipFile(self.archive) as archive:
            for item in proof["signedFiles"]:
                path = "Text-to-SVG.4dbase/" + item["path"]
                self.assertEqual(signing.hashlib.sha256(archive.read(path)).hexdigest(), item["sha256"])
            inner = zipfile.ZipFile(io.BytesIO(archive.read("Text-to-SVG.4dbase/Text-to-SVG.4DZ")))
            self.assertFalse(any(name.endswith(".4dm") for name in inner.namelist()))
            self.assertTrue(any(name.endswith("MX64 123") for name in inner.namelist()))
        signing_args = [args for args, _ in self.calls if "--sign" in args]
        self.assertTrue(signing_args[0][-1].endswith("Libraries/nested/helper"))
        self.assertTrue(signing_args[-2][-1].endswith("Resources/bin/macOS/text-to-svg"))
        self.assertTrue(all(not any("refs/tags/" in a for a in args) for args, _ in self.calls))
        notarized = [args for args, _ in self.calls if "--check-notarization" in args]
        self.assertEqual(len(notarized), len(proof["signedFiles"]))
        self.assertTrue(all(args[0] == signing.CODESIGN and "-R=notarized" in args
                            and "--all-architectures" in args for args in notarized))
        self.assertFalse(any(args[0] == signing.SPCTL and "execute" in args
                             for args, _ in self.calls))
        self.assertTrue(any(args[0] == signing.SPCTL and "context:primary-signature" in args
                            for args, _ in self.calls))

    def test_signing_requires_explicit_flag(self):
        with patch.object(signing.platform, "system", return_value="Darwin"), \
                patch.object(signing.subprocess, "run", side_effect=self.fake_run), \
                self.assertRaisesRegex(signing.SigningError, "require-apple-signing"):
            signing.sign_release(self.root, self.env)

    def test_unsigned_zip_must_exactly_match_compiled_package(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(signing.SigningError, "unsigned ZIP"):
            self.run_sign()
        self.assertFalse(any(args[0] == signing.SECURITY for args, _ in self.calls))

    def test_stale_compile_marker_rejected(self):
        self.write("component/Text-to-SVG/Resources/changed.txt", "new input")
        with self.assertRaisesRegex(ValueError, "input_sha256"):
            self.run_sign()

    def test_wrong_version_rejected_before_credentials(self):
        self.env["RELEASE_TAG"] = "v9.9.9"
        with self.assertRaises(ValueError):
            self.run_sign()
        self.assertFalse(any(args[0] == signing.SECURITY for args, _ in self.calls))

    def test_failures_cleanup_keychain_and_preserve_unsigned_zip(self):
        original = self.archive.read_bytes()
        for operation in ("import", "--sign", "notarytool submit", "stapler staple",
                          "--check-notarization", "--assess", "attach"):
            with self.subTest(operation=operation):
                self.fail_label = operation
                with self.assertRaises(signing.SigningError):
                    self.run_sign()
                self.assertFalse(signing.private_directory(self.env).exists())
                self.assertEqual(original, self.archive.read_bytes())
                self.assertTrue(any(args[1] == "delete-keychain" for args, _ in self.calls
                                    if args[0] == signing.SECURITY))
                self.calls.clear()

    def test_team_and_timestamp_signature_fail_closed(self):
        for team, timestamp in (("WRONG12345", self.timestamp), (TEAM, "none"), (TEAM, "not set")):
            with self.subTest(team=team, timestamp=timestamp):
                self.signature_team, self.timestamp = team, timestamp
                with self.assertRaisesRegex(signing.SigningError, "signature display"):
                    self.run_sign()
                self.assertFalse(signing.private_directory(self.env).exists())

    def test_notary_nonzero_success_shaped_response_fails(self):
        self.submit_exit = 1
        with self.assertRaisesRegex(signing.SigningError, "not successfully Accepted"):
            self.run_sign()
        self.assertTrue((self.root / "build/signing-diagnostics/notary-log.json").is_file())
        self.assertFalse(any(args[1:3] == ["stapler", "staple"] for args, _ in self.calls))

    def test_rejection_retains_redacted_logs(self):
        self.submit_status = "Invalid"
        def issues(log):
            log["issues"] = [{"message": self.env["NOTARYTOOL_PASSWORD"] +
                              " https://example.invalid/private?signed=credential"}]
            return log
        self.ticket_transform = issues
        with self.assertRaisesRegex(signing.SigningError, "not successfully Accepted"):
            self.run_sign()
        text = (self.root / "build/signing-diagnostics/notary-log.json").read_text()
        self.assertNotIn(self.env["NOTARYTOOL_PASSWORD"], text)
        self.assertNotIn("https://", text)
        self.assertIn("[REDACTED", text)

    def test_missing_library_ticket_blocks_delivery(self):
        def missing(log):
            log["ticketContents"].pop(1)
            return log
        self.ticket_transform = missing
        with self.assertRaisesRegex(signing.SigningError, "ticket coverage"):
            self.run_sign()

    def test_notary_log_status_checksum_architecture_and_cdhash_are_bound(self):
        transformations = [
            lambda log: dict(log, sha256="d" * 64),
            lambda log: dict(log, jobId="00000000-0000-0000-0000-000000000000"),
            lambda log: dict(log, archiveFilename="unrelated.dmg"),
            lambda log: dict(log, statusCode=True),
            lambda log: dict(log, issues=[{"message": "warning is not clean acceptance"}]),
            lambda log: dict(log, ticketContents=[]),
        ]
        def wrong_slice(log):
            log["ticketContents"][-1]["arch"] = "arm64"
            return log
        def wrong_cdhash(log):
            log["ticketContents"][1]["cdhash"] = "d" * 40
            return log
        def bad_algorithm(log):
            log["ticketContents"][1]["digestAlgorithm"] = "SHA-1"
            return log
        for transform in transformations + [wrong_slice, wrong_cdhash, bad_algorithm]:
            self.ticket_transform = transform
            with self.subTest(transform=transform), self.assertRaises(signing.SigningError):
                self.run_sign()
            self.assertFalse(signing.private_directory(self.env).exists())
            self.assertFalse(any(args[1:3] == ["stapler", "staple"] for args, _ in self.calls))
            self.calls.clear()

    def test_cli_and_libraries_do_not_gain_unnecessary_entitlements(self):
        self.entitlements = plistlib.dumps({"com.apple.security.cs.disable-library-validation": True}).decode()
        with self.assertRaisesRegex(signing.SigningError, "unexpected code entitlements"):
            self.run_sign()

    def test_final_mounted_content_must_equal_signed_payload(self):
        self.mutate_mount = True
        unsigned = self.archive.read_bytes()
        with self.assertRaisesRegex(signing.SigningError, "immutable package content"):
            self.run_sign()
        self.assertEqual(self.archive.read_bytes(), unsigned)
        self.assertTrue(any(args[1] == "detach" for args, _ in self.calls if args[0] == signing.HDIUTIL))
        self.assertFalse(signing.private_directory(self.env).exists())

    def test_changed_unsigned_input_or_compile_marker_during_signing_rejected(self):
        original_zip = self.archive.read_bytes()
        marker = self.root / "build/compile-status.json"
        original_marker = marker.read_bytes()
        for path, content, message in (
            (self.archive, original_zip + b"changed", "input ZIP changed"),
            (marker, original_marker + b" ", "compilation marker changed"),
        ):
            with self.subTest(path=path):
                self.after_assessment = lambda: path.write_bytes(content)
                with self.assertRaisesRegex(signing.SigningError, message):
                    self.run_sign()
                self.assertFalse((self.root / "dist/Text-to-SVG-signing.json").exists())
                self.archive.write_bytes(original_zip)
                marker.write_bytes(original_marker)

    def test_missing_or_ambiguous_identity_cleanup(self):
        for text in ("0 valid identities found", self.identity_output + self.identity_output):
            self.identity_output = text
            with self.assertRaisesRegex(signing.SigningError, "exactly one"):
                self.run_sign()
            self.assertFalse(signing.private_directory(self.env).exists())

    def test_all_actual_libraries_only_and_inside_out(self):
        paths = signing.code_inventory(self.component, "text-to-svg")
        self.assertEqual([p.relative_to(self.component).as_posix() for p in paths], [
            "Libraries/nested/helper", "Libraries/lib4d-arm64.dylib",
            "Resources/bin/macOS/text-to-svg"])
        self.write("component/Text-to-SVG/Libraries/not-code.dylib", "invalid")
        with self.assertRaisesRegex(signing.SigningError, "not Mach-O"):
            signing.code_inventory(self.component, "text-to-svg")

    def test_cleanup_is_credential_free_isolated_and_idempotent(self):
        private = signing.private_directory(self.env)
        self.write("runner/Text-to-SVG-release-signing/home/Library/Preferences/state", "{}")
        self.write("runner/Text-to-SVG-release-signing/release.keychain-db", "keychain")
        with patch.object(signing.subprocess, "run", side_effect=self.fake_run):
            signing.cleanup_release(self.root, {"RUNNER_TEMP": str(self.runner)})
            signing.cleanup_release(self.root, {"RUNNER_TEMP": str(self.runner)})
        self.assertFalse(private.exists())
        self.assertEqual(len(self.calls), 1)

    def test_cleanup_requires_runner_temp_and_rejects_symlink(self):
        with self.assertRaisesRegex(signing.SigningError, "RUNNER_TEMP"):
            signing.cleanup_release(self.root, {})
        target = self.root / "keep"
        target.mkdir()
        signing.private_directory(self.env).symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(signing.SigningError, "symlink"):
            signing.cleanup_release(self.root, self.env)
        self.assertTrue(target.is_dir())

    def test_cleanup_does_not_follow_preferences_symlink(self):
        home = self.runner / "Text-to-SVG-release-signing/home"
        (home / "Library").mkdir(parents=True)
        target = self.root / "keep"
        target.mkdir()
        (home / "Library/Preferences").symlink_to(target, target_is_directory=True)
        with patch.object(signing.subprocess, "run") as run:
            with self.assertRaisesRegex(signing.SigningError, "symlink"):
                signing.cleanup_release(self.root, self.env)
            run.assert_not_called()
        self.assertTrue(target.is_dir())

    def test_cleanup_command_needs_no_signing_secrets(self):
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.runner)}, clear=True):
            self.assertEqual(signing.main(["cleanup", "--root", str(self.root)]), 0)
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(signing.main(["cleanup", "--root", str(self.root)]), 1)

    def test_existing_workspace_is_not_removed_by_concurrent_signer(self):
        scratch = signing.private_directory(self.env)
        scratch.mkdir()
        (scratch / "sentinel").write_text("active signing session")
        with self.assertRaises(FileExistsError):
            self.run_sign()
        self.assertTrue((scratch / "sentinel").is_file())

    def test_cli_failure_retains_only_redacted_diagnostic(self):
        secret = self.env["NOTARYTOOL_PASSWORD"]
        with patch.dict(os.environ, self.env), patch.object(
                signing, "sign_release", side_effect=signing.SigningError("failed " + secret)), patch(
                "sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(signing.main(["sign", "--root", str(self.root)]), 1)
        diagnostic = (self.root / "build/signing-diagnostics/signing-error.txt").read_text()
        self.assertIn("[REDACTED]", diagnostic)
        self.assertNotIn(secret, diagnostic + stderr.getvalue())

    def test_six_credentials_no_explicit_identity(self):
        self.assertEqual(len(signing.SECRET_NAMES), 6)
        self.assertNotIn("CODESIGN_APPLE_ID", signing.credentials(self.env))
        for name in signing.SECRET_NAMES:
            env = dict(self.env)
            env.pop(name)
            with self.subTest(name=name), self.assertRaisesRegex(signing.SigningError, "missing required"):
                signing.credentials(env)
        for value in ("", "!!!", " "):
            secrets = dict(self.env, APPLE_DEVELOPER_ID_CERTIFICATE=value)
            with self.assertRaises(signing.SigningError):
                signing.certificate_bytes(secrets)

    def test_publish_context_rejects_pr_branch_tag_and_sha_mismatches(self):
        commands = signing.Commands(self.env, {})
        for changes in (
            {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "pull_request_target"},
            {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_REF": "refs/heads/feature"},
            {"GITHUB_REF": "refs/tags/0.1.0"}, {"RELEASE_SHA": "b" * 40},
            {"RELEASE_TAG": "v0.1.0"}, {"RELEASE_TAG": ""},
        ):
            with self.subTest(changes=changes):
                env = dict(self.env, **changes)
                with patch.object(signing.subprocess, "run", side_effect=self.fake_run), \
                        self.assertRaises(signing.SigningError):
                    signing.trusted_context(self.root, env, commands)

    def test_no_non_github_or_non_mac_signing(self):
        with patch.object(signing.platform, "system", return_value="Linux"), \
                self.assertRaisesRegex(signing.SigningError, "macOS GitHub"):
            signing.sign_release(self.root, self.env)


class HelperTests(unittest.TestCase):
    def test_identity_filters_team_type_and_ambiguity(self):
        line = f'1) {FINGERPRINT} "{IDENTITY}"'
        extra = f'2) {"a" * 40} "Apple Development: Other ({TEAM})"'
        self.assertEqual(signing.select_identity(line + "\n" + extra, TEAM), (FINGERPRINT, IDENTITY))
        for text in (extra, line.replace(TEAM, "ZZZZ123456"), line + "\n" + line):
            with self.assertRaises(signing.SigningError):
                signing.select_identity(text, TEAM)

    def test_command_diagnostics_redact_secrets_and_urls(self):
        secrets = {"password": 'private"secret'}
        result = subprocess.CompletedProcess([], 1, secrets["password"],
                                             "https://example.invalid/signed?token=secret")
        error = signing.CommandError("failure", result, secrets)
        self.assertNotIn("private", str(error))
        self.assertNotIn("https://", str(error))
        self.assertNotIn("example.invalid", signing.redact(
            r'{"url":"https:\/\/example.invalid\/signed?token=secret"}', {}))
        with patch.object(signing.subprocess, "run", side_effect=subprocess.TimeoutExpired(["secret"], 1)):
            with self.assertRaisesRegex(signing.SigningError, "timed out") as caught:
                signing.Commands({}, secrets).run("tool", [signing.SECURITY])
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(signing.SigningError, "untrusted"):
            signing.Commands({}, {}).run("shell", ["/bin/sh", "-c", "anything"])

    def test_staple_retries_only_bounded_transient_network_failures(self):
        commands = signing.Commands({}, {})
        network = signing.CommandError("staple", subprocess.CompletedProcess(
            [], 68, "", "Error Domain=NSURLErrorDomain Code=-1009"), {})
        with patch.object(commands, "run", side_effect=[network, network, ("", "")]) as run, \
                patch.object(signing.time, "sleep") as sleep:
            signing.staple(commands, Path("artifact.dmg"))
            self.assertEqual(run.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])
        with patch.object(commands, "run", side_effect=network) as run, patch.object(signing.time, "sleep"):
            with self.assertRaises(signing.SigningError):
                signing.staple(commands, Path("artifact.dmg"))
            self.assertEqual(run.call_count, 3)
        permanent = signing.CommandError("staple", subprocess.CompletedProcess(
            [], 68, "", "ticket not found"), {})
        with patch.object(commands, "run", side_effect=permanent) as run:
            with self.assertRaises(signing.SigningError):
                signing.staple(commands, Path("artifact.dmg"))
            self.assertEqual(run.call_count, 1)

    def test_uuid_strictness(self):
        self.assertEqual(signing.submission_uuid(SUBMISSION), SUBMISSION)
        for value in (None, {}, "", "123", SUBMISSION.replace("-", "")):
            with self.assertRaises(signing.SigningError):
                signing.submission_uuid(value)

    def test_malformed_ticket_log_fails_closed(self):
        # No filesystem access occurs for these schema errors.
        for value in (None, [], {}, {"logFormatVersion": True},
                      {"logFormatVersion": 1, "jobId": 123}):
            with self.subTest(value=value), self.assertRaises(signing.SigningError):
                signing.validate_ticket_log(value, Path("artifact.dmg"), SUBMISSION, {})

    def test_signature_requires_runtime_sha256_cdhash_and_correct_authority(self):
        good = (f"Authority={IDENTITY}\nTeamIdentifier={TEAM}\nTimestamp=Nov 4, 2026\n"
                f"Hash type=sha256 size=32\nCDHash={'c' * 40}\n"
                "CodeDirectory v=20500 flags=0x10000(runtime)\n")
        commands = signing.Commands({}, {})
        for display in (
            good.replace("0x10000(runtime)", "0x0(none)"),
            good.replace("0x10000(runtime)", "0x0(runtime)"),
            good.replace("sha256", "sha1"),
            good.replace("c" * 40, "invalid"),
            good.replace(IDENTITY, f"Developer ID Application: Wrong ({TEAM})"),
        ):
            with self.subTest(display=display), patch.object(
                    commands, "run", side_effect=[("", ""), (display, "")]):
                with self.assertRaisesRegex(signing.SigningError, "signature display"):
                    signing.signature(commands, Path("code"), TEAM, architectures=["arm64"],
                                      identity=IDENTITY)


if __name__ == "__main__":
    unittest.main()
