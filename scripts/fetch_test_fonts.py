#!/usr/bin/env python3
"""Restore pinned public OFL fixtures; no font discovery or system-font fallback."""
import hashlib
import json
from pathlib import Path
import struct
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "tests" / "fonts"


def collection(sources):
    """Wrap unchanged TrueType/CFF tables in a TTC v1 with relocated offsets."""
    header_size = 12 + 4 * len(sources)
    bodies = bytearray()
    offsets = []
    for source in sources:
        base = header_size + len(bodies)
        offsets.append(base)
        data = bytearray(source)
        count = struct.unpack_from(">H", data, 4)[0]
        for index in range(count):
            location = 12 + index * 16 + 8
            offset = struct.unpack_from(">I", data, location)[0]
            struct.pack_into(">I", data, location, offset + base)
        bodies.extend(data)
        bodies.extend(b"\0" * (-len(bodies) % 4))
    return (b"ttcf" + struct.pack(">II", 0x00010000, len(sources))
            + b"".join(struct.pack(">I", offset) for offset in offsets) + bodies)


def main():
    manifest = json.loads((FONTS / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = FONTS / entry["name"]
        data = path.read_bytes() if path.exists() else None
        if data is None or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            with urllib.request.urlopen(entry["url"], timeout=60) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("fixture checksum mismatch: " + entry["name"])
            path.write_bytes(data)
    generated = manifest["generated"]
    data = collection([(FONTS / source).read_bytes() for source in generated["sources"]])
    if "sha256" in generated and hashlib.sha256(data).hexdigest() != generated["sha256"]:
        raise ValueError("generated TTC checksum mismatch")
    (FONTS / generated["name"]).write_bytes(data)


if __name__ == "__main__":
    main()
