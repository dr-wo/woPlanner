"""Small, read-only FastF1 authentication and archive-response diagnostics.

The token is never returned, decoded, logged or copied. Authentication itself
belongs to FastF1's explicit CLI, outside the live GUI process.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from importlib import import_module
import logging
import re
import sys
import time
import weakref
from urllib.parse import urlsplit


def authentication_status() -> dict:
    """Report local expiry only; a server response must establish access."""
    try:
        account = import_module("fastf1.internals.f1account")
    except ImportError:
        return {"state": "AUTH_API_UNAVAILABLE", "stored_token": False}
    token = account.token_manager.get_stored()
    if token is None:
        return {"state": "MISSING_OR_UNREADABLE", "stored_token": False}
    try:
        remaining = float(token.expires_at) - time.time()
        valid_time = token.is_valid()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"state": "INVALID_LOCAL_STATE", "stored_token": True}
    return {
        "state": ("STORED_UNEXPIRED" if valid_time else
                  "EXPIRED" if remaining <= 0 else "EXPIRING"),
        "stored_token": True,
        "note": "Local expiry check only; archive access is not verified.",
    }


@contextmanager
def observe_archive_responses(cache):
    """Observe only safe response metadata on FastF1's two request sessions.

    This bounded private compatibility seam is tested against FastF1 3.9. No
    request headers, query strings, bodies or account claims enter diagnostics.
    It uses ordinary requests response hooks rather than replacing transport.
    Run acquisition in a separate process to avoid global cache concurrency.
    """
    records, attached, seen = [], [], {}

    def observe(response, *args, **kwargs):
        url = urlsplit(response.url)
        if url.hostname in {"livetiming.formula1.com", "livetiming-mirror.fastf1.dev"}:
            # requests-cache dispatches response hooks again after wrapping a
            # fresh response in place. Count that response once. Weak refs
            # retain no payload and permit later reuse of an object address.
            identity = id(response)
            if identity in seen and seen[identity]() is response:
                return response
            seen[identity] = weakref.ref(response)
            cached = bool(getattr(response, "from_cache", False))
            records.append({"host": url.hostname, "path": url.path,
                            "status": int(response.status_code),
                            "from_cache": cached,
                            "authentication_attached": (None if cached else
                                "Authorization" in getattr(getattr(response, "request", None), "headers", {}))})
        return response

    for name in ("_requests_session", "_requests_session_cached"):
        session = getattr(cache, name, None)
        if session is None or any(session is item for item in attached):
            continue
        if isinstance(getattr(session, "hooks", None), dict):
            session.hooks.setdefault("response", []).append(observe)
            attached.append(session)
    try:
        yield records
    finally:
        for session in attached:
            if observe in session.hooks.get("response", ()):
                session.hooks["response"].remove(observe)


@contextmanager
def observe_parsed_cache_usage():
    """Keep only function names from FastF1's explicit cache-hit messages."""
    counts = Counter()
    class Hits(logging.Handler):
        def emit(self, record):
            match = re.fullmatch(r"Using cached data for ([A-Za-z_][A-Za-z0-9_]*)", record.getMessage())
            if match:
                counts[match[1]] += 1
    # FastF1 3.9's get_logger(__name__) prefixes the full module name; use
    # its root so the exact-message filter also sees fastf1.fastf1.req.
    logger = logging.getLogger("fastf1")
    handler = Hits()
    logger.addHandler(handler)
    try:
        yield counts
    finally:
        logger.removeHandler(handler)


def archive_summary(records, parsed_cache_hits=None) -> dict:
    primary = [r for r in records if r["host"] == "livetiming.formula1.com"]
    fresh = [r for r in primary if not r["from_cache"]]
    statuses = Counter(r["status"] for r in fresh)
    if statuses[401]:
        status = "AUTHENTICATION_OR_ACCESS_DENIED"
    elif statuses[403]:
        status = "ACCESS_DENIED"
    elif statuses[404]:
        status = "RESOURCE_NOT_FOUND"
    elif any(200 <= code < 300 for code in statuses):
        status = "FRESH_RESPONSES_RECEIVED"
    elif primary:
        status = "CACHED_RESPONSES_ONLY"
    elif parsed_cache_hits:
        status = "PARSED_CACHE_ONLY"
    else:
        status = "NO_ARCHIVE_RESPONSE_OBSERVED"
    return {"status": status,
            "primary_fresh_status_counts": {str(k): v for k, v in sorted(statuses.items())},
            "primary_cached_responses": sum(r["from_cache"] for r in primary),
            "parsed_cache_hits": dict(parsed_cache_hits or {}),
            "mirror_status_counts": dict(Counter(str(r["status"]) for r in records
                                                 if r["host"] != "livetiming.formula1.com")),
            "responses": list(records)}


def access_recovery_message(diagnostics) -> str:
    access = diagnostics.get("archive_access", {}).get("status")
    auth = diagnostics.get("authentication_before", {}).get("state")
    command = f"{sys.executable} -m fastf1 auth f1tv --authenticate --method browser"
    if access in {"AUTHENTICATION_OR_ACCESS_DENIED", "ACCESS_DENIED"}:
        return ("F1 archive access was rejected (HTTP 401/403). Re-authenticate with "
                "FastF1 outside this window, then retry. If rejection continues, "
                "check account entitlement/access; a parser failure is not established.\n"
                f"Official sign-in: {command}")
    if access == "RESOURCE_NOT_FOUND":
        return "The archive returned HTTP 404: the requested resource was not found. This is distinct from an authentication rejection."
    if auth in {"MISSING_OR_UNREADABLE", "EXPIRED", "EXPIRING", "INVALID_LOCAL_STATE"}:
        return ("No locally usable FastF1 authentication was found. Cached data may still "
                "work; fresh archive access requires official sign-in outside this window.\n"
                f"Official sign-in: {command}")
    return ""
