"""Guards for the HTTP control plane when FlowXer is reachable beyond localhost."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")

_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "kubernetes.default",
    "kubernetes.default.svc",
}


class SecurityError(ValueError):
    """Rejected host, URL, or filesystem path."""


def require_safe_id(value: str, *, what: str = "id") -> str:
    if not SAFE_ID_RE.fullmatch(value or ""):
        raise SecurityError(f"invalid {what}")
    return value


def contained_path(path: Path | str, *roots: Path) -> Path:
    """Resolve *path* and require it to sit under one of *roots*."""
    candidate = Path(path)
    resolved = candidate.resolve()
    for root in roots:
        root_resolved = Path(root).resolve()
        root_resolved.mkdir(parents=True, exist_ok=True)
        if resolved.is_relative_to(root_resolved):
            return resolved
    raise SecurityError("path must be under mixer storage")


def resolve_under(root: Path, file_path: str) -> Path:
    """Join a relative (or already-contained absolute) path onto *root*."""
    root_resolved = root.resolve()
    root_resolved.mkdir(parents=True, exist_ok=True)
    candidate = Path(file_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root_resolved / candidate).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise SecurityError("path must be under mixer storage")
    return resolved


def _host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    name = host.strip().lower().rstrip(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name or "/" in name or "\\" in name or name in _BLOCKED_HOSTS:
        raise SecurityError("host is not allowed")
    if "%" in name:
        raise SecurityError("host is not allowed")
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror as exc:
        raise SecurityError(f"host is not resolvable: {host}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        raw = info[4][0]
        ip = ipaddress.ip_address(raw)
        mapped = getattr(ip, "ipv4_mapped", None)
        addresses.append(mapped if mapped is not None else ip)
    if not addresses:
        raise SecurityError(f"host is not resolvable: {host}")
    return addresses


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_loopback:
        return False
    return bool(ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


def assert_egress_host_allowed(host: str, *, what: str = "host") -> str:
    """Allow LAN/public receivers; block link-local, multicast, and cloud metadata."""
    name = (host or "").strip()
    if not name:
        raise SecurityError(f"{what} is required")
    for ip in _host_ips(name):
        if _ip_blocked(ip):
            raise SecurityError(f"{what} is not allowed")
    return name


def assert_http_url(url: str, *, what: str = "URL") -> str:
    cleaned = (url or "").strip()
    if any(char in cleaned for char in ' \t\n\r"\'!;|\\'):
        raise SecurityError(f"{what} contains invalid characters")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise SecurityError(f"{what} must be http or https")
    if parsed.username or parsed.password:
        raise SecurityError(f"{what} must not include userinfo")
    if not parsed.hostname:
        raise SecurityError(f"{what} host is required")
    assert_egress_host_allowed(parsed.hostname, what=what)
    return cleaned
