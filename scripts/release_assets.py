#!/usr/bin/env python3
"""Verify exact accepted release assets; --publish creates the new bare tag and GitHub release."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

from build_provenance import commit, sha256, source_digest
from ci_config import ROOT, SEMVER, TARGETS, load_config, release_tag_matches
from sign_release import submission_uuid
from test_4d import _read_json, validate_status

# Adapted from miyako/4d-component-template (MIT), immutable revision
# 96da0b4a0f628536b906855a03f2a54ec6cdb002.


def digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is not None


def regular(path, *, missing=False, directory=False):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("release paths must not traverse symlinks")
    if missing and not path.exists():
        return path
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("regular release file/directory required: " + path.name)
    return path


def asset_record(root, path):
    path = regular(path)
    if path.stat().st_size <= 0:
        raise ValueError("release asset is empty: " + path.name)
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256(path),
            "bytes": path.stat().st_size}


def check_compile(status, config):
    if (not isinstance(status, dict) or status.get("success") is not True
            or status.get("errors") != [] or not isinstance(status.get("targets"), list)
            or len(status["targets"]) != 2
            or set(status["targets"]) != {"x86_64_generic", "arm64_macOS_lib"}
            or any(not digest(status.get(field)) for field in ("input_sha256", "output_sha256"))):
        raise ValueError("clean compiler status, both targets and input/output provenance are required")
    project = status.get("project")
    if (not isinstance(project, str) or "\\" in project
            or not project.endswith("/component/" + config["repository"]
                                    + "/Project/" + config["repository"] + ".4DProject")):
        raise ValueError("compiler status must identify the configured project")
    return status


def signed_archive_files(root, config):
    """Read canonical native payload entries, rejecting aliases and ZIP symlinks."""
    archive = regular(Path(root) / "dist" / (config["repository"] + ".zip"))
    prefix = config["repository"] + ".4dbase/"
    cli = "Resources/bin/macOS/" + config["cli"]
    result = {}
    seen = set()
    with zipfile.ZipFile(archive) as packed:
        for entry in packed.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (not entry.filename.startswith(prefix) or path.is_absolute()
                    or any(token in ("", ".", "..") for token in entry.filename.rstrip("/").split("/"))
                    or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or entry.filename in seen):
                raise ValueError("unsafe or duplicate signed archive path")
            seen.add(entry.filename)
            relative = entry.filename[len(prefix):]
            if entry.is_dir():
                continue
            content = packed.read(entry)
            macho = content[:4] in {
                b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
            }
            if relative == cli or relative.startswith("Libraries/") and (
                    macho or Path(relative).suffix in {".dylib", ".so"}):
                if not content or not macho:
                    raise ValueError("signed CLI/native Library must be a nonempty Mach-O")
                result[relative] = hashlib.sha256(content).hexdigest()
    if not {cli, "Libraries/lib4d-arm64.dylib"} <= result.keys():
        raise ValueError("signed archive requires the configured CLI and native Libraries")
    return result


def verify_signing_proof(root, config, revision, compile_input, unsigned_hash, assets):
    if not digest(revision, 40) or not digest(compile_input) or not digest(unsigned_hash):
        raise ValueError("signing proof requires normalized source and unsigned compilation provenance")
    proof_path = regular(Path(root) / "dist" / (config["repository"] + "-signing.json"))
    proof = _read_json(proof_path)
    hashes = {asset["path"]: asset["sha256"] for asset in assets}
    prefix = "dist/" + config["repository"]
    if hashes.get(prefix + "-signing.json") != sha256(proof_path):
        raise ValueError("signing proof bytes differ from the accepted asset")
    if (not isinstance(proof, dict) or type(proof.get("schema")) is not int or proof["schema"] != 1
            or proof.get("version") != config["version"] or proof.get("sourceSHA") != revision
            or not release_tag_matches(proof.get("releaseTag"), config["version"])
            or proof.get("unsignedCompileInputSHA256") != compile_input
            or proof.get("unsignedZIPHashSHA256") != unsigned_hash
            or proof.get("hostValidation") != "required-after-signing"):
        raise ValueError("signing proof differs from the accepted release provenance")
    notary, dmg, archive = (proof.get(name) for name in ("notarization", "dmg", "zip"))
    if (not isinstance(notary, dict) or notary.get("status") != "Accepted"
            or not isinstance(dmg, dict) or dmg.get("stapled") is not True
            or dmg.get("validated") is not True or dmg.get("sha256") != hashes.get(prefix + ".dmg")
            or not isinstance(archive, dict) or archive.get("sha256") != hashes.get(prefix + ".zip")
            or archive.get("stapled") is not False or archive.get("notarizedViaDMG") is not True
            or archive.get("origin") != "final-mounted-accepted-DMG"):
        raise ValueError("Accepted notarization, stapled DMG and final-mounted ZIP hashes are required")
    submission_uuid(notary.get("submissionUUID"))
    signed = proof.get("signedFiles")
    if (not isinstance(signed, list) or not signed
            or any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                   or entry.get("verified") is not True for entry in signed)):
        raise ValueError("verified signedFiles are required")
    paths = [entry["path"] for entry in signed]
    actual = signed_archive_files(root, config)
    if len(set(paths)) != len(paths) or set(paths) != set(actual):
        raise ValueError("signedFiles must match actual canonical CLI and native Library paths")
    team = None
    expected_tickets = {}
    for entry in signed:
        if entry.get("sha256") != actual[entry["path"]]:
            raise ValueError("signedFiles hash differs from final ZIP")
        slices = entry.get("signatures")
        architectures = {"x86_64", "arm64"} if entry["path"].startswith("Resources/") else {"arm64"}
        if not isinstance(slices, dict) or set(slices) != architectures:
            raise ValueError("signing proof must verify every native code architecture")
        for architecture, receipt in slices.items():
            if not isinstance(receipt, dict):
                raise ValueError("signing proof signature receipt is missing")
            current = receipt.get("team")
            authority = receipt.get("authority")
            timestamp = receipt.get("timestamp")
            if (not isinstance(current, str) or not re.fullmatch(r"[A-Z0-9]{10}", current)
                    or (team is not None and current != team)
                    or not isinstance(authority, str) or not authority.startswith("Developer ID Application: ")
                    or not authority.endswith(" (" + current + ")")
                    or not isinstance(timestamp, str) or not timestamp.strip()
                    or timestamp.strip().lower() in {"none", "not set"}
                    or receipt.get("hardenedRuntime") is not True or not digest(receipt.get("cdhash"), 40)):
                raise ValueError("signing proof requires one Developer ID team, timestamp and hardened runtime")
            team = current
            expected_tickets[(
                config["repository"] + ".dmg/" + config["repository"] + ".4dbase/" + entry["path"],
                architecture)] = receipt["cdhash"]
    if proof.get("assessments") != {"codeNotarization": "passed", "dmgOpen": "passed"}:
        raise ValueError("signing proof requires online code notarization and DMG Gatekeeper assessment")
    tickets = notary.get("ticketContents")
    if not isinstance(tickets, list):
        raise ValueError("Accepted notary ticket coverage is required")
    observed = {}
    container = config["repository"] + ".dmg"
    for ticket in tickets:
        if (not isinstance(ticket, dict) or not isinstance(ticket.get("path"), str)
                or ticket.get("arch") not in (None, "arm64", "x86_64")
                or ticket.get("digestAlgorithm") != "SHA-256" or not digest(ticket.get("cdhash"), 40)):
            raise ValueError("malformed notarization ticket")
        key = ticket["path"], ticket.get("arch")
        if key in observed:
            raise ValueError("duplicate notarization ticket")
        observed[key] = ticket["cdhash"]
    container_hash = observed.pop((container, None), None)
    if not digest(container_hash, 40) or observed != expected_tickets:
        raise ValueError("Accepted ticket coverage must bind all code slices and the DMG")
    return proof


def verify_signed_payload(root, config, component, expected_team=None):
    """Reverify final ZIP bytes, independent of the signer's staging directory."""
    files = signed_archive_files(root, config)
    proof = _read_json(regular(Path(root) / "dist" / (config["repository"] + "-signing.json")))
    receipts = {entry["path"]: entry["signatures"] for entry in proof["signedFiles"]}
    component = regular(component, directory=True)
    team = expected_team
    entries = []
    for relative, expected_hash in files.items():
        path = regular(component / relative)
        if not path.resolve().is_relative_to(component.resolve()) or sha256(path) != expected_hash:
            raise ValueError("signed payload realpath/hash differs from final archive")
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", str(path)],
                       capture_output=True, check=True, timeout=60)
        cli = relative == "Resources/bin/macOS/" + config["cli"]
        architectures = ["x86_64", "arm64"] if cli else ["arm64"]
        if cli:
            subprocess.run(["/usr/bin/lipo", str(path), "-verify_arch", *architectures],
                           capture_output=True, check=True, timeout=30)
        for architecture in architectures:
            display = subprocess.run(
                ["/usr/bin/codesign", "--display", "--verbose=4", "--arch", architecture, str(path)],
                capture_output=True, check=True, text=True, timeout=30)
            text = display.stdout + "\n" + display.stderr
            actual = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", text, re.MULTILINE)
            authority = re.search(r"^Authority=(Developer ID Application: .+)$", text, re.MULTILINE)
            timestamp = re.search(r"^Timestamp=(.+)$", text, re.MULTILINE)
            cdhash = re.search(r"^CDHash=([0-9a-f]{40})$", text, re.MULTILINE)
            if team is None and actual:
                team = actual[1]
            if (not actual or actual[1] != team or not authority
                    or not authority[1].endswith(" (" + team + ")")
                    or not timestamp or not timestamp[1].strip()
                    or timestamp[1].strip().lower() in {"none", "not set"}
                    or not cdhash
                    or not re.search(r"^CodeDirectory .*flags=.*\(.*\bruntime\b.*\)", text, re.MULTILINE)
                    or re.search(r"^Signature=adhoc$", text, re.MULTILINE)):
                raise ValueError("final signatures require the expected Developer ID team, timestamp and runtime")
            receipt = receipts[relative][architecture]
            # The full file hash binds timestamp bytes; display text varies by locale/time zone.
            if (receipt["team"] != team or receipt["authority"] != authority[1]
                    or receipt["cdhash"] != cdhash[1]):
                raise ValueError("final signature differs from the accepted signing proof")
        entries.append({"path": relative, "sha256": expected_hash, "verified": True,
                        "architectures": architectures})
    return {"team": team, "signedFiles": entries}


def verify_assets(root, repository, tag, revision, apple_signing=False):
    root = regular(root, directory=True).resolve()
    config = load_config(root, initialized=True, repository=repository, tag=tag)
    if not digest(revision, 40) or commit(root) != revision:
        raise ValueError("publication checkout must match the normalized release SHA")
    evidence = regular(root / "dist" / (config["repository"] + ".acceptance.json"))
    report = _read_json(evidence)
    if (not isinstance(report, dict) or type(report.get("schema")) is not int or report["schema"] != 1
            or report.get("finished") is not True or report.get("result") != "partial"
            or report.get("release_ready") is not False or report.get("scope") != "hosted"
            or report.get("commit") != revision or report.get("configuration") != config
            or report.get("require_apple_signing") is not apple_signing):
        raise ValueError("acceptance evidence must identify the exact completed release inputs and policy")
    steps = report.get("steps")
    if not isinstance(steps, dict):
        raise ValueError("acceptance steps are missing")

    def passed(name):
        step = steps.get(name)
        if not isinstance(step, dict) or step.get("status") != "passed":
            raise ValueError("required acceptance step did not pass: " + name)
        return step.get("details")

    contract = _read_json(root / "tests/host/contract.json")
    validate_status(passed("source_host"), contract, False)
    validate_status(passed("unsigned_compiled_host"), contract, True)
    validate_status(passed("compiled_host"), contract, True)
    for name in ("assembly", "package", "integrity"):
        passed(name)
    dependencies = passed("dependencies")
    if (not isinstance(dependencies, dict) or any(
            not isinstance(dependencies.get(name), dict)
            or dependencies[name].get("status") != "passed"
            or dependencies[name].get("target") != target
            or not isinstance(dependencies[name].get("dependencies"), list)
            for name, target in (("macOS", "macos-arm64"), ("Windows", "windows-x64")))):
        raise ValueError("actual macOS and Windows native import inspection is required")
    compiled = check_compile(passed("compile"), config)
    if report.get("unsigned_compile") != compiled:
        raise ValueError("pre-sign compiler input/output provenance must be preserved")
    source = source_digest(root)
    for target in TARGETS:
        manifest = passed("cli_test_" + target)
        if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
                or manifest["schema"] != 1 or manifest.get("target") != target
                or manifest.get("execution") != "passed" or manifest.get("commit") != revision
                or manifest.get("source_sha256") != source or not digest(manifest.get("binary_sha256"))
                or not isinstance(manifest.get("toolchain"), str) or not manifest["toolchain"]):
            raise ValueError("native build evidence differs from the release source: " + target)
    tool = report.get("tool4d")
    if (not isinstance(tool, dict) or not isinstance(tool.get("version"), str)
            or not tool["version"].strip() or not digest(tool.get("binary_sha256"))):
        raise ValueError("tool4d identity is required")
    expected = ["dist/" + config["repository"] + ".zip"]
    if apple_signing:
        passed("signing")
        passed("signing_proof")
        expected += ["dist/" + config["repository"] + ".dmg",
                     "dist/" + config["repository"] + "-signing.json"]
    elif any(not isinstance(steps.get(name), dict) or steps[name].get("status") != "not_run"
             for name in ("signing", "signing_proof", "apple_signatures")):
        raise ValueError("unsigned publication must not conceal signing evidence")
    assets = report.get("assets")
    if (not isinstance(assets, list) or len(assets) != len(expected)
            or any(not isinstance(asset, dict) for asset in assets)
            or [asset.get("path") for asset in assets] != expected or report.get("artifact") != assets[0]):
        raise ValueError("acceptance evidence must list the exact policy-required assets")
    paths = []
    for asset in assets:
        path = root / asset["path"]
        if (type(asset.get("bytes")) is not int or asset["bytes"] <= 0
                or not digest(asset.get("sha256")) or asset_record(root, path) != asset):
            raise ValueError("downloaded asset differs from the accepted bytes: " + path.name)
        paths.append(path)
    unsigned = report.get("unsigned_artifact")
    if (not isinstance(unsigned, dict) or unsigned.get("path") != expected[0]
            or not digest(unsigned.get("sha256")) or type(unsigned.get("bytes")) is not int
            or unsigned["bytes"] <= 0):
        raise ValueError("unsigned ZIP provenance is required")
    if apple_signing:
        proof = verify_signing_proof(
            root, config, revision, compiled["input_sha256"], unsigned["sha256"], assets)
        if passed("signing_proof") != proof:
            raise ValueError("accepted signing proof differs from the downloaded proof")
        signatures = passed("apple_signatures")
        files = signed_archive_files(root, config)
        if (not isinstance(signatures, dict) or not isinstance(signatures.get("team"), str)
                or not re.fullmatch(r"[A-Z0-9]{10}", signatures["team"])
                or not isinstance(signatures.get("signedFiles"), list)):
            raise ValueError("verified final Developer ID team/signatures are required")
        entries = signatures["signedFiles"]
        if (len(entries) != len(files) or any(
                not isinstance(entry, dict) or entry.get("path") not in files
                or entry.get("sha256") != files[entry["path"]] or entry.get("verified") is not True
                or entry.get("architectures") != (
                    ["x86_64", "arm64"] if entry["path"].startswith("Resources/") else ["arm64"])
                for entry in entries) or len({entry["path"] for entry in entries}) != len(files)):
            raise ValueError("verified signatures differ from final ZIP native file bytes")
        teams = {receipt["team"] for entry in proof["signedFiles"]
                 for receipt in entry["signatures"].values()}
        if teams != {signatures["team"]}:
            raise ValueError("signing proof team differs from verified final signatures")
    elif unsigned != assets[0]:
        raise ValueError("unsigned ZIP differs from the host-tested package")
    if {path.name for path in evidence.parent.iterdir()} != {path.name for path in (*paths, evidence)}:
        raise ValueError("release artifact directory contains unvalidated assets")
    return [*paths, evidence]


def publish(root, repository, tag, revision, apple_signing=False):
    if not isinstance(tag, str) or not re.fullmatch(SEMVER, tag, flags=re.ASCII):
        raise ValueError("new release tags are bare <SemVer> without a v prefix")
    assets = verify_assets(root, repository, tag, revision, apple_signing)
    remote = subprocess.run(["git", "ls-remote", "--tags", "origin", "refs/tags/" + tag],
                            cwd=root, check=True, capture_output=True, text=True, timeout=60)
    if remote.stdout.strip():
        raise ValueError("release tag already exists; Publish never moves or reuses a tag")
    notes = (
        f"## Text-to-SVG {tag}\n\n"
        f"Source: `{revision}`.\n\n"
        "Download **Text-to-SVG.zip**, unpack it, and place **Text-to-SVG.4dbase** "
        "in the host project's **Components** folder (4D 20.7 LTS minimum). "
        "The exact ZIP name is the dependency-manager asset identity.\n\n"
        "Compiled component; the exact final ZIP passed compiled-host acceptance on macOS 15 ARM64. "
        + ("Developer ID signed macOS code; notarized, stapled DMG. ZIP copied from the final mounted DMG. "
           if apple_signing else "Unsigned package. ")
        + "Adjacent evidence binds final bytes, source, native CLI manifests and host reports. "
        "Native Intel/Windows 4D loading and Windows Authenticode are `not_run`.")
    subprocess.run(["gh", "release", "create", tag, *(str(path) for path in assets),
                    "--repo", repository, "--target", revision, "--latest",
                    "--title", tag, "--notes", notes],
                   cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--require-apple-signing", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="create the bare tag and GitHub release from verified assets; default only verifies")
    args = parser.parse_args()
    try:
        if args.publish:
            publish(args.root, args.repository, args.tag, args.sha, args.require_apple_signing)
        else:
            for asset in verify_assets(args.root, args.repository, args.tag, args.sha,
                                       args.require_apple_signing):
                print(asset)
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile):
        parser.exit(2, "release asset verification/publication failed\n")


if __name__ == "__main__":
    main()
