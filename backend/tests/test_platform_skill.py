"""Portable client behavior, verified against a local HTTP service."""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/prt-platform/scripts/prt_api.py"
spec = importlib.util.spec_from_file_location("prt_skill_client", SCRIPT)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class PlatformHandler(BaseHTTPRequestHandler):
    requests = []
    upload = None

    def log_message(self, *args):
        pass

    def send_json(self, value, code=200):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).requests.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/redirect":
            self.send_response(302)
            self.send_header("Location", "/api/leak")
            self.end_headers()
        elif self.path == "/api/partial":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"partial")
            self.close_connection = True
        elif self.path == "/api/binary":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ZIP")
        elif self.path == "/api/openapi.json":
            self.send_json({"openapi": "3.1.0", "info": {"version": "test"},
                "paths": {"/api/projects": {"post": {"summary": "Create project", "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProjectCreate"}}}}}}},
                "components": {"schemas": {"ProjectCreate": {"type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string"}, "api_key": {"type": "string"}}}}}})
        elif self.path == "/api/pending":
            self.send_json({"status": "running", "id": "job_test"})
        elif self.path == "/api/review":
            self.send_json({"status": "pending_review", "id": "job_test"})
        elif self.path == "/api/failed":
            self.send_json({"status": "failed", "id": "job_test"})
        else:
            self.send_json({"status": "ok", "password": "must-hide", "api_key": "must-hide",
                "must_change_password": False, "nested": {"ACCESS_TOKEN": "must-hide"}})

    def do_POST(self):
        type(self).requests.append(("POST", self.path, self.headers.get("Authorization")))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/auth/login":
            value = json.loads(body)
            if value["password"] != "test secret ":
                return self.send_json({"detail": "invalid credentials"}, 401)
            self.send_json({"access_token": "test-platform-token", "expires_in": 600,
                "user": {"id": "usr_test", "email": value["email"], "role": "user", "must_change_password": False}})
        elif self.path == "/api/datasets":
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            type(self).upload = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            self.send_json({"id": "ds_test", "status": "ready"}, 201)
        elif self.path == "/api/uncertain":
            self.close_connection = True
        elif self.path == "/api/error":
            self.send_json({"detail": "received " + json.loads(body)["api_key"]}, 400)
        else:
            self.send_json({"id": "prj_test", **json.loads(body)}, 201)


class PlatformSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PlatformHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prt-skill-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.client = api.Client(self.url, "test-platform-token", timeout=2)
        PlatformHandler.requests = []

    def test_login_cli_preserves_password_spaces_and_hides_token(self):
        session = self.root / "session.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "--session", str(session), "login",
            "--url", self.url + "/login", "--email", "agent@example.com", "--password-stdin"],
            input="test secret \n", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test-platform-token", result.stdout + result.stderr)
        self.assertNotIn("test secret", result.stdout + result.stderr)
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(api.Client.from_session(session).base, self.url)
        self.assertFalse(json.loads(result.stdout)["user"]["must_change_password"])
        self.assertNotIn("password", json.loads(session.read_text()))

    def test_api_origin_and_url_normalization(self):
        self.assertEqual(api.normalize_base(self.url + "/jobs/job_123?project=x"), self.url)
        self.assertEqual(api.normalize_base(self.url + "/prefix/api"), self.url + "/prefix")
        url = api.api_url(self.url, "/api/jobs", [("q", "中文 空格"), ("status", "running"), ("status", "queued")])
        self.assertIn("status=running&status=queued", url)
        for path in ["https://elsewhere.example/api/jobs", "//elsewhere.example/jobs", "/%2e%2e/auth", "/jobs#fragment"]:
            with self.assertRaises(api.ClientError):
                self.client.request("GET", path)
        self.assertEqual(PlatformHandler.requests, [])

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaisesRegex(api.ClientError, "重定向"):
            self.client.request("GET", "/redirect")
        self.assertEqual([r[1] for r in PlatformHandler.requests], ["/api/redirect"])

    def test_response_and_error_redaction(self):
        result = self.client.request("GET", "/auth/me")
        self.assertNotIn("must-hide", json.dumps(result))
        self.assertFalse(result["must_change_password"])
        with self.assertRaises(api.ClientError) as error:
            self.client.request("POST", "/error", payload={"api_key": "supplied-test-secret"})
        self.assertNotIn("supplied-test-secret", str(error.exception))

    def test_mutation_with_unknown_outcome_is_never_retried(self):
        with self.assertRaisesRegex(api.ClientError, "结果未知"):
            self.client.request("POST", "/uncertain", payload={"name": "one job"})
        self.assertEqual(len(PlatformHandler.requests), 1)

    def test_streaming_multipart_upload_preserves_archive_bytes(self):
        archive = self.root / "题包.zip"
        content = b"archive" * 200000
        archive.write_bytes(content)
        result = self.client.upload(archive, {"name": "中文题包", "kind": "harbor_tasks", "project_id": "prj_test"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(PlatformHandler.upload["file"], content)
        self.assertEqual(PlatformHandler.upload["name"].decode(), "中文题包")
        self.assertEqual(PlatformHandler.requests[0][2], "Bearer test-platform-token")

    def test_download_completeness_and_existing_file_protection(self):
        target = self.root / "result.zip"
        with self.assertRaises(api.ClientError):
            self.client.request("GET", "/partial", output=target)
        self.assertFalse(target.exists())
        result = self.client.request("GET", "/binary", output=target)
        self.assertEqual(result["bytes"], 3)
        with self.assertRaises(api.ClientError):
            self.client.request("GET", "/binary", output=target)
        self.assertEqual(target.read_bytes(), b"ZIP")

    def test_openapi_selection_keeps_schema_for_sensitive_field_names(self):
        document = self.client.request("GET", "/openapi.json", schema=True)
        result = api.schema_selection(document, "/projects", "POST", None, "")
        self.assertEqual(result["components"]["ProjectCreate"]["properties"]["api_key"], {"type": "string"})
        self.assertEqual(api.schema_selection(document, None, "GET", None, "projects")["operations"][0]["method"], "POST")

    def test_wait_is_bounded_and_distinguishes_review_from_success(self):
        start = time.monotonic()
        result = api.wait_resource(self.client, "/pending", {"succeeded"}, {"failed"}, interval=0.01, max_seconds=0.1)
        self.assertEqual(result["wait_state"], "pending")
        self.assertLess(time.monotonic() - start, 1)
        for path, expected in [("/review", "attention"), ("/failed", "failed")]:
            result = api.wait_resource(self.client, path, {"succeeded"}, {"failed"}, interval=1, max_seconds=1)
            self.assertEqual(result["wait_state"], expected)


if __name__ == "__main__":
    unittest.main()
