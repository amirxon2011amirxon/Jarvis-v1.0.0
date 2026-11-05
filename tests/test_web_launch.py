"""Launcher helpers: free ports and the loopback boundary."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from web import launch
from web import server


class PortTests(unittest.TestCase):
    def test_find_free_port_skips_an_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            found = launch.find_free_port(port, attempts=8)
            self.assertNotEqual(found, port)
            self.assertGreater(found, 0)

    def test_port_open_reflects_a_listening_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(launch.port_open(port))
        self.assertFalse(launch.port_open(port, timeout=0.1))


class BoundaryTests(unittest.TestCase):
    def test_loopback_hosts_only(self):
        self.assertTrue(server._host_is_loopback("127.0.0.1:8765"))
        self.assertTrue(server._host_is_loopback("localhost:8765"))
        self.assertTrue(server._host_is_loopback("[::1]:8765"))
        self.assertFalse(server._host_is_loopback("evil.example:8765"))
        self.assertFalse(server._host_is_loopback(""))

    def test_origin_must_match_the_host_or_a_dev_origin(self):
        self.assertTrue(server._origin_allowed(None, "127.0.0.1:8765"))
        self.assertTrue(server._origin_allowed(
            "http://127.0.0.1:8765", "127.0.0.1:8765"))
        self.assertFalse(server._origin_allowed(
            "http://127.0.0.1:9999", "127.0.0.1:8765"))
        self.assertFalse(server._origin_allowed(
            "https://evil.example", "127.0.0.1:8765"))
        with mock.patch.dict(os.environ, {
            "AZE_DEV_ORIGINS": "http://127.0.0.1:5173"}):
            self.assertTrue(server._origin_allowed(
                "http://127.0.0.1:5173", "127.0.0.1:8765"))


class MissingFrontendTests(unittest.TestCase):
    def test_launcher_refuses_to_start_without_assets(self):
        with mock.patch.object(launch, "DIST_INDEX",
                               launch.ROOT / "no-such-index.html"):
            code = launch.main(["--no-browser"])
        self.assertEqual(code, 2)

    def test_windows_break_and_terminate_use_normal_cleanup(self):
        installed = {}

        def install(signum, handler):
            installed[signum] = handler

        with mock.patch.object(launch.signal, "signal",
                               side_effect=install):
            launch._install_shutdown_signals()
        self.assertTrue(installed)
        handler = next(iter(installed.values()))
        with self.assertRaises(KeyboardInterrupt):
            handler(1, None)


class _HealthResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ExistingInstanceTests(unittest.TestCase):
    """Typing `aze` twice must reuse the running app, never start a second."""

    def test_aze_health_is_recognised(self):
        with mock.patch.object(
                launch.urllib.request, "urlopen",
                return_value=_HealthResponse({"app": "Jarvis"})):
            self.assertTrue(launch.existing_instance(8765))

    def test_another_service_on_the_port_is_not_aze(self):
        with mock.patch.object(
                launch.urllib.request, "urlopen",
                return_value=_HealthResponse({"app": "nginx"})):
            self.assertFalse(launch.existing_instance(8765))

    def test_unreachable_port_is_not_aze(self):
        with mock.patch.object(
                launch.urllib.request, "urlopen",
                side_effect=OSError("connection refused")):
            self.assertFalse(launch.existing_instance(8765))

    def test_launcher_reuses_a_running_instance(self):
        with mock.patch.object(launch, "existing_instance", return_value=True), \
                mock.patch.object(launch.webbrowser, "open",
                                  return_value=True) as opened:
            code = launch.main([])
        self.assertEqual(code, 0)
        opened.assert_called_once()

    def test_reuse_honours_no_browser(self):
        with mock.patch.object(launch, "existing_instance", return_value=True), \
                mock.patch.object(launch.webbrowser, "open") as opened:
            code = launch.main(["--no-browser"])
        self.assertEqual(code, 0)
        opened.assert_not_called()


class LauncherProcessTests(unittest.TestCase):
    def test_launcher_serves_the_built_ui_and_setup_screen(self):
        """Run the real launcher with an isolated, keyless config."""
        port = launch.find_free_port(8911)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "browser-opened.txt"
            (directory / "api_keys.json").write_text("{}", encoding="utf-8")
            script = (
                "import config\n"
                "import webbrowser\n"
                f"from pathlib import Path\n"
                f"config.CONFIG_DIR = Path({str(directory)!r})\n"
                "config.CONFIG_FILE = config.CONFIG_DIR / 'api_keys.json'\n"
                "def fake_open(url, *args, **kwargs):\n"
                f"    Path({str(marker)!r}).write_text(url, encoding='utf-8')\n"
                "    return True\n"
                "webbrowser.open = fake_open\n"
                "import web.launch\n"
                "raise SystemExit(web.launch.main("
                f"['--port', '{port}']))\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=str(launch.ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline and not marker.exists():
                    if proc.poll() is not None:
                        output = proc.stdout.read() if proc.stdout else ""
                        self.fail(f"launcher exited early:\n{output}")
                    time.sleep(0.1)
                self.assertTrue(marker.exists(), "launcher did not open a URL")
                url = marker.read_text(encoding="utf-8").strip()
                parsed = urllib.parse.urlsplit(url)
                self.assertEqual(parsed.hostname, "127.0.0.1")
                self.assertTrue(url.startswith("http://127.0.0.1:"))

                health = None
                health_deadline = time.monotonic() + 10
                while time.monotonic() < health_deadline:
                    try:
                        with urllib.request.urlopen(
                                f"{url}/api/health", timeout=0.5) as response:
                            health = json.loads(response.read().decode("utf-8"))
                            break
                    except Exception:
                        time.sleep(0.15)
                self.assertIsNotNone(health, "launcher did not become healthy")
                self.assertFalse(health["configured"])
                with urllib.request.urlopen(f"{url}/", timeout=2) as response:
                    page = response.read().decode("utf-8")
                    status = response.status
                self.assertEqual(status, 200)
                self.assertIn('<div id="root">', page)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
