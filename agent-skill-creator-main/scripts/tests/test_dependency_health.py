"""Tests for scripts.dependency_health — HTTP HEAD probes (mocked, no network)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dependency_health import (
    check_dependency_health,
    probe_dependency_health,
)


def _mock_response(status: int) -> MagicMock:
    """Build a context-manager-compatible mock with .status = status."""
    resp = MagicMock()
    resp.status = status
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None
    return cm


class DependencyHealthTest(unittest.TestCase):
    @patch("dependency_health.urlopen")
    def test_2xx_is_healthy_info(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(200)
        issues = check_dependency_health([{"name": "API", "url": "https://example.com/v1"}])
        self.assertTrue(any(i["level"] == "info" and "healthy" in i["message"] for i in issues))
        self.assertFalse(any(i["level"] in ("warning", "error") for i in issues))

    @patch("dependency_health.urlopen")
    def test_4xx_is_warning(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(404)
        issues = check_dependency_health([{"name": "Gone", "url": "https://example.com/old"}])
        self.assertTrue(any(i["level"] == "warning" and "client error" in i["message"] for i in issues))

    @patch("dependency_health.urlopen")
    def test_5xx_is_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(503)
        issues = check_dependency_health([{"name": "API", "url": "https://example.com"}])
        self.assertTrue(any(i["level"] == "error" and "server error" in i["message"] for i in issues))

    @patch("dependency_health.urlopen")
    def test_urlerror_is_unreachable(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = URLError("Name or service not known")
        issues = check_dependency_health([{"name": "Dead", "url": "https://nope.example"}])
        self.assertTrue(any(i["level"] == "error" and "unreachable" in i["message"] for i in issues))

    def test_empty_url_is_warning(self) -> None:
        issues = check_dependency_health([{"name": "MissingURL", "url": ""}])
        self.assertTrue(any(i["level"] == "warning" and "no URL" in i["message"] for i in issues))

    def test_non_http_url_is_warning(self) -> None:
        issues = check_dependency_health([{"name": "FTP", "url": "ftp://example.com"}])
        self.assertTrue(any(i["level"] == "warning" and "non-HTTP" in i["message"] for i in issues))

    @patch("dependency_health.urlopen")
    def test_http_error_is_classified_by_status_code(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = HTTPError(
            "https://example.com/not-found", 404, "Not Found", {}, None
        )
        issues = check_dependency_health([{"name": "Gone", "url": "https://example.com/not-found"}])
        self.assertEqual(issues[0]["level"], "warning")
        self.assertIn("client error", issues[0]["message"])

    @patch("dependency_health.urlopen")
    def test_http_5xx_error_is_server_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = HTTPError(
            "https://example.com/failure", 503, "Unavailable", {}, None
        )
        issues = check_dependency_health([{"name": "API", "url": "https://example.com/failure"}])
        self.assertEqual(issues[0]["level"], "error")
        self.assertIn("server error", issues[0]["message"])

    @patch("dependency_health.urlopen")
    def test_structured_probe_scrubs_credentials_and_query_secrets(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_urlopen.return_value = _mock_response(200)
        results = probe_dependency_health([{
            "name": "Private API",
            "url": "https://user:password@example.com/v1?token=secret#fragment",
        }])
        self.assertEqual(results[0]["status"], "healthy")
        self.assertNotIn("password", results[0]["url"])
        self.assertNotIn("secret", results[0]["url"])
        mock_urlopen.assert_called_once()

    def test_timeout_must_be_positive_and_finite(self) -> None:
        for timeout in (0, -1, float("inf"), float("nan"), 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                probe_dependency_health([], timeout=timeout)
        with self.assertRaises(TypeError):
            probe_dependency_health([], timeout=True)


if __name__ == "__main__":
    unittest.main()
