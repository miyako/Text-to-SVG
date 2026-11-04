from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_dependencies import inspect, windows_imports


def pe_fixture(dependency=b"KERNEL32.dll"):
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<HH", data, 68, 0x8664, 1)
    struct.pack_into("<H", data, 84, 240)
    struct.pack_into("<H", data, 88, 0x20B)
    struct.pack_into("<I", data, 88 + 108, 16)
    struct.pack_into("<II", data, 88 + 112 + 8, 4096, 40)
    struct.pack_into("<IIII", data, 88 + 240 + 8, 512, 4096, 512, 512)
    struct.pack_into("<IIIII", data, 512, 0, 0, 0, 4160, 0)
    data[576:576 + len(dependency) + 1] = dependency + b"\0"
    return data


class NativeDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.executable = Path(temporary.name) / "fixture.exe"

    def test_native_system_import_and_static_crt_requirement(self):
        self.executable.write_bytes(pe_fixture())
        self.assertEqual(windows_imports(self.executable), ["KERNEL32.DLL"])
        self.assertEqual(inspect(self.executable, "windows-x64")["status"], "passed")
        for dependency in (b"VCRUNTIME140.dll", b"freetype.dll", b"harfbuzz.dll"):
            self.executable.write_bytes(pe_fixture(dependency))
            with self.subTest(dependency=dependency), self.assertRaisesRegex(ValueError, "unbundled"):
                inspect(self.executable, "windows-x64")

    def test_malformed_and_delay_imports_fail_closed(self):
        fixtures = [b"", b"MZ", pe_fixture()[:550]]
        delayed = pe_fixture()
        struct.pack_into("<II", delayed, 88 + 112 + 13 * 8, 4096, 32)
        fixtures.append(delayed)
        address = pe_fixture()
        struct.pack_into("<I", address, 512 + 12, 0xFFFFFF00)
        fixtures.append(address)
        no_terminator = pe_fixture()
        struct.pack_into("<II", no_terminator, 88 + 112 + 8, 4096, 20)
        fixtures.append(no_terminator)
        for fixture in fixtures:
            self.executable.write_bytes(fixture)
            with self.subTest(length=len(fixture)), self.assertRaises(ValueError):
                windows_imports(self.executable)
