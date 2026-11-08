import http.client
import tempfile
import threading
import unittest
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
