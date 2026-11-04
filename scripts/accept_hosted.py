#!/usr/bin/env python3
"""Build hosted acceptance evidence for the exact final ZIP; never publish or create tags."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from build_provenance import commit, sha256, verify
from ci_config import ROOT, TARGETS, assemble, load_config, require_tool4d_host
from compile_component import compile_component
from native_dependencies import inspect
from package_component import package, unpack_for_test, validate_compile
from release_assets import (asset_record, check_compile, digest, regular, verify_signing_proof,
                            verify_signed_payload)
from sign_release import release_policy
from test_4d import _read_json, test_4d, validate_status

# Adapted from miyako/4d-component-template (MIT), immutable revision
# 96da0b4a0f628536b906855a03f2a54ec6cdb002; no feature fixtures are imported.


def accept(root, repository, tool4d, tag=None, require_apple_signing=False):
    root = Path(root).absolute()
    regular(root, directory=True)
    root = root.resolve()
    output = root / "build/hosted-acceptance.json"
    regular(output, missing=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    names = [*("cli_test_" + target for target in TARGETS), "assembly", "dependencies",
             "source_host", "compile", "package", "unsigned_compiled_host", "signing",
             "signing_proof", "apple_signatures", "compiled_host",
             "integrity", "native_intel_4d", "windows_4d", "release_download"]
    report = {
        "schema": 1, "scope": "hosted", "commit": commit(root),
        "host": {"system": platform.system(), "machine": platform.machine()},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "result": "failed", "finished": False, "release_ready": False,
        "steps": {name: {"status": "not_run", "reason": "not exercised"} for name in names},
    }
    active = "configuration"

    def save():
        regular(output, missing=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def step(name, action):
        nonlocal active
        active = name
        report["steps"][name] = {"status": "failed", "reason": "step did not complete"}
        save()
        value = action()
        report["steps"][name] = {"status": "passed"}
        if isinstance(value, dict):
            report["steps"][name]["details"] = value
        save()
        return value

    save()
    try:
        config = load_config(root, initialized=True, repository=repository, tag=tag)
        report["configuration"] = config
        signing = require_apple_signing or release_policy(root)
        report["require_apple_signing"] = signing
        report["candidate"] = os.environ.get("CANDIDATE_MODE", "").lower() == "true"
        revision = report["commit"]
        if not digest(revision, 40):
            raise ValueError("hosted acceptance requires a source commit")
        if os.environ.get("RELEASE_SHA", revision) != revision:
            raise ValueError("RELEASE_SHA differs from the checked-out source")
        if os.environ.get("RELEASE_TAG", tag or "v" + config["version"]) != "v" + config["version"]:
            raise ValueError("RELEASE_TAG differs from the configured version")
        require_tool4d_host()
        tool = Path(tool4d).absolute()
        regular(tool)
        version = subprocess.run([str(tool), "--version"], check=True, capture_output=True,
                                 text=True, timeout=30)
        if not version.stdout.strip():
            raise ValueError("tool4d version is unverified")
        report["tool4d"] = {"version": version.stdout.strip(), "binary_sha256": sha256(tool)}
        contract = _read_json(root / "tests/host/contract.json")
        for target in TARGETS:
            executable = root / "build" / target / (
                config["cli"] + (".exe" if target == "windows-x64" else ""))

            def native():
                regular(executable)
                manifest = verify(root, target, executable)
                if (type(manifest.get("schema")) is not int or manifest["schema"] != 1
                        or manifest.get("execution") != "passed"
                        or not isinstance(manifest.get("toolchain"), str) or not manifest["toolchain"]):
                    raise ValueError("native CLI execution is required: " + target)
                return manifest
            step("cli_test_" + target, native)
        step("assembly", lambda: assemble(root))
        component = root / "component" / config["repository"]

        def dependencies():
            return {
                "macOS": inspect(component / "Resources/bin/macOS" / config["cli"], "macos-arm64"),
                "Windows": inspect(component / "Resources/bin/Windows" / (config["cli"] + ".exe"),
                                   "windows-x64"),
                "scope": "Actual binary imports; native execution and 4D loading are separate checks",
            }
        step("dependencies", dependencies)

        def host(name, path=None):
            status = validate_status(test_4d(root, tool, path), contract, path is not None)
            destination = output.parent / (name.replace("_", "-") + "-status.json")
            regular(destination, missing=True)
            destination.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return status

        step("source_host", lambda: host("source-host"))

        def compile_clean():
            status = compile_component(root, tool)
            check_compile(status, config)
            validate_compile(root, component, config["repository"])
            return status
        compiled = step("compile", compile_clean)
        marker = root / "build/compile-status.json"
        marker_hash = sha256(marker)
        archive = step("package", lambda: package(
            root, repository=repository, tag=tag or "v" + config["version"]))
        regular(archive)
        report["unsigned_artifact"] = asset_record(root, archive)
        step("unsigned_compiled_host", lambda: host(
            "unsigned-compiled-host", unpack_for_test(root, archive)))
        if sha256(archive) != report["unsigned_artifact"]["sha256"]:
            raise ValueError("unsigned ZIP changed during host acceptance")
        report["unsigned_compile"] = dict(compiled)
        assets = [archive]
        if signing:
            def sign():
                helper = root / "scripts/sign_release.py"
                regular(helper)
                try:
                    subprocess.run([sys.executable, str(helper), "sign", "--root", str(root),
                                    "--require-apple-signing"], cwd=root, check=True,
                                   capture_output=True, timeout=2400)
                except subprocess.SubprocessError:
                    raise ValueError("Apple signing helper failed; child output withheld") from None
                if sha256(marker) != marker_hash:
                    raise ValueError("signer changed the pre-sign compile marker")
                validate_compile(root, component, config["repository"])
            step("signing", sign)
            assets += [archive.with_suffix(".dmg"),
                       archive.parent / (config["repository"] + "-signing.json")]
        report["assets"] = [asset_record(root, asset) for asset in assets]
        report["artifact"] = report["assets"][0]
        if signing:
            step("signing_proof", lambda: verify_signing_proof(
                root, config, revision, compiled["input_sha256"],
                report["unsigned_artifact"]["sha256"], report["assets"]))
        final = unpack_for_test(root, archive)
        if signing:
            step("apple_signatures", lambda: verify_signed_payload(
                root, config, final, os.environ.get("NOTARYTOOL_TEAM_ID")))
        step("compiled_host", lambda: host("compiled-host", final))
        def integrity():
            if sha256(marker) != marker_hash:
                raise ValueError("pre-sign compiler evidence changed during acceptance")
            validate_compile(root, component, config["repository"])
            if [asset_record(root, asset) for asset in assets] != report["assets"]:
                raise ValueError("final assets changed during host acceptance")
        step("integrity", integrity)
        report.update(result="partial", finished=True,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      reason="Exact final package verified; native Intel/Windows 4D and released installation not run")
        save()
        evidence = archive.with_suffix(".acceptance.json")
        regular(evidence, missing=True)
        evidence.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return report
    except Exception as error:
        # Child exceptions can contain passwords, URLs or captured command output.
        report.update(result="failed", finished=False, failed_step=active,
                      error=type(error).__name__ + ": hosted acceptance stopped at " + active)
        if active in {"source_host", "unsigned_compiled_host", "compiled_host"}:
            raw = root / "build/host-status.json"
            if raw.is_file() and not raw.is_symlink():
                try:
                    report["failure_host_status"] = _read_json(raw)
                except (ValueError, OSError):
                    pass
        save()
        raise ValueError(report["error"]) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tool4d", type=Path, required=True)
    parser.add_argument("--tag", help="version identity only; no tag is created or published")
    parser.add_argument("--require-apple-signing", action="store_true")
    args = parser.parse_args()
    try:
        report = accept(args.root, args.repository, args.tool4d, args.tag, args.require_apple_signing)
    except (ValueError, OSError):
        parser.exit(2, "hosted acceptance failed; see build/hosted-acceptance.json\n")
    print(json.dumps({key: report[key] for key in
                      ("artifact", "result", "finished", "release_ready")}, indent=2))


if __name__ == "__main__":
    main()
