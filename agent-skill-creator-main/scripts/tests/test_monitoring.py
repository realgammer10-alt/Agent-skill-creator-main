"""Tests for persisted dependency monitoring and its local HTTP dashboard."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import monitor
from monitoring import HealthHistory, create_dashboard_handler


def _result(
    status: str,
    checked_at: str,
    *,
    name: str = "Example API",
    url: str = "https://example.com/v1",
    http_status: int | None = 200,
) -> dict:
    return {
        "name": name,
        "url": url,
        "checked_at": checked_at,
        "status": status,
        "http_status": http_status,
        "response_time_ms": 12.5,
        "message": f"HTTP {http_status}" if http_status else "Endpoint unreachable",
    }


class HealthHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "monitor.db"
        self.history = HealthHistory(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persists_latest_status_and_history(self) -> None:
        self.history.record([_result("healthy", "2026-10-28T07:00:00+00:00")])
        self.history.record([_result("unhealthy", "2026-10-28T07:01:00+00:00", http_status=None)])

        current = HealthHistory(self.database).status()
        self.assertEqual(current["status"], "unhealthy")
        self.assertEqual(current["dependency_count"], 1)
        self.assertEqual(current["dependencies"][0]["checked_at"], "2026-10-28T07:01:00+00:00")
        self.assertEqual(len(self.history.history()), 2)
        self.assertEqual(self.history.history()[0]["status"], "unhealthy")

    def test_overall_status_uses_worst_dependency(self) -> None:
        self.history.record([
            _result("healthy", "2026-10-28T07:00:00+00:00"),
            _result(
                "degraded", "2026-10-28T07:00:00+00:00",
                name="Other API", url="https://other.example", http_status=429,
            ),
        ])
        self.assertEqual(self.history.status()["status"], "degraded")

    def test_status_can_exclude_dependencies_removed_from_skill(self) -> None:
        self.history.record([_result("healthy", "2026-10-28T07:00:00+00:00")])
        current = self.history.status(expected=[])
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(current["dependencies"], [])

    def test_rejects_invalid_history_limits_and_records(self) -> None:
        for limit in (0, 1001, True, "1"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.history.history(limit)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.history.record([{"status": "unknown"}])


class DashboardHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.history = HealthHistory(Path(self.temp.name) / "monitor.db")
        self.history.record([_result("healthy", "2026-10-28T07:00:00+00:00")])
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_dashboard_handler(self.history)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_dashboard_and_status_api(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Dependency monitor", html)
            self.assertIn("window.setInterval(refresh, 2000)", html)

        with urlopen(self.base_url + "/api/status") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["dependencies"][0]["name"], "Example API")

    def test_history_api_validates_limit_and_unknown_routes(self) -> None:
        with urlopen(self.base_url + "/api/history?limit=1") as response:
            payload = json.loads(response.read())
        self.assertEqual(len(payload["history"]), 1)

        with self.assertRaises(HTTPError) as invalid:
            urlopen(self.base_url + "/api/history?limit=1001")
        self.assertEqual(invalid.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base_url + "/missing")
        self.assertEqual(missing.exception.code, 404)


class MonitorCommandTest(unittest.TestCase):
    def test_cli_validates_interval_timeout_and_port(self) -> None:
        parser = monitor._build_parser()
        for option, value in (
            ("--interval", "0"),
            ("--interval", "86401"),
            ("--timeout", "0.01"),
            ("--timeout", "301"),
            ("--port", "65536"),
        ):
            with self.subTest(option=option, value=value), self.assertRaises(SystemExit):
                parser.parse_args(["./skill", option, value])

    def test_load_dependencies_requires_valid_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: monitor-test\ndescription: test\nmetadata:\n"
                "  dependencies:\n    - name: Example\n      url: https://example.com\n"
                "---\n",
                encoding="utf-8",
            )
            dependencies = monitor.load_dependencies(skill_dir)
        self.assertEqual(dependencies, [{"name": "Example", "url": "https://example.com"}])

    def test_once_records_and_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: monitor-test\ndescription: test\nmetadata:\n"
                "  dependencies:\n    - name: Example\n      url: https://example.com\n"
                "---\n",
                encoding="utf-8",
            )
            database = skill_dir / "health.db"
            result = _result("healthy", "2026-10-28T07:00:00+00:00")
            with patch("monitor.probe_dependency_health", return_value=[result]):
                from contextlib import redirect_stdout
                from io import StringIO

                output = StringIO()
                with redirect_stdout(output):
                    code = monitor.main([
                        str(skill_dir), "--once", "--database", str(database),
                    ])
            self.assertEqual(code, 0)
            self.assertTrue(database.exists())
            self.assertEqual(json.loads(output.getvalue())["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
