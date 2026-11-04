"""Hermetic native tests for the pinned C++ text outline engine."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import unittest
import uuid
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "tests" / "fonts"
LATIN = FONTS / "NotoSerif-Regular.ttf"
JAPANESE = FONTS / "NotoSansJP-Regular.otf"
COLLECTION = FONTS / "NotoSerif-NotoSansJP.ttc"
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
TOKEN = re.compile(r"[MLQCZmlqcz]|" + NUMBER)


def json_line(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sfnt_tables(path):
    data = path.read_bytes()
    tables = {}
    for index in range(struct.unpack_from(">H", data, 4)[0]):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        tables[tag] = data[offset:offset + length]
    return tables


def path_geometry(data):
    """Evaluate Bézier extrema independently of FreeType's bounding-box code."""
    tokens = TOKEN.findall(data)
    if TOKEN.sub("", data).strip(" ,\t\r\n"):
        raise AssertionError("unexpected outline syntax: " + data)
    position = (0.0, 0.0)
    start = position
    bounds = []
    contours = []
    contour = []
    index = 0
    command = None

    def at(points, t):
        work = list(points)
        while len(work) > 1:
            work = [(a[0] * (1 - t) + b[0] * t, a[1] * (1 - t) + b[1] * t)
                    for a, b in zip(work, work[1:])]
        return work[0]

    def roots(points, axis):
        p = [point[axis] for point in points]
        if len(p) == 3:
            denominator = p[0] - 2 * p[1] + p[2]
            return [(p[0] - p[1]) / denominator] if denominator else []
        if len(p) == 4:
            a = -p[0] + 3 * p[1] - 3 * p[2] + p[3]
            b = 2 * (p[0] - 2 * p[1] + p[2])
            c = p[1] - p[0]
            if abs(a) < 1e-14:
                return [-c / b] if b else []
            discriminant = b * b - 4 * a * c
            if discriminant >= 0:
                return [(-b + sign * math.sqrt(discriminant)) / (2 * a) for sign in (-1, 1)]
        return []

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command.upper() == "Z":
                if not contour:
                    raise AssertionError("empty closed contour")
                contour.append(start)
                contours.append(contour)
                contour = []
                position = start
                command = None
                continue
        if command is None:
            raise AssertionError("missing path command")
        kind = command.upper()
        count = {"M": 2, "L": 2, "Q": 4, "C": 6}[kind]
        values = list(map(float, tokens[index:index + count]))
        if len(values) != count or not all(math.isfinite(value) for value in values):
            raise AssertionError("incomplete or non-finite path")
        index += count
        points = list(zip(values[::2], values[1::2]))
        if command.islower():
            points = [(x + position[0], y + position[1]) for x, y in points]
        if kind == "M":
            if contour:
                raise AssertionError("outline contour must be closed")
            position = start = points[0]
            contour = [position]
            command = "l" if command.islower() else "L"
            continue
        curve = [position, *points]
        bounds.extend((position, points[-1]))
        for axis in (0, 1):
            bounds.extend(at(curve, t) for t in roots(curve, axis) if 0 < t < 1)
        contour.extend(at(curve, step / 32) for step in range(1, 33))
        position = points[-1]
    if contour:
        raise AssertionError("unclosed outline contour")
    if not bounds:
        return None, contours
    xs, ys = zip(*bounds)
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs),
            "height": max(ys) - min(ys)}, contours


class FixtureTests(unittest.TestCase):
    def test_pinned_fixture_integrity_and_formats(self):
        manifest = json.loads((FONTS / "manifest.json").read_text(encoding="utf-8"))
        for entry in [*manifest["files"], manifest["generated"]]:
            with self.subTest(file=entry["name"]):
                self.assertEqual(hashlib.sha256((FONTS / entry["name"]).read_bytes()).hexdigest(),
                                 entry["sha256"])
                if "url" in entry:
                    self.assertRegex(entry["url"], r"https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/")
        self.assertEqual(LATIN.read_bytes()[:4], b"\0\1\0\0")
        self.assertEqual(JAPANESE.read_bytes()[:4], b"OTTO")
        self.assertEqual(COLLECTION.read_bytes()[:4], b"ttcf")
        self.assertEqual(struct.unpack_from(">I", COLLECTION.read_bytes(), 8)[0], 2)
        for name in ("NotoSerif-LICENSE.txt", "NotoSansJP-LICENSE.txt"):
            self.assertIn("SIL OPEN FONT LICENSE", (FONTS / name).read_text(encoding="utf-8"))

    def test_independent_curve_extrema(self):
        quadratic, _ = path_geometry("M0 0 Q10 20 20 0 Z")
        self.assertEqual(quadratic, {"x": 0, "y": 0, "width": 20, "height": 10})
        cubic, _ = path_geometry("M0 0 C0 20 20 20 20 0 Z")
        self.assertEqual(cubic, {"x": 0, "y": 0, "width": 20, "height": 15})


class TextToSVGCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get("TEST_CLI_LANGUAGE")
        if selected == "none":
            raise unittest.SkipTest("CLI execution explicitly disabled")
        if selected not in (None, "cpp"):
            raise RuntimeError("Text-to-SVG selects only the C++ engine")
        override = os.environ.get("TEXT_TO_SVG_EXECUTABLE")
        if override:
            cls.executable = Path(override).resolve(strict=True)
            return
        if shutil.which("cmake") is None:
            raise RuntimeError("required CMake is unavailable")
        build = ROOT / "build" / ("test-cli-" + uuid.uuid4().hex)
        build.mkdir(parents=True)
        cls.addClassCleanup(shutil.rmtree, build)
        commands = [
            ["cmake", "-S", str(ROOT / "cli"), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"],
            ["cmake", "--build", str(build), "--config", "Release", "--parallel", "2"],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, timeout=600)
            if result.returncode:
                raise RuntimeError("build failed: " + repr(command) + "\n"
                                   + result.stdout.decode("utf-8", errors="replace")
                                   + result.stderr.decode("utf-8", errors="replace"))
        name = "text-to-svg" + (".exe" if os.name == "nt" else "")
        cls.executable = next((path for path in (build / name, build / "Release" / name)
                               if path.is_file()), None)
        if cls.executable is None:
            raise RuntimeError("CMake did not produce text-to-svg")

    def run_cli(self, args=("convert",), payload=None, raw=None):
        if raw is None and payload is not None:
            raw = json_line(payload)
        return subprocess.run([os.fspath(self.executable), *args], input=raw,
                              capture_output=True, timeout=20)

    def request(self, text="OB", font=LATIN, **changes):
        request = {"protocol": 1, "fontFile": str(font), "faceIndex": 0,
                   "text": text, "fontSize": 10, "units": "mm"}
        request.update(changes)
        return request

    def assert_json_line(self, stream):
        self.assertTrue(stream.endswith(b"\n"), repr(stream))
        self.assertEqual(stream.count(b"\n"), 1, repr(stream))
        return json.loads(stream.decode("utf-8"))

    def assert_error(self, request=None, code=None, raw=None, args=("convert",)):
        result = self.run_cli(args=args, payload=request, raw=raw)
        self.assertEqual(result.returncode, 2, repr(result))
        self.assertEqual(result.stdout, b"")
        error = self.assert_json_line(result.stderr)
        self.assertEqual(error["protocol"], 1)
        self.assertIsInstance(error["error"]["code"], str)
        self.assertTrue(error["error"]["code"])
        if code:
            self.assertEqual(error["error"]["code"], code)
        self.assertIsInstance(error["error"]["message"], str)
        self.assertTrue(error["error"]["message"])

    def convert(self, text="OB", font=LATIN, **changes):
        request = self.request(text, font, **changes)
        result = self.run_cli(payload=request)
        self.assertEqual(result.returncode, 0, repr(result))
        self.assertEqual(result.stderr, b"")
        self.assertLessEqual(len(result.stdout), 16 * 1024 * 1024)
        value = self.assert_json_line(result.stdout)
        self.assertEqual(value["protocol"], 1)
        self.assertIs(value["ok"], True)
        self.assertEqual(value["fontSize"], request["fontSize"])
        self.assertEqual(value["units"], request["units"])
        self.assertIs(type(value["glyphCount"]), int)
        self.assertGreaterEqual(value["glyphCount"], 0)
        for axis in ("x", "y"):
            self.assertIsInstance(value["advance"][axis], (int, float))
            self.assertTrue(math.isfinite(value["advance"][axis]))
        self.assertAlmostEqual(value["advance"]["y"], 0, places=6)
        svg = ET.fromstring(value["svg"])
        self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertFalse(any(node.tag.rsplit("}", 1)[-1] in ("text", "tspan", "image", "use")
                             for node in svg.iter()))
        paths = list(svg.iter("{http://www.w3.org/2000/svg}path"))
        self.assertTrue(all(node.get("d", "") == value["pathData"] for node in paths))
        self.assertLessEqual(len(paths), 1)
        self.assertIsInstance(value["pathData"], str)
        measured, _ = path_geometry(value["pathData"])
        viewbox = list(map(float, re.split(r"[\s,]+", svg.attrib["viewBox"].strip())))
        self.assertEqual(len(viewbox), 4)
        if measured is None:
            self.assertIsNone(value["inkBounds"])
            expected = [0, 0, max(value["advance"]["x"], request["fontSize"]), request["fontSize"]]
        else:
            self.assertEqual(len(paths), 1)
            self.assertEqual(set(value["inkBounds"]), {"x", "y", "width", "height"})
            expected = [measured[key] for key in ("x", "y", "width", "height")]
            for key, actual in value["inkBounds"].items():
                self.assertAlmostEqual(actual, measured[key], delta=2e-5)
        for actual, wanted in zip(viewbox, expected):
            self.assertAlmostEqual(actual, wanted, delta=2e-5)
        for attribute, wanted in zip(("width", "height"), viewbox[2:]):
            self.assertRegex(svg.attrib[attribute], "^" + NUMBER + re.escape(request["units"]) + "$")
            self.assertAlmostEqual(float(svg.attrib[attribute][:-len(request["units"])]), wanted, delta=2e-5)
        return value

    def test_version_and_literal_echo(self):
        result = self.run_cli(args=["--version"])
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, b'{"protocol":1,"version":"0.1.0"}\n', b""))
        for literal in ('😀 "quoted" \\backslash; \'$()\'', "世界 café\n\t", "", "--version"):
            with self.subTest(literal=literal):
                result = self.run_cli(args=["--echo", literal])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(self.assert_json_line(result.stdout),
                                 {"protocol": 1, "argument": literal})

    def test_no_arguments_or_unknown_command(self):
        for args in ([], [""], ["unknown"], ["convert", "extra"], ["--version", "extra"],
                     ["--echo"], ["--echo", "a", "b"]):
            with self.subTest(args=args):
                self.assert_error(args=args, code="invalid_arguments")

    def test_quadratic_cubic_contours_and_holes(self):
        for font, curve in ((LATIN, "Q"), (JAPANESE, "C")):
            for text, count in (("O", 2), ("B", 3)):
                with self.subTest(text=text, font=font.name):
                    value = self.convert(text, font=font)
                    self.assertIn(curve, value["pathData"].upper())
                    _, contours = path_geometry(value["pathData"])
                    self.assertEqual(len(contours), count)
                    areas = [sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:])) / 2
                             for points in contours]
                    outer = max(areas, key=abs)
                    self.assertTrue(all(area * outer < 0 for area in areas if area != outer))
                    self.assertLess(value["inkBounds"]["y"], 0, "baseline is zero, SVG y points down")
        japanese = self.convert("日本語あいうアイウ", font=JAPANESE)
        self.assertEqual(japanese["glyphCount"], 9)
        self.assertIn("C", japanese["pathData"].upper())

    def test_baseline_and_descenders(self):
        bounds = self.convert("gyp")["inkBounds"]
        self.assertLess(bounds["y"], 0)
        self.assertGreater(bounds["y"] + bounds["height"], 0)

    def test_absolute_em_scale_from_font_tables(self):
        # In these pinned fonts, A's top is exactly the OS/2 cap height.
        # This catches a wrong CFF/CID scale even when size ratios still agree.
        for font in (LATIN, JAPANESE):
            with self.subTest(font=font.name):
                tables = sfnt_tables(font)
                upem = struct.unpack_from(">H", tables[b"head"], 18)[0]
                cap_height = struct.unpack_from(">h", tables[b"OS/2"], 88)[0]
                self.assertEqual(upem, 1000)
                self.assertGreater(cap_height, 0)
                value = self.convert("A", font=font)
                self.assertAlmostEqual(value["inkBounds"]["y"], -10 * cap_height / upem,
                                       delta=2e-5)
                self.assertAlmostEqual(value["inkBounds"]["height"], 10 * cap_height / upem,
                                       delta=2e-5)

    def test_kerning_and_ligatures(self):
        a = self.convert("A")["advance"]["x"]
        v = self.convert("V")["advance"]["x"]
        pair = self.convert("AV")
        self.assertLess(pair["advance"]["x"], a + v - 0.001)
        self.assertLess(self.convert("ffi")["glyphCount"], 3)

    def test_composed_decomposed_accents(self):
        composed = self.convert("é")
        decomposed = self.convert("e\u0301")
        self.assertEqual(composed["glyphCount"], decomposed["glyphCount"])
        self.assertEqual(composed["pathData"], decomposed["pathData"])
        self.assertEqual(composed["advance"], decomposed["advance"])
        self.convert("café Ångström")

    def test_empty_and_whitespace(self):
        empty = self.convert("")
        self.assertEqual(empty["pathData"], "")
        self.assertIsNone(empty["inkBounds"])
        self.assertEqual(empty["glyphCount"], 0)
        self.assertEqual(empty["advance"], {"x": 0, "y": 0})
        spaces = self.convert("   ")
        self.assertEqual(spaces["pathData"], "")
        self.assertIsNone(spaces["inkBounds"])
        self.assertEqual(spaces["glyphCount"], 3)
        self.assertGreater(spaces["advance"]["x"], 0)

    def test_size_scaling_and_physical_units(self):
        reference = self.convert("OBé", fontSize=10)
        for size in (1, 20, 72.5):
            with self.subTest(size=size):
                value = self.convert("OBé", fontSize=size)
                for key in ("x", "y", "width", "height"):
                    self.assertAlmostEqual(value["inkBounds"][key],
                                           reference["inkBounds"][key] * size / 10, delta=2e-5)
                self.assertAlmostEqual(value["advance"]["x"], reference["advance"]["x"] * size / 10,
                                       delta=2e-5)
        # All four requests describe the same physical em size: one inch.
        inches = self.convert("OB", fontSize=1, units="in")
        for unit, size, per_inch in (("mm", 25.4, 25.4), ("pt", 72, 72), ("px", 96, 96)):
            with self.subTest(unit=unit):
                value = self.convert("OB", fontSize=size, units=unit)
                for key in ("x", "y", "width", "height"):
                    self.assertAlmostEqual(value["inkBounds"][key] / per_inch,
                                           inches["inkBounds"][key], delta=2e-5)

    def test_collection_and_optional_face_index(self):
        expected = self.convert("AV ffi é")
        value = self.convert("AV ffi é", font=COLLECTION)
        self.assertEqual(value["pathData"], expected["pathData"])
        self.assertEqual(value["advance"], expected["advance"])
        request = self.request()
        del request["faceIndex"]
        result = self.run_cli(payload=request)
        self.assertEqual(result.returncode, 0, repr(result))
        self.assertEqual(result.stderr, b"")

    def test_collection_positive_nonzero_face_selection(self):
        japanese = self.convert("日本語あいうアイウ", font=COLLECTION, faceIndex=1)
        direct = self.convert("日本語あいうアイウ", font=JAPANESE)
        self.assertEqual(japanese["pathData"], direct["pathData"])
        self.assertEqual(japanese["advance"], direct["advance"])
        self.assertEqual(japanese["inkBounds"], direct["inkBounds"])
        self.assert_error(self.request("日本語", font=COLLECTION, faceIndex=0),
                          code="missing_glyph")
        latin = self.convert("OB", font=COLLECTION, faceIndex=0)
        cff_latin = self.convert("OB", font=COLLECTION, faceIndex=1)
        self.assertNotEqual(latin["pathData"], cff_latin["pathData"])
        self.assertEqual(cff_latin["pathData"], self.convert("OB", font=JAPANESE)["pathData"])

    def test_literal_font_path(self):
        folder = ROOT / "build" / ("fixture path 日本 " + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        copied = folder / ('font.ttf' if os.name == "nt" else 'font \' " ; $.ttf')
        shutil.copyfile(LATIN, copied)
        self.assertEqual(self.convert("OB", font=copied)["pathData"], self.convert("OB")["pathData"])

    def test_missing_glyph_no_fallback(self):
        for text in ("日本語", "😀"):
            with self.subTest(text=text):
                self.assert_error(self.request(text), code="missing_glyph")
        self.assert_error(self.request("\U0010ffff"))

    def test_single_line_ltr_and_single_script(self):
        for text in ("a\nb", "a\rb", "a\tb", "a\0b", "a\u2028b", "a\u2029b",
                     "שלום", "مرحبا", "aא", "a日本", "aБ", "\u202eabc",
                     "a\u200db", "a\ufe0f"):
            with self.subTest(text=text):
                self.assert_error(self.request(text))

    def test_invalid_font_file_and_index(self):
        self.assert_error(self.request(font=FONTS / "does-not-exist.ttf"))
        self.assert_error(self.request(font=FONTS))
        self.assert_error(self.request(font=FONTS / "NotoSerif-LICENSE.txt"))
        for font in (LATIN, JAPANESE, COLLECTION):
            for face in (-1, 2 if font == COLLECTION else 1, 1000000, 0.5, True, None, "0"):
                with self.subTest(font=font.name, face=face):
                    self.assert_error(self.request(font=font, faceIndex=face))

    def test_invalid_fields(self):
        for key, invalid in {
            "protocol": (None, True, 0, 2, "1", [], {}),
            "fontFile": (None, 42, False, [], {}, "", "bad\0font.ttf"),
            "text": (None, 42, False, [], {}),
            "fontSize": (None, True, 0, -1, "10", [], {}, 1e308),
            "units": (None, True, "", "cm", "MM", [], {}),
        }.items():
            for value in invalid:
                with self.subTest(key=key, value=value):
                    self.assert_error(self.request(**{key: value}))
        for key in ("protocol", "fontFile", "text", "fontSize", "units"):
            request = self.request()
            del request[key]
            with self.subTest(missing=key):
                self.assert_error(request)

    def test_malformed_input_and_invalid_utf8(self):
        for raw in (b"", b"{", b"null", b"[]", b"true", b"1", b'{"protocol":1}{}',
                    b'{"text":"\xff"}', b'{"text":"\xc0\xaf"}', b'{"text":"\\ud800"}',
                    b'{"text":"\\udfff"}', b'{"fontSize":NaN}', b'{"fontSize":Infinity}'):
            with self.subTest(raw=raw):
                self.assert_error(raw=raw)

    @unittest.skipUnless(os.name == "posix", "byte argv is a POSIX interface")
    def test_invalid_utf8_arguments(self):
        for raw in (b"\x80", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"):
            with self.subTest(raw=raw):
                self.assert_error(args=[b"--echo", raw])

    def test_byte_limits(self):
        # Multibyte text must be counted as UTF-8 bytes, not Unicode scalars.
        self.assert_error(self.request("é" * 8193), code="limit_exceeded")
        self.assert_error(self.request("a" * 16385), code="limit_exceeded")
        self.assert_error(raw=b" " * (1024 * 1024 + 1), code="limit_exceeded")
        folder = ROOT / "build" / ("large-font-" + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        font = folder / "oversize.ttf"
        with font.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        self.assert_error(self.request(font=font), code="limit_exceeded")

    def test_exact_input_limits_are_accepted(self):
        value = self.convert(" " * 16384)
        self.assertEqual(value["glyphCount"], 16384)
        request = json_line(self.request("O"))
        raw = request + b" " * (1024 * 1024 - len(request))
        result = self.run_cli(raw=raw)
        self.assertEqual(result.returncode, 0, repr(result))
        self.assertEqual(result.stderr, b"")
        self.assertIs(self.assert_json_line(result.stdout)["ok"], True)

    def test_output_limit_is_a_structured_failure(self):
        # A dense CFF outline repeated within the input limit exceeds 16 MiB.
        self.assert_error(self.request("鬱" * 5461, font=JAPANESE), code="output_limit")


if __name__ == "__main__":
    unittest.main()
