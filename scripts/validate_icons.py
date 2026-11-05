#!/usr/bin/env python3
"""Validate the committed component icons (custom artwork is the source of truth).

Checks that Resources/logo.png is a PNG (signature and a well-formed IHDR chunk)
and Resources/logo.svg is well-formed XML with an SVG root element. The icons are
never compared against, or regenerated from, any generator output.
"""
import argparse
import json
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SVG_ROOT = "{http://www.w3.org/2000/svg}svg"
VALID_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def validate_png(path):
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("icon is not a PNG (bad signature): " + str(path))
    if len(data) < 33:
        raise ValueError("icon PNG is truncated: " + str(path))
    length, kind = struct.unpack(">I4s", data[8:16])
    payload = data[16:16 + length]
    if kind != b"IHDR" or length != 13:
        raise ValueError("icon PNG does not start with a valid IHDR chunk: " + str(path))
    (checksum,) = struct.unpack(">I", data[29:33])
    if checksum != zlib.crc32(kind + payload) & 0xffffffff:
        raise ValueError("icon PNG IHDR checksum mismatch: " + str(path))
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
    if (width == 0 or height == 0 or depth not in VALID_DEPTHS.get(color, ())
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise ValueError("icon PNG IHDR has invalid values: " + str(path))
    return width, height


def validate_svg(path):
    root = ET.parse(path).getroot()
    if root.tag != SVG_ROOT:
        raise ValueError("icon is not an SVG document: " + str(path))


def validate(root):
    root = Path(root)
    config = json.loads((root / "template.json").read_text())
    directory = root / "component" / config["repository"] / "Resources"
    png, svg = directory / "logo.png", directory / "logo.svg"
    for path in (png, svg):
        if not path.is_file():
            raise ValueError("icon is missing: " + str(path))
    validate_png(png)
    validate_svg(svg)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        validate(args.root)
    except (ValueError, OSError, ET.ParseError, json.JSONDecodeError, KeyError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
