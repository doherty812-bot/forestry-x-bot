#!/usr/bin/env python3
"""Actions 承認経路向けのユニットテスト（ネットワーク・実投稿なし）。"""

import http.server
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from scripts import fetch_draft_from_artifacts as fetch


class TestConfirmAndProvider(unittest.TestCase):
    def test_confirm_rejects_false(self):
        with self.assertRaises(SystemExit):
            fetch.require_confirm_flag("false")
        with self.assertRaises(SystemExit):
            fetch.require_confirm_flag("")

    def test_confirm_accepts_true(self):
        fetch.require_confirm_flag("true")
        fetch.require_confirm_flag("1")

    def test_provider_validation(self):
        self.assertEqual(fetch.validate_provider("Grok"), "grok")
        with self.assertRaises(SystemExit):
            fetch.validate_provider("claude")


class TestFindDraftJson(unittest.TestCase):
    def test_finds_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "run-1"
            nested.mkdir()
            path = nested / "20260924T062759Z-1200.json"
            path.write_text('{"id":"20260924T062759Z-1200"}', encoding="utf-8")
            found = fetch.find_draft_json(root, "20260924T062759Z-1200")
            self.assertEqual(found, path)

    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch.find_draft_json(Path(tmp), "nope"))


class TestAlreadyApproved(unittest.TestCase):
    def test_detects_draft_in_run_name(self):
        runs = [
            {
                "name": "Approve 20260924T062759Z-1200 (grok)",
                "conclusion": "success",
            }
        ]
        self.assertTrue(
            fetch.already_approved_in_runs(runs, "20260924T062759Z-1200")
        )

    def test_ignores_other_drafts(self):
        runs = [
            {
                "name": "Approve 20260924T000000Z-1200 (openai)",
                "conclusion": "success",
            }
        ]
        self.assertFalse(
            fetch.already_approved_in_runs(runs, "20260924T062759Z-1200")
        )


class TestExtractZip(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "abc.json").write_text('{"id":"abc"}', encoding="utf-8")
            zip_path = Path(tmp) / "a.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.write(src / "abc.json", arcname="abc.json")
            dest = Path(tmp) / "out"
            fetch.extract_zip_to(zip_path.read_bytes(), dest)
            self.assertTrue((dest / "abc.json").exists())


class _RedirectAuthHandler(http.server.BaseHTTPRequestHandler):
    """GitHub→Azure 相当: /zip は Auth 必須 302、/blob は Auth 付きなら 401。"""

    payload = b"PK\x03\x04-fake-zip-bytes"

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/zip"):
            if self.headers.get("Authorization") != "Bearer test-token":
                self.send_error(401, "missing github auth")
                return
            self.send_response(302)
            self.send_header("Location", "/blob?sig=sas")
            self.end_headers()
            return
        if self.path.startswith("/blob"):
            if self.headers.get("Authorization"):
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Server failed to authenticate the request.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        self.send_error(404)


class TestDownloadBytesRedirect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._httpd = socketserver.TCPServer(("127.0.0.1", 0), _RedirectAuthHandler)
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._httpd.shutdown()
        cls._httpd.server_close()

    def test_download_succeeds_when_auth_stripped_on_redirect(self):
        url = f"http://127.0.0.1:{self._port}/zip"
        data = fetch._download_bytes(url, "test-token")
        self.assertEqual(data, _RedirectAuthHandler.payload)

    def test_default_urlopen_keeps_auth_and_fails(self):
        """回帰: urllib 既定の follow だと Auth が残り 401 になることを固定。"""
        url = f"http://127.0.0.1:{self._port}/zip"
        req = urllib.request.Request(
            url,
            headers={"Authorization": "Bearer test-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_strip_handler_removes_authorization(self):
        from email.message import Message

        handler = fetch._StripAuthOnRedirectHandler()
        req = urllib.request.Request(
            "http://example.test/zip",
            headers={
                "Authorization": "Bearer secret",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "test",
            },
        )
        hdrs = Message()
        hdrs["Location"] = "http://blob.example.test/file?sig=1"
        new_req = handler.redirect_request(
            req, None, 302, "Found", hdrs, "http://blob.example.test/file?sig=1"
        )
        self.assertIsNotNone(new_req)
        self.assertFalse(new_req.has_header("Authorization"))
        self.assertFalse(new_req.has_header("X-GitHub-Api-Version"))


if __name__ == "__main__":
    unittest.main()
