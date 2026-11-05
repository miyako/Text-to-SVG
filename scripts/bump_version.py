#!/usr/bin/env python3
"""Compute the next release version from existing tags and write it to every version file.

The latest stable tag (bare ``X.Y.Z`` or legacy ``vX.Y.Z``; prerelease tags are
ignored) is bumped by --mode patch|minor|major. Without tags, template.json's
version is bumped. Never creates, moves or deletes tags.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from ci_config import ROOT, load_config

MODES = ("patch", "minor", "major")
STABLE_TAG = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)


def version_files(config):
    """Exact literal occurrences of the component/CLI version; {v} is substituted."""
    feature = "component/{}/Project/Sources/Classes/{}.4dm".format(
        config["repository"], config.get("feature_class", "Greeting"))
    return (
        ("template.json", '"version": "{v}"', 1),
        ("package.json", '"version": "{v}"', 1),
        ("cli/CMakeLists.txt", "project(" + config["cli"] + " VERSION {v} ", 1),
        (feature, '$payload.version#"{v}"', 1),
        ("tests/host/Project/Sources/Methods/Smoke.4dm", '$result.value.version#"{v}"', 1),
        ("tests/host/contract.json", '\\"version\\":\\"{v}\\"', 2),
        ("tests/host/contract.json", '"version": "{v}"', 1),
    )


def parse(version):
    match = STABLE_TAG.fullmatch(version)
    if not match:
        raise ValueError("not a stable X.Y.Z version: " + repr(version))
    return tuple(int(part) for part in match.groups())


def latest(tags):
    stable = [parse(tag) for tag in tags if STABLE_TAG.fullmatch(tag)]
    return max(stable) if stable else None


def bump(base, mode):
    major, minor, patch = base
    if mode == "major":
        return major + 1, 0, 0
    if mode == "minor":
        return major, minor + 1, 0
    if mode == "patch":
        return major, minor, patch + 1
    raise ValueError("mode must be one of " + ", ".join(MODES))


def next_version(tags, mode, configured):
    base = latest(tags)
    if base is None:
        base = parse(configured)
    return ".".join(str(part) for part in bump(base, mode))


def git_tags(root):
    return subprocess.run(["git", "tag", "--list"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.split()


def apply(root, new):
    root = Path(root)
    config = load_config(root, initialized=True)
    old = config["version"]
    parse(new)
    if old == new:
        return []
    changed = {}
    for name, literal, count in version_files(config):
        text = changed.get(name) or (root / name).read_text(encoding="utf-8")
        before, after = literal.format(v=old), literal.format(v=new)
        if text.count(before) != count:
            raise ValueError(f"{name}: expected {count} x {before!r}, found {text.count(before)}")
        changed[name] = text.replace(before, after)
    for name, text in changed.items():
        (root / name).write_text(text, encoding="utf-8")
    load_config(root, initialized=True)
    return sorted(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--write", action="store_true", help="rewrite the version files")
    args = parser.parse_args()
    try:
        config = load_config(args.root, initialized=True)
        version = next_version(git_tags(args.root), args.mode, config["version"])
        files = apply(args.root, version) if args.write else []
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"bump_version: {error}", file=sys.stderr)
        return 1
    print(version)
    for name in files:
        print("updated " + name, file=sys.stderr)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
