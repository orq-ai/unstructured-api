"""Cloudflare-aware request context for structured logging.

Public traffic reaches this service through Cloudflare -> the orq gateway ->
the kuma mesh, so ``request.client.host`` is the mesh/proxy address, not the
caller. Cloudflare injects the real client identity + tracing headers, and
because the origin is locked to Cloudflare IPs these cannot be spoofed on the
public hostnames (``my.orq.ai`` / ``api.orq.ai``).

Precedence for the client IP follows Cloudflare's guidance:
``CF-Connecting-IP`` -> ``True-Client-IP`` (enterprise) -> first
``X-Forwarded-For`` hop -> socket peer. Internal traffic (mesh, health checks)
has none of these, so the values fall back to the socket / ``"-"``.
"""

from __future__ import annotations

from typing import Dict

from fastapi import Request


def client_ip(request: Request) -> str:
    """Best-effort real client IP, preferring the Cloudflare-provided header."""
    cf = request.headers.get("cf-connecting-ip") or request.headers.get("true-client-ip")
    if cf:
        return cf
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def request_context(request: Request) -> Dict[str, str]:
    """Cloudflare request context for logs: real client IP, CF-Ray, ISO country.

    ``ray`` (CF-Ray) ties an app log line back to the Cloudflare dashboard /
    support tickets; ``country`` (CF-IPCountry) enables geo signals on the auth
    and blocked-request metrics without a GeoIP lookup. Missing headers -> "-".
    """
    return {
        "ip": client_ip(request),
        "ray": request.headers.get("cf-ray", "-"),
        "country": request.headers.get("cf-ipcountry", "-"),
    }
