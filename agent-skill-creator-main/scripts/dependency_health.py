#!/usr/bin/env python3
"""
Dependency health check for a skill.

Issues an HTTP HEAD to each declared dependency URL and reports whether the
endpoint is reachable, returning a normal status, or has moved / errored.
Non-HTTP URLs and URLs without a scheme are reported as skipped.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

HTTP_TIMEOUT_SECONDS = 10
MAX_HTTP_TIMEOUT_SECONDS = 300
_USER_AGENT = "agent-skill-staleness-check/1.0"


def probe_dependency_health(
    dependencies: list[dict],
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> list[dict]:
    """Return one structured health result for each declared dependency."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number")
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_HTTP_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout must be a positive finite number no greater than "
            f"{MAX_HTTP_TIMEOUT_SECONDS} seconds"
        )

    results: list[dict] = []
    for index, dep in enumerate(dependencies, start=1):
        if not isinstance(dep, dict):
            dep = {}

        raw_name = dep.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name:
            name = f"Dependency {index}"

        raw_url = dep.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        display_url = _display_url(url)
        checked_at = datetime.now(timezone.utc).isoformat()

        if not url:
            results.append(_result(
                name, display_url, checked_at, "degraded",
                "Dependency has no URL", None, None,
            ))
            continue

        try:
            if any(character.isspace() or ord(character) < 32 for character in url):
                raise ValueError("URL contains whitespace or control characters")
            parsed = urlsplit(url)
            if (
                parsed.scheme.lower() not in ("http", "https")
                or not parsed.hostname
            ):
                raise ValueError("URL must be HTTP(S) and include a host")
            # Accessing port validates malformed port numbers.
            _ = parsed.port
        except ValueError as exc:
            results.append(_result(
                name, display_url, checked_at, "degraded",
                f"Invalid dependency URL: {exc}", None, None,
            ))
            continue

        req = Request(url, method="HEAD")
        req.add_header("User-Agent", _USER_AGENT)
        started = time.monotonic()
        try:
            with urlopen(req, timeout=timeout) as response:
                status_code = response.status
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            results.append(_result(
                name, display_url, checked_at,
                _http_status(status_code),
                f"HTTP {status_code}", status_code, elapsed_ms,
            ))
        except HTTPError as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            if exc.fp is not None:
                exc.close()
            results.append(_result(
                name, display_url, checked_at,
                _http_status(exc.code),
                f"HTTP {exc.code}", exc.code, elapsed_ms,
            ))
        except (URLError, TimeoutError, OSError) as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            results.append(_result(
                name, display_url, checked_at, "unhealthy",
                f"Endpoint unreachable ({type(exc).__name__})", None, elapsed_ms,
            ))

    return results


def _http_status(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "healthy"
    if 400 <= status_code < 500:
        return "degraded"
    return "unhealthy"


def _display_url(url: str) -> str:
    """Remove query strings and fragments so monitoring output cannot leak tokens."""
    try:
        parsed = urlsplit(url)
        if parsed.netloc:
            try:
                hostname = parsed.hostname
                port = parsed.port
            except ValueError:
                return parsed.path
            if not hostname:
                return parsed.path
            if ":" in hostname:
                hostname = f"[{hostname}]"
            port_text = f":{port}" if port is not None else ""
            return urlunsplit((parsed.scheme, f"{hostname}{port_text}", parsed.path, "", ""))
        return parsed.path
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]


def _result(
    name: str,
    url: str,
    checked_at: str,
    status: str,
    message: str,
    http_status: int | None,
    response_time_ms: float | None,
) -> dict:
    return {
        "name": name,
        "url": url,
        "checked_at": checked_at,
        "status": status,
        "http_status": http_status,
        "response_time_ms": response_time_ms,
        "message": message,
    }


def check_dependency_health(
    dependencies: list[dict],
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> list[dict]:
    """
    Probe each dependency's URL. Returns a list of issue dicts (level, message,
    detail). Status thresholds:
        2xx-3xx -> info "healthy"
        4xx     -> warning "client error / may have moved"
        5xx     -> error  "server error"
        Connection failures -> error "unreachable / check failed"
    """
    issues: list[dict] = []
    for result in probe_dependency_health(dependencies, timeout=timeout):
        status = result["status"]
        name = result["name"]
        message = result["message"]
        url = result["url"]

        if status == "healthy":
            level = "info"
            summary = f"Dependency '{name}' is healthy"
        elif status == "degraded":
            level = "warning"
            if result["http_status"] is not None:
                summary = f"Dependency '{name}' returned client error"
            elif "no URL" in message:
                summary = f"Dependency '{name}' has no URL"
            else:
                summary = f"Dependency '{name}' has non-HTTP URL"
        else:
            level = "error"
            if result["http_status"] is not None:
                summary = f"Dependency '{name}' returned server error"
            else:
                summary = f"Dependency '{name}' is unreachable"

        detail = message
        if url:
            detail += f" from {url}"
        if status == "degraded" and result["http_status"] is not None:
            detail += " The endpoint may have moved or require authentication."
        issues.append({"level": level, "message": summary, "detail": detail})

    return issues
