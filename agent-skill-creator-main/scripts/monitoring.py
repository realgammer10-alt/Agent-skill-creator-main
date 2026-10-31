#!/usr/bin/env python3
"""Persistent health history and a local dashboard for dependency monitoring."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Agent Skill Dependency Monitor</title>
  <style>
    :root { font: 16px/1.5 system-ui, sans-serif; color-scheme: light dark; }
    body { max-width: 1000px; margin: 3rem auto; padding: 0 1.25rem; }
    h1 { margin-bottom: .25rem; }
    .muted { opacity: .7; }
    .summary { display: flex; align-items: center; gap: .75rem; margin: 1.5rem 0; }
    .badge { border-radius: 999px; padding: .2rem .75rem; font-weight: 700; }
    .healthy { background: #d1fae5; color: #065f46; }
    .degraded { background: #fef3c7; color: #92400e; }
    .unhealthy { background: #fee2e2; color: #991b1b; }
    .unknown { background: #e5e7eb; color: #374151; }
    @media (prefers-color-scheme: dark) {
      .healthy { background: #064e3b; color: #d1fae5; }
      .degraded { background: #78350f; color: #fef3c7; }
      .unhealthy { background: #7f1d1d; color: #fee2e2; }
      .unknown { background: #374151; color: #f3f4f6; }
    }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th, td { padding: .75rem; border-bottom: 1px solid #8885; }
    td.url { overflow-wrap: anywhere; }
    #error { color: #b91c1c; }
  </style>
</head>
<body>
  <h1>Dependency monitor</h1>
  <p class="muted">Live status for the URLs declared by this skill.</p>
  <section class="summary" aria-live="polite">
    <span id="overall" class="badge unknown">UNKNOWN</span>
    <span id="checked" class="muted">Waiting for first status update…</span>
  </section>
  <p id="error" role="alert"></p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Dependency</th><th>Endpoint</th><th>Status</th><th>HTTP</th><th>Latency</th><th>Last checked</th></tr></thead>
      <tbody id="dependencies"><tr><td colspan="6">No status available yet.</td></tr></tbody>
    </table>
  </div>
  <h2>Recent checks</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Checked</th><th>Dependency</th><th>Status</th><th>Result</th></tr></thead>
      <tbody id="history"><tr><td colspan="4">No check history yet.</td></tr></tbody>
    </table>
  </div>
  <script>
    const statusLabels = {healthy: 'Healthy', degraded: 'Degraded', unhealthy: 'Unhealthy', unknown: 'Unknown'};
    function cell(row, value, className) {
      const element = document.createElement('td');
      element.textContent = value == null ? '—' : String(value);
      if (className) element.className = className;
      row.appendChild(element);
    }
    async function refresh() {
      try {
        const [statusResponse, historyResponse] = await Promise.all([
          fetch('/api/status', {cache: 'no-store'}),
          fetch('/api/history?limit=20', {cache: 'no-store'})
        ]);
        if (!statusResponse.ok) throw new Error('Status API returned HTTP ' + statusResponse.status);
        if (!historyResponse.ok) throw new Error('History API returned HTTP ' + historyResponse.status);
        const [data, historyData] = await Promise.all([
          statusResponse.json(),
          historyResponse.json()
        ]);
        const overall = document.getElementById('overall');
        overall.className = 'badge ' + data.status;
        overall.textContent = (statusLabels[data.status] || 'Unknown').toUpperCase();
        document.getElementById('checked').textContent = data.checked_at
          ? 'Last check: ' + new Date(data.checked_at).toLocaleString()
          : 'Waiting for first status update…';
        const body = document.getElementById('dependencies');
        body.replaceChildren();
        if (!data.dependencies.length) {
          const row = body.insertRow();
          cell(row, 'No dependencies have been checked yet.');
          const empty = document.createElement('td');
          empty.colSpan = 5;
          row.appendChild(empty);
        }
        for (const dependency of data.dependencies) {
          const row = body.insertRow();
          cell(row, dependency.name);
          cell(row, dependency.url, 'url');
          const status = document.createElement('td');
          const badge = document.createElement('span');
          badge.className = 'badge ' + dependency.status;
          badge.textContent = statusLabels[dependency.status] || 'Unknown';
          status.appendChild(badge);
          row.appendChild(status);
          cell(row, dependency.http_status);
          cell(row, dependency.response_time_ms == null ? null : dependency.response_time_ms + ' ms');
          cell(row, dependency.checked_at ? new Date(dependency.checked_at).toLocaleString() : null);
        }
        const historyBody = document.getElementById('history');
        historyBody.replaceChildren();
        if (!historyData.history.length) {
          const row = historyBody.insertRow();
          const empty = document.createElement('td');
          empty.colSpan = 4;
          empty.textContent = 'No check history yet.';
          row.appendChild(empty);
        }
        for (const item of historyData.history) {
          const row = historyBody.insertRow();
          cell(row, item.checked_at ? new Date(item.checked_at).toLocaleString() : null);
          cell(row, item.name);
          const status = document.createElement('td');
          const badge = document.createElement('span');
          badge.className = 'badge ' + item.status;
          badge.textContent = statusLabels[item.status] || 'Unknown';
          status.appendChild(badge);
          row.appendChild(status);
          cell(row, item.message);
        }
        document.getElementById('error').textContent = '';
      } catch (error) {
        document.getElementById('error').textContent = 'Could not refresh status: ' + error.message;
      }
    }
    refresh();
    window.setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


class HealthHistory:
    """Store probe results in SQLite and expose the latest status snapshot."""

    def __init__(self, database: str | Path):
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS health_checks (
                    id INTEGER PRIMARY KEY,
                    checked_at TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('healthy', 'degraded', 'unhealthy')
                    ),
                    http_status INTEGER,
                    response_time_ms REAL,
                    message TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS health_checks_name_url_id "
                "ON health_checks (name, url, id)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, results: list[dict[str, Any]]) -> None:
        """Append a single polling cycle to history."""
        for result in results:
            if not isinstance(result, dict):
                raise TypeError("each health result must be an object")
            if result.get("status") not in ("healthy", "degraded", "unhealthy"):
                raise ValueError("health result has an invalid status")
            for field in ("checked_at", "name", "url", "message"):
                if not isinstance(result.get(field), str):
                    raise TypeError(f"health result field '{field}' must be a string")
            http_status = result.get("http_status")
            if http_status is not None and (
                isinstance(http_status, bool) or not isinstance(http_status, int)
            ):
                raise TypeError("health result field 'http_status' must be an integer or null")
            if http_status is not None and not 100 <= http_status <= 599:
                raise ValueError("health result field 'http_status' must be an HTTP status or null")
            response_time = result.get("response_time_ms")
            if response_time is not None and (
                isinstance(response_time, bool)
                or not isinstance(response_time, (int, float))
            ):
                raise TypeError("health result field 'response_time_ms' must be numeric or null")
            if response_time is not None and (
                not math.isfinite(response_time) or response_time < 0
            ):
                raise ValueError("health result field 'response_time_ms' must be non-negative or null")
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO health_checks (
                    checked_at, name, url, status, http_status,
                    response_time_ms, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result["checked_at"],
                        result["name"],
                        result["url"],
                        result["status"],
                        result.get("http_status"),
                        result.get("response_time_ms"),
                        result["message"],
                    )
                    for result in results
                ],
            )

    def status(self, expected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Return the latest result for each dependency and aggregate status."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT checked_at, name, url, status, http_status,
                       response_time_ms, message
                FROM health_checks AS current
                WHERE id = (
                    SELECT MAX(latest.id)
                    FROM health_checks AS latest
                    WHERE latest.name = current.name AND latest.url = current.url
                )
                ORDER BY name, url
                """
            ).fetchall()
        dependencies = [
            {
                "checked_at": row[0],
                "name": row[1],
                "url": row[2],
                "status": row[3],
                "http_status": row[4],
                "response_time_ms": row[5],
                "message": row[6],
            }
            for row in rows
        ]
        if expected is not None:
            expected_keys = {(item["name"], item["url"]) for item in expected}
            dependencies = [
                item for item in dependencies
                if (item["name"], item["url"]) in expected_keys
            ]
        statuses = {dependency["status"] for dependency in dependencies}
        overall = next(
            (status for status in ("unhealthy", "degraded", "healthy") if status in statuses),
            "unknown",
        )
        return {
            "status": overall,
            "checked_at": max(
                (dependency["checked_at"] for dependency in dependencies),
                default=None,
            ),
            "dependency_count": len(dependencies),
            "dependencies": dependencies,
        }

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return at most `limit` most recent checks, newest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("history limit must be an integer between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT checked_at, name, url, status, http_status,
                       response_time_ms, message
                FROM health_checks
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "checked_at": row[0],
                "name": row[1],
                "url": row[2],
                "status": row[3],
                "http_status": row[4],
                "response_time_ms": row[5],
                "message": row[6],
            }
            for row in rows
        ]


def create_dashboard_handler(
    history: HealthHistory,
    expected: list[dict[str, Any]] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler for the dashboard and read-only JSON endpoints."""

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, _DASHBOARD_HTML, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                try:
                    payload = history.status(expected)
                except sqlite3.Error as exc:
                    self._send_json(500, {"error": f"health history unavailable: {exc}"})
                    return
                self._send_json(200, payload)
                return
            if parsed.path == "/api/history":
                values = parse_qs(parsed.query).get("limit", ["100"])
                if len(values) != 1 or not values[0].isdecimal():
                    self._send_json(400, {"error": "limit must be an integer between 1 and 1000"})
                    return
                try:
                    records = history.history(int(values[0]))
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                except sqlite3.Error as exc:
                    self._send_json(500, {"error": f"health history unavailable: {exc}"})
                    return
                self._send_json(200, {"history": records})
                return
            self._send_json(404, {"error": "not found"})

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            self._send(
                status_code,
                json.dumps(payload, ensure_ascii=True),
                "application/json; charset=utf-8",
            )

        def _send(self, status_code: int, body: str, content_type: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *args: object) -> None:
            return

    return DashboardHandler
