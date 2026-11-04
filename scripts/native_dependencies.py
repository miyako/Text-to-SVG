"""Inspect shipped native imports without relying on the build machine's DLL search path."""
import struct
import subprocess
from pathlib import Path


def windows_imports(executable):
    data = Path(executable).read_bytes()

    def unpack(format_, offset):
        size = struct.calcsize(format_)
        if offset < 0 or offset + size > len(data):
            raise ValueError("truncated PE dependency table")
        return struct.unpack_from(format_, data, offset)

    if data[:2] != b"MZ":
        raise ValueError("expected Windows PE executable")
    pe, = unpack("<I", 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("invalid PE signature")
    machine, count = unpack("<HH", pe + 4)
    optional_size, = unpack("<H", pe + 20)
    optional = pe + 24
    magic, = unpack("<H", optional)
    if machine != 0x8664 or magic != 0x20B or optional_size < 112 + 14 * 8:
        raise ValueError("expected x64 PE32+ dependency directories")
    directories, = unpack("<I", optional + 108)
    if directories < 14:
        raise ValueError("missing PE dependency directories")
    sections = []
    for index in range(count):
        section = optional + optional_size + index * 40
        virtual_size, address, raw_size, raw_offset = unpack("<IIII", section + 8)
        sections.append((address, virtual_size, raw_size, raw_offset))

    def offset(rva, size):
        for address, virtual_size, raw_size, raw_offset in sections:
            relative = rva - address
            if 0 <= relative < max(virtual_size, raw_size):
                if relative + size > raw_size or raw_offset + relative + size > len(data):
                    raise ValueError("unmapped PE dependency bytes")
                return raw_offset + relative
        raise ValueError("unmapped PE dependency address")

    delayed, delayed_size = unpack("<II", optional + 112 + 13 * 8)
    if delayed or delayed_size:
        raise ValueError("delay-loaded dependencies require explicit review")
    rva, size = unpack("<II", optional + 112 + 8)
    if not rva and not size:
        return []
    if not rva or size < 20:
        raise ValueError("invalid PE import directory")
    imports = []
    for index in range(min(size // 20, 4096)):
        descriptor = unpack("<IIIII", offset(rva + index * 20, 20))
        if not any(descriptor):
            return sorted(set(imports))
        name = offset(descriptor[3], 1)
        end = data.find(b"\0", name, min(name + 256, len(data)))
        if end < 0:
            raise ValueError("unterminated PE dependency name")
        try:
            dependency = data[name:end].decode("ascii").upper()
        except UnicodeDecodeError as error:
            raise ValueError("non-ASCII PE dependency name") from error
        if not dependency or "/" in dependency or "\\" in dependency:
            raise ValueError("invalid PE dependency name")
        imports.append(dependency)
    raise ValueError("unterminated PE import directory")


def inspect(executable, target):
    if target == "windows-x64":
        dependencies = windows_imports(executable)
        system = {"KERNEL32.DLL", "NTDLL.DLL", "ADVAPI32.DLL", "USER32.DLL",
                  "SHELL32.DLL", "OLE32.DLL", "OLEAUT32.DLL", "WS2_32.DLL",
                  "BCRYPT.DLL", "CRYPT32.DLL"}
        extra = [name for name in dependencies
                 if name not in system and not name.startswith("API-MS-WIN-CORE-")]
    else:
        result = subprocess.run(["otool", "-L", str(executable)], check=True,
                                capture_output=True, text=True)
        dependencies = [line.strip().split(" (", 1)[0] for line in result.stdout.splitlines()
                        if " (compatibility version " in line]
        extra = [name for name in dependencies
                 if not name.startswith(("/usr/lib/", "/System/Library/"))]
    if extra:
        raise ValueError("unbundled native runtime dependencies: " + repr(extra))
    return {"target": target, "dependencies": dependencies, "status": "passed"}
