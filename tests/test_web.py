import http.client
import tempfile
import threading
import unittest
from importlib import resources
from pathlib import Path

from kanbot.web import make_server


class AllowedHostsTest(unittest.TestCase):
    def start(self, allowed):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = make_server(Path(tmp.name) / "t.db", 0, allowed_hosts=allowed)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def status(self, port, host, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host, **({"Origin": origin} if origin else {})}
        connection.request("GET", "/api/tickets", headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_extra_hosts_are_allowed_case_insensitively(self):
        port = self.start(["NAS.local", " 192.168.1.10 ", ""])
        self.assertEqual(self.status(port, f"nas.local:{port}"), 200)
        self.assertEqual(self.status(port, f"192.168.1.10:{port}"), 200)
        self.assertEqual(self.status(port, f"localhost:{port}"), 200)
        self.assertEqual(self.status(port, f"evil.example:{port}"), 403)
        self.assertEqual(self.status(port, f"nas.local:{port}", "http://evil.example"), 403)

    def test_wildcard_allows_any_host_but_still_checks_origin(self):
        port = self.start(["*"])
        self.assertEqual(self.status(port, f"anything:{port}"), 200)
        self.assertEqual(self.status(port, f"anything:{port}", "http://evil.example"), 403)

    def test_default_is_local_only(self):
        port = self.start(())
        self.assertEqual(self.status(port, f"nas.local:{port}"), 403)


class StartupTest(unittest.TestCase):
    def test_taken_port_leaves_database_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_server(Path(tmp) / "first.db", 0)
            second = Path(tmp) / "second.db"
            try:
                with self.assertRaises(OSError):
                    make_server(second, first.server_port)
            finally:
                first.server_close()
            self.assertFalse(second.exists())


class StaticRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.tmp.name) / "t.db", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_serves_each_allowed_type_with_its_content_type(self):
        for path, content_type in [
            ("/static/icon.svg", "image/svg+xml"),
            ("/static/index.html", "text/html; charset=utf-8"),
        ]:
            with self.subTest(path=path):
                response, body = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), content_type)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                disk = resources.files("kanbot").joinpath("static", *path.split("/")[2:])
                self.assertEqual(body, disk.read_bytes())

    def test_path_traversal_is_not_found(self):
        for path in [
            "/static/../web.py",
            "/static/../../pyproject.toml",
            "/static/css/../../web.py",
            "/static/%2e%2e/web.py",
            "/static/%2E%2E/web.py",
            "/static/..%2fweb.py",
            "/static/..%2Fweb.py",
            "/static/css/..%2f..%2fweb.py",
            "/static/%2e%2e%2fweb.py",
            "/static/%252e%252e/web.py",
            "/static/..%5cweb.py",
            "/static/..\\web.py",
            "/static//etc/passwd",
            "/static/%2fetc/passwd",
            "/static/%2Fetc%2Fpasswd",
            "/static/css/./tokens.css",
            "/static/css//tokens.css",
            "/static/icon.svg%00.py",
            "/static/icon.svg%0a",
            "/static/./icon.svg",
            "/static/.hidden.css",
            "/static/%ff.css",
            "/etc/passwd",
            "/static/..",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0].status, 404)

    def test_unknown_files_directories_and_extensions_are_not_found(self):
        for path in [
            "/static/missing.css",
            "/static/js/missing.js",
            "/static/css",
            "/static/css/",
            "/static/",
            "/static",
            "/static/js/main.js/",
            "/static/__init__.py",
            "/static/js/main.js.map",
            "/static/notes.txt",
            "/static/icon",
            "/static/ICON.SVG.bak",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0].status, 404)

    def test_host_and_origin_checks_apply(self):
        port = self.server.server_port
        response, _ = self.get("/static/icon.svg", {"Host": f"evil.example:{port}"})
        self.assertEqual(response.status, 403)
        response, _ = self.get("/static/icon.svg", {"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.get("/static/icon.svg", {"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(response.status, 200)

    def test_only_get_is_routed(self):
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", "/static/icon.svg", body=b"{}")
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()
