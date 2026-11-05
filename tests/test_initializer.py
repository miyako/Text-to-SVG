import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("initialize", ROOT / "scripts" / "initialize.py")
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class InitializerTests(unittest.TestCase):
    def setUp(self):
        if json.loads((ROOT / "template.json").read_text())["initialized"]:
            self.skipTest("fresh-template initializer tests require retained cli-starters/")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("component", "cli-starters", "tests/host"):
            shutil.copytree(ROOT / name, self.root / name)
        for name in ("template.json", "package.json", "README.md"):
            shutil.copy2(ROOT / name, self.root / name)

    def configure(self, language="go", **changes):
        args = dict(repository="hello-tools", namespace="AcmeHello", cli="hello-cli",
                    language=language, feature="A deterministic greeting")
        args.update(changes)
        return initializer.initialize(self.root, **args)

    def test_each_language_names_namespace_docs_and_resources(self):
        for language in ("go", "rust", "cpp"):
            with self.subTest(language=language):
                # Each language starts with an untouched template.
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                destination = Path(temporary.name) / "checkout"
                shutil.copytree(self.root, destination)
                initializer.initialize(destination, "hello-tools", "AcmeHello", "hello-cli", language, "Greeting")
                config = json.loads((destination / "template.json").read_text())
                self.assertEqual(config["language"], language)
                self.assertTrue(config["initialized"])
                component = destination / "component" / "hello-tools"
                self.assertTrue((component / "Project" / "hello-tools.4DProject").is_file())
                self.assertFalse((destination / "component" / "4d-component-template").exists())
                self.assertFalse((destination / "cli-starters").exists())
                manifests = {"go": "go.mod", "rust": "Cargo.toml", "cpp": "CMakeLists.txt"}
                self.assertTrue((destination / "cli" / manifests[language]).is_file())
                settings = ET.parse(component / "Project" / "Sources" / "settings.4DSettings")
                self.assertEqual(settings.find(".//general").attrib["component_classStore_name"], "AcmeHello")
                catalog = ET.parse(component / "Project" / "Sources" / "catalog.4DCatalog").getroot()
                self.assertEqual(catalog.attrib["name"], "hello-tools")
                self.assertEqual(catalog.attrib["uuid"], uuid.uuid5(uuid.NAMESPACE_URL, "hello-tools/AcmeHello").hex.upper())
                self.assertEqual(json.loads((destination / "package.json").read_text())["name"], "hello-tools")
                self.assertEqual(json.loads((destination / "tests/host/Project/Sources/dependencies.json").read_text())["dependencies"], {"hello-tools": {}})
                for directory in (component, destination / "cli", destination / "tests/host"):
                    for path in directory.rglob("*"):
                        if path.is_file() and path.suffix != ".png":
                            text = path.read_text(encoding="utf-8")
                            self.assertNotIn("TemplateCLI", text, path)
                            self.assertNotIn("4d-component-template", text, path)
                            self.assertNotIn("component-cli", text, path)
                self.assertIn("cs.AcmeHello.Greeting", (destination / "README.md").read_text())
                self.assertTrue((component / "Resources" / "logo.png").is_file())
                notices = {"go": "Go-LICENSE.txt", "rust": "Rust-LICENSE-MIT.txt"}
                if language in notices:
                    self.assertEqual((component / "Resources/licenses" / notices[language]).read_bytes(),
                                     (destination / "cli/licenses" / notices[language]).read_bytes())

    def test_idempotence_and_reconfiguration_rejection(self):
        self.assertTrue(self.configure())
        files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertFalse(self.configure())
        self.assertEqual(files, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        with self.assertRaisesRegex(ValueError, "already initialized"):
            self.configure(namespace="AnotherNamespace")

    def test_invalid_inputs_do_not_mutate(self):
        cases = [
            {"repository": "../escape"}, {"repository": "owner/name"}, {"repository": "name.with.dot"},
            {"repository": "4d-component-template"}, {"repository": ""}, {"repository": "a" * 81},
            {"repository": "CON"}, {"repository": "Lpt9"},
            {"namespace": "cs"}, {"namespace": "TemplateCLI"}, {"namespace": "123abc"},
            {"namespace": "Name.Space"}, {"namespace": "name-with-dash"}, {"namespace": "a" * 32},
            {"cli": "../../bad"}, {"cli": "BadName"}, {"cli": "x; echo"}, {"cli": "a" * 64},
            {"cli": "nul"}, {"cli": "com1"},
            {"language": "python"}, {"feature": ""}, {"feature": "bad\nline"}, {"feature": "x" * 201},
        ]
        original = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.configure(**changes)
            self.assertEqual(original, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_missing_project_does_not_partially_initialize(self):
        (self.root / "component/4d-component-template/Project/4d-component-template.4DProject").unlink()
        with self.assertRaisesRegex(ValueError, ".4DProject"):
            self.configure()
        self.assertFalse((self.root / "cli").exists())
        self.assertFalse(json.loads((self.root / "template.json").read_text())["initialized"])

    def test_case_and_substitution_collision(self):
        self.configure(repository="TemplateCLI", namespace="OtherSpace", cli="component-cli")
        settings = ET.parse(self.root / "component/TemplateCLI/Project/Sources/settings.4DSettings")
        self.assertEqual(settings.find(".//general").attrib["component_classStore_name"], "OtherSpace")
        catalog = ET.parse(self.root / "component/TemplateCLI/Project/Sources/catalog.4DCatalog")
        self.assertEqual(catalog.getroot().attrib["name"], "TemplateCLI")

    def test_readme_provenance_urls_are_not_destination_identities(self):
        readme = self.root / "README.md"
        readme.write_text(readme.read_text() + "\nhttps://github.com/miyako/4d-component-template/pull/2\n")
        self.configure()
        self.assertIn("https://github.com/miyako/4d-component-template/pull/2", readme.read_text())
        self.assertIn("component/hello-tools/", readme.read_text())


class IconTests(unittest.TestCase):
    """Committed icons are custom artwork; only structural validity is required."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import validate_icons
        import generate_icons
        self.validate_icons = validate_icons
        self.generate_icons = generate_icons
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.json").write_text(json.dumps({"repository": "Icons"}))
        self.resources = self.root / "component/Icons/Resources"
        self.resources.mkdir(parents=True)
        (self.root / "assets").mkdir()
        shutil.copy(ROOT / "assets/icon-source.svg", self.root / "assets/icon-source.svg")

    def tearDown(self):
        sys.path.remove(str(ROOT / "scripts"))
        self.temp.cleanup()

    def test_committed_icons_are_valid(self):
        self.validate_icons.validate(ROOT)

    def test_custom_icons_accepted_without_matching_generator(self):
        png = (ROOT / "component" / json.loads((ROOT / "template.json").read_text())["repository"]
               / "Resources/logo.png").read_bytes()
        (self.resources / "logo.png").write_bytes(png)
        (self.resources / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.validate_icons.validate(self.root)

    def test_invalid_icons_rejected(self):
        good = ROOT / "component" / json.loads((ROOT / "template.json").read_text())["repository"] / "Resources"
        cases = [
            ("logo.png", b"not a png", "signature"),
            ("logo.png", (good / "logo.png").read_bytes()[:20], "truncated"),
            ("logo.png", (good / "logo.png").read_bytes()[:29] + b"\0\0\0\0", "checksum"),
            ("logo.svg", b"<svg", "."),
            ("logo.svg", b"<html/>", "not an SVG"),
        ]
        for name, content, message in cases:
            with self.subTest(name=name, message=message):
                shutil.copy(good / "logo.png", self.resources / "logo.png")
                (self.resources / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
                (self.resources / name).write_bytes(content)
                with self.assertRaisesRegex(Exception, message):
                    self.validate_icons.validate(self.root)
        (self.resources / "logo.svg").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.validate_icons.validate(self.root)

    def test_generator_bootstraps_but_never_overwrites(self):
        self.generate_icons.generate(self.root)
        self.validate_icons.validate(self.root)
        custom = b"custom"
        (self.resources / "logo.png").write_bytes(custom)
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.generate_icons.generate(self.root)
        self.assertEqual((self.resources / "logo.png").read_bytes(), custom)
