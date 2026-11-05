#!/usr/bin/env python3
"""Fail-closed Apple signing of a compiled release, notarized through a DMG.

`sign` requires the Publish workflow (workflow_dispatch on main) on macOS GitHub
Actions, --require-apple-signing and six credentials. It never signs the source
component or treats a .4dbase as an app. RELEASE_TAG is the new bare version
about to be published; RELEASE_SHA must equal the checked-out HEAD.
The resulting ZIP is copied from the accepted, stapled, mounted DMG; the ZIP
itself cannot be stapled. The caller must still run the real compiled host test.
`cleanup` needs only RUNNER_TEMP and recovers the isolated signing directory.
"""
import argparse
import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from ci_config import ROOT, SEMVER, load_config, release_tag
import package_component

# MIT lifecycle: miyako/4d-component-template, commit
# 96da0b4a0f628536b906855a03f2a54ec6cdb002. Isolated keychain, derived identity,
# ticket coverage and bounded stapling adapted from miyako/hash (MIT).

SECRET_NAMES = (
    "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
    "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
    "NOTARYTOOL_PASSWORD",
)
SECURITY = "/usr/bin/security"
CODESIGN = "/usr/bin/codesign"
HDIUTIL = "/usr/bin/hdiutil"
XCRUN = "/usr/bin/xcrun"
GIT = "/usr/bin/git"
SPCTL = "/usr/sbin/spctl"
TRUSTED_TOOLS = {SECURITY, CODESIGN, HDIUTIL, XCRUN, GIT, SPCTL}
PUBLISH_REF = "refs/heads/main"


class SigningError(ValueError):
    pass


class CommandError(SigningError):
    def __init__(self, label, result, secrets):
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.returncode = result.returncode
        details = redact(result.stderr + "\n" + result.stdout, secrets)
        super().__init__(f"{label}: exit {result.returncode}: {details.strip()}")


def redact(text, secrets):
    values = set(secrets.values())
    certificate = secrets.get("APPLE_DEVELOPER_ID_CERTIFICATE", "")
    if certificate:
        values.add("".join(certificate.split()))
    values.update(json.dumps(value, ensure_ascii=True)[1:-1] for value in tuple(values) if value)
    values.update(json.dumps(value, ensure_ascii=False)[1:-1] for value in tuple(values) if value)
    for value in sorted((value for value in values if value), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return re.sub(r"https?:(?:/|\\/){2}[^\s<>\"']+", "[REDACTED URL]", text)


def credentials(env):
    missing = [name for name in SECRET_NAMES if not env.get(name, "").strip()]
    if missing:
        raise SigningError("missing required credentials: " + ", ".join(missing))
    secrets = {name: env[name] for name in SECRET_NAMES}
    if not re.fullmatch(r"[A-Z0-9]{10}", secrets["NOTARYTOOL_TEAM_ID"]):
        raise SigningError("invalid expected Apple team")
    return secrets


class Commands:
    """No command line or unredacted child diagnostic is ever printed."""

    def __init__(self, env, secrets):
        self.secrets = secrets
        self.env = {key: value for key, value in env.items() if key not in SECRET_NAMES}
        self.env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

    def run(self, label, args, timeout=120, *, cwd=None):
        if not args or args[0] not in TRUSTED_TOOLS:
            raise SigningError("untrusted signing tool")
        try:
            result = subprocess.run(
                [str(arg) for arg in args], env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                cwd=cwd,
            )
        except (OSError, subprocess.SubprocessError):
            # TimeoutExpired and CalledProcessError stringify the secret argv.
            raise SigningError(label + ": tool launch failed or timed out") from None
        if result.returncode:
            raise CommandError(label, result, self.secrets)
        return result.stdout, result.stderr


def certificate_bytes(secrets):
    try:
        data = base64.b64decode(
            "".join(secrets["APPLE_DEVELOPER_ID_CERTIFICATE"].split()), validate=True)
    except (ValueError, binascii.Error):
        raise SigningError("certificate: invalid base64 PKCS#12 input") from None
    if not data:
        raise SigningError("certificate: empty PKCS#12 input")
    return data


def safe_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SigningError("signing paths must not traverse symlinks")
    return path


def private_directory(env):
    if not env.get("RUNNER_TEMP"):
        raise SigningError("RUNNER_TEMP must name an existing runner-owned directory")
    base = safe_path(env["RUNNER_TEMP"])
    if not base.is_dir():
        raise SigningError("RUNNER_TEMP must name an existing runner-owned directory")
    return safe_path(base / "Text-to-SVG-release-signing")


def select_identity(output, team):
    identities = []
    for line in output.splitlines():
        match = re.fullmatch(r'\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]+)"\s*', line)
        if (match and match[2].startswith("Developer ID Application: ")
                and match[2].endswith(f" ({team})")):
            identities.append((match[1], match[2]))
    if len(identities) != 1:
        raise SigningError("expected exactly one valid Developer ID Application identity for the team")
    return identities[0]


class Keychain:
    def __init__(self, commands, scratch, secrets):
        self.commands = commands
        self.secrets = secrets
        self.path = scratch / "release.keychain-db"
        self.p12 = scratch / "certificate.p12"
        self.home = scratch / "home"
        self.home.mkdir(mode=0o700)
        for directory in ("Library/Keychains", "Library/Preferences"):
            (self.home / directory).mkdir(parents=True, mode=0o700)
        self.commands.env.update(HOME=str(self.home), CFFIXED_USER_HOME=str(self.home))
        self.created = False

    def prepare(self, data):
        fd = os.open(self.p12, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        password = self.secrets["KEYCHAIN_PASSWORD"]
        # A tool failure can occur after it has created the keychain on disk.
        self.created = True
        self.commands.run("keychain create", [SECURITY, "create-keychain", "-p", password, self.path])
        self.commands.run("keychain unlock", [SECURITY, "unlock-keychain", "-p", password, self.path])
        self.commands.run("keychain settings", [SECURITY, "set-keychain-settings", "-lut", "21600", self.path])
        self.commands.run("certificate import", [
            SECURITY, "import", self.p12, "-k", self.path,
            "-P", self.secrets["APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD"],
            "-T", CODESIGN, "-T", SECURITY,
        ])
        self.commands.run("keychain partition ACL", [
            SECURITY, "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
            "-s", "-k", password, self.path,
        ])
        self.commands.run("keychain isolate", [
            SECURITY, "list-keychains", "-d", "user", "-s", self.path,
            "/Library/Keychains/System.keychain"])
        self.p12.unlink()
        out, _ = self.commands.run("derive signing identity", [
            SECURITY, "find-identity", "-v", "-p", "codesigning", self.path])
        return select_identity(out, self.secrets["NOTARYTOOL_TEAM_ID"])

    def cleanup(self):
        errors = []
        if self.created:
            try:
                self.commands.run("keychain delete", [SECURITY, "delete-keychain", self.path])
            except (ValueError, OSError) as error:
                errors.append(str(error))
        try:
            self.p12.unlink(missing_ok=True)
        except OSError as error:
            errors.append("certificate cleanup: " + str(error))
        return errors


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cleanup_release(root=ROOT, env=None):
    """Credential-free, idempotent recovery for a workflow always() step."""
    env = os.environ if env is None else env
    scratch = private_directory(env)
    if not scratch.exists():
        return
    if not scratch.is_dir():
        raise SigningError("private signing path is not a directory")
    keychain = safe_path(scratch / "release.keychain-db")
    home = safe_path(scratch / "home")
    for relative in ("Library", "Library/Keychains", "Library/Preferences"):
        safe_path(home / relative)
    commands = Commands(env, {})
    commands.env.update(HOME=str(home), CFFIXED_USER_HOME=str(home))
    try:
        if keychain.exists():
            commands.run("delete abandoned isolated keychain", [SECURITY, "delete-keychain", keychain])
    finally:
        shutil.rmtree(scratch)


@contextmanager
def private_workspace(env):
    scratch = private_directory(env)
    # Never erase another in-flight signing session.
    scratch.mkdir(mode=0o700)
    failure = None
    cleanup = None
    try:
        scratch.chmod(0o700)
        yield scratch
    except Exception as error:
        failure = error
    finally:
        try:
            shutil.rmtree(scratch)
        except OSError as error:
            cleanup = "signing workspace cleanup: " + str(error)
    if failure or cleanup:
        raise SigningError("; ".join(([str(failure)] if failure else []) + ([cleanup] if cleanup else [])))


def snapshot(base):
    if base.is_symlink() or not base.is_dir():
        raise SigningError("package root must be a regular directory")
    entries = {".": (None, stat.S_IMODE(base.stat().st_mode))}
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise SigningError("package contains nonregular entry")
        relative = path.relative_to(base).as_posix()
        entries[relative] = (path.read_bytes() if path.is_file() else None,
                             stat.S_IMODE(path.stat().st_mode))
    return entries


def assert_immutable(before, after, signed=()):
    if set(before) != set(after):
        raise SigningError("package layout changed")
    for name in before:
        if before[name][1] != after[name][1] or (name not in signed and before[name] != after[name]):
            raise SigningError("immutable package content or permissions changed: " + name)


def macho_architectures(path):
    data = path.read_bytes()
    thin = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">",
            b"\xce\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xce": ">"}
    cpus = {0x01000007: "x86_64", 0x0100000C: "arm64"}
    try:
        if data[:4] in thin:
            return [cpus[struct.unpack_from(thin[data[:4]] + "I", data, 4)[0]]]
        if data[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            wide = data[:4] == b"\xca\xfe\xba\xbf"
            count = struct.unpack_from(">I", data, 4)[0]
            if not 1 <= count <= 2:
                raise SigningError("unsupported Mach-O architectures")
            return [cpus[struct.unpack_from(">I", data, 8 + i * (32 if wide else 20))[0]]
                    for i in range(count)]
    except (KeyError, struct.error):
        raise SigningError("invalid Mach-O header") from None
    raise SigningError("native Library is not a supported Mach-O")


def signature(commands, path, team, *, runtime=True, architectures=None, identity=None):
    commands.run("signature verify", [CODESIGN, "--verify", "--strict", "--all-architectures", path])
    receipts = {}
    for architecture in architectures or [None]:
        args = [CODESIGN, "--display", "--verbose=4"]
        if architecture:
            args += ["--arch", architecture]
        out, err = commands.run("signature display", [*args, path])
        display = out + "\n" + err
        actual = re.search(r"^TeamIdentifier=(.+)$", display, re.MULTILINE)
        authorities = re.findall(r"^Authority=(.+)$", display, re.MULTILINE)
        timestamp = re.search(r"^Timestamp=(.+)$", display, re.MULTILINE)
        cdhash = re.search(r"^CDHash=([a-fA-F0-9]{40})$", display, re.MULTILINE)
        flags = re.search(r"\bflags=0x([0-9a-fA-F]+)\(([^)]*)\)", display)
        if (not re.search(r"^Authority=Developer ID Application:", display, re.MULTILINE)
                or actual is None or actual.group(1).strip() != team
                or (identity is not None and (not authorities or authorities[0] != identity))
                or re.search(r"^Signature=adhoc$", display, re.MULTILINE)
                or (runtime and (not flags or not int(flags[1], 16) & 0x10000
                                 or "runtime" not in flags[2].split(",")))
                or not timestamp or not timestamp[1].strip()
                or timestamp[1].strip().lower() in {"none", "not set"}
                or not cdhash or not re.search(r"^Hash type=sha256(?:\s|$)", display, re.MULTILINE)):
            raise SigningError("signature display: required Developer ID, team, timestamp or runtime missing")
        if runtime:
            args = [CODESIGN, "--display", "--entitlements", ":-"]
            if architecture:
                args += ["--arch", architecture]
            entitlements, _ = commands.run("signature entitlements", [*args, path])
            if entitlements.strip():
                try:
                    empty = plistlib.loads(entitlements.encode()) == {}
                except (ValueError, plistlib.InvalidFileException):
                    empty = False
                if not empty:
                    raise SigningError("unexpected code entitlements")
        receipts[architecture or "container"] = {
            "cdhash": cdhash[1].lower(), "team": team, "authority": authorities[0],
            "timestamp": timestamp[1], "hardenedRuntime": runtime,
        }
    return receipts


def sign_file(commands, path, identity, keychain, identifier, *, runtime=True):
    args = [CODESIGN, "--force", "--sign", identity, "--timestamp",
            "--keychain", keychain, "--identifier", identifier]
    if runtime:
        args += ["--options", "runtime"]
    commands.run("codesign sign", [*args, path])


def prepare_payload(root, scratch, config, unsigned):
    """Reproduce the unsigned artifact to bind every archive byte to compilation."""
    mirror = scratch / "inputs"
    mirror.mkdir()
    for name in ("template.json", "package.json", "LICENSE"):
        shutil.copy2(root / name, mirror / name)
    component = root / "component" / config["repository"]
    if component.is_symlink() or component.parent.is_symlink():
        raise SigningError("component directories must not be symlinks")
    shutil.copytree(component, mirror / "component" / config["repository"], symlinks=True)
    (mirror / "build").mkdir()
    marker = json.loads((root / "build/compile-status.json").read_text(encoding="utf-8"))
    marker["project"] = "component/" + config["repository"] + "/Project/" + config["repository"] + ".4DProject"
    (mirror / "build/compile-status.json").write_text(json.dumps(marker), encoding="utf-8")
    expected = package_component.package(
        mirror, repository="owner/" + config["repository"], tag=release_tag(config["version"]))
    if expected.read_bytes() != unsigned.read_bytes():
        raise SigningError("unsigned ZIP does not match freshly compiled package inputs")
    unpacked = package_component.unpack_for_test(mirror, unsigned)
    payload = scratch / "payload"
    payload.mkdir()
    staged = payload / unpacked.name
    shutil.copytree(unpacked, staged)
    package_component.validate_resources(staged, config["cli"])
    return payload, staged


def code_inventory(staged, cli):
    mac = staged / "Resources/bin/macOS" / cli
    if set(macho_architectures(mac)) != {"arm64", "x86_64"}:
        raise SigningError("CLI must have both arm64 and x86_64 slices")
    library_root = staged / "Libraries"
    libraries = []
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
             b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
    for path in library_root.rglob("*"):
        if path.is_file():
            if path.read_bytes()[:4] in magic:
                macho_architectures(path)
                libraries.append(path)
            elif path.suffix.lower() in {".dylib", ".so"}:
                raise SigningError("native library is not Mach-O")
    required = library_root / "lib4d-arm64.dylib"
    if required not in libraries or macho_architectures(required) != ["arm64"]:
        raise SigningError("required 4D arm64 library is missing or has wrong architecture")
    # Sign every real library, deepest first; generic compiled code is not Mach-O.
    return sorted(libraries, key=lambda p: (-len(p.parts), p.as_posix())) + [mac]


def notary_credentials(secrets):
    return ["--apple-id", secrets["NOTARYTOOL_APPLE_ID"],
            "--team-id", secrets["NOTARYTOOL_TEAM_ID"],
            "--password", secrets["NOTARYTOOL_PASSWORD"]]


def submission_uuid(value):
    if not isinstance(value, str):
        raise SigningError("notarytool: missing submission UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise SigningError("notarytool: invalid submission UUID") from None
    if str(parsed) != value.lower():
        raise SigningError("notarytool: invalid submission UUID")
    return str(parsed)


def validate_ticket_log(log, dmg, identifier, expected):
    if (not isinstance(log, dict) or type(log.get("logFormatVersion")) is not int
            or log["logFormatVersion"] != 1 or not isinstance(log.get("jobId"), str)
            or log["jobId"].lower() != identifier
            or log.get("status") != "Accepted" or type(log.get("statusCode")) is not int
            or log["statusCode"] != 0 or log.get("archiveFilename") != dmg.name
            or log.get("sha256") != sha256(dmg) or log.get("issues") not in (None, [])):
        raise SigningError("notary log status, identity or archive checksum mismatch")
    observed = {}
    contents = log.get("ticketContents")
    if not expected or not isinstance(contents, list) or not contents:
        raise SigningError("notary log has no ticket coverage")
    for item in contents:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item.get("arch") not in (None, "arm64", "x86_64")
                or item.get("digestAlgorithm") != "SHA-256"
                or not isinstance(item.get("cdhash"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{40}", item["cdhash"])):
            raise SigningError("malformed notarization ticket")
        key = item["path"], item.get("arch")
        if key in observed:
            raise SigningError("duplicate notarization tickets")
        observed[key] = item["cdhash"].lower()
    if observed != expected:
        raise SigningError("notarization ticket coverage does not match all signed code and DMG")
    return contents


def staple(commands, dmg):
    deadline = time.monotonic() + 180
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SigningError("stapling retry deadline exceeded")
        try:
            commands.run("DMG staple", [XCRUN, "stapler", "staple", dmg],
                         timeout=min(60, remaining))
            return
        except CommandError as error:
            codes = {int(code) for code in re.findall(
                r"\b(?:Error )?Domain=NSURLErrorDomain\s+Code=(-\d+)\b",
                error.stdout + "\n" + error.stderr)}
            if (error.returncode != 68 or not codes
                    or not codes <= {-1001, -1003, -1004, -1005, -1006, -1009}
                    or attempt == 2):
                raise
            delay = (5, 15)[attempt]
            if deadline - time.monotonic() <= delay:
                raise SigningError("stapling retry deadline exceeded") from None
            time.sleep(delay)


def notarize(commands, dmg, secrets, expected, diagnostics):
    diagnostics = safe_path(diagnostics)
    diagnostics.mkdir(parents=True, exist_ok=True)
    command_error = None
    try:
        out, _ = commands.run("notarytool submit", [
            XCRUN, "notarytool", "submit", dmg, *notary_credentials(secrets),
            "--wait", "--timeout", "30m", "--output-format", "json",
        ], timeout=1860)
    except CommandError as error:
        # A rejected submission may also exit nonzero; still fetch its issues.
        command_error = error
        out = error.stdout
    safe_path(diagnostics / "notary-submission.txt").write_text(
        redact(out, secrets) + "\n", encoding="utf-8")
    try:
        response = json.loads(out)
    except json.JSONDecodeError:
        if command_error:
            raise command_error
        raise SigningError("notarytool submit: invalid JSON response") from None
    if not isinstance(response, dict):
        if command_error:
            raise command_error
        raise SigningError("notarytool submit: invalid response")
    try:
        identifier = submission_uuid(response.get("id"))
    except SigningError:
        if command_error:
            raise command_error
        raise
    safe_path(diagnostics / "notary-submission.json").write_text(
        redact(json.dumps(response, indent=2), secrets) + "\n", encoding="utf-8")
    try:
        log, _ = commands.run("notarytool log", [
            XCRUN, "notarytool", "log", identifier, *notary_credentials(secrets)])
        safe_path(diagnostics / "notary-log.json").write_text(redact(log, secrets) + "\n", encoding="utf-8")
    except SigningError:
        if command_error or response.get("status") != "Accepted":
            raise SigningError("notarytool: submission failed; diagnostic log unavailable") from None
        raise
    if command_error or response.get("status") != "Accepted":
        raise SigningError("notarytool: submission was not successfully Accepted")
    try:
        report = json.loads(log)
    except ValueError:
        raise SigningError("notarytool: invalid accepted log JSON") from None
    coverage = validate_ticket_log(report, dmg, identifier, expected)
    staple(commands, dmg)
    commands.run("DMG staple validate", [XCRUN, "stapler", "validate", dmg])
    return {"status": "Accepted", "submissionUUID": identifier, "ticketContents": coverage}


def detach(commands, mount):
    try:
        commands.run("DMG detach", [HDIUTIL, "detach", mount])
        return []
    except SigningError as first:
        try:
            commands.run("DMG force detach", [HDIUTIL, "detach", "-force", mount])
            return []
        except SigningError as second:
            return [str(first), str(second)]


def zip_from_mount(commands, dmg, scratch, staged, signed_paths, secrets, config, identity):
    mount = scratch / "mount"
    mount.mkdir()
    attached = False
    failure = None
    cleanup = []
    destination = scratch / (config["repository"] + ".zip")
    try:
        # Mark before attach: a failed hdiutil invocation can still leave a mount.
        attached = True
        out, _ = commands.run("DMG attach", [
            HDIUTIL, "attach", dmg, "-readonly", "-nobrowse", "-mountpoint", mount, "-plist"])
        info = plistlib.loads(out.encode("utf-8"))
        if (not isinstance(info, dict) or not isinstance(info.get("system-entities"), list)
                or any(not isinstance(entry, dict) for entry in info["system-entities"])):
            raise SigningError("DMG attach: malformed mount report")
        points = [entry.get("mount-point") for entry in info["system-entities"]
                  if entry.get("mount-point")]
        if points != [str(mount)]:
            raise SigningError("DMG attach: unexpected mount point")
        metadata = {".Trashes", ".fseventsd", ".HFS+ Private Directory Data\r", ".Spotlight-V100"}
        if {path.name for path in mount.iterdir()} - metadata != {staged.name}:
            raise SigningError("mounted DMG payload identity mismatch")
        mounted = mount / staged.name
        after = snapshot(mounted)
        assert_immutable(snapshot(staged), after)
        package_component.validate_resources(mounted, config["cli"])
        for relative in signed_paths:
            signature(commands, mounted / relative, secrets["NOTARYTOOL_TEAM_ID"],
                      architectures=macho_architectures(mounted / relative), identity=identity)
        entries = [(staged.name + "/" + name, data, mode)
                   for name, (data, mode) in after.items() if data is not None]
        package_component.write_zip(destination, entries)
        # Exercise the same safe extraction/layout checks as the caller's host test.
        unpacked = package_component.unpack_for_test(scratch / "inputs", destination)
        assert_immutable(after, snapshot(unpacked))
    except (ValueError, OSError, plistlib.InvalidFileException, TypeError) as error:
        failure = error
    finally:
        if attached:
            cleanup = detach(commands, mount)
    if failure or cleanup:
        raise SigningError("; ".join(([str(failure)] if failure else []) + cleanup))
    return destination


def trusted_context(root, env, commands):
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REF") != PUBLISH_REF:
        raise SigningError("signing runs only from the Publish workflow on main; pull requests cannot sign")
    tag = env.get("RELEASE_TAG")
    if not isinstance(tag, str) or not re.fullmatch(SEMVER, tag, flags=re.ASCII):
        raise SigningError("RELEASE_TAG must be the bare SemVer being published")
    source_sha = env.get("RELEASE_SHA")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        raise SigningError("sign requires resolved RELEASE_SHA")
    head, _ = commands.run("source HEAD", [GIT, "rev-parse", "HEAD"], cwd=root)
    if head.strip().lower() != source_sha.lower():
        raise SigningError("source HEAD does not match the resolved release commit")
    return tag, source_sha.lower()


def sign_release(root=ROOT, env=None, *, require_apple_signing=False):
    root = safe_path(root).resolve()
    env = os.environ if env is None else env
    if platform.system() != "Darwin" or env.get("GITHUB_ACTIONS") != "true":
        raise SigningError("sign requires macOS GitHub Actions")
    commands = Commands(env, {})
    tag, source_sha = trusted_context(root, env, commands)
    if not env.get("GITHUB_REPOSITORY") or not tag:
        raise SigningError("sign requires actual GITHUB_REPOSITORY and release tag")
    config = load_config(root, initialized=True, repository=env["GITHUB_REPOSITORY"],
                         tag=tag)
    if not require_apple_signing:
        raise SigningError("signing requires explicit --require-apple-signing")
    secrets = credentials(env)
    commands.secrets = secrets
    data = certificate_bytes(secrets)
    component = root / "component" / config["repository"]
    package_component.validate_compile(root, component, config["repository"])
    package_component.validate_resources(component, config["cli"])
    unsigned = safe_path(root / "dist" / (config["repository"] + ".zip"))
    if not unsigned.is_file() or unsigned.is_symlink():
        raise SigningError("fresh unsigned compiled ZIP is required")
    unsigned_hash = sha256(unsigned)
    marker = safe_path(root / "build/compile-status.json")
    marker_hash = sha256(marker)
    digest = json.loads(marker.read_text())["input_sha256"]
    build = root / "build"
    if build.is_symlink() or (root / "dist").is_symlink():
        raise SigningError("build and dist must not be symlinks")
    for suffix in (".dmg", "-signing.json"):
        safe_path(root / "dist" / (config["repository"] + suffix))
    with private_workspace(env) as scratch:
        keychain = Keychain(commands, scratch, secrets)
        failure = None
        cleanup = []
        try:
            payload, staged = prepare_payload(root, scratch, config, unsigned)
            before = snapshot(staged)
            signed = code_inventory(staged, config["cli"])
            relative_paths = [path.relative_to(staged).as_posix() for path in signed]
            fingerprint, identity = keychain.prepare(data)
            signatures = {}
            for path, relative in zip(signed, relative_paths):
                sign_file(commands, path, fingerprint, keychain.path,
                          "org.4d." + config["repository"] + "." + relative.replace("/", "."))
                signatures[relative] = signature(commands, path, secrets["NOTARYTOOL_TEAM_ID"],
                          architectures=macho_architectures(path), identity=identity)
            assert_immutable(before, snapshot(staged), relative_paths)
            package_component.validate_resources(staged, config["cli"])
            dmg = scratch / (config["repository"] + ".dmg")
            commands.run("DMG create", [
                HDIUTIL, "create", "-srcfolder", payload, "-volname", config["repository"],
                "-fs", "HFS+", "-format", "UDZO", dmg,
            ])
            sign_file(commands, dmg, fingerprint, keychain.path,
                      "org.4d." + config["repository"] + ".release", runtime=False)
            container = signature(commands, dmg, secrets["NOTARYTOOL_TEAM_ID"],
                                  runtime=False, identity=identity)
            expected = {(f"{dmg.name}/{staged.name}/{relative}", arch): receipt["cdhash"]
                        for relative, slices in signatures.items() for arch, receipt in slices.items()}
            expected[dmg.name, None] = container["container"]["cdhash"]
            notarization = notarize(commands, dmg, secrets, expected, build / "signing-diagnostics")
            if signature(commands, dmg, secrets["NOTARYTOOL_TEAM_ID"],
                         runtime=False, identity=identity) != container:
                raise SigningError("DMG signature changed while stapling")
            for path in signed:
                commands.run("code online notarization requirement", [
                    CODESIGN, "--verify", "--strict", "--all-architectures",
                    "--check-notarization", "-R=notarized", path])
            commands.run("DMG Gatekeeper assessment", [
                SPCTL, "--assess", "--verbose=4", "--type", "open",
                "--context", "context:primary-signature", dmg])
            final_zip = zip_from_mount(commands, dmg, scratch, staged, relative_paths, secrets, config, identity)
            proof = {
                "schema": 1, "version": config["version"], "sourceSHA": source_sha.lower(),
                "releaseTag": tag,
                "unsignedCompileInputSHA256": digest, "unsignedZIPHashSHA256": unsigned_hash,
                "signedFiles": [{"path": path, "sha256": sha256(staged / path),
                                 "verified": True, "signatures": signatures[path]}
                                for path in relative_paths],
                "notarization": notarization,
                "assessments": {"codeNotarization": "passed", "dmgOpen": "passed"},
                "dmg": {"stapled": True, "validated": True, "sha256": sha256(dmg)},
                "zip": {"sha256": sha256(final_zip), "stapled": False,
                        "notarizedViaDMG": True, "origin": "final-mounted-accepted-DMG"},
                "hostValidation": "required-after-signing",
            }
            proof_file = scratch / (config["repository"] + "-signing.json")
            proof_file.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
        except (ValueError, OSError, TypeError, KeyError, zipfile.BadZipFile) as error:
            failure = error
        finally:
            cleanup = keychain.cleanup()
        if failure or cleanup:
            raise SigningError(redact("; ".join(([str(failure)] if failure else []) + cleanup), secrets))
        if sha256(unsigned) != unsigned_hash:
            raise SigningError("unsigned input ZIP changed during signing")
        if sha256(marker) != marker_hash:
            raise SigningError("unsigned compilation marker changed during signing")
        package_component.validate_compile(root, component, config["repository"])
        trusted_context(root, env, commands)
        artifacts = [(source.name, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
                     for source in (dmg, proof_file, final_zip)]
    # No intermediate certificate/keychain/mount/workspace remains at delivery.
    pending = []
    try:
        for name, content, mode in artifacts:
            with tempfile.NamedTemporaryFile(prefix=".apple-release-", dir=root / "dist", delete=False) as output:
                pending.append((Path(output.name), root / "dist" / name))
                output.write(content)
                os.fchmod(output.fileno(), mode)
        # Replace ZIP last: the unchanged unsigned ZIP survives any earlier error.
        for source, destination in pending:
            source.replace(destination)
    finally:
        for source, _ in pending:
            source.unlink(missing_ok=True)
    print("Apple notarization Accepted: " + ", ".join("dist/" + name for name, _, _ in artifacts))
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sign", "cleanup"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--require-apple-signing", action="store_true",
                        help="mandatory for sign; signing never happens implicitly")
    args = parser.parse_args(argv)
    try:
        if args.command == "sign":
            sign_release(args.root, require_apple_signing=args.require_apple_signing)
        else:
            cleanup_release(args.root)
    except (ValueError, OSError, TypeError, KeyError, zipfile.BadZipFile) as error:
        secrets = {name: os.environ.get(name, "") for name in SECRET_NAMES}
        diagnostic = "Apple release signing failed: " + redact(str(error), secrets)
        try:
            directory = safe_path(args.root / "build/signing-diagnostics")
            directory.mkdir(parents=True, exist_ok=True)
            safe_path(directory / "signing-error.txt").write_text(diagnostic + "\n", encoding="utf-8")
        except (ValueError, OSError):
            print("Unable to persist redacted signing diagnostic", file=sys.stderr)
        print(diagnostic, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
