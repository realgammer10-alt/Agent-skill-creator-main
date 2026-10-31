#!/usr/bin/env python3
"""Continuously check skill dependency URLs and serve a local live dashboard."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import threading
from collections.abc import Sequence
from http.server import ThreadingHTTPServer
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dependency_health import (
    HTTP_TIMEOUT_SECONDS,
    MAX_HTTP_TIMEOUT_SECONDS,
    probe_dependency_health,
)
from monitoring import HealthHistory, create_dashboard_handler
from skill_document import SkillDoc


def _bounded_number(value: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite number in the allowed range") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"must be between {minimum:g} and {maximum:g} seconds"
        )
    return number


def _interval_seconds(value: str) -> float:
    return _bounded_number(value, 1, 86400)


def _timeout_seconds(value: str) -> float:
    return _bounded_number(value, 0.1, MAX_HTTP_TIMEOUT_SECONDS)


def _port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer between 0 and 65535") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be an integer between 0 and 65535")
    return port


def load_dependencies(skill_path: str | Path) -> list[dict]:
    """Read declared dependency URLs from a skill's validated frontmatter."""
    skill_dir = Path(skill_path).expanduser().resolve()
    if not skill_dir.exists():
        raise ValueError(f"skill path does not exist: {skill_dir}")
    if not skill_dir.is_dir():
        raise ValueError(f"skill path is not a directory: {skill_dir}")
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError(f"SKILL.md not found in {skill_dir}")
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {skill_file}: {exc}") from exc
    doc = SkillDoc.from_text(content)
    if doc.frontmatter is None:
        raise ValueError(f"SKILL.md has no valid frontmatter: {skill_file}")
    return doc.list_of_objects("metadata", "dependencies")


def _run_check(
    dependencies: list[dict],
    history: HealthHistory,
    timeout: float,
) -> tuple[dict, list[dict]]:
    results = probe_dependency_health(dependencies, timeout=timeout)
    history.record(results)
    return history.status(results), results


def _poll_until_stopped(
    dependencies: list[dict],
    history: HealthHistory,
    timeout: float,
    interval: float,
    stop: threading.Event,
) -> None:
    while not stop.wait(interval):
        result, _results = _run_check(dependencies, history, timeout)
        print(
            f"Monitor check: {result['status']} "
            f"({result['dependency_count']} dependencies)",
            file=sys.stderr,
            flush=True,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor declared skill dependencies and serve a localhost dashboard."
    )
    parser.add_argument("skill_path", help="Path to a skill directory containing SKILL.md.")
    parser.add_argument(
        "--interval",
        type=_interval_seconds,
        default=60,
        metavar="SECONDS",
        help="Seconds between checks while serving the dashboard (default: 60).",
    )
    parser.add_argument(
        "--timeout",
        type=_timeout_seconds,
        default=HTTP_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Timeout per endpoint probe (default: {HTTP_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite history file (default: <skill>/.agent-skill-monitor/monitor.db).",
    )
    parser.add_argument(
        "--port",
        type=_port_number,
        default=8765,
        help="Local dashboard port; use 0 to select an available port (default: 8765).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check, print its JSON result, and exit without starting the dashboard.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        skill_dir = Path(args.skill_path).expanduser().resolve()
        dependencies = load_dependencies(skill_dir)
        database = args.database or skill_dir / ".agent-skill-monitor" / "monitor.db"
        history = HealthHistory(database)
        snapshot, initial_results = _run_check(dependencies, history, args.timeout)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))

    if args.once:
        print(json.dumps(snapshot, indent=2))
        return 0 if snapshot["status"] == "healthy" else 2

    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        create_dashboard_handler(history, initial_results),
    )
    stop = threading.Event()
    poller = threading.Thread(
        target=_poll_until_stopped,
        args=(dependencies, history, args.timeout, args.interval, stop),
        name="dependency-health-poller",
        daemon=True,
    )
    poller.start()
    print(
        f"Monitoring {len(dependencies)} dependencies for {skill_dir}\n"
        f"Dashboard: http://127.0.0.1:{server.server_port}/\n"
        f"History: {history.database}\n"
        "Press Ctrl+C to stop.",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping dependency monitor.", flush=True)
    finally:
        stop.set()
        server.server_close()
        poller.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
