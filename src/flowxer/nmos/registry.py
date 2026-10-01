from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 3.0


class RegistryClient:
    """IS-04 Registration API client for a static nmos-cpp (or compatible) registry."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def register(self, resource_type: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"type": resource_type, "data": data}).encode("utf-8")
        self._request(
            "POST",
            f"{self.base}/x-nmos/registration/v1.3/resource",
            payload,
        )

    def heartbeat(self, node_id: str) -> None:
        self._request(
            "POST",
            f"{self.base}/x-nmos/registration/v1.3/health/nodes/{node_id}",
            b"{}",
        )

    def _request(self, method: str, url: str, body: bytes) -> None:
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            # 200/201 success; 409 already registered is fine.
            if exc.code in {200, 201, 204, 409}:
                return
            raise
