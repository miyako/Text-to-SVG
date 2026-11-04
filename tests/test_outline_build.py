import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_config
from build_cli import smoke_test


class OutlineBuildTests(unittest.TestCase):
    def test_smoke_stdin_uses_checkout_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ('checkout path' if os.name == "nt" else 'checkout "quoted" path')
            root.mkdir()
            (root / "tests").mkdir()
            contract = root / "tests/cli-contract.json"
            contract.write_text(json.dumps({"schema": 1, "cases": [
                {"args": ["convert"], "stdin": '{"fontFile":"$ROOT/font.ttf"}', "exitCode": 0,
                 "stdout": "success\n", "stderr": ""},
                {"args": ["invalid"], "exitCode": 2, "stdout": "", "stderr": "error\n"}
            ]}))
            with patch("build_cli.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, b"success\n", b""),
                subprocess.CompletedProcess([], 2, b"", b"error\n")
            ]) as run:
                smoke_test(root / "tool", "0.1.0", contract)
            request = json.loads(run.call_args_list[0].kwargs["input"])
            self.assertEqual(request["fontFile"], root.resolve().as_posix() + "/font.ttf")
            self.assertIsNone(run.call_args_list[1].kwargs["input"])

    def test_vendor_cdn_still_requires_exact_checksum_and_https_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("tool4d.app/Contents/MacOS/tool4d", b"test vendor binary")
            data = buffer.getvalue()
            digest = hashlib.sha256(data).hexdigest()

            def response(url):
                result = io.BytesIO(data)
                result.geturl = lambda: url
                return result

            for url in ("http://builds2.4d.com/tool.zip",
                        "https://builds2.4d.com.evil.example/tool.zip",
                        "https://@builds2.4d.com/tool.zip",
                        "https://user@builds2.4d.com/tool.zip"):
                with patch.object(ci_config.urllib.request.OpenerDirector, "open", return_value=response(url)):
                    with self.assertRaisesRegex(ValueError, "origin"):
                        ci_config.download_tool4d(
                            root, url="https://resources-download.4d.com/20.7/tool.zip", digest=digest)
            with patch.object(ci_config.urllib.request.OpenerDirector, "open",
                              return_value=response("https://builds2.4d.com/fixed.zip")):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    ci_config.download_tool4d(
                        root, url="https://resources-download.4d.com/20.7/tool.zip", digest="0" * 64)
            with patch.object(ci_config.urllib.request.OpenerDirector, "open",
                              return_value=response("https://builds2.4d.com/fixed.zip")):
                executable = ci_config.download_tool4d(
                    root, url="https://resources-download.4d.com/20.7/tool.zip", digest=digest)
            self.assertEqual(executable.read_bytes(), b"test vendor binary")

    def test_every_redirect_hop_is_checked(self):
        handler = ci_config.ToolRedirectHandler()
        request = ci_config.urllib.request.Request("https://resources-download.4d.com/fixed.tar.xz")
        for url in ("http://builds2.4d.com/file", "https://evil.example/file",
                    "https://builds2.4d.com:444/file"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "origin"):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://builds2.4d.com/fixed.tar.xz")
        self.assertEqual(redirected.host, "builds2.4d.com")
